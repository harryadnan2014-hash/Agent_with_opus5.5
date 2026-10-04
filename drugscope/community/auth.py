"""Accounts, passwords and roles.

Passwords are stored as salted scrypt hashes and compared in constant time. Repeated
failures lock a username for a while, which blunts online guessing. Roles are never
taken from the session alone: `require_admin` re-reads the account from the
database on every privileged call, so demoting or deactivating an admin takes
effect immediately, even for a session that is already signed in.

The administrator account is created from `.env` (`DRUGSCOPE_ADMIN_USERNAME` and
`DRUGSCOPE_ADMIN_PASSWORD`) rather than hard-coded, and that username cannot be
claimed through the public sign-up form.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import connect, now_iso

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256
LOCKOUT_FAILURES = 5
LOCKOUT_MINUTES = 15

# scrypt work factors - roughly 50 ms per hash on a laptop.
_N, _R, _P = 2**14, 8, 1


class AuthError(Exception):
    """A sign-in or sign-up problem, worded for the person at the keyboard."""


class PermissionDenied(Exception):
    """The caller's role does not allow this operation."""


@dataclass(frozen=True)
class Principal:
    id: int
    username: str
    role: str
    plan: str
    plan_expires_at: str | None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=32,
        )
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #

def _admin_username() -> str:
    return (os.getenv("DRUGSCOPE_ADMIN_USERNAME") or "").strip()


def _row_to_principal(row) -> Principal:
    return Principal(
        id=row["id"], username=row["username"], role=row["role"],
        plan=row["plan"], plan_expires_at=row["plan_expires_at"],
    )


def register(username: str, password: str, *, confirmed_adult: bool) -> Principal:
    username = (username or "").strip()
    if not confirmed_adult:
        raise AuthError("You must confirm you are 18 or older to create an account.")
    if not USERNAME_RE.match(username):
        raise AuthError("Usernames are 3-32 characters: letters, numbers, dot, dash or underscore.")
    if _admin_username() and username.lower() == _admin_username().lower():
        raise AuthError("That username is reserved.")
    if not (MIN_PASSWORD_LENGTH <= len(password or "") <= MAX_PASSWORD_LENGTH):
        raise AuthError(f"Passwords need at least {MIN_PASSWORD_LENGTH} characters.")
    if password.lower() == username.lower():
        raise AuthError("Your password cannot be your username.")

    with connect() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise AuthError("That username is taken.")
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), now_iso()),
        )
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return _row_to_principal(row)


def _locked_out(conn, username: str) -> bool:
    since = (datetime.now(timezone.utc) - timedelta(minutes=LOCKOUT_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    failures = conn.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE username = ? AND success = 0 AND created_at >= ?",
        (username, since),
    ).fetchone()[0]
    return failures >= LOCKOUT_FAILURES


def authenticate(username: str, password: str) -> Principal:
    username = (username or "").strip()
    if not username or not password:
        raise AuthError("Enter your username and password.")
    with connect() as conn:
        if _locked_out(conn, username):
            raise AuthError(
                f"Too many failed attempts. Try again in {LOCKOUT_MINUTES} minutes."
            )
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        # Hash even for unknown users so response time does not reveal which exist.
        ok = verify_password(password, row["password_hash"] if row else hash_password("x" * 12))
        ok = ok and row is not None and bool(row["is_active"])
        conn.execute(
            "INSERT INTO login_attempts (username, success, created_at) VALUES (?, ?, ?)",
            (username, int(ok), now_iso()),
        )
    if not ok:
        raise AuthError("Incorrect username or password.")
    return _row_to_principal(row)


def load_principal(user_id: int | None) -> Principal | None:
    """The current state of an account, or None if it is gone or deactivated."""
    if not user_id:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)
        ).fetchone()
    return _row_to_principal(row) if row else None


def require_user(principal: Principal | None) -> Principal:
    fresh = load_principal(principal.id if principal else None)
    if fresh is None:
        raise PermissionDenied("Sign in to do this.")
    return fresh


def require_admin(principal: Principal | None) -> Principal:
    """Re-check the role in the database - a session flag is never enough."""
    fresh = load_principal(principal.id if principal else None)
    if fresh is None or not fresh.is_admin:
        raise PermissionDenied("Administrator access is required.")
    return fresh


# --------------------------------------------------------------------------- #
# Remembered sign-in
# --------------------------------------------------------------------------- #

SESSION_DAYS = 30


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(principal: Principal) -> str:
    """A new random sign-in token for a browser cookie. Only its hash is stored."""
    user = require_user(principal)
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as conn:
        conn.execute("INSERT INTO sessions (user_id, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
                     (user.id, _token_hash(token), now_iso(), expires))
    return token


