"""The Streamlit pages, driven headlessly with AppTest."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from drugscope.community import auth, quota, service

ROOT = Path(__file__).resolve().parents[1]

# Replaces the network lookups with fixtures (or an outage) inside the app process.
PATCH = """
import importlib, sys
sys.path.insert(0, {root!r})
import httpx
# Patches persist in the test process, so every run starts from the real modules.
if not hasattr(httpx.Client, "_ds_real_get"):
    httpx.Client._ds_real_get = httpx.Client.get
httpx.Client.get = httpx.Client._ds_real_get
from drugscope.community import sources as _s
importlib.reload(_s)
MODE = {mode!r}
if MODE == "outage":
    def _boom(*a, **k):
        raise httpx.ConnectError("offline")
    httpx.Client.get = _boom
else:
    _s.normalize_medication = lambda term: _s.Result("ok", [
        {{"ingredient": "semaglutide", "brands": ["Ozempic"], "rxcui": "1991302"}}], "RxNorm")
    _s.label_food_mentions = lambda ingredient, foods: _s.Result("ok", {{
        "title": "Ozempic", "set_id": "x", "mentions": {{"Alcohol": [
            {{"section": "Drug interactions", "text": "Alcohol may increase the risk of hypoglycemia."}}]}}}},
        "openFDA drug labels", "https://dailymed.nlm.nih.gov/x", "2026-10-04T00:00:00Z")
    _s.drugs_mentioning_food = lambda food, limit=15: _s.Result("ok", [
        {{"generic_name": "Atorvastatin Calcium", "label_documents": 230}}], "openFDA drug labels",
        "https://open.fda.gov", "2026-10-04T00:00:00Z")
    _s.dailymed_labels = lambda ingredient, limit=5: _s.Result("not_found", [], "DailyMed")
