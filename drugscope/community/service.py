"""Community research operations - the only code that touches the research tables.

Every public function takes the caller's `Principal` and checks it against the
database before doing anything, so permissions hold no matter which page, button or
script calls in. The split that matters:

* **Anyone** may read moderated, aggregated summaries: how many approved reports name
  a drug->symptom or drug<->food pair, and how many people said "me too".
* **Signed-in users** may submit reports, vote once per entry, suggest categories, and
  see or delete their own reports.
* **Administrators only** may see individual reports, moderate, export, and manage
  categories, hypotheses and accounts - and every such action is audit-logged.

Counts here are reporting volume from self-selected people. They are never turned
into rates or risk scores, and the UI says so wherever they appear.
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import io
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from ..sources.base import clean_text
from .auth import Principal, require_admin, require_user
from .db import connect, now_iso

ONSET_CHOICES = {
    "lt_1h": "Within 1 hour", "1_6h": "1-6 hours", "6_24h": "6-24 hours",
    "1_7d": "1-7 days", "gt_7d": "More than a week", "unknown": "Not sure",
}
SEVERITY_CHOICES = {
    "mild": "Mild - noticeable, did not get in the way",
    "moderate": "Moderate - got in the way of daily activities",
    "severe": "Severe - needed medical help or stopped normal activities",
}
FREQUENCY_CHOICES = {"once": "Once", "repeatedly": "Repeatedly"}
REPORT_STATUSES = ("pending", "approved", "rejected", "flagged")

MAX_SYMPTOMS = 8
MAX_CONTEXT = 500

# (max actions, per minutes) - generous for people, tight for scripts.
RATE_LIMITS = {
    "report": (10, 60), "suggestion": (5, 60), "vote": (120, 60), "assistant": (40, 60),
    "comment": (20, 60), "like": (200, 60), "flag": (20, 60),
}
MAX_COMMENT = 1000
# A comment this many people flag is hidden until an administrator reviews it.
AUTO_HIDE_FLAGS = 3


class ValidationError(Exception):
    """Input the server refuses, with a message the user can act on."""


class RateLimited(Exception):
    pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def audit(conn: sqlite3.Connection, admin: Principal, action: str, target: str = "", detail: str = "") -> None:
    conn.execute(
        "INSERT INTO admin_audit_log (admin_id, action, target, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (admin.id, action, target[:120], detail[:500], now_iso()),
    )


def _rows(cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def _since(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_rate(conn, action: str, user_id: int) -> None:
    """Count this action against the user's limit, or refuse it."""
    limit, minutes = RATE_LIMITS[action]
    count = conn.execute(
        "SELECT COUNT(*) FROM rate_events WHERE user_id = ? AND action = ? AND created_at >= ?",
        (user_id, action, _since(minutes)),
    ).fetchone()[0]
    if count >= limit:
        raise RateLimited(f"Too many {action}s in a short time - please try again later.")
    conn.execute(
        "INSERT INTO rate_events (user_id, action, created_at) VALUES (?, ?, ?)",
        (user_id, action, now_iso()),
    )


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def redact_identifiers(text: str) -> tuple[str, bool]:
    """Strip emails, phone numbers and links from free text before it is stored."""
    redacted = _URL.sub("[link removed]", text)
    redacted = _EMAIL.sub("[email removed]", redacted)
    redacted = _PHONE.sub("[number removed]", redacted)
    return redacted, redacted != text