def principal_from_session(token: str | None) -> Principal | None:
    """The account a remembered-sign-in cookie belongs to, if it is still valid."""
    if not isinstance(token, str) or not token or len(token) > 100:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1",
            (_token_hash(token), now_iso())).fetchone()
    return _row_to_principal(row) if row else None


def end_session(token: str | None) -> None:
    if token:
        with connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def end_all_sessions(principal: Principal) -> None:
    """Sign every browser out - after a password change, for instance."""
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (principal.id,))


def login_external(provider: str, subject: str) -> Principal:
    """Sign in with an identity provider, creating the DrugScope account on first use.

    Only a hash of the provider's stable subject id is stored - no email and no name -
    and the new account gets a neutral username (comments are public under it) that
    its owner can change on the Account page. External accounts have no password.
    """
    if not subject:
        raise AuthError("The sign-in provider did not return an account id.")
    external_id = hashlib.sha256(f"{provider}:{subject}".encode()).hexdigest()
    with connect(immediate=True) as conn:
        row = conn.execute("SELECT * FROM users WHERE auth_provider = ? AND external_id = ?",
                           (provider, external_id)).fetchone()
        if row is None:
            while True:
                username = f"member-{secrets.token_hex(3)}"
                if not conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                    break
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash, auth_provider, external_id, created_at) "
                "VALUES (?, '!', ?, ?, ?)", (username, provider, external_id, now_iso()))
            row = conn.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
        if not row["is_active"]:
            raise AuthError("This account has been deactivated.")
    return _row_to_principal(row)


def is_external(principal: Principal) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT auth_provider FROM users WHERE id = ?", (principal.id,)).fetchone()
    return bool(row) and row["auth_provider"] != "password"


def change_username(principal: Principal, new: str) -> None:
    user = require_user(principal)
    new = (new or "").strip()
    if not USERNAME_RE.match(new):
        raise AuthError("Usernames are 3-32 characters: letters, numbers, dot, dash or underscore.")
    if user.is_admin:
        raise AuthError("The administrator username is set in .env.")
    if _admin_username() and new.lower() == _admin_username().lower():
        raise AuthError("That username is reserved.")
    with connect() as conn:
        clash = conn.execute("SELECT id FROM users WHERE username = ? AND id != ?", (new, user.id)).fetchone()
        if clash:
            raise AuthError("That username is taken.")
        conn.execute("UPDATE users SET username = ? WHERE id = ?", (new, user.id))


def change_password(principal: Principal, current: str, new: str) -> None:
    user = require_user(principal)
    if not (MIN_PASSWORD_LENGTH <= len(new or "") <= MAX_PASSWORD_LENGTH):
        raise AuthError(f"Passwords need at least {MIN_PASSWORD_LENGTH} characters.")
    with connect() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user.id,)).fetchone()
        if not verify_password(current, row["password_hash"]):
            raise AuthError("Your current password is incorrect.")
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new), user.id))
        # Any other browser that stayed signed in must sign in again with the new password.
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))


def delete_account(principal: Principal, password: str) -> None:
    """Erase an account and everything it submitted (reports, votes, usage).

    Suggestions keep their wording for the moderation audit trail but lose the
    link to the person who made them.
    """
    user = require_user(principal)
    with connect() as conn:
        row = conn.execute("SELECT password_hash, role, auth_provider, username FROM users WHERE id = ?",
                           (user.id,)).fetchone()
        if row["auth_provider"] != "password":
            # No password exists: typing the username is the confirmation.
            if password.strip().lower() != row["username"].lower():
                raise AuthError("Type your username exactly to confirm - account not deleted.")
        elif not verify_password(password, row["password_hash"]):
            raise AuthError("Password incorrect - account not deleted.")
        if row["role"] == "admin":
            raise AuthError("The administrator account is managed through .env and cannot be deleted here.")
        conn.execute("DELETE FROM users WHERE id = ?", (user.id,))


def bootstrap_admin() -> str | None:
    """Create or refresh the administrator account from `.env`. Idempotent.

    The environment is authoritative: if someone else somehow holds the reserved
    username, it is promoted *and* its password replaced, so only the person with
    access to `.env` can sign in as the administrator.
    """
    username = _admin_username()
    password = os.getenv("DRUGSCOPE_ADMIN_PASSWORD") or ""
    if not username:
        return None
    if len(password) < MIN_PASSWORD_LENGTH:
        return (
            f"DRUGSCOPE_ADMIN_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters; "
            "the admin account was not created."
        )
    if not USERNAME_RE.match(username):
        return "DRUGSCOPE_ADMIN_USERNAME has invalid characters; the admin account was not created."

    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
                (username, hash_password(password), now_iso()),
            )
        elif row["role"] != "admin" or not verify_password(password, row["password_hash"]) or not row["is_active"]:
            conn.execute(
                "UPDATE users SET role = 'admin', is_active = 1, password_hash = ? WHERE id = ?",
                (hash_password(password), row["id"]),
            )
    return None
