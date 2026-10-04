"""Community features: auth, permissions, reports, votes, moderation, quotas."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from drugscope.community import auth, quota, service
from drugscope.community.auth import AuthError, PermissionDenied
from drugscope.community.db import connect

SEMAGLUTIDE = {"ingredient": "semaglutide", "brands": ["Ozempic", "Wegovy"], "rxcui": "1991302"}


def _symptom(name):
    return next(s["id"] for s in service.symptoms() if s["name"] == name)


def _food(name):
    return next(f["id"] for f in service.foods() if f["name"].startswith(name))


def _report(user, symptoms=("Nausea",), food=None, kind="side_effect", context=""):
    return service.submit_report(
        user, report_type=kind, medication_typed="Ozempic", medication_candidate=SEMAGLUTIDE,
        symptom_ids=[_symptom(s) for s in symptoms], food_id=_food(food) if food else None,
        onset="1_6h", severity="mild", frequency="once", context=context, consent=True,
    )


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #

def test_password_is_hashed_and_verifies(make_user):
    user = make_user()
    with connect() as conn:
        stored = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user.id,)).fetchone()[0]
    assert stored.startswith("scrypt$") and "alice-password-1" not in stored
    assert auth.authenticate("ALICE", "alice-password-1").id == user.id  # usernames case-insensitive
    with pytest.raises(AuthError):
        auth.authenticate("alice", "wrong-password")


def test_registration_rules(make_user):
    make_user()
    with pytest.raises(AuthError, match="taken"):
        auth.register("Alice", "another-password", confirmed_adult=True)
    with pytest.raises(AuthError, match="18"):
        auth.register("bob", "bob-password-1", confirmed_adult=False)
    with pytest.raises(AuthError, match="at least"):
        auth.register("bob", "short", confirmed_adult=True)
    with pytest.raises(AuthError, match="reserved"):
        auth.register("CHIEF", "some-password-1", confirmed_adult=True)


def test_lockout_after_repeated_failures(make_user):
    make_user()
    for _ in range(auth.LOCKOUT_FAILURES):
        with pytest.raises(AuthError):
            auth.authenticate("alice", "nope-nope-nope")
    with pytest.raises(AuthError, match="Too many"):
        auth.authenticate("alice", "alice-password-1")  # even the right password


def test_admin_bootstrap_takes_over_squatted_name(monkeypatch):
    monkeypatch.setenv("DRUGSCOPE_ADMIN_USERNAME", "")
    squatter = auth.register("chief", "squatter-pass-1", confirmed_adult=True)
    monkeypatch.setenv("DRUGSCOPE_ADMIN_USERNAME", "chief")
    assert auth.bootstrap_admin() is None
    with pytest.raises(AuthError):
        auth.authenticate("chief", "squatter-pass-1")
    assert auth.authenticate("chief", "admin-password-123").is_admin
    assert auth.load_principal(squatter.id).is_admin


# --------------------------------------------------------------------------- #
# Permissions - enforced in the service layer, not the UI
# --------------------------------------------------------------------------- #

def test_non_admin_cannot_reach_private_data(make_user, admin):
    user = make_user()
    _report(user)
    admin_only = [
        lambda p: service.admin_overview(p),
        lambda p: service.admin_reports(p),
        lambda p: service.export_csv(p, "reports"),
        lambda p: service.export_csv(p, "votes"),
        lambda p: service.moderate_report(p, 1, "approved"),
        lambda p: service.admin_suggestions(p),
        lambda p: service.admin_users(p),
        lambda p: service.audit_log(p),
        lambda p: service.admin_evidence(p),
        lambda p: quota.generate_codes(p, count=1, days=30),
        lambda p: quota.set_plan(p, user.id, plan="pro", days=30),
    ]
    for call in admin_only:
        with pytest.raises(PermissionDenied):
            call(user)
        with pytest.raises(PermissionDenied):
            call(None)
    assert service.admin_reports(admin)  # the admin can


def test_demoted_admin_loses_access_immediately(admin):
    with connect() as conn:
        conn.execute("UPDATE users SET role = 'user' WHERE id = ?", (admin.id,))
    with pytest.raises(PermissionDenied):
        service.admin_overview(admin)  # the stale Principal still says admin


def test_deactivated_user_cannot_act(make_user, admin):
    user = make_user()
    service.set_user_active(admin, user.id, False)
    with pytest.raises(PermissionDenied):
        _report(user)


# --------------------------------------------------------------------------- #
# Reports and public aggregates
# --------------------------------------------------------------------------- #

def test_report_pending_until_approved_and_private(make_user, admin):
    user = make_user()
    result = _report(user, symptoms=("Nausea", "Headache"), context="call me at +971 50 123 4567 or a@b.com")
    assert result["redacted"]
    assert service.public_entries() == []  # nothing public before moderation

    raw = service.admin_reports(admin)[0]
    assert "+971" not in raw["context"] and "a@b.com" not in raw["context"]
    assert raw["medication"] == "semaglutide" and raw["medication_as_entered"] == "Ozempic"

    service.moderate_report(admin, result["id"], "approved")
    entries = service.public_entries()
    assert {(e["medication"], e["symptom"]) for e in entries} == {("semaglutide", "Nausea"), ("semaglutide", "Headache")}
    assert all("context" not in e and "user_id" not in e for e in entries)


def test_duplicate_report_same_day_rejected(make_user):
    user = make_user()
    _report(user)
    with pytest.raises(service.ValidationError, match="already submitted"):
        _report(user)


def test_report_validation(make_user):
    user = make_user()
    with pytest.raises(service.ValidationError, match="consent"):
        service.submit_report(user, report_type="side_effect", medication_typed="x", medication_candidate=SEMAGLUTIDE,
                              symptom_ids=[1], food_id=None, onset="1_6h", severity="mild", frequency="once",
                              context="", consent=False)
    with pytest.raises(service.ValidationError, match="food"):
        _report(user, kind="interaction")
    with pytest.raises(service.ValidationError):
        service.submit_report(user, report_type="side_effect", medication_typed="x", medication_candidate=SEMAGLUTIDE,
                              symptom_ids=[1], food_id=None, onset="DROP TABLE", severity="mild",
                              frequency="once", context="", consent=True)


def test_sql_injection_text_is_stored_as_data(make_user, admin):
    user = make_user()
    payload = "'); DROP TABLE users; --"
    service.submit_report(user, report_type="side_effect", medication_typed=payload, medication_candidate=None,
                          symptom_ids=[_symptom("Nausea")], food_id=None, onset="1_6h", severity="mild",
                          frequency="once", context=payload, consent=True)
    assert auth.authenticate("alice", "alice-password-1")  # users table intact
    assert service.admin_reports(admin)[0]["medication_verified"] == 0


def test_interaction_entry_and_both_directions(make_user, admin):
    user = make_user()
    rid = _report(user, food="Grapefruit", kind="interaction", symptoms=("Dizziness",))["id"]
    service.moderate_report(admin, rid, "approved")
    grapefruit = _food("Grapefruit")
    by_food = service.public_entries(kind="interaction", food_id=grapefruit)
    assert by_food[0]["medication"] == "semaglutide"
    assert by_food[0]["symptoms"][0]["name"] == "Dizziness"
    by_drug = service.public_entries(kind="interaction", medication_id=by_food[0]["medication_id"])
    assert by_drug[0]["food"].startswith("Grapefruit")


def test_user_can_delete_own_report_only(make_user):
    alice, bob = make_user("alice"), make_user("bob")
    rid = _report(alice)["id"]
    with pytest.raises(service.ValidationError):
        service.delete_my_report(bob, rid)
    service.delete_my_report(alice, rid)
    assert service.my_reports(alice) == []


# --------------------------------------------------------------------------- #
# Voting
# --------------------------------------------------------------------------- #

def _public_entry(make_user, admin):
    reporter = make_user("reporter")
    service.moderate_report(admin, _report(reporter)["id"], "approved")
    return service.public_entries()[0]["id"]


def test_one_vote_per_user_change_and_remove(make_user, admin):
    entry = _public_entry(make_user, admin)
    voter = make_user("voter")
    service.cast_vote(voter, entry, 1)
    service.cast_vote(voter, entry, 1)  # repeat is an upsert, not a second vote
    row = service.public_entries(voter)[0]
    assert (row["yes_votes"], row["no_votes"], row["my_vote"]) == (1, 0, 1)
    service.cast_vote(voter, entry, -1)
    row = service.public_entries(voter)[0]
    assert (row["yes_votes"], row["no_votes"], row["my_vote"]) == (0, 1, -1)
    service.remove_vote(voter, entry)
    row = service.public_entries(voter)[0]
    assert (row["yes_votes"], row["no_votes"], row["my_vote"]) == (0, 0, None)


def test_duplicate_vote_blocked_by_database_constraint(make_user, admin):
    entry = _public_entry(make_user, admin)
    voter = make_user("voter")
    service.cast_vote(voter, entry, 1)
    with pytest.raises(sqlite3.IntegrityError):
        with connect() as conn:  # bypass the service entirely
            conn.execute("INSERT INTO votes (user_id, entry_id, value, created_at, updated_at) "
                         "VALUES (?, ?, 1, 'x', 'x')", (voter.id, entry))


def test_cannot_vote_without_account_or_on_hidden_entry(make_user, admin):
    entry = _public_entry(make_user, admin)
    with pytest.raises(PermissionDenied):
        service.cast_vote(None, entry, 1)
    with pytest.raises(service.ValidationError):
        service.cast_vote(make_user("voter"), 99999, 1)


def test_vote_rate_limit(make_user, admin, monkeypatch):
    monkeypatch.setitem(service.RATE_LIMITS, "vote", (2, 60))
    entry = _public_entry(make_user, admin)
    voter = make_user("voter")
    service.cast_vote(voter, entry, 1)
    service.cast_vote(voter, entry, -1)
    with pytest.raises(service.RateLimited):
        service.cast_vote(voter, entry, 1)


# --------------------------------------------------------------------------- #
# Category moderation
# --------------------------------------------------------------------------- #

def test_suggestion_dedup_and_moderation(make_user, admin):
    user, other = make_user("alice"), make_user("bob")
    with pytest.raises(service.ValidationError, match="already exists"):
        service.suggest_category(user, "symptom", "  NAUSEA ")
    assert any(m["name"] == "Headache" for m in service.similar_categories("symptom", "headaches"))

    sid = service.suggest_category(user, "symptom", "Metallic taste", "taste like coins")
    with pytest.raises(service.ValidationError, match="already suggested"):
        service.suggest_category(other, "symptom", "metallic  taste!")
    with pytest.raises(PermissionDenied):
        service.resolve_suggestion(user, sid, "approve")

    service.resolve_suggestion(admin, sid, "approve", final_name="Metallic taste")
    assert "Metallic taste" in [s["name"] for s in service.symptoms()]
    suggestion = service.admin_suggestions(admin, status="")[0]
    assert suggestion["submitted_name"] == "Metallic taste" and suggestion["status"] == "approved"

    sid2 = service.suggest_category(other, "symptom", "Queasy stomach")
    service.resolve_suggestion(admin, sid2, "merge", merge_into_id=_symptom("Nausea"))
    sid3 = service.suggest_category(other, "food", "Pickled herring")
    service.resolve_suggestion(admin, sid3, "reject")
    statuses = {s["submitted_name"]: s["status"] for s in service.admin_suggestions(admin, status="")}
    assert statuses["Queasy stomach"] == "merged" and statuses["Pickled herring"] == "rejected"
    actions = [row["action"] for row in service.audit_log(admin)]
    assert {"suggestion_approved", "suggestion_merged", "suggestion_rejected"} <= set(actions)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def test_export_is_audited_and_formula_safe(make_user, admin):
    user = make_user()
    service.submit_report(user, report_type="side_effect", medication_typed="aspirin", medication_candidate=None,
                          symptom_ids=[_symptom("Rash")], food_id=None, onset="1_6h", severity="mild",
                          frequency="once", context="=HYPERLINK(\"http://evil\")", consent=True)
    text = service.export_csv(admin, "reports")
    assert "'=HYPERLINK" in text
    assert service.audit_log(admin)[0]["action"] == "export_csv"


# --------------------------------------------------------------------------- #
# Quotas and upgrades
# --------------------------------------------------------------------------- #

def test_quota_blocks_then_resets_after_window(make_user):
    user = make_user()
    start = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    quota.consume(user, 2, kind="research", now=start)
    quota.consume(user, 2, kind="research", now=start + timedelta(hours=1))
    with pytest.raises(quota.QuotaExceeded) as blocked:
        quota.consume(user, 2, kind="research", now=start + timedelta(hours=2))
    status = blocked.value.status
    assert (status.used, status.remaining) == (4, 1)
    assert status.resets_at == start + timedelta(hours=24)
    assert status.resets_in_text(start + timedelta(hours=2)) == "22h 00m"
    quota.consume(user, 1, kind="research", now=start + timedelta(hours=2))  # a Scan still fits
    # 24h after the first run its credits return.
    quota.consume(user, 2, kind="research", now=start + timedelta(hours=24, minutes=1))


def test_window_hours_configurable(make_user, monkeypatch):
    monkeypatch.setenv("DRUGSCOPE_QUOTA_WINDOW_HOURS", "5")
    user = make_user()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    quota.consume(user, 5, kind="research", now=start)
    with pytest.raises(quota.QuotaExceeded):
        quota.consume(user, 1, kind="research", now=start + timedelta(hours=4))
    quota.consume(user, 1, kind="research", now=start + timedelta(hours=5, seconds=1))


def test_refund_returns_credits(make_user):
    user = make_user()
    event = quota.consume(user, 4, kind="research")
    quota.refund(user, event)
    assert quota.status(user).used == 0


def test_admin_is_unlimited(admin):
    for _ in range(20):
        quota.consume(admin, 4, kind="research")
    assert quota.status(admin).unlimited


def test_upgrade_code_single_use_and_extends(make_user, admin):
    alice, bob = make_user("alice"), make_user("bob")
    code, = quota.generate_codes(admin, count=1, days=30, note="test")
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM upgrade_codes WHERE code_hash LIKE 'DS-%'").fetchone()[0] == 0
    expires = quota.redeem(alice, code.lower().replace("-", " "))  # forgiving input
    assert quota.status(alice).plan == "pro" and quota.status(alice).limit == 60
    with pytest.raises(ValueError):
        quota.redeem(bob, code)
    second, = quota.generate_codes(admin, count=1, days=10)
    later = quota.redeem(alice, second)
    assert later > expires  # stacked onto the existing Pro period


def test_pro_expires_back_to_free(make_user, admin):
    user = make_user()
    quota.set_plan(admin, user.id, plan="pro", days=1)
    assert quota.status(user).plan == "pro"
    assert quota.status(user, now=datetime.now(timezone.utc) + timedelta(days=2)).plan == "free"


def test_account_deletion_removes_data(make_user, admin):
    user = make_user()
    _report(user)
    quota.consume(user, 1, kind="research")
    auth.delete_account(user, "alice-password-1")
    assert service.admin_reports(admin) == []
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# Drug <-> drug, plans, insights, migrations
# --------------------------------------------------------------------------- #

OMEPRAZOLE = {"ingredient": "omeprazole", "brands": ["Prilosec"], "rxcui": "7646"}
CLOPIDOGREL = {"ingredient": "clopidogrel", "brands": ["Plavix"], "rxcui": "32968"}


def _pair_report(user, first, second, symptoms=("Chest pain",)):
    return service.submit_report(
        user, report_type="drug_interaction", medication_typed=first["ingredient"], medication_candidate=first,
        other_medication_typed=second["ingredient"], other_medication_candidate=second,
        symptom_ids=[_symptom(s) for s in symptoms], food_id=None, onset="1_7d", severity="moderate",
        frequency="repeatedly", context="", consent=True,
    )


def test_drug_pair_reports_merge_regardless_of_order(make_user, admin):
    alice, bob = make_user("alice"), make_user("bob")
    service.moderate_report(admin, _pair_report(alice, CLOPIDOGREL, OMEPRAZOLE)["id"], "approved")
    service.moderate_report(admin, _pair_report(bob, OMEPRAZOLE, CLOPIDOGREL, ("Fatigue",))["id"], "approved")
    entries = service.public_entries(kind="drug_interaction")
    assert len(entries) == 1 and entries[0]["reports"] == 2
    assert {s["name"] for s in entries[0]["symptoms"]} == {"Chest pain", "Fatigue"}
    # A drug-drug report is not attributed to either drug alone as a side effect.
    assert service.public_entries(kind="side_effect") == []
    a = service.find_medication("clopidogrel")["id"]
    b = service.find_medication("omeprazole")["id"]
    assert service.public_entries(kind="drug_interaction", medication_id=b, other_medication_id=a)
    service.cast_vote(make_user("carol"), entries[0]["id"], 1)
    assert service.public_entries(kind="drug_interaction")[0]["yes_votes"] == 1


def test_drug_pair_needs_two_different_drugs(make_user):
    user = make_user()
    with pytest.raises(service.ValidationError, match="different"):
        _pair_report(user, OMEPRAZOLE, OMEPRAZOLE)
    with pytest.raises(service.ValidationError, match="second medication"):
        service.submit_report(user, report_type="drug_interaction", medication_typed="omeprazole",
                              medication_candidate=OMEPRAZOLE, symptom_ids=[1], food_id=None, onset="1_6h",
                              severity="mild", frequency="once", context="", consent=True)


def test_team_plan_codes(make_user, admin):
    user = make_user()
    code, = quota.generate_codes(admin, count=1, days=10, plan="team")
    quota.redeem(user, code)
    status = quota.status(user)
    assert (status.plan, status.limit) == ("team", 200)
    with pytest.raises(ValueError):
        quota.generate_codes(admin, count=1, days=10, plan="free")


def test_insights_public_excludes_pending_and_admin_scope_is_protected(make_user, admin):
    alice = make_user("alice")
    approved = _report(alice)["id"]
    _report(alice, symptoms=("Rash",))  # stays pending
    service.moderate_report(admin, approved, "approved")
    service.cast_vote(make_user("bob"), service.public_entries()[0]["id"], 1)
    public = service.insights(scope="public")
    assert public["reports"] == 1 and [s["symptom"] for s in public["top_symptoms"]] == ["Nausea"]
    assert public["votes"] == {"total": 1, "yes": 1, "no": 0, "voters": 1}
    private = service.insights(admin, scope="admin")
    assert private["reports"] == 2 and private["by_status"] == {"approved": 1, "pending": 1}
    with pytest.raises(PermissionDenied):
        service.insights(alice, scope="admin")


def test_migration_from_v1_keeps_data(tmp_path, monkeypatch):
    from drugscope.community import db
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path, isolation_level=None)
    for statement in db._split(db.MIGRATIONS[0]):
        conn.execute(statement)
    db._seed(conn)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO users (username, password_hash, plan, created_at) VALUES ('old', 'x', 'pro', 'now')")
    conn.close()
    monkeypatch.setenv("DRUGSCOPE_DB_PATH", str(path))
    db.reset_for_tests()
    with connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
        assert conn.execute("SELECT plan FROM users WHERE username = 'old'").fetchone()[0] == "pro"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# Comments, likes, Google accounts, projects, assistant
# --------------------------------------------------------------------------- #

def test_comments_replies_likes_and_flags(make_user, admin):
    entry = _public_entry(make_user, admin)
    alice, bob, carol, dave = (make_user(n) for n in ("alice", "bob", "carol", "dave"))
    top = service.add_comment(alice, entry, "Same here - nausea for 2 days. Mail me a@b.com")
    reply = service.add_comment(bob, entry, "Did it ease off?", parent_id=top)
    nested = service.add_comment(carol, entry, "For me yes", parent_id=reply)  # flattened to one level
    thread = service.comments(bob, entry)
    assert len(thread) == 1 and [r["id"] for r in thread[0]["replies"]] == [reply, nested]
    assert "a@b.com" not in thread[0]["body"]  # identifiers stripped before saving
    assert service.toggle_like(bob, top) is True and service.toggle_like(carol, top) is True
    assert service.comments(bob, entry)[0]["likes"] == 2 and service.comments(bob, entry)[0]["liked"]
    assert service.toggle_like(bob, top) is False  # unlike
    assert service.public_entries()[0]["comments"] == 3
    with pytest.raises(service.ValidationError):
        service.delete_comment(bob, top)  # not his
    with pytest.raises(service.ValidationError):
        service.flag_comment(alice, top)  # own comment
    assert service.flag_comment(bob, top) is False
    with pytest.raises(service.ValidationError, match="already"):
        service.flag_comment(bob, top)
    service.flag_comment(carol, top)
    assert service.flag_comment(dave, top) is True  # third report hides it
    assert service.comments(None, entry) == []  # hidden with its replies
    with pytest.raises(PermissionDenied):
        service.admin_comments(alice)
    service.moderate_comment(admin, top, "visible")  # admin restores it, reports cleared
    assert len(service.comments(None, entry)) == 1
    service.delete_comment(alice, top)
    assert service.comments(None, entry) == []


def test_comment_rules(make_user, admin, monkeypatch):
    entry = _public_entry(make_user, admin)
    user = make_user("alice")
    with pytest.raises(PermissionDenied):
        service.add_comment(None, entry, "anonymous?")
    with pytest.raises(service.ValidationError):
        service.add_comment(user, 99999, "no such entry")
    with pytest.raises(service.ValidationError):
        service.add_comment(user, entry, "x" * (service.MAX_COMMENT + 1))
    monkeypatch.setitem(service.RATE_LIMITS, "comment", (2, 60))
    service.add_comment(user, entry, "one")
    service.add_comment(user, entry, "two")
    with pytest.raises(service.RateLimited):
        service.add_comment(user, entry, "three")


def test_google_accounts_are_pseudonymous_and_stable():
    first = auth.login_external("google", "1098765")
    again = auth.login_external("google", "1098765")
    other = auth.login_external("google", "555")
    assert first.id == again.id != other.id and first.username.startswith("member-")
    with connect() as conn:
        row = conn.execute("SELECT external_id, password_hash FROM users WHERE id = ?", (first.id,)).fetchone()
    assert "1098765" not in row["external_id"] and row["password_hash"] == "!"
    with pytest.raises(AuthError):
        auth.authenticate(first.username, "!")  # no password login for Google accounts
    auth.change_username(first, "semaglutide_fan")
    assert auth.load_principal(first.id).username == "semaglutide_fan"
    with pytest.raises(AuthError):
        auth.delete_account(first, "wrong name")
    auth.delete_account(first, "semaglutide_fan")
    assert auth.load_principal(first.id) is None


def test_projects_are_private_and_track_questions(make_user):
    from drugscope.community import projects

    alice, bob = make_user("alice"), make_user("bob")
    pid = projects.create(alice, "semaglutide and the heart", "Scan")
    assert projects.list_projects(alice)[0]["status"] == "running" and projects.list_projects(bob) == []
    for intruder in (lambda: projects.get(bob, pid), lambda: projects.messages(bob, pid),
                     lambda: projects.ask(bob, pid, "hi"), lambda: projects.delete(bob, pid)):
        with pytest.raises(projects.ProjectNotFound):
            intruder()
    q = projects.ask(alice, pid, "What did SELECT find?")
    assert [p["id"] for p in projects.pending_questions(pid)] == [q]
    assert projects.mark_interrupted() == 1 and projects.get(alice, pid)["status"] == "failed"
    auth.delete_account(alice, "alice-password-1")
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


def test_assistant_strips_invented_citations_and_finds_relevant_sources():
    from drugscope.analysis import assistant
    from drugscope.models import SourceRecord

    text, removed, used = assistant.audit("MACE fell [S1]; also see [S99] and [s1].", {"S1"})
    assert removed == 1 and used == ["S1"] and "[S99]" not in text
    from tests.test_ui import _small_report  # reuse the fixture report

    report = _small_report(records=[
        SourceRecord(sid="S1", kind="paper", title="SELECT cardiovascular outcomes", url="u", source="PubMed",
                     snippet="semaglutide reduced MACE"),
        SourceRecord(sid="T1", kind="trial", title="STEP-HFpEF heart failure trial", url="u",
                     source="ClinicalTrials.gov", snippet="heart failure symptoms improved"),
    ])
    assert assistant.relevant_sources(report, "what about heart failure symptoms?")[0] == "T1"
    assert assistant.relevant_sources(report, "explain S1 please")[0] == "S1"
    prompt, detailed = assistant.build_prompt(report, "heart failure?", [{"role": "user", "content": "hi"}])
    assert "VERDICT" in prompt and "CONVERSATION SO FAR" in prompt and "T1" in detailed


def test_running_app_upgrades_schema_without_restart(tmp_path, monkeypatch):
    """A long-running app that checked the database before an update must still pick
    up new tables - this exact staleness caused 'no such table: projects'."""
    import threading

    from drugscope.community import db
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    for statement in db._split(db.MIGRATIONS[0]):
        conn.execute(statement)
    db._seed(conn)
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    monkeypatch.setenv("DRUGSCOPE_DB_PATH", str(path))
    db._ready.add(str(path))  # as if this process had already "checked" it
    errors = []

    def use_new_table():
        try:
            with connect() as c:
                c.execute("SELECT COUNT(*) FROM projects").fetchone()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=use_new_table) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with connect() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_reports_publish_immediately_by_default(make_user, monkeypatch):
    monkeypatch.setenv("DRUGSCOPE_AUTO_APPROVE", "1")
    result = _report(make_user(), context="my private note")
    assert result["published"]
    entries = service.public_entries()
    assert entries and entries[0]["reports"] == 1 and "context" not in entries[0]