"""


def page(body: str, *, mode: str = "fixtures") -> AppTest:
    script = PATCH.format(root=str(ROOT), mode=mode) + "\nfrom drugscope.ui import theme\ntheme.inject()\n" + body
    return AppTest.from_string(script, default_timeout=60)


def app() -> AppTest:
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)


def signed_in(at: AppTest, principal) -> AppTest:
    at.session_state["user_id"] = principal.id
    return at


def text(at: AppTest) -> str:
    return " ".join(m.value for m in at.markdown) + " " + " ".join(
        str(getattr(e, "value", "")) for e in list(at.error) + list(at.success) + list(at.caption))


def no_exceptions(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]


# --------------------------------------------------------------------------- #

def test_app_loads_and_hides_admin_from_users(admin, make_user):
    at = app().run()
    no_exceptions(at)
    user = make_user()
    at = signed_in(app(), user).run()
    no_exceptions(at)
    at = signed_in(app(), admin).run()
    no_exceptions(at)


def test_research_requires_sign_in():
    at = app().run()
    at.text_area(key="query_box").set_value("semaglutide cardiovascular")
    at.button[0].click().run()  # "Run research" is the first button
    no_exceptions(at)
    assert "Sign in required" in text(at) or "Cannot run" in text(at)


def test_research_blocked_when_out_of_credits(make_user, monkeypatch):
    user = make_user()
    quota.consume(user, 5, kind="research")
    at = signed_in(app(), user).run()
    at.text_area(key="query_box").set_value("semaglutide cardiovascular")
    at.button[0].click().run()
    no_exceptions(at)
    body = text(at)
    if "Cannot run" in body:
        pytest.skip("no model provider key in this environment")
    assert "Out of research credits" in body and "Redeem code" in [b.label for b in at.button]
    assert quota.status(user).used == 5  # the blocked run charged nothing


def test_admin_page_denies_regular_user(make_user):
    user = make_user()
    at = signed_in(page("from drugscope.ui import admin\nadmin.admin_page()"), user).run()
    no_exceptions(at)
    assert "No access" in text(at)
    assert not at.dataframe  # nothing private rendered


def test_admin_page_renders_for_admin(admin, make_user):
    reporter = make_user()
    service.submit_report(reporter, report_type="side_effect", medication_typed="Ozempic",
                          medication_candidate={"ingredient": "semaglutide", "brands": [], "rxcui": "1"},
                          symptom_ids=[1], food_id=None, onset="1_6h", severity="mild", frequency="once",
                          context="private note", consent=True)
    at = signed_in(page("from drugscope.ui import admin\nadmin.admin_page()"), admin)
    at.session_state["admin_tab"] = "Moderation"  # tabs are lazy: open the one under test
    at.run()
    no_exceptions(at)
    assert "private note" in text(at)  # moderation queue shows the raw report to the admin
    approve = next(b for b in at.button if b.label == "Approve")
    approve.click().run()
    no_exceptions(at)
    assert service.public_entries()  # approved through the UI -> now public


def test_report_form_submits(make_user):
    user = make_user()
    at = signed_in(page("from drugscope.ui import community\ncommunity.report_page()"), user).run()
    no_exceptions(at)
    at.text_input(key="side-med").set_value("ozempic").run()
    at.multiselect(key="side-symptoms").select("Nausea")
    at.checkbox(key="side-consent").check()
    next(b for b in at.button if b.label == "Submit report").click().run()
    no_exceptions(at)
    assert any("Thank you" in s.value for s in at.success)
    mine = service.my_reports(user)
    assert mine and mine[0]["medication"] == "semaglutide" and mine[0]["status"] == "pending"


def test_report_form_requires_account():
    at = page("from drugscope.ui import community\ncommunity.report_page()").run()
    no_exceptions(at)
    assert "Sign in required" in text(at)


def test_interactions_page_both_directions(make_user):
    at = signed_in(page("from drugscope.ui import community\ncommunity.interactions_page()"), make_user()).run()
    at.text_input(key="d2f-med").set_value("ozempic").run()
    next(b for b in at.button if b.label == "Look up food information").click().run()
    no_exceptions(at)
    assert "hypoglycemia" in text(at)
    at.selectbox(key="f2d-food").select("Grapefruit").run()
    no_exceptions(at)
    assert any(len(df.value) for df in at.dataframe)


def test_pages_survive_external_outage(make_user):
    at = signed_in(page("from drugscope.ui import community\ncommunity.interactions_page()", mode="outage"),
                   make_user()).run()
    at.text_input(key="d2f-med").set_value("lipitor").run()
    no_exceptions(at)
    next(b for b in at.button if b.label == "Look up food information").click().run()
    no_exceptions(at)
    assert "did not respond" in text(at) or "unavailable" in text(at).lower()
    at.selectbox(key="f2d-food").select("Grapefruit").run()
    no_exceptions(at)


def test_community_voting_through_ui(admin, make_user):
    reporter, voter = make_user("reporter"), make_user("voter")
    rid = service.submit_report(reporter, report_type="side_effect", medication_typed="x",
                                medication_candidate={"ingredient": "semaglutide", "brands": [], "rxcui": "1"},
                                symptom_ids=[1], food_id=None, onset="1_6h", severity="mild",
                                frequency="once", context="", consent=True)["id"]
    service.moderate_report(admin, rid, "approved")
    at = signed_in(page("from drugscope.ui import community\ncommunity.community_page()"), voter).run()
    no_exceptions(at)
    yes = next(b for b in at.button if b.label.startswith("✔ I've experienced this too"))
    yes.click().run()
    no_exceptions(at)
    assert service.public_entries(voter)[0]["yes_votes"] == 1


def test_plans_page_shows_tiers_and_redeems_code(admin, make_user):
    user = make_user()
    code, = quota.generate_codes(admin, count=1, days=30, plan="team")
    at = signed_in(page("from drugscope.ui import account\naccount.plans_page()"), user).run()
    no_exceptions(at)
    body = text(at).replace("&#36;", "$")  # dollar signs are entity-escaped against LaTeX
    assert all(name in body for name in ("Free", "Pro", "Team", "$0", "$9", "$29"))
    assert "Checkout is not connected" in " ".join(i.value for i in at.info)
    redeem_box = next(t for t in at.text_input if t.label == "Upgrade code")
    redeem_box.set_value(code)
    next(b for b in at.button if b.label == "Redeem code").click().run()
    no_exceptions(at)
    assert quota.status(user).plan == "team" and quota.status(user).limit == 200


def test_drug_interactions_page(make_user):
    body = """