def normalize_name(name: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    return " ".join(text.split())


def _clean_name(name: str, *, what: str) -> str:
    cleaned = clean_text(name, 80).strip()
    if not (2 <= len(cleaned) <= 60) or not re.search(r"[A-Za-z]", cleaned):
        raise ValidationError(f"Enter a {what} name of 2-60 characters.")
    return cleaned


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

def foods() -> list[dict[str, Any]]:
    with connect() as conn:
        return _rows(conn.execute(
            "SELECT id, name, keywords FROM foods WHERE status = 'approved' ORDER BY name"
        ))


def symptoms() -> list[dict[str, Any]]:
    with connect() as conn:
        return _rows(conn.execute(
            "SELECT id, name, meddra_term, description FROM symptom_categories "
            "WHERE status = 'approved' ORDER BY name"
        ))


def known_medications(term: str = "", limit: int = 8) -> list[dict[str, Any]]:
    """Medications already in the database that match what was typed."""
    like = f"%{normalize_name(term)}%"
    with connect() as conn:
        return _rows(conn.execute(
            "SELECT id, name, ingredient, brand_names, rxcui, verified FROM medications "
            "WHERE lower(name) LIKE ? OR lower(brand_names) LIKE ? ORDER BY verified DESC, name LIMIT ?",
            (like, like, limit),
        ))


def _medication_id(conn, candidate: dict[str, Any] | None, typed: str) -> int:
    """The medication row for a report, creating it the first time it is seen.

    A candidate resolved through RxNorm is stored under its active ingredient and
    marked verified; free text that RxNorm did not recognise is kept as typed and
    marked unverified for an administrator to review.
    """
    if candidate and candidate.get("ingredient"):
        name = candidate["ingredient"].strip().lower()
        brands = ", ".join(candidate.get("brands") or [])[:300]
        conn.execute(
            "INSERT INTO medications (name, ingredient, brand_names, rxcui, verified, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?) ON CONFLICT(name) DO UPDATE SET "
            "verified = 1, rxcui = excluded.rxcui, ingredient = excluded.ingredient, "
            "brand_names = CASE WHEN medications.brand_names = '' THEN excluded.brand_names "
            "ELSE medications.brand_names END",
            (name, name, brands, str(candidate.get("rxcui") or ""), now_iso()),
        )
    else:
        name = _clean_name(typed, what="medication").lower()
        conn.execute(
            "INSERT OR IGNORE INTO medications (name, verified, created_at) VALUES (?, 0, ?)",
            (name, now_iso()),
        )
    return conn.execute("SELECT id FROM medications WHERE name = ?", (name,)).fetchone()[0]


def medications_with_reports() -> list[dict[str, Any]]:
    """Medications that appear in at least one approved report - for public filters."""
    with connect() as conn:
        return _rows(conn.execute(
            "SELECT DISTINCT m.id, m.name FROM medications m JOIN reaction_reports r "
            "ON (r.medication_id = m.id OR r.other_medication_id = m.id) WHERE r.status = 'approved' "
            "ORDER BY m.name"))


def find_medication(name: str) -> dict[str, Any] | None:
    """A medication already in the database, by ingredient or as-typed name.

    Read-only on purpose: looking something up must not create rows.
    """
    with connect() as conn:
        row = conn.execute("SELECT * FROM medications WHERE name = ?", ((name or "").strip().lower(),)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

REPORT_TYPES = ("side_effect", "interaction", "drug_interaction")


def submit_report(
    principal: Principal,
    *,
    report_type: str,
    medication_typed: str,
    medication_candidate: dict[str, Any] | None,
    symptom_ids: list[int],
    food_id: int | None,
    onset: str,
    severity: str,
    frequency: str,
    context: str,
    consent: bool,
    other_medication_typed: str = "",
    other_medication_candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Store one experience as `pending` until an administrator reviews it."""
    user = require_user(principal)
    if not consent:
        raise ValidationError("Please confirm the consent statement before submitting.")
    if report_type not in REPORT_TYPES:
        raise ValidationError("Unknown report type.")
    symptom_ids = sorted({int(s) for s in symptom_ids or []})
    if not (1 <= len(symptom_ids) <= MAX_SYMPTOMS):
        raise ValidationError(f"Choose between 1 and {MAX_SYMPTOMS} symptoms.")
    if report_type == "interaction" and not food_id:
        raise ValidationError("Choose the food or drink involved.")
    if report_type == "drug_interaction" and not (other_medication_typed or "").strip():
        raise ValidationError("Enter the second medication.")
    if report_type != "drug_interaction":
        other_medication_typed, other_medication_candidate = "", None
    if onset not in ONSET_CHOICES or severity not in SEVERITY_CHOICES or frequency not in FREQUENCY_CHOICES:
        raise ValidationError("Complete the timing, severity and frequency fields.")
    if len(context or "") > MAX_CONTEXT:
        raise ValidationError(f"Keep the context under {MAX_CONTEXT} characters.")
    context, redacted = redact_identifiers(clean_text(context or "", MAX_CONTEXT + 50))
    typed = clean_text(medication_typed, 80)

    with connect(immediate=True) as conn:
        _check_rate(conn, "report", user.id)
        approved = {r[0] for r in conn.execute("SELECT id FROM symptom_categories WHERE status = 'approved'")}
        if not set(symptom_ids) <= approved:
            raise ValidationError("One of the symptoms is not an available category.")
        if food_id is not None and not conn.execute(
            "SELECT 1 FROM foods WHERE id = ? AND status = 'approved'", (food_id,)
        ).fetchone():
            raise ValidationError("That food or drink is not an available category.")

        med_id = _medication_id(conn, medication_candidate, typed)
        other_typed = clean_text(other_medication_typed, 80)
        other_id = _medication_id(conn, other_medication_candidate, other_typed) if other_typed else None
        if other_id is not None and other_id == med_id:
            raise ValidationError("Choose two different medications.")
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pair = sorted([med_id, other_id]) if other_id else [med_id]
        fingerprint = hashlib.sha256(
            f"{user.id}|{report_type}|{pair}|{food_id or 0}|{symptom_ids}|{day}".encode()
        ).hexdigest()
        try:
            cursor = conn.execute(
                "INSERT INTO reaction_reports (user_id, report_type, medication_id, medication_as_entered, "
                "food_id, other_medication_id, other_medication_as_entered, onset, severity, frequency, "
                "context, fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user.id, report_type, med_id, typed or "", food_id, other_id, other_typed, onset,
                 severity, frequency, context, fingerprint, now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            if "fingerprint" in str(exc):
                raise ValidationError("You already submitted this same report today.") from exc
            raise
        report_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO report_symptoms (report_id, symptom_id) VALUES (?, ?)",
            [(report_id, s) for s in symptom_ids],
        )
    return {"id": report_id, "redacted": redacted}


def my_reports(principal: Principal) -> list[dict[str, Any]]:
    user = require_user(principal)
    with connect() as conn:
        return _rows(conn.execute(
            "SELECT r.id, r.created_at, r.report_type, r.status, m.name AS medication, "
            "m2.name AS other_medication, f.name AS food, r.severity, r.onset, r.frequency, "
            "(SELECT group_concat(s.name, ', ') FROM report_symptoms rs "
            " JOIN symptom_categories s ON s.id = rs.symptom_id WHERE rs.report_id = r.id) AS symptoms "
            "FROM reaction_reports r JOIN medications m ON m.id = r.medication_id "
            "LEFT JOIN medications m2 ON m2.id = r.other_medication_id "
            "LEFT JOIN foods f ON f.id = r.food_id WHERE r.user_id = ? ORDER BY r.created_at DESC",
            (user.id,),
        ))


def delete_my_report(principal: Principal, report_id: int) -> None:
    user = require_user(principal)
    with connect() as conn:
        deleted = conn.execute(
            "DELETE FROM reaction_reports WHERE id = ? AND user_id = ?", (report_id, user.id)
        ).rowcount
    if not deleted:
        raise ValidationError("Report not found.")


def _ensure_entries(conn, report_id: int) -> None:
    """Create the public, votable entries an approved report belongs to."""
    report = conn.execute("SELECT * FROM reaction_reports WHERE id = ?", (report_id,)).fetchone()
    stamp = now_iso()
    if report["report_type"] == "drug_interaction":
        # One entry per unordered pair: (A, B) and (B, A) are the same combination.
        low, high = sorted([report["medication_id"], report["other_medication_id"]])
        conn.execute(
            "INSERT OR IGNORE INTO reaction_entries (kind, medication_id, other_medication_id, created_at) "
            "VALUES ('drug_interaction', ?, ?, ?)", (low, high, stamp),
        )
        return
    for (symptom_id,) in conn.execute("SELECT symptom_id FROM report_symptoms WHERE report_id = ?", (report_id,)).fetchall():
        conn.execute(
            "INSERT OR IGNORE INTO reaction_entries (kind, medication_id, symptom_id, created_at) "
            "VALUES ('side_effect', ?, ?, ?)",
            (report["medication_id"], symptom_id, stamp),
        )
    if report["food_id"]:
        conn.execute(
            "INSERT OR IGNORE INTO reaction_entries (kind, medication_id, food_id, created_at) "
            "VALUES ('interaction', ?, ?, ?)",
            (report["medication_id"], report["food_id"], stamp),
        )


# --------------------------------------------------------------------------- #
# Public aggregates and voting
# --------------------------------------------------------------------------- #

# Approved reports behind an entry. A drug-drug report belongs to its pair in either
# order, and is not attributed to either drug alone as a side effect.
_PAIR_MATCH = (
    "r.report_type = 'drug_interaction' AND ("
    "(r.medication_id = e.medication_id AND r.other_medication_id = e.other_medication_id) OR "
    "(r.medication_id = e.other_medication_id AND r.other_medication_id = e.medication_id))"
)


def _entry_aggregate(fn: str) -> str:
    return f"""
    CASE e.kind
      WHEN 'side_effect' THEN (
        SELECT {fn.format('DISTINCT r.id')} FROM reaction_reports r
        JOIN report_symptoms rs ON rs.report_id = r.id
        WHERE r.status = 'approved' AND r.report_type != 'drug_interaction'
          AND r.medication_id = e.medication_id AND rs.symptom_id = e.symptom_id)
      WHEN 'interaction' THEN (
        SELECT {fn.format('*')} FROM reaction_reports r
        WHERE r.status = 'approved' AND r.medication_id = e.medication_id AND r.food_id = e.food_id)
      ELSE (
        SELECT {fn.format('*')} FROM reaction_reports r WHERE r.status = 'approved' AND {_PAIR_MATCH})
    END
"""


_ENTRY_REPORTS = _entry_aggregate("COUNT({})")
_ENTRY_LATEST = _entry_aggregate("MAX(r.created_at)")


def public_entries(
    viewer: Principal | None = None,
    *,
    kind: str | None = None,
    medication_id: int | None = None,
    food_id: int | None = None,
    other_medication_id: int | None = None,
    sort: str = "confirmations",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Moderated, aggregated pairs - never individual reports or their free text.

    `medication_id` matches either side of a drug-drug pair; with
    `other_medication_id` as well, only that exact pair (in either order).
    """
    where, params = ["1 = 1"], []
    if kind:
        where.append("e.kind = ?"); params.append(kind)
    if medication_id and other_medication_id:
        low, high = sorted([medication_id, other_medication_id])
        where.append("e.medication_id = ? AND e.other_medication_id = ?"); params += [low, high]
    elif medication_id:
        where.append("(e.medication_id = ? OR e.other_medication_id = ?)"); params += [medication_id, medication_id]
    if food_id:
        where.append("e.food_id = ?"); params.append(food_id)
    order = "latest DESC" if sort == "recent" else "yes_votes DESC, reports DESC"
    viewer_id = viewer.id if viewer else -1
    with connect() as conn:
        rows = _rows(conn.execute(
            f"SELECT * FROM (SELECT e.id, e.kind, m.name AS medication, m.id AS medication_id, "
            f"m2.name AS other_medication, e.other_medication_id, "
            f"s.name AS symptom, f.name AS food, f.id AS food_id, "
            f"{_ENTRY_REPORTS} AS reports, {_ENTRY_LATEST} AS latest, "
            f"(SELECT COUNT(*) FROM votes v WHERE v.entry_id = e.id AND v.value = 1) AS yes_votes, "
            f"(SELECT COUNT(*) FROM votes v WHERE v.entry_id = e.id AND v.value = -1) AS no_votes, "
            f"(SELECT COUNT(*) FROM entry_comments c WHERE c.entry_id = e.id AND c.status = 'visible') AS comments, "
            f"(SELECT v.value FROM votes v WHERE v.entry_id = e.id AND v.user_id = ?) AS my_vote "
            f"FROM reaction_entries e JOIN medications m ON m.id = e.medication_id "
            f"LEFT JOIN medications m2 ON m2.id = e.other_medication_id "
            f"LEFT JOIN symptom_categories s ON s.id = e.symptom_id LEFT JOIN foods f ON f.id = e.food_id "
            f"WHERE {' AND '.join(where)}) WHERE reports >= 1 ORDER BY {order} LIMIT ?",
            (viewer_id, *params, limit),
        ))
        for row in rows:
            # What people reported alongside a pair - aggregated, top five.
            if row["kind"] == "interaction":
                match, args = "r.medication_id = ? AND r.food_id = ?", (row["medication_id"], row["food_id"])
            elif row["kind"] == "drug_interaction":
                a, b = row["medication_id"], row["other_medication_id"]
                match = ("r.report_type = 'drug_interaction' AND ((r.medication_id = ? AND r.other_medication_id = ?) "
                         "OR (r.medication_id = ? AND r.other_medication_id = ?))")
                args = (a, b, b, a)
            else:
                continue
            row["symptoms"] = _rows(conn.execute(
                "SELECT s.name, COUNT(*) AS n FROM reaction_reports r "
                "JOIN report_symptoms rs ON rs.report_id = r.id "
                "JOIN symptom_categories s ON s.id = rs.symptom_id "
                f"WHERE r.status = 'approved' AND {match} GROUP BY s.name ORDER BY n DESC LIMIT 5",
                args,
            ))
    return rows


def _entry_is_public(conn, entry_id: int) -> bool:
    row = conn.execute(
        f"SELECT {_ENTRY_REPORTS} AS reports FROM reaction_entries e WHERE e.id = ?", (entry_id,)
    ).fetchone()
    return bool(row and row["reports"])


def cast_vote(principal: Principal, entry_id: int, value: int) -> None:
    """One active vote per person per entry - enforced by UNIQUE(user_id, entry_id)."""
    user = require_user(principal)
    if value not in (1, -1):
        raise ValidationError("Invalid vote.")
    with connect(immediate=True) as conn:
        if not _entry_is_public(conn, entry_id):
            raise ValidationError("That entry is not open for voting.")
        _check_rate(conn, "vote", user.id)
        stamp = now_iso()
        conn.execute(
            "INSERT INTO votes (user_id, entry_id, value, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, entry_id) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (user.id, entry_id, value, stamp, stamp),
        )


def remove_vote(principal: Principal, entry_id: int) -> None:
    user = require_user(principal)
    with connect() as conn:
        conn.execute("DELETE FROM votes WHERE user_id = ? AND entry_id = ?", (user.id, entry_id))


# --------------------------------------------------------------------------- #
# Comments and likes
# --------------------------------------------------------------------------- #

def comments(viewer: Principal | None, entry_id: int) -> list[dict[str, Any]]:
    """Visible comments on an entry, oldest first, each with its replies nested."""
    viewer_id = viewer.id if viewer else -1
    with connect() as conn:
        rows = _rows(conn.execute(
            "SELECT c.id, c.parent_id, c.body, c.created_at, c.user_id, u.username, "
            "(SELECT COUNT(*) FROM comment_likes l WHERE l.comment_id = c.id) AS likes, "
            "EXISTS (SELECT 1 FROM comment_likes l WHERE l.comment_id = c.id AND l.user_id = ?) AS liked, "
            "EXISTS (SELECT 1 FROM comment_flags f WHERE f.comment_id = c.id AND f.user_id = ?) AS flagged "
            "FROM entry_comments c JOIN users u ON u.id = c.user_id "
            "WHERE c.entry_id = ? AND c.status = 'visible' ORDER BY c.created_at, c.id",
            (viewer_id, viewer_id, entry_id)))
    top: list[dict[str, Any]] = []
    by_id: dict[int, dict[str, Any]] = {}
    for row in rows:
        row["mine"] = row.pop("user_id") == viewer_id
        row["replies"] = []
        by_id[row["id"]] = row
    for row in rows:
        if not row["parent_id"]:
            top.append(row)
        elif row["parent_id"] in by_id:
            by_id[row["parent_id"]]["replies"].append(row)
        # A reply whose comment is hidden is hidden with it - never promoted to the top.
    return top


def add_comment(principal: Principal, entry_id: int, body: str, *, parent_id: int | None = None) -> int:
    """Publish a comment (or a reply) on a public entry."""
    user = require_user(principal)
    body = (body or "").strip()
    if len(body) < 2:
        raise ValidationError("Write a comment first.")
    if len(body) > MAX_COMMENT:
        raise ValidationError(f"Keep comments under {MAX_COMMENT} characters.")
    body, _ = redact_identifiers(clean_text(body, MAX_COMMENT + 50))
    with connect(immediate=True) as conn:
        if not _entry_is_public(conn, entry_id):
            raise ValidationError("That entry is not open for comments.")
        if parent_id is not None:
            parent = conn.execute("SELECT id, entry_id, parent_id, status FROM entry_comments WHERE id = ?",
                                  (parent_id,)).fetchone()
            if parent is None or parent["entry_id"] != entry_id or parent["status"] != "visible":
                raise ValidationError("You can't reply to that comment.")
            parent_id = parent["parent_id"] or parent["id"]  # one level of replies, like a thread
        _check_rate(conn, "comment", user.id)
        return int(conn.execute(
            "INSERT INTO entry_comments (entry_id, user_id, parent_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
            (entry_id, user.id, parent_id, body, now_iso())).lastrowid)


def delete_comment(principal: Principal, comment_id: int) -> None:
    """Authors may delete their own comments (and with them, the replies)."""
    user = require_user(principal)
    with connect() as conn:
        if not conn.execute("DELETE FROM entry_comments WHERE id = ? AND user_id = ?", (comment_id, user.id)).rowcount:
            raise ValidationError("You can only delete your own comments.")


def toggle_like(principal: Principal, comment_id: int) -> bool:
    """Like or unlike a comment. Returns whether it is now liked."""
    user = require_user(principal)
    with connect(immediate=True) as conn:
        if not conn.execute("SELECT 1 FROM entry_comments WHERE id = ? AND status = 'visible'",
                            (comment_id,)).fetchone():
            raise ValidationError("That comment is no longer available.")
        if conn.execute("DELETE FROM comment_likes WHERE user_id = ? AND comment_id = ?",
                        (user.id, comment_id)).rowcount:
            return False
        _check_rate(conn, "like", user.id)
        conn.execute("INSERT INTO comment_likes (user_id, comment_id, created_at) VALUES (?, ?, ?)",
                     (user.id, comment_id, now_iso()))
        return True


def flag_comment(principal: Principal, comment_id: int, reason: str = "") -> bool:
    """Report a comment. Returns True if it is now hidden pending review."""
    user = require_user(principal)
    with connect(immediate=True) as conn:
        row = conn.execute("SELECT user_id FROM entry_comments WHERE id = ? AND status = 'visible'",
                           (comment_id,)).fetchone()
        if row is None:
            raise ValidationError("That comment is no longer available.")
        if row["user_id"] == user.id:
            raise ValidationError("You can delete your own comment instead.")
        _check_rate(conn, "flag", user.id)
        try:
            conn.execute("INSERT INTO comment_flags (user_id, comment_id, reason, created_at) VALUES (?, ?, ?, ?)",
                         (user.id, comment_id, clean_text(reason, 200), now_iso()))
        except sqlite3.IntegrityError as exc:
            raise ValidationError("You have already reported this comment.") from exc
        flags = conn.execute("SELECT COUNT(*) FROM comment_flags WHERE comment_id = ?", (comment_id,)).fetchone()[0]
        if flags >= AUTO_HIDE_FLAGS:
            conn.execute("UPDATE entry_comments SET status = 'hidden' WHERE id = ?", (comment_id,))
            return True
    return False


# --------------------------------------------------------------------------- #
# Category suggestions
# --------------------------------------------------------------------------- #

_CATEGORY_TABLE = {"symptom": "symptom_categories", "food": "foods"}


def similar_categories(kind: str, name: str, limit: int = 5) -> list[dict[str, str]]:
    """Existing categories and open suggestions that look like `name`."""
    wanted = normalize_name(name)
    if len(wanted) < 2:
        return []
    with connect() as conn:
        existing = [(r["name"], "existing") for r in conn.execute(
            f"SELECT name FROM {_CATEGORY_TABLE[kind]} WHERE status = 'approved'")]
        pending = [(r["submitted_name"], "pending") for r in conn.execute(
            "SELECT submitted_name FROM category_suggestions WHERE kind = ? AND status = 'pending'", (kind,))]
    scored = []
    for label, state in existing + pending:
        candidate = normalize_name(label)
        ratio = difflib.SequenceMatcher(None, wanted, candidate).ratio()
        if ratio >= 0.72 or wanted in candidate or candidate in wanted:
            scored.append((ratio, {"name": label, "state": state}))
    scored.sort(key=lambda pair: -pair[0])
    return [item for _, item in scored[:limit]]


def suggest_category(principal: Principal, kind: str, name: str, description: str = "") -> int:
    user = require_user(principal)
    if kind not in _CATEGORY_TABLE:
        raise ValidationError("Unknown category type.")
    submitted = _clean_name(name, what="category")
    normalized = normalize_name(submitted)
    description = redact_identifiers(clean_text(description or "", 300))[0]
    with connect(immediate=True) as conn:
        _check_rate(conn, "suggestion", user.id)
        for row in conn.execute(f"SELECT name FROM {_CATEGORY_TABLE[kind]} WHERE status = 'approved'"):
            if normalize_name(row["name"]) == normalized:
                raise ValidationError(f"'{row['name']}' already exists - choose it from the list.")
        try:
            cursor = conn.execute(
                "INSERT INTO category_suggestions (kind, submitted_name, normalized_name, description, "
                "user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (kind, submitted, normalized, description, user.id, now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationError("Someone has already suggested this - it is awaiting review.") from exc
    return int(cursor.lastrowid)


# --------------------------------------------------------------------------- #
# Hypotheses (public view)
# --------------------------------------------------------------------------- #

_HYPOTHESIS_SELECT = (
    "SELECT h.*, m.name AS medication, m2.name AS other_medication, f.name AS food, s.name AS symptom "
    "FROM hypotheses h LEFT JOIN medications m ON m.id = h.medication_id "
    "LEFT JOIN medications m2 ON m2.id = h.other_medication_id LEFT JOIN foods f ON f.id = h.food_id "
    "LEFT JOIN symptom_categories s ON s.id = h.symptom_id"
)


def public_hypotheses(*, medication_id: int | None = None, food_id: int | None = None) -> list[dict[str, Any]]:
    where, params = ["h.is_public = 1"], []
    if medication_id:
        where.append("(h.medication_id = ? OR h.other_medication_id = ?)"); params += [medication_id, medication_id]
    if food_id:
        where.append("h.food_id = ?"); params.append(food_id)
    with connect() as conn:
        return _rows(conn.execute(
            f"{_HYPOTHESIS_SELECT} WHERE {' AND '.join(where)} ORDER BY h.updated_at DESC", params
        ))


# --------------------------------------------------------------------------- #
# Insights - exploratory summaries of the research data
# --------------------------------------------------------------------------- #

def insights(viewer: Principal | None = None, *, scope: str = "public",
             medication_id: int | None = None) -> dict[str, Any]:
    """Aggregate statistics for the Insights page and the admin workspace.

    `scope="public"` reads approved reports only - safe for anyone. `scope="admin"`
    reads every report and adds moderation and engagement detail; it requires the
    administrator role. Nothing here is per-person: every figure is a count over many
    reports, and none of it is a rate - reporters choose themselves.
    """
    admin = scope == "admin"
    if admin:
        require_admin(viewer)
    where = ["1 = 1" if admin else "r.status = 'approved'"]
    params: list[Any] = []
    if medication_id:
        where.append("(r.medication_id = ? OR r.other_medication_id = ?)")
        params += [medication_id, medication_id]
    scope_sql = " AND ".join(where)

    def rows(sql: str, extra: tuple = ()) -> list[dict[str, Any]]:
        return _rows(conn.execute(sql, (*params, *extra)))

    def one(sql: str) -> Any:
        return conn.execute(sql, params).fetchone()[0]

    entry_filter = ("WHERE (e.medication_id = ? OR e.other_medication_id = ?)" if medication_id else "")
    entry_params = (medication_id, medication_id) if medication_id else ()

    with connect() as conn:
        data: dict[str, Any] = {
            "reports": one(f"SELECT COUNT(*) FROM reaction_reports r WHERE {scope_sql}"),
            "contributors": one(f"SELECT COUNT(DISTINCT r.user_id) FROM reaction_reports r WHERE {scope_sql}"),
            "medications": one(
                f"SELECT COUNT(*) FROM (SELECT r.medication_id FROM reaction_reports r WHERE {scope_sql} "
                f"UNION SELECT r.other_medication_id FROM reaction_reports r WHERE {scope_sql} "
                f"AND r.other_medication_id IS NOT NULL)") if not medication_id else 1,
            "symptoms": one(f"SELECT COUNT(DISTINCT rs.symptom_id) FROM reaction_reports r "
                            f"JOIN report_symptoms rs ON rs.report_id = r.id WHERE {scope_sql}"),
            "first": one(f"SELECT MIN(r.created_at) FROM reaction_reports r WHERE {scope_sql}"),
            "last": one(f"SELECT MAX(r.created_at) FROM reaction_reports r WHERE {scope_sql}"),
            "type_mix": rows(f"SELECT r.report_type AS type, COUNT(*) AS reports FROM reaction_reports r "
                             f"WHERE {scope_sql} GROUP BY r.report_type ORDER BY reports DESC"),
            "severity_mix": rows(f"SELECT r.severity, COUNT(*) AS reports FROM reaction_reports r "
                                 f"WHERE {scope_sql} GROUP BY r.severity"),
            "onset_mix": rows(f"SELECT r.onset, COUNT(*) AS reports FROM reaction_reports r "
                              f"WHERE {scope_sql} GROUP BY r.onset"),
            "frequency_mix": rows(f"SELECT r.frequency, COUNT(*) AS reports FROM reaction_reports r "
                                  f"WHERE {scope_sql} GROUP BY r.frequency"),
            "top_symptoms": rows(f"SELECT s.name AS symptom, COUNT(*) AS reports FROM reaction_reports r "
                                 f"JOIN report_symptoms rs ON rs.report_id = r.id "
                                 f"JOIN symptom_categories s ON s.id = rs.symptom_id WHERE {scope_sql} "
                                 f"GROUP BY s.name ORDER BY reports DESC LIMIT 12"),
            "top_medications": rows(
                f"SELECT name AS medication, COUNT(*) AS reports FROM ("
                f"SELECT m.name FROM reaction_reports r JOIN medications m ON m.id = r.medication_id WHERE {scope_sql} "
                f"UNION ALL SELECT m.name FROM reaction_reports r JOIN medications m ON m.id = r.other_medication_id "
                f"WHERE {scope_sql}) GROUP BY name ORDER BY reports DESC LIMIT 12", tuple(params)),
            "top_foods": rows(f"SELECT f.name AS food, COUNT(*) AS reports FROM reaction_reports r "
                              f"JOIN foods f ON f.id = r.food_id WHERE {scope_sql} "
                              f"GROUP BY f.name ORDER BY reports DESC LIMIT 10"),
            "reports_by_week": rows(
                f"SELECT date(substr(r.created_at, 1, 10), '-6 days', 'weekday 1') AS week, COUNT(*) AS reports "
                f"FROM reaction_reports r WHERE {scope_sql} GROUP BY week ORDER BY week"),
        }
        votes = conn.execute(
            f"SELECT COUNT(*) AS total, SUM(v.value = 1) AS yes, SUM(v.value = -1) AS no, "
            f"COUNT(DISTINCT v.user_id) AS voters FROM votes v JOIN reaction_entries e ON e.id = v.entry_id "
            f"{entry_filter}", entry_params,
        ).fetchone()
        data["votes"] = {k: int(votes[k] or 0) for k in ("total", "yes", "no", "voters")}
        on_entries = ("WHERE (e.medication_id = ? OR e.other_medication_id = ?)" if medication_id
                      else "WHERE 1 = 1") + " AND c.status = 'visible'"
        talk = conn.execute(
            f"SELECT COUNT(*) AS comments, COUNT(DISTINCT c.user_id) AS commenters FROM entry_comments c "
            f"JOIN reaction_entries e ON e.id = c.entry_id {on_entries}", entry_params).fetchone()
        likes = conn.execute(
            f"SELECT COUNT(*) FROM comment_likes l JOIN entry_comments c ON c.id = l.comment_id "
            f"JOIN reaction_entries e ON e.id = c.entry_id {on_entries}", entry_params).fetchone()[0]
        data["discussion"] = {"comments": int(talk["comments"] or 0),
                              "commenters": int(talk["commenters"] or 0), "likes": int(likes or 0)}
        data["votes_by_week"] = _rows(conn.execute(
            f"SELECT date(substr(v.created_at, 1, 10), '-6 days', 'weekday 1') AS week, COUNT(*) AS votes "
            f"FROM votes v JOIN reaction_entries e ON e.id = v.entry_id {entry_filter} "
            f"GROUP BY week ORDER BY week", entry_params))
        if admin:
            data["by_status"] = {r["status"]: r["n"] for r in conn.execute(
                f"SELECT r.status, COUNT(*) AS n FROM reaction_reports r WHERE {scope_sql} GROUP BY r.status",
                params)}
            data["plan_mix"] = _rows(conn.execute(
                "SELECT CASE WHEN plan != 'free' AND plan_expires_at IS NOT NULL AND plan_expires_at <= ? "
                "THEN 'free' ELSE plan END AS plan, COUNT(*) AS users FROM users WHERE role = 'user' "
                "GROUP BY 1", (now_iso(),)))
            data["credits_by_day"] = _rows(conn.execute(
                "SELECT substr(created_at, 1, 10) AS day, SUM(credits) AS credits, COUNT(*) AS runs "
                "FROM usage_events WHERE created_at >= ? GROUP BY day ORDER BY day", (_since(60 * 24 * 30),)))
    data["most_confirmed"] = public_entries(None, medication_id=medication_id, sort="confirmations", limit=10)
    return data


# --------------------------------------------------------------------------- #
# Administration - every function starts with require_admin
# --------------------------------------------------------------------------- #

def admin_overview(admin: Principal) -> dict[str, Any]:
    require_admin(admin)
    with connect() as conn:
        one = lambda sql, *p: conn.execute(sql, p).fetchone()[0]  # noqa: E731
        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM reaction_reports GROUP BY status")}
        return {
            "reports": sum(by_status.values()),
            "by_status": by_status,
            "medications": one("SELECT COUNT(DISTINCT medication_id) FROM reaction_reports"),
            "foods": one("SELECT COUNT(DISTINCT food_id) FROM reaction_reports WHERE food_id IS NOT NULL"),
            "symptoms": one("SELECT COUNT(DISTINCT symptom_id) FROM report_symptoms"),
            "pairs": one("SELECT COUNT(*) FROM (SELECT DISTINCT medication_id, food_id FROM reaction_reports "
                         "WHERE food_id IS NOT NULL)"),
            "yes_votes": one("SELECT COUNT(*) FROM votes WHERE value = 1"),
            "no_votes": one("SELECT COUNT(*) FROM votes WHERE value = -1"),
            "pending_suggestions": one("SELECT COUNT(*) FROM category_suggestions WHERE status = 'pending'"),
            "users": one("SELECT COUNT(*) FROM users WHERE role = 'user'"),
            "pro_users": one("SELECT COUNT(*) FROM users WHERE role = 'user' AND plan = 'pro' "
                             "AND (plan_expires_at IS NULL OR plan_expires_at > ?)", now_iso()),
            "first_report": one("SELECT MIN(created_at) FROM reaction_reports"),
            "last_report": one("SELECT MAX(created_at) FROM reaction_reports"),
            "trend": _rows(conn.execute(
                "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS reports FROM reaction_reports "
                "WHERE created_at >= ? GROUP BY day ORDER BY day", (_since(60 * 24 * 90),))),
            "top_drug_symptom": _rows(conn.execute(
                "SELECT m.name AS medication, s.name AS symptom, COUNT(*) AS reports, "
                "SUM(r.status = 'approved') AS approved FROM reaction_reports r "
                "JOIN report_symptoms rs ON rs.report_id = r.id "
                "JOIN symptom_categories s ON s.id = rs.symptom_id JOIN medications m ON m.id = r.medication_id "
                "GROUP BY m.name, s.name ORDER BY reports DESC LIMIT 15")),
            "top_food_drug": _rows(conn.execute(
                "SELECT f.name AS food, m.name AS medication, COUNT(*) AS reports, "
                "SUM(r.status = 'approved') AS approved FROM reaction_reports r "
                "JOIN foods f ON f.id = r.food_id JOIN medications m ON m.id = r.medication_id "
                "GROUP BY f.name, m.name ORDER BY reports DESC LIMIT 15")),
            "top_drug_drug": _rows(conn.execute(
                "SELECT MIN(m.name, m2.name) AS medication_a, MAX(m.name, m2.name) AS medication_b, "
                "COUNT(*) AS reports, SUM(r.status = 'approved') AS approved FROM reaction_reports r "
                "JOIN medications m ON m.id = r.medication_id JOIN medications m2 ON m2.id = r.other_medication_id "
                "WHERE r.report_type = 'drug_interaction' GROUP BY medication_a, medication_b "
                "ORDER BY reports DESC LIMIT 15")),
            "drug_pairs": one("SELECT COUNT(*) FROM (SELECT DISTINCT MIN(medication_id, other_medication_id), "
                              "MAX(medication_id, other_medication_id) FROM reaction_reports "
                              "WHERE other_medication_id IS NOT NULL)"),
        }


def _report_filters(medication: str = "", symptom: str = "", food: str = "", status: str = "",
                    report_type: str = "", date_from: str = "", date_to: str = "") -> tuple[str, list[Any]]:
    where, params = ["1 = 1"], []
    if medication:
        where.append("(lower(m.name) LIKE ? OR lower(IFNULL(m2.name, '')) LIKE ?)")
        params += [f"%{medication.lower()}%"] * 2
    if food:
        where.append("lower(f.name) LIKE ?"); params.append(f"%{food.lower()}%")
    if symptom:
        where.append("EXISTS (SELECT 1 FROM report_symptoms rs JOIN symptom_categories s ON s.id = rs.symptom_id "
                     "WHERE rs.report_id = r.id AND lower(s.name) LIKE ?)")
        params.append(f"%{symptom.lower()}%")
    if status:
        where.append("r.status = ?"); params.append(status)
    if report_type:
        where.append("r.report_type = ?"); params.append(report_type)
    if date_from:
        where.append("r.created_at >= ?"); params.append(f"{date_from}T00:00:00Z")
    if date_to:
        where.append("r.created_at <= ?"); params.append(f"{date_to}T23:59:59Z")
    return " AND ".join(where), params


_REPORT_SELECT = (
    "SELECT r.id, r.created_at, r.status, r.report_type, r.provenance, m.name AS medication, "
    "m.verified AS medication_verified, m.rxcui, r.medication_as_entered, "
    "m2.name AS other_medication, r.other_medication_as_entered, f.name AS food, "
    "(SELECT group_concat(s.name, '; ') FROM report_symptoms rs JOIN symptom_categories s "
    " ON s.id = rs.symptom_id WHERE rs.report_id = r.id) AS symptoms, "
    "r.onset, r.severity, r.frequency, r.context, r.user_id, r.reviewed_by, r.reviewed_at "
    "FROM reaction_reports r JOIN medications m ON m.id = r.medication_id "
    "LEFT JOIN medications m2 ON m2.id = r.other_medication_id "
    "LEFT JOIN foods f ON f.id = r.food_id"
)


def admin_reports(admin: Principal, *, limit: int = 500, **filters: str) -> list[dict[str, Any]]:
    require_admin(admin)
    where, params = _report_filters(**filters)
    with connect() as conn:
        return _rows(conn.execute(
            f"{_REPORT_SELECT} WHERE {where} ORDER BY r.created_at DESC LIMIT ?", (*params, limit)
        ))


def moderate_report(admin: Principal, report_id: int, status: str) -> None:
    who = require_admin(admin)
    if status not in REPORT_STATUSES:
        raise ValidationError("Unknown status.")
    with connect() as conn:
        updated = conn.execute(
            "UPDATE reaction_reports SET status = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
            (status, who.id, now_iso(), report_id),
        ).rowcount
        if not updated:
            raise ValidationError("Report not found.")
        if status == "approved":
            _ensure_entries(conn, report_id)
        audit(conn, who, f"report_{status}", f"report:{report_id}")


def admin_delete_report(admin: Principal, report_id: int) -> None:
    who = require_admin(admin)
    with connect() as conn:
        conn.execute("DELETE FROM reaction_reports WHERE id = ?", (report_id,))
        audit(conn, who, "report_deleted", f"report:{report_id}")


def admin_suggestions(admin: Principal, status: str = "pending") -> list[dict[str, Any]]:
    require_admin(admin)
    with connect() as conn:
        return _rows(conn.execute(
            "SELECT id, kind, submitted_name, normalized_name, description, status, created_at, "
            "resolved_category_id FROM category_suggestions WHERE (? = '' OR status = ?) "
            "ORDER BY created_at DESC", (status, status),
        ))


def resolve_suggestion(admin: Principal, suggestion_id: int, action: str, *,
                       merge_into_id: int | None = None, final_name: str = "") -> None:
    """Approve (creates the category), reject, or merge into an existing category.

    The submitted wording is kept as-is on the suggestion for auditing; the category
    that analysis uses carries the normalised name the administrator approved.
    """
    who = require_admin(admin)
    with connect(immediate=True) as conn:
        row = conn.execute("SELECT * FROM category_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        if row is None or row["status"] != "pending":
            raise ValidationError("That suggestion is not awaiting review.")
        table = _CATEGORY_TABLE[row["kind"]]
        stamp = now_iso()
        if action == "approve":
            name = _clean_name(final_name or row["submitted_name"].strip().capitalize(), what="category")
            clash = conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
            if clash:
                raise ValidationError(f"'{name}' already exists - merge into it instead.")
            if row["kind"] == "symptom":
                cursor = conn.execute(
                    "INSERT INTO symptom_categories (name, description, created_at) VALUES (?, ?, ?)",
                    (name, row["description"], stamp))
            else:
                cursor = conn.execute(
                    "INSERT INTO foods (name, keywords, created_at) VALUES (?, ?, ?)",
                    (name, normalize_name(name), stamp))
            resolved, status = cursor.lastrowid, "approved"
        elif action == "merge":
            if not merge_into_id or not conn.execute(
                f"SELECT 1 FROM {table} WHERE id = ?", (merge_into_id,)
            ).fetchone():
                raise ValidationError("Choose the existing category to merge into.")
            resolved, status = merge_into_id, "merged"
        elif action == "reject":
            resolved, status = None, "rejected"
        else:
            raise ValidationError("Unknown action.")
        conn.execute(
            "UPDATE category_suggestions SET status = ?, resolved_category_id = ?, reviewed_by = ?, "
            "reviewed_at = ? WHERE id = ?", (status, resolved, who.id, stamp, suggestion_id))
        audit(conn, who, f"suggestion_{status}", f"suggestion:{suggestion_id}",
              f"{row['kind']} '{row['submitted_name']}' -> {resolved}")


def retire_category(admin: Principal, kind: str, category_id: int) -> None:
    """Remove a category from the forms. Past reports keep their reference."""
    who = require_admin(admin)
    with connect() as conn:
        conn.execute(f"UPDATE {_CATEGORY_TABLE[kind]} SET status = 'retired' WHERE id = ?", (category_id,))
        audit(conn, who, "category_retired", f"{kind}:{category_id}")


def admin_hypotheses(admin: Principal) -> list[dict[str, Any]]:
    require_admin(admin)
    with connect() as conn:
        return _rows(conn.execute(f"{_HYPOTHESIS_SELECT} ORDER BY h.updated_at DESC"))


def save_hypothesis(admin: Principal, *, hypothesis_id: int | None = None, title: str, description: str,
                    medication_id: int | None, food_id: int | None, symptom_id: int | None,
                    status: str, is_public: bool, evidence_urls: str = "",
                    other_medication_id: int | None = None) -> int:
    who = require_admin(admin)
    title = clean_text(title, 160)
    description = clean_text(description, 3000)
    if len(title) < 5 or len(description) < 10:
        raise ValidationError("Give the hypothesis a title and a description.")
    if status not in ("proposed", "investigating", "supported", "not_supported"):
        raise ValidationError("Unknown status.")
    urls = "\n".join(u.strip() for u in (evidence_urls or "").splitlines()
                     if u.strip().startswith(("http://", "https://")))[:2000]
    stamp = now_iso()
    with connect() as conn:
        if hypothesis_id:
            conn.execute(
                "UPDATE hypotheses SET title = ?, description = ?, medication_id = ?, food_id = ?, "
                "symptom_id = ?, other_medication_id = ?, status = ?, is_public = ?, evidence_urls = ?, "
                "updated_at = ? WHERE id = ?",
                (title, description, medication_id, food_id, symptom_id, other_medication_id, status,
                 int(is_public), urls, stamp, hypothesis_id))
            target = hypothesis_id
        else:
            target = conn.execute(
                "INSERT INTO hypotheses (title, description, medication_id, food_id, symptom_id, "
                "other_medication_id, status, is_public, evidence_urls, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, description, medication_id, food_id, symptom_id, other_medication_id, status,
                 int(is_public), urls, who.id, stamp, stamp)).lastrowid
        audit(conn, who, "hypothesis_saved", f"hypothesis:{target}", f"{status}, public={is_public}")
    return int(target)


def admin_medications(admin: Principal) -> list[dict[str, Any]]:
    require_admin(admin)
    with connect() as conn:
        return _rows(conn.execute("SELECT id, name, verified FROM medications ORDER BY name"))


def admin_users(admin: Principal) -> list[dict[str, Any]]:
    require_admin(admin)
    with connect() as conn:
        return _rows(conn.execute(
            "SELECT u.id, u.username, u.role, u.plan, u.plan_expires_at, u.is_active, u.created_at, "
            "(SELECT COUNT(*) FROM reaction_reports r WHERE r.user_id = u.id) AS reports, "
            "(SELECT COALESCE(SUM(credits), 0) FROM usage_events e WHERE e.user_id = u.id "
            " AND e.created_at >= ?) AS credits_30d FROM users u ORDER BY u.created_at DESC",
            (_since(60 * 24 * 30),),
        ))


def set_user_active(admin: Principal, user_id: int, active: bool) -> None:
    who = require_admin(admin)
    if user_id == who.id:
        raise ValidationError("You cannot deactivate your own account.")
    with connect() as conn:
        conn.execute("UPDATE users SET is_active = ? WHERE id = ? AND role = 'user'", (int(active), user_id))
        audit(conn, who, "user_activated" if active else "user_deactivated", f"user:{user_id}")


def admin_comments(admin: Principal, *, flagged_only: bool = False, limit: int = 200) -> list[dict[str, Any]]:
    """Recent comments with their flag counts - the comment moderation queue."""
    require_admin(admin)
    with connect() as conn:
        return _rows(conn.execute(
            "SELECT * FROM (SELECT c.id, c.created_at, c.status, u.username, c.user_id, c.entry_id, "
            "m.name || ' ' || COALESCE('-> ' || s.name, '+ ' || f.name, '+ ' || m2.name, '') AS entry, "
            "c.parent_id IS NOT NULL AS is_reply, c.body, "
            "(SELECT COUNT(*) FROM comment_flags g WHERE g.comment_id = c.id) AS flags, "
            "(SELECT group_concat(g.reason, '; ') FROM comment_flags g WHERE g.comment_id = c.id) AS reasons, "
            "(SELECT COUNT(*) FROM comment_likes l WHERE l.comment_id = c.id) AS likes "
            "FROM entry_comments c JOIN users u ON u.id = c.user_id JOIN reaction_entries e ON e.id = c.entry_id "
            "JOIN medications m ON m.id = e.medication_id LEFT JOIN symptom_categories s ON s.id = e.symptom_id "
            "LEFT JOIN foods f ON f.id = e.food_id LEFT JOIN medications m2 ON m2.id = e.other_medication_id) "
            "WHERE (? = 0 OR flags > 0 OR status = 'hidden') ORDER BY flags DESC, created_at DESC LIMIT ?",
            (int(flagged_only), limit)))


def moderate_comment(admin: Principal, comment_id: int, action: str) -> None:
    """Show, hide or delete a comment. Showing it again clears its flags."""
    who = require_admin(admin)
    with connect() as conn:
        if action == "delete":
            conn.execute("DELETE FROM entry_comments WHERE id = ?", (comment_id,))
        elif action in ("visible", "hidden"):
            conn.execute("UPDATE entry_comments SET status = ? WHERE id = ?", (action, comment_id))
            if action == "visible":
                conn.execute("DELETE FROM comment_flags WHERE comment_id = ?", (comment_id,))
        else:
            raise ValidationError("Unknown action.")
        audit(conn, who, f"comment_{action}", f"comment:{comment_id}")


def admin_evidence(admin: Principal, limit: int = 300) -> list[dict[str, Any]]:
    require_admin(admin)
    with connect() as conn:
        return _rows(conn.execute(
            "SELECT id, provenance, source, kind, medication, food, symptom, title, url, excerpt, retrieved_at "
            "FROM evidence_sources ORDER BY retrieved_at DESC LIMIT ?", (limit,)))


def audit_log(admin: Principal, limit: int = 300) -> list[dict[str, Any]]:
    require_admin(admin)
    with connect() as conn:
        return _rows(conn.execute(
            "SELECT l.created_at, u.username AS admin, l.action, l.target, l.detail FROM admin_audit_log l "
            "LEFT JOIN users u ON u.id = l.admin_id ORDER BY l.created_at DESC LIMIT ?", (limit,)))


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

EXPORT_DATASETS = {
    "reports": "Individual reports (raw, including private context)",
    "votes": "Individual votes (internal user ids)",
    "entries": "Aggregated drug-symptom and drug-food entries",
    "comments": "Comments on community entries, with likes and flags",
    "suggestions": "Category suggestions and their outcomes",
    "hypotheses": "Research hypotheses",
    "evidence": "Imported official-source records",
}


def _csv_safe(value: Any) -> Any:
    """Defuse spreadsheet formula injection in user-submitted text."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def export_csv(admin: Principal, dataset: str, **filters: str) -> str:
    who = require_admin(admin)
    if dataset not in EXPORT_DATASETS:
        raise ValidationError("Unknown dataset.")
    with connect() as conn:
        if dataset == "reports":
            where, params = _report_filters(**filters)
            rows = _rows(conn.execute(f"{_REPORT_SELECT} WHERE {where} ORDER BY r.created_at", params))
        elif dataset == "votes":
            rows = _rows(conn.execute(
                "SELECT v.id, v.entry_id, e.kind, m.name AS medication, s.name AS symptom, f.name AS food, "
                "v.user_id, v.value, v.created_at, v.updated_at FROM votes v "
                "JOIN reaction_entries e ON e.id = v.entry_id JOIN medications m ON m.id = e.medication_id "
                "LEFT JOIN symptom_categories s ON s.id = e.symptom_id LEFT JOIN foods f ON f.id = e.food_id "
                "ORDER BY v.created_at"))
        elif dataset == "entries":
            rows = public_entries(None, limit=100000)
            for row in rows:
                row["symptoms"] = "; ".join(f"{s['name']} ({s['n']})" for s in row.get("symptoms") or [])
                row.pop("my_vote", None)
        elif dataset == "comments":
            rows = admin_comments(who, limit=100000)
        elif dataset == "suggestions":
            rows = _rows(conn.execute("SELECT * FROM category_suggestions ORDER BY created_at"))
        elif dataset == "hypotheses":
            rows = _rows(conn.execute(f"{_HYPOTHESIS_SELECT} ORDER BY h.created_at"))
        else:
            rows = _rows(conn.execute("SELECT * FROM evidence_sources ORDER BY retrieved_at"))
        audit(conn, who, "export_csv", dataset, f"{len(rows)} rows; filters={ {k: v for k, v in filters.items() if v} }")

    buffer = io.StringIO()
    if rows:
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()), extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_safe(v) for k, v in row.items()})
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #

def apply_retention() -> int:
    """Clear free-text context older than the retention period. Returns rows cleared.

    The structured fields stay for research; the free text - the part most likely
    to carry something personal - does not outlive its usefulness.
    """
    try:
        days = int(os.getenv("DRUGSCOPE_CONTEXT_RETENTION_DAYS") or 365)
    except ValueError:
        days = 365
    if days <= 0:
        return 0
    with connect() as conn:
        return conn.execute(
            "UPDATE reaction_reports SET context = '' WHERE context != '' AND created_at < ?",
            (_since(60 * 24 * days),),
        ).rowcount
