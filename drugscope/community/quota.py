"""Usage quotas, plans and upgrades.

Every research run costs credits, and each plan gets a fixed number of credits per
**rolling window** (24 hours by default). Credits come back one run at a time as
each run ages out of the window, so "resets in 4h 12m" is always exact: it is when
the oldest run in the window expires.

The check and the charge happen in one `BEGIN IMMEDIATE` transaction, so two
browser tabs starting runs at the same moment cannot both squeeze under the limit.
A run that fails is refunded.

Upgrades are fulfilled with single-use codes an administrator issues. That works
with any payment method - a payment link, an invoice, a bank transfer - without
the app ever handling card data. Codes are stored only as hashes.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .auth import Principal, require_admin, require_user
from .db import connect, now_iso
from .service import audit


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, "") or default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Plan:
    key: str
    label: str
    credits: int
    blurb: str
    price: str
    upgrade_url: str


PAID_PLANS = ("pro", "team")


def plans() -> dict[str, Plan]:
    """The plan ladder. Allowances, prices and checkout links all come from `.env`."""
    env = lambda name, default="": (os.getenv(name) or default).strip()  # noqa: E731
    return {
        "free": Plan("free", "Free", _int_env("DRUGSCOPE_FREE_CREDITS", 5),
                     "Get oriented.", "$0", ""),
        "pro": Plan("pro", "Pro", _int_env("DRUGSCOPE_PRO_CREDITS", 60),
                    "Research every day.", env("DRUGSCOPE_PRO_PRICE", "$9"),
                    env("DRUGSCOPE_UPGRADE_URL")),
        "team": Plan("team", "Team", _int_env("DRUGSCOPE_TEAM_CREDITS", 200),
                     "Heavy, deep research.", env("DRUGSCOPE_TEAM_PRICE", "$29"),
                     env("DRUGSCOPE_UPGRADE_URL_TEAM")),
    }


def window_hours() -> int:
    return max(1, _int_env("DRUGSCOPE_QUOTA_WINDOW_HOURS", 24))


# What one run costs at each research depth.
DEPTH_COST = {"Scan": 1, "Standard": 2, "Deep": 4}


def checkout_connected() -> bool:
    return any(plans()[key].upgrade_url for key in PAID_PLANS)


class QuotaExceeded(Exception):
    def __init__(self, status: "QuotaStatus", needed: int) -> None:
        self.status = status
        self.needed = needed
        super().__init__(
            f"This run needs {needed} credit(s) and {status.remaining} remain on the "
            f"{status.plan_label} plan."
        )


@dataclass(frozen=True)
class QuotaStatus:
    plan: str
    plan_label: str
    limit: int
    used: int
    window_hours: int
    resets_at: datetime | None
    plan_expires_at: str | None
    unlimited: bool = False

    @property
    def remaining(self) -> int:
        return 10**9 if self.unlimited else max(0, self.limit - self.used)

    def resets_in_text(self, now: datetime | None = None) -> str:
        if self.resets_at is None:
            return ""
        seconds = max(0, int((self.resets_at - (now or _now())).total_seconds()))
        hours, minutes = divmod(seconds // 60, 60)
        return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def effective_plan(plan: str, expires_at: str | None, now: datetime | None = None) -> str:
    if plan in PAID_PLANS and (not expires_at or _parse(expires_at) > (now or _now())):
        return plan
    return "free"


def _status(conn, user_row, now: datetime) -> QuotaStatus:
    if user_row["role"] == "admin":
        return QuotaStatus("admin", "Administrator", 0, 0, window_hours(), None, None, unlimited=True)

    plan_key = effective_plan(user_row["plan"], user_row["plan_expires_at"], now)
    plan = plans()[plan_key]
    hours = window_hours()
    since = _iso(now - timedelta(hours=hours))
    rows = conn.execute(
        "SELECT credits, created_at FROM usage_events "
        "WHERE user_id = ? AND created_at > ? ORDER BY created_at",
        (user_row["id"], since),
    ).fetchall()
    used = sum(r["credits"] for r in rows)
    resets_at = None
    if rows:
        # Credits return as runs age out; the next return is the oldest run's.
        resets_at = _parse(rows[0]["created_at"]) + timedelta(hours=hours)
    return QuotaStatus(
        plan_key, plan.label, plan.credits, used, hours, resets_at,
        user_row["plan_expires_at"] if plan_key in PAID_PLANS else None,
    )


def usage_history(principal: Principal, limit: int = 20) -> list[dict[str, Any]]:
    """The signed-in user's own recent charges, newest first."""
    user = require_user(principal)
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT created_at, credits, kind, detail FROM usage_events WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?", (user.id, limit))]


def status(principal: Principal, now: datetime | None = None) -> QuotaStatus:
    user = require_user(principal)
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user.id,)).fetchone()
        return _status(conn, row, now or _now())


