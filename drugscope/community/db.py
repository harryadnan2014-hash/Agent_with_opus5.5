"""The research database: one SQLite file, versioned migrations, seed vocabulary.

SQLite is the right size here. It ships with Python, needs no server, and every
constraint the community features depend on - foreign keys, uniqueness, check
constraints, partial and expression indexes - is enforced by the engine itself, so
a duplicate vote is rejected by the database even if application code is bypassed.

Migrations are append-only: each entry in `MIGRATIONS` runs once, in order, inside
a transaction, and `PRAGMA user_version` records how far a database has got. Never
edit a migration that has shipped - add a new one.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = _ROOT / "data" / "drugscope.db"

_lock = threading.Lock()
_ready: set[str] = set()


def db_path() -> Path:
    return Path(os.getenv("DRUGSCOPE_DB_PATH") or DEFAULT_PATH)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def connect(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """One short-lived connection per operation, committed on success.

    Streamlit serves each browser session on its own thread, so connections are
    never shared; WAL mode lets readers proceed while one writer commits.
    `immediate=True` takes the write lock up front, for check-then-write
    operations (quota charges, code redemption) that must not interleave.
    """
    path = db_path()
    _ensure_ready(path)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    # A running app picks up code updates without restarting, so the schema is
    # re-checked on every connection (one cheap pragma) rather than once per process.
    if conn.execute("PRAGMA user_version").fetchone()[0] < len(MIGRATIONS):
        migrate(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _ensure_ready(path: Path) -> None:
    key = str(path)
    if key in _ready:
        return
    with _lock:
        if key in _ready:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=15, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            migrate(conn)
        finally:
            conn.close()
        _ready.add(key)


def reset_for_tests() -> None:
    _ready.clear()


# --------------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------------- #

_TS = "TEXT NOT NULL"

MIGRATIONS: list[str] = [
    # 1 - accounts, quotas, community research data, moderation, audit
    f"""
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
        plan TEXT NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'pro')),
        plan_expires_at TEXT,
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        created_at {_TS}
    );

    CREATE TABLE login_attempts (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL COLLATE NOCASE,
        success INTEGER NOT NULL CHECK (success IN (0, 1)),
        created_at {_TS}
    );
    CREATE INDEX ix_login_attempts ON login_attempts (username, created_at);

    -- Every rate-limited action, so limits count actions rather than rows
    -- (a vote changed ten times is still one row in `votes`).
    CREATE TABLE rate_events (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        action TEXT NOT NULL,
        created_at {_TS}
    );
    CREATE INDEX ix_rate_events ON rate_events (user_id, action, created_at);

    CREATE TABLE usage_events (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        credits INTEGER NOT NULL CHECK (credits >= 0),
        kind TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        created_at {_TS}
    );
    CREATE INDEX ix_usage_user_time ON usage_events (user_id, created_at);

    CREATE TABLE upgrade_codes (
        id INTEGER PRIMARY KEY,
        code_hash TEXT NOT NULL UNIQUE,
        plan TEXT NOT NULL CHECK (plan IN ('pro')),
        days INTEGER NOT NULL CHECK (days BETWEEN 1 AND 3660),
        note TEXT NOT NULL DEFAULT '',
        created_by INTEGER REFERENCES users (id) ON DELETE SET NULL,
        created_at {_TS},
        redeemed_by INTEGER REFERENCES users (id) ON DELETE SET NULL,
        redeemed_at TEXT
    );

    CREATE TABLE medications (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        ingredient TEXT NOT NULL DEFAULT '',
        brand_names TEXT NOT NULL DEFAULT '',
        rxcui TEXT NOT NULL DEFAULT '',
        verified INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
        created_at {_TS}
    );

    CREATE TABLE foods (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        keywords TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'approved' CHECK (status IN ('approved', 'retired')),
        created_at {_TS}
    );

    CREATE TABLE symptom_categories (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        meddra_term TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'approved' CHECK (status IN ('approved', 'retired')),
        created_at {_TS}
    );

    CREATE TABLE category_suggestions (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('symptom', 'food')),
        submitted_name TEXT NOT NULL,
        normalized_name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'approved', 'rejected', 'merged')),
        resolved_category_id INTEGER,
        user_id INTEGER REFERENCES users (id) ON DELETE SET NULL,
        reviewed_by INTEGER REFERENCES users (id) ON DELETE SET NULL,
        reviewed_at TEXT,
        created_at {_TS}
    );
    -- Only one open suggestion per normalised name; history is kept.
    CREATE UNIQUE INDEX ux_suggestion_pending
        ON category_suggestions (kind, normalized_name) WHERE status = 'pending';

    CREATE TABLE reaction_reports (
        id INTEGER PRIMARY KEY,
        user_id INTEGER REFERENCES users (id) ON DELETE CASCADE,
        report_type TEXT NOT NULL CHECK (report_type IN ('side_effect', 'interaction')),
        medication_id INTEGER NOT NULL REFERENCES medications (id),
        medication_as_entered TEXT NOT NULL,
        food_id INTEGER REFERENCES foods (id),
        onset TEXT NOT NULL,
        severity TEXT NOT NULL CHECK (severity IN ('mild', 'moderate', 'severe')),
        frequency TEXT NOT NULL CHECK (frequency IN ('once', 'repeatedly')),
        context TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'approved', 'rejected', 'flagged')),
        provenance TEXT NOT NULL DEFAULT 'community_report',
        fingerprint TEXT NOT NULL UNIQUE,
        reviewed_by INTEGER REFERENCES users (id) ON DELETE SET NULL,
        reviewed_at TEXT,
        created_at {_TS},
        CHECK (report_type = 'side_effect' OR food_id IS NOT NULL)
    );
    CREATE INDEX ix_reports_status ON reaction_reports (status, created_at);
    CREATE INDEX ix_reports_medication ON reaction_reports (medication_id);
    CREATE INDEX ix_reports_food ON reaction_reports (food_id);
    CREATE INDEX ix_reports_user ON reaction_reports (user_id);

    CREATE TABLE report_symptoms (
        report_id INTEGER NOT NULL REFERENCES reaction_reports (id) ON DELETE CASCADE,
        symptom_id INTEGER NOT NULL REFERENCES symptom_categories (id),
        PRIMARY KEY (report_id, symptom_id)
    );
    CREATE INDEX ix_report_symptoms_symptom ON report_symptoms (symptom_id);

    -- The public, votable unit: an aggregated drug->symptom or drug<->food pair.
    -- Individual reports stay private; entries only exist once a report about
    -- that pair has been approved.
    CREATE TABLE reaction_entries (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('side_effect', 'interaction')),
        medication_id INTEGER NOT NULL REFERENCES medications (id),
        symptom_id INTEGER REFERENCES symptom_categories (id),
        food_id INTEGER REFERENCES foods (id),
        created_at {_TS},
        CHECK (
            (kind = 'side_effect' AND symptom_id IS NOT NULL AND food_id IS NULL)
            OR (kind = 'interaction' AND food_id IS NOT NULL AND symptom_id IS NULL)
        )
    );
    CREATE UNIQUE INDEX ux_entry
        ON reaction_entries (kind, medication_id, IFNULL(symptom_id, 0), IFNULL(food_id, 0));

    CREATE TABLE votes (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        entry_id INTEGER NOT NULL REFERENCES reaction_entries (id) ON DELETE CASCADE,
        value INTEGER NOT NULL CHECK (value IN (1, -1)),
        created_at {_TS},
        updated_at {_TS},
        UNIQUE (user_id, entry_id)
    );
    CREATE INDEX ix_votes_entry ON votes (entry_id);

    CREATE TABLE evidence_sources (
        id INTEGER PRIMARY KEY,
        provenance TEXT NOT NULL DEFAULT 'official_source',
        source TEXT NOT NULL,
        kind TEXT NOT NULL,
        lookup_key TEXT NOT NULL,
        medication TEXT NOT NULL DEFAULT '',
        food TEXT NOT NULL DEFAULT '',
        symptom TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL,
        excerpt TEXT NOT NULL DEFAULT '',
        metadata TEXT NOT NULL DEFAULT '{{}}',
        retrieved_at {_TS}
    );
    CREATE INDEX ix_evidence_lookup ON evidence_sources (kind, lookup_key, retrieved_at);

    CREATE TABLE hypotheses (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        medication_id INTEGER REFERENCES medications (id),
        food_id INTEGER REFERENCES foods (id),
        symptom_id INTEGER REFERENCES symptom_categories (id),
        status TEXT NOT NULL DEFAULT 'proposed'
            CHECK (status IN ('proposed', 'investigating', 'supported', 'not_supported')),
        is_public INTEGER NOT NULL DEFAULT 0 CHECK (is_public IN (0, 1)),
        evidence_urls TEXT NOT NULL DEFAULT '',
        provenance TEXT NOT NULL DEFAULT 'research_hypothesis',
        created_by INTEGER REFERENCES users (id) ON DELETE SET NULL,
        created_at {_TS},
        updated_at {_TS}
    );

    CREATE TABLE admin_audit_log (
        id INTEGER PRIMARY KEY,
        admin_id INTEGER REFERENCES users (id) ON DELETE SET NULL,
        action TEXT NOT NULL,
        target TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        created_at {_TS}
    );
    CREATE INDEX ix_audit_time ON admin_audit_log (created_at);
    """,

    # 2 - drug<->drug interaction reports, a third plan tier.
    # SQLite cannot alter a CHECK constraint, so the affected tables are rebuilt
    # with the documented create-copy-drop-rename procedure. `migrate` runs this
    # with foreign keys off and verifies them before committing.
    f"""
    CREATE TABLE users_v2 (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
        plan TEXT NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'pro', 'team')),
        plan_expires_at TEXT,
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        created_at {_TS}
    );
    INSERT INTO users_v2 SELECT id, username, password_hash, role, plan, plan_expires_at, is_active, created_at FROM users;
    DROP TABLE users;
    ALTER TABLE users_v2 RENAME TO users;

    CREATE TABLE upgrade_codes_v2 (
        id INTEGER PRIMARY KEY,
        code_hash TEXT NOT NULL UNIQUE,
        plan TEXT NOT NULL CHECK (plan IN ('pro', 'team')),
        days INTEGER NOT NULL CHECK (days BETWEEN 1 AND 3660),
        note TEXT NOT NULL DEFAULT '',
        created_by INTEGER REFERENCES users (id) ON DELETE SET NULL,
        created_at {_TS},
        redeemed_by INTEGER REFERENCES users (id) ON DELETE SET NULL,
        redeemed_at TEXT
    );
    INSERT INTO upgrade_codes_v2 SELECT id, code_hash, plan, days, note, created_by, created_at, redeemed_by, redeemed_at FROM upgrade_codes;
    DROP TABLE upgrade_codes;
    ALTER TABLE upgrade_codes_v2 RENAME TO upgrade_codes;

    CREATE TABLE reaction_reports_v2 (
        id INTEGER PRIMARY KEY,
        user_id INTEGER REFERENCES users (id) ON DELETE CASCADE,
        report_type TEXT NOT NULL CHECK (report_type IN ('side_effect', 'interaction', 'drug_interaction')),
        medication_id INTEGER NOT NULL REFERENCES medications (id),
        medication_as_entered TEXT NOT NULL,
        food_id INTEGER REFERENCES foods (id),
        other_medication_id INTEGER REFERENCES medications (id),
        other_medication_as_entered TEXT NOT NULL DEFAULT '',
        onset TEXT NOT NULL,
        severity TEXT NOT NULL CHECK (severity IN ('mild', 'moderate', 'severe')),
        frequency TEXT NOT NULL CHECK (frequency IN ('once', 'repeatedly')),
        context TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'approved', 'rejected', 'flagged')),
        provenance TEXT NOT NULL DEFAULT 'community_report',
        fingerprint TEXT NOT NULL UNIQUE,
        reviewed_by INTEGER REFERENCES users (id) ON DELETE SET NULL,
        reviewed_at TEXT,
        created_at {_TS},
        CHECK (
            report_type = 'side_effect'
            OR (report_type = 'interaction' AND food_id IS NOT NULL)
            OR (report_type = 'drug_interaction' AND other_medication_id IS NOT NULL
                AND other_medication_id != medication_id)
        )
    );
    INSERT INTO reaction_reports_v2 (id, user_id, report_type, medication_id, medication_as_entered, food_id,
        onset, severity, frequency, context, status, provenance, fingerprint, reviewed_by, reviewed_at, created_at)
        SELECT id, user_id, report_type, medication_id, medication_as_entered, food_id, onset, severity,
        frequency, context, status, provenance, fingerprint, reviewed_by, reviewed_at, created_at FROM reaction_reports;
    DROP TABLE reaction_reports;
    ALTER TABLE reaction_reports_v2 RENAME TO reaction_reports;
    CREATE INDEX ix_reports_status ON reaction_reports (status, created_at);
    CREATE INDEX ix_reports_medication ON reaction_reports (medication_id);
    CREATE INDEX ix_reports_other_medication ON reaction_reports (other_medication_id);
    CREATE INDEX ix_reports_food ON reaction_reports (food_id);
    CREATE INDEX ix_reports_user ON reaction_reports (user_id);

    CREATE TABLE reaction_entries_v2 (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('side_effect', 'interaction', 'drug_interaction')),
        medication_id INTEGER NOT NULL REFERENCES medications (id),
        symptom_id INTEGER REFERENCES symptom_categories (id),
        food_id INTEGER REFERENCES foods (id),
        other_medication_id INTEGER REFERENCES medications (id),
        created_at {_TS},
        CHECK (
            (kind = 'side_effect' AND symptom_id IS NOT NULL AND food_id IS NULL AND other_medication_id IS NULL)
            OR (kind = 'interaction' AND food_id IS NOT NULL AND symptom_id IS NULL AND other_medication_id IS NULL)
            OR (kind = 'drug_interaction' AND other_medication_id IS NOT NULL AND symptom_id IS NULL
                AND food_id IS NULL AND medication_id < other_medication_id)
        )
    );
    INSERT INTO reaction_entries_v2 (id, kind, medication_id, symptom_id, food_id, created_at)
        SELECT id, kind, medication_id, symptom_id, food_id, created_at FROM reaction_entries;
    DROP TABLE reaction_entries;
    ALTER TABLE reaction_entries_v2 RENAME TO reaction_entries;
    CREATE UNIQUE INDEX ux_entry ON reaction_entries
        (kind, medication_id, IFNULL(symptom_id, 0), IFNULL(food_id, 0), IFNULL(other_medication_id, 0));

    ALTER TABLE hypotheses ADD COLUMN other_medication_id INTEGER REFERENCES medications (id);
    """,

    # 3 - research projects: every run is saved with its report and its assistant chat.
    f"""
    CREATE TABLE projects (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        query TEXT NOT NULL,
        depth TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
        error TEXT NOT NULL DEFAULT '',
        report_json TEXT,
        summary TEXT NOT NULL DEFAULT '{{}}',
        created_at {_TS},
        updated_at {_TS}
    );
    CREATE INDEX ix_projects_user ON projects (user_id, updated_at);

    CREATE TABLE project_messages (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content TEXT NOT NULL,
        reply_to INTEGER REFERENCES project_messages (id) ON DELETE CASCADE,
        created_at {_TS}
    );
    CREATE INDEX ix_messages_project ON project_messages (project_id, id);
    -- One answer per question.
    CREATE UNIQUE INDEX ux_messages_reply ON project_messages (reply_to) WHERE reply_to IS NOT NULL;

    -- Public discussion on each community entry: comments, one level of replies,
    -- likes and flags. Published immediately, hidden automatically when enough
    -- people flag them, and always removable by the administrator.
    CREATE TABLE entry_comments (
        id INTEGER PRIMARY KEY,
        entry_id INTEGER NOT NULL REFERENCES reaction_entries (id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        parent_id INTEGER REFERENCES entry_comments (id) ON DELETE CASCADE,
        body TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'visible' CHECK (status IN ('visible', 'hidden')),
        created_at {_TS}
    );
    CREATE INDEX ix_comments_entry ON entry_comments (entry_id, created_at);
    CREATE INDEX ix_comments_parent ON entry_comments (parent_id);

    CREATE TABLE comment_likes (
        user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        comment_id INTEGER NOT NULL REFERENCES entry_comments (id) ON DELETE CASCADE,
        created_at {_TS},
        PRIMARY KEY (user_id, comment_id)
    );
    CREATE INDEX ix_likes_comment ON comment_likes (comment_id);

    -- "Stay signed in": one row per browser that signed in. Only a hash of the
    -- cookie's token is stored, so a copy of the database cannot be used to log in.
    CREATE TABLE sessions (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL UNIQUE,
        created_at {_TS},
        expires_at {_TS}
    );
    CREATE INDEX ix_sessions_user ON sessions (user_id);

    -- Sign-in with an external identity provider (Google). Only the provider's
    -- stable subject id is kept, hashed - never the email address.
    ALTER TABLE users ADD COLUMN auth_provider TEXT NOT NULL DEFAULT 'password';
    ALTER TABLE users ADD COLUMN external_id TEXT;
    CREATE UNIQUE INDEX ux_users_external ON users (auth_provider, external_id) WHERE external_id IS NOT NULL;

    CREATE TABLE comment_flags (
        user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        comment_id INTEGER NOT NULL REFERENCES entry_comments (id) ON DELETE CASCADE,
        reason TEXT NOT NULL DEFAULT '',
        created_at {_TS},
        PRIMARY KEY (user_id, comment_id)
    );
    """,
]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration the database has not seen yet. Returns the version.

    Foreign keys are switched off while migrating (a table rebuild drops and
    recreates tables other tables point at) and checked before each commit, so a
    migration that would leave a dangling reference is rolled back instead.
    """
    conn.execute("PRAGMA foreign_keys = OFF")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS, 1):
        if number <= version:
            continue
        # Take the write lock first, then re-read the version: another process or
        # thread may have applied this migration while we waited.
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA user_version").fetchone()[0] >= number:
            conn.execute("COMMIT")
            continue
        try:
            for statement in _split(script):
                conn.execute(statement)
            if number == 1:
                _seed(conn)
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise sqlite3.IntegrityError(f"migration {number} broke foreign keys: {broken[:3]}")
            conn.execute(f"PRAGMA user_version = {number}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        version = number
    conn.execute("PRAGMA foreign_keys = ON")
    return version


def _split(script: str) -> list[str]:
    statements, buffer = [], []
    for line in script.splitlines():
        if line.strip().startswith("--"):
            continue
        buffer.append(line)
        if line.rstrip().endswith(";"):
            statement = "\n".join(buffer).strip()
            if statement.rstrip(";").strip():
                statements.append(statement)
            buffer = []
    return statements


# --------------------------------------------------------------------------- #
# Seed vocabulary
# --------------------------------------------------------------------------- #

# (name, keywords searched for in official label text)
SEED_FOODS: list[tuple[str, str]] = [
    ("Grapefruit", "grapefruit|pomelo|seville orange"),
    ("Alcohol", "alcohol|alcoholic|ethanol"),
    ("Caffeine (coffee, tea, energy drinks)", "caffeine|caffeinated|coffee|energy drink"),
    ("Dairy and calcium-rich foods", "dairy|milk|calcium-rich|calcium rich|yogurt|yoghurt"),
    ("High-fat meal", "high-fat|high fat|fatty meal"),
    ("Vitamin K-rich foods (leafy greens)", "vitamin k|leafy green|green leafy|spinach|kale"),
    ("Tyramine-rich foods (aged cheese, cured meats)", "tyramine|aged cheese|cured meat|fermented"),
    ("Licorice", "licorice|liquorice|glycyrrhiz"),
    ("Potassium-rich foods and salt substitutes", "salt substitute|potassium-rich|potassium rich|potassium-containing"),
    ("Cranberry", "cranberry"),
    ("Green tea", "green tea"),
    ("Soy", "soy|soybean"),
    ("High-fibre foods", "high-fiber|high fiber|high-fibre|dietary fiber|dietary fibre|bran"),
    ("Food in general (meals, empty stomach)", "with food|with meals|empty stomach|without food|with a meal|after a meal"),
]

# (lay name, MedDRA preferred term used for FAERS counts)
SEED_SYMPTOMS: list[tuple[str, str]] = [
    ("Nausea", "nausea"), ("Vomiting", "vomiting"), ("Diarrhoea", "diarrhoea"),
    ("Constipation", "constipation"), ("Abdominal pain", "abdominal pain"),
    ("Heartburn or indigestion", "dyspepsia"), ("Loss of appetite", "decreased appetite"),
    ("Headache", "headache"), ("Dizziness", "dizziness"), ("Fatigue", "fatigue"),
    ("Drowsiness", "somnolence"), ("Insomnia", "insomnia"), ("Anxiety", "anxiety"),
    ("Low mood", "depressed mood"), ("Rash", "rash"), ("Itching", "pruritus"),
    ("Hives", "urticaria"), ("Swelling", "swelling"), ("Flushing", "flushing"),
    ("Sweating", "hyperhidrosis"), ("Shortness of breath", "dyspnoea"),
    ("Cough", "cough"), ("Palpitations", "palpitations"), ("Chest pain", "chest pain"),
    ("Muscle pain", "myalgia"), ("Joint pain", "arthralgia"), ("Tremor", "tremor"),
    ("Tingling or numbness", "paraesthesia"), ("Dry mouth", "dry mouth"),
    ("Blurred vision", "vision blurred"), ("Hair loss", "alopecia"),
    ("Weight gain", "weight increased"), ("Easy bruising or bleeding", "haemorrhage"),
    ("Low blood sugar symptoms", "hypoglycaemia"),
]


def _seed(conn: sqlite3.Connection) -> None:
    stamp = now_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO foods (name, keywords, created_at) VALUES (?, ?, ?)",
        [(name, keywords, stamp) for name, keywords in SEED_FOODS],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO symptom_categories (name, meddra_term, created_at) VALUES (?, ?, ?)",
        [(name, term, stamp) for name, term in SEED_SYMPTOMS],
    )