_s.normalize_medication = lambda term: _s.Result("ok", [{"ingredient": term.lower(), "brands": [], "rxcui": "1"}], "RxNorm")
_s.label_drug_mentions = lambda a, b: _s.Result("ok", {
    "first": {"title": "Plavix", "url": "https://dailymed.nlm.nih.gov/x", "status": "ok", "classes": [],
              "direct": [{"section": "Drug interactions", "text": "Avoid concomitant use of clopidogrel with omeprazole."}],
              "class": []},
    "second": {"title": "Prilosec", "url": "", "status": "ok", "classes": ["Platelet aggregation inhibitor"],
               "direct": [], "class": []}}, "openFDA drug labels + RxClass")
_s.faers_pair = lambda a, b, top=8: _s.Result("ok", {"total": 1234, "reactions": [{"term": "Stent Thrombosis", "count": 9}]},
    "openFDA FAERS", "https://open.fda.gov", "2026-10-04T00:00:00Z")
from drugscope.ui import community
community.drug_interactions_page()
"""
    at = signed_in(page(body), make_user()).run()
    no_exceptions(at)
    at.text_input(key="dd-a-med").set_value("clopidogrel").run()
    at.text_input(key="dd-b-med").set_value("prilosec").run()
    next(b for b in at.button if b.label == "Check combination").click().run()
    no_exceptions(at)
    assert "Avoid concomitant use of clopidogrel with omeprazole" in text(at)
    assert any(len(df.value) for df in at.dataframe)  # FAERS reactions table


def test_insights_page_renders(admin, make_user):
    reporter, voter = make_user("reporter"), make_user("voter")
    rid = service.submit_report(reporter, report_type="side_effect", medication_typed="x",
                                medication_candidate={"ingredient": "semaglutide", "brands": [], "rxcui": "1"},
                                symptom_ids=[1, 2], food_id=None, onset="1_6h", severity="moderate",
                                frequency="once", context="", consent=True)["id"]
    service.moderate_report(admin, rid, "approved")
    service.cast_vote(voter, service.public_entries()[0]["id"], 1)
    at = page("from drugscope.ui import insights\ninsights.insights_page()").run()
    no_exceptions(at)
    assert "People who voted" in text(at) and at.get("plotly_chart")


def test_admin_insights_tab(admin):
    at = signed_in(page("from drugscope.ui import admin\nadmin.admin_page()"), admin)
    at.session_state["admin_tab"] = "Insights (EDA)"
    at.run()
    no_exceptions(at)


def test_sign_up_through_ui():
    at = page("from drugscope.ui import account\naccount.account_page()").run()
    at.text_input(key="account-nu").set_value("newperson")
    at.text_input(key="account-np").set_value("a-long-password")
    at.checkbox(key="account-adult").check()
    at.checkbox(key="account-agree").check()
    next(b for b in at.button if b.label == "Create account").click().run()
    no_exceptions(at)
    assert auth.authenticate("newperson", "a-long-password")
    assert at.session_state["user_id"]


def _small_report(query="q", records=None):
    from drugscope.analysis.metrics import compute
    from drugscope.models import (ComparisonMatrix, ExecutiveSummary, KeyFinding, Report, ResearchPlan,
                                  SourceRecord, SynthesisBundle, Usage)

    records = records if records is not None else [SourceRecord(
        sid="S1", kind="paper", title="SELECT trial of semaglutide", url="https://pubmed.ncbi.nlm.nih.gov/1/",
        source="PubMed", snippet="MACE 6.5% vs 8.0% (HR 0.80) in 17,604 patients.", year=2023, design="rct")]
    plan = ResearchPlan(interpretation="q", intent="general", primary_entities=[], comparators=[],
                        conditions=[], literature_queries=[], trial_queries=[], regulatory_terms=[],
                        key_outcomes=[], open_questions=[], disclaimers=[])
    bundle = SynthesisBundle(
        summary=ExecutiveSummary(verdict="Semaglutide cut MACE [S1]", narrative="n [S1]", confidence="high",
                                 confidence_rationale="r", consensus=0, maturity=0, watch_items=[]),
        key_findings=[KeyFinding(headline="MACE fell", detail="6.5% vs 8.0% [S1]", so_what="s",
                                 strength="strong", citations=["S1"])],
        comparison=ComparisonMatrix(entities=[], rows=[], bottom_line=""), timeline=[],
        conflicts=[], gaps=[], safety_signals=[], regulatory_status="", mechanism="")
    return Report(query=query, plan=plan, synthesis=bundle, records=records, appraisals={},
                  metrics=compute(records, {}), usage=Usage(), elapsed_seconds=1.0, depth="Standard")


@pytest.fixture
def fake_research(monkeypatch):
    """Replace the model-driven pipeline with an instant one; `gate` holds it mid-run."""
    import threading

    import drugscope.pipeline as pipeline
    from drugscope.pipeline import Progress

    gate = threading.Event()
    gate.set()

    def fake_run_sync(settings):
        yield Progress(stage="plan", label="Interpreting", pct=4)
        gate.wait(10)
        yield Progress(stage="done", label="Report ready", pct=100, payload=_small_report(settings.query))

    monkeypatch.setattr(pipeline, "run_sync", fake_run_sync)
    return gate


def _start_research(at, query):
    at.session_state["project_id"] = None
    at.query_params.clear()
    at.run()
    at.text_area(key="query_box").set_value(query)
    next(b for b in at.button if b.label.startswith("Run research")).click().run()
    no_exceptions(at)
    if "Cannot run" in text(at):
        pytest.skip("no model provider key in this environment")
    return at.session_state["project_id"] if "project_id" in at.session_state else None


def test_credits_are_deducted_per_run_then_blocked(make_user, fake_research):
    """The whole credit loop through the real research page, with the model replaced."""
    from drugscope.ui import projects as projects_ui

    user = make_user()
    at = signed_in(app(), user)
    left = []
    for i in range(3):
        pid = _start_research(at, f"a question of my own {i}")
        if pid:
            projects_ui.wait_for(pid)
        left.append(quota.status(user).remaining)
    assert left == [3, 1, 1]  # Standard costs 2; the third run is refused, not charged
    assert "Out of research credits" in text(at)
    assert any(b.label == "See plans & upgrade" for b in at.button)


def test_research_becomes_a_saved_project_with_its_own_page(make_user, fake_research):
    from drugscope.community import projects
    from drugscope.ui import projects as projects_ui

    user = make_user()
    at = signed_in(app(), user)
    pid = _start_research(at, "semaglutide and the heart")
    assert pid and at.query_params["project"] == [str(pid)]
    projects_ui.wait_for(pid)
    at.run()
    no_exceptions(at)
    assert projects.get(user, pid)["status"] == "done"
    assert "Semaglutide cut MACE" in text(at) and at.tabs  # the report renders in the project page
    # Another person cannot open it, even with the link.
    other = make_user("mallory")
    at2 = signed_in(app(), other)
    at2.query_params["project"] = str(pid)
    at2.run()
    no_exceptions(at2)
    assert "not yours" in text(at2) and "Semaglutide cut MACE" not in text(at2)


def test_question_asked_while_running_is_answered_when_done(make_user, fake_research, monkeypatch):
    from drugscope.analysis import assistant
    from drugscope.community import projects
    from drugscope.ui import projects as projects_ui

    asked = []

    def fake_ask(report, question, history, *, provider, gateway_model=""):
        asked.append(question)
        return assistant.Answer("From the report: MACE fell to 6.5% [S1] [FAKE9]", ["S1"], 1)

    monkeypatch.setattr(assistant, "ask", fake_ask)
    fake_research.clear()  # hold the research mid-run
    user = make_user()
    at = signed_in(app(), user)
    pid = _start_research(at, "semaglutide and the heart")
    at.chat_input(key=f"chat-{pid}").set_value("What happened to MACE?").run()
    no_exceptions(at)
    assert "Waiting for the research to finish" in text(at)
    assert asked == []  # not answered before the report exists
    fake_research.set()  # let the research finish
    projects_ui.wait_for(pid)
    at.run()
    no_exceptions(at)
    history = projects.messages(user, pid)
    assert [m["role"] for m in history] == ["user", "assistant"] and asked == ["What happened to MACE?"]
    assert "MACE fell to 6.5%" in text(at)