def consume(principal: Principal, credits: int, *, kind: str, detail: str = "",
            now: datetime | None = None) -> int:
    """Charge credits for a run, or raise `QuotaExceeded`. Returns the event id."""
    user = require_user(principal)
    moment = now or _now()
    with connect(immediate=True) as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user.id,)).fetchone()
        current = _status(conn, row, moment)
        if not current.unlimited and current.remaining < credits:
            raise QuotaExceeded(current, credits)
        cursor = conn.execute(
            "INSERT INTO usage_events (user_id, credits, kind, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (user.id, 0 if current.unlimited else credits, kind, detail[:300], _iso(moment)),
        )
        return int(cursor.lastrowid)


def refund(principal: Principal, event_id: int, reason: str = "run failed") -> None:
    """Give back the credits for a run that failed.

    The charge is zeroed, not deleted, so the usage history still shows that the run
    happened and why it cost nothing - a refund that erases its own record looks
    exactly like a quota that never charged.
    """
    user = require_user(principal)
    with connect() as conn:
        conn.execute(
            "UPDATE usage_events SET credits = 0, kind = 'refunded', detail = ? || ' (' || ? || ')' "
            "WHERE id = ? AND user_id = ? AND kind != 'refunded'",
            ("Refunded", reason[:80], event_id, user.id))


# --------------------------------------------------------------------------- #
# Upgrade codes
# --------------------------------------------------------------------------- #

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to misread


def _normalise_code(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def _hash_code(code: str) -> str:
    return hashlib.sha256(_normalise_code(code).encode("ascii")).hexdigest()


def generate_codes(admin: Principal, *, count: int, days: int, note: str = "",
                   plan: str = "pro") -> list[str]:
    """Issue single-use upgrade codes. The plain codes are returned once and never stored."""
    who = require_admin(admin)
    if plan not in PAID_PLANS:
        raise ValueError("Codes can grant Pro or Team.")
    if not (1 <= count <= 100) or not (1 <= days <= 3660):
        raise ValueError("Issue 1-100 codes of 1-3660 days each.")
    codes = []
    with connect() as conn:
        for _ in range(count):
            raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
            code = f"DS-{raw[:4]}-{raw[4:8]}-{raw[8:]}"
            conn.execute(
                "INSERT INTO upgrade_codes (code_hash, plan, days, note, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_hash_code(code), plan, days, note[:120], who.id, now_iso()),
            )
            codes.append(code)
        audit(conn, who, "generate_upgrade_codes", plan, f"{count} code(s), {days} day(s); {note[:80]}")
    return codes


def redeem(principal: Principal, code: str, now: datetime | None = None) -> str:
    """Apply a code to the signed-in account. Returns the new plan expiry (ISO).

    Redeeming the plan you already have extends it; a code for a different paid
    plan switches to that plan for the code's period.
    """
    user = require_user(principal)
    moment = now or _now()
    with connect(immediate=True) as conn:
        row = conn.execute(
            "SELECT * FROM upgrade_codes WHERE code_hash = ?", (_hash_code(code),)
        ).fetchone()
        if row is None or row["redeemed_by"] is not None:
            raise ValueError("That code is not valid or has already been used.")
        # Single use, enforced in the UPDATE itself rather than by the read above.
        claimed = conn.execute(
            "UPDATE upgrade_codes SET redeemed_by = ?, redeemed_at = ? "
            "WHERE id = ? AND redeemed_by IS NULL",
            (user.id, _iso(moment), row["id"]),
        ).rowcount
        if claimed != 1:
            raise ValueError("That code has already been used.")
        account = conn.execute("SELECT plan, plan_expires_at FROM users WHERE id = ?", (user.id,)).fetchone()
        start = moment
        current = effective_plan(account["plan"], account["plan_expires_at"], moment)
        if current == row["plan"] and account["plan_expires_at"]:
            start = max(moment, _parse(account["plan_expires_at"]))
        expires = _iso(start + timedelta(days=int(row["days"])))
        conn.execute(
            "UPDATE users SET plan = ?, plan_expires_at = ? WHERE id = ?", (row["plan"], expires, user.id)
        )
    return expires


def set_plan(admin: Principal, user_id: int, *, plan: str, days: int | None) -> None:
    """Grant or revoke Pro by hand. `days=None` means no expiry."""
    who = require_admin(admin)
    if plan not in plans():
        raise ValueError("Unknown plan.")
    expires = _iso(_now() + timedelta(days=days)) if (plan in PAID_PLANS and days) else None
    with connect() as conn:
        updated = conn.execute(
            "UPDATE users SET plan = ?, plan_expires_at = ? WHERE id = ? AND role = 'user'",
            (plan, expires, user_id),
        ).rowcount
        if not updated:
            raise ValueError("No such user.")
        audit(conn, who, "set_plan", f"user:{user_id}", f"{plan} until {expires or 'no expiry'}")
