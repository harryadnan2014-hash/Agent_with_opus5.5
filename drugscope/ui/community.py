"""Community pages: food-drug and drug-drug checks, experience reports, the voting board.

Four kinds of information appear on these pages and are never blended:

* **Official label information** - quoted from US prescribing information.
* **Official database reports** - FAERS spontaneous-report counts.
* **Community experiences** - moderated, aggregated reports from DrugScope users.
* **Research hypotheses** - ideas under investigation, published by the administrator.

Each block carries its own label, source and caveat.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from ..community import service, sources
from ..community.auth import PermissionDenied
from . import account
from . import components as ui
from . import nav

EMERGENCY = (
    "If a reaction is severe - trouble breathing, swelling of the face, lips or throat, "
    "chest pain, fainting, or confusion - seek emergency care now (UAE: call 998 for an "
    "ambulance; elsewhere, your local emergency number). Do not wait to file a report."
)
NOT_ADVICE = (
    "DrugScope is a research tool, not a diagnostic or prescribing service. Reported "
    "experiences and votes show what people said happened to them - they do not establish "
    "that a medicine or food caused anything, how often it happens, or that a combination "
    "is safe. Never start, stop or combine medicines because of what you read here; talk "
    "to a doctor or pharmacist."
)
US_ONLY = (
    "Label data comes from US sources (FDA, DailyMed). Products sold in the UAE or "
    "elsewhere can have different labelling."
)


def _intro(eyebrow: str, title: str, subtitle: str) -> None:
    account.sidebar_status()
    ui.masthead(meta=[])
    st.markdown(
        f"<div class='ds-eyebrow'>{ui.esc(eyebrow)}</div><div class='ds-headline'>{ui.esc(title)}</div>"
        f"<div class='ds-subhead'>{ui.esc(subtitle)}</div>",
        unsafe_allow_html=True,
    )
    ui.note(NOT_ADVICE, status="warning", label="Research only.")


def _go(page: str) -> None:
    st.switch_page(nav.PAGES[page])


def add_actions(key: str) -> None:
    """The three ways to add an experience, always one click away."""
    a, b, c = st.columns(3)
    if a.button("➕ Report a side effect", key=f"{key}-cta-side", width="stretch", type="primary"):
        _go("report")
    if b.button("➕ Report a food interaction", key=f"{key}-cta-food", width="stretch"):
        st.session_state["open-tab-food"] = True
        _go("interactions")
    if c.button("➕ Report a drug interaction", key=f"{key}-cta-drug", width="stretch"):
        _go("drugs")


# --------------------------------------------------------------------------- #
# Shared widgets
# --------------------------------------------------------------------------- #

class _Unavailable(Exception):
    pass


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def _normalize_cached(term: str) -> sources.Result:
    result = sources.normalize_medication(term)
    if result.status == "unavailable":
        raise _Unavailable(result.message)  # an exception is never cached
    return result


def _normalize(term: str) -> sources.Result:
    try:
        return _normalize_cached(term.lower())
    except _Unavailable as exc:
        return sources.Result("unavailable", [], "RxNorm", message=str(exc))


def medication_picker(key: str, label: str = "Medication - generic or brand name") -> tuple[dict[str, Any] | None, str]:
    """Search by generic or brand name. Returns (RxNorm candidate or None, typed text)."""
    typed = st.text_input(label, key=f"{key}-med", placeholder="e.g. Ozempic, atorvastatin, Lipitor").strip()
    if len(typed) < 2:
        return None, typed

    with st.spinner("Looking up the medication..."):
        result = _normalize(typed)
    candidates = result.data or []
    labels = [
        c["ingredient"] + (f"  ({', '.join(c['brands'][:3])})" if c.get("brands") else "")
        for c in candidates
    ]
    as_typed = f"➕ Add “{typed}” as a new medication (not verified)"
    choice = st.selectbox("Matching medication", labels + [as_typed], key=f"{key}-match",
                          help="Brand names are matched to their active ingredient through RxNorm. "
                               "If yours is not listed, add it as typed - an administrator will check it.")
    if result.status == "unavailable":
        st.caption(f"⚠ {result.message}")
    elif not candidates:
        st.caption("RxNorm did not recognise that name - check the spelling, or add it as typed.")
    if choice == as_typed:
        return None, typed
    return candidates[labels.index(choice)], typed


def _identity(candidate: dict[str, Any] | None, typed: str) -> dict[str, Any]:
    if candidate:
        return {"ingredient": candidate["ingredient"], "brands": candidate.get("brands") or []}
    return {"ingredient": typed.lower(), "brands": []}


def _result_note(result: sources.Result, empty: str) -> bool:
    """Explain a non-ok lookup. Returns True when there is data to show."""
    if result.status == "unavailable":
        ui.note(result.message, status="warning", label="Source unavailable.")
        return False
    if result.status == "not_found":
        st.caption(result.message or empty)
        return False
    return True


def _source_caption(result: sources.Result) -> None:
    when = (result.retrieved_at or "")[:10]
    st.markdown(
        f"<div style='font-size:.72rem;color:var(--ds-ink-3);margin:-.2rem 0 .8rem'>"
        f"{ui.badge('Official source', status='good')} {ui.esc(result.source)}"
        + (f" · retrieved {ui.esc(when)}" if when else "")
        + (f" · <a href='{ui.esc(result.url)}' target='_blank' rel='noopener'>open source</a>" if result.url else "")
        + "</div>",
        unsafe_allow_html=True,
    )


def _entry_title(entry: dict[str, Any]) -> str:
    if entry["kind"] == "side_effect":
        return f"{entry['medication']} → {entry['symptom']}"
    if entry["kind"] == "drug_interaction":
        return f"{entry['medication']} + {entry['other_medication']}"
    return f"{entry['medication']} + {entry['food']}"


def entry_cards(entries: list[dict[str, Any]], *, key: str, empty: str) -> None:
    """Aggregated community entries with their vote buttons."""
    user = account.current_user()
    if not entries:
        st.caption(empty)
        return
    if user is None:
        st.caption("Sign in on the Account page to add your experience to the counts.")
    kinds = {"side_effect": "Side effect", "interaction": "Food interaction", "drug_interaction": "Drug interaction"}
    for entry in entries:
        reported = ""
        if entry.get("symptoms"):
            reported = "Reported alongside: " + ", ".join(f"{s['name']} ({s['n']})" for s in entry["symptoms"])
        with st.container(border=True):
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;gap:.6rem;flex-wrap:wrap'>"
                f"<b style='color:var(--ds-ink)'>{ui.esc(_entry_title(entry))}</b>"
                f"<span>{ui.badge(kinds[entry['kind']], slot=1)} "
                f"{ui.badge(str(entry['reports']) + ' approved report(s)', dot=False)}</span></div>"
                f"<div style='font-size:.74rem;color:var(--ds-ink-3);margin:.2rem 0 .4rem'>"
                f"Latest report {ui.esc((entry.get('latest') or '')[:10])}"
                + (f" · {ui.esc(reported)}" if reported else "") + "</div>",
                unsafe_allow_html=True,
            )
            account.entry_vote_buttons(entry, user, key)
            comment_thread(entry, user, key)


# --------------------------------------------------------------------------- #
# Comments
# --------------------------------------------------------------------------- #

def _comment_body(comment: dict[str, Any]) -> None:
    likes = f" · \U0001F44D {comment['likes']}" if comment["likes"] else ""
    st.markdown(
        f"<div style='font-size:.78rem;color:var(--ds-ink-3)'><b style='color:var(--ds-ink)'>"
        f"{ui.esc(comment['username'])}</b> · {ui.esc(comment['created_at'][:16].replace('T', ' '))}{likes}</div>"
        f"<div style='font-size:.86rem;color:var(--ds-ink-2);margin:.15rem 0 .25rem;white-space:pre-wrap'>"
        f"{ui.esc(comment['body'])}</div>",
        unsafe_allow_html=True,
    )


def _comment_actions(comment: dict[str, Any], user, key: str, *, can_reply: bool) -> None:
    if user is None:
        return
    cid = comment["id"]
    cols = st.columns([1, 1, 1, 3] if can_reply else [1, 1, 4])
    like_label = ("\U0001F44D Liked" if comment["liked"] else "\U0001F44D Like") + \
        (f" ({comment['likes']})" if comment["likes"] else "")
    try:
        if cols[0].button(like_label, key=f"{key}-like-{cid}", type="tertiary"):
            service.toggle_like(user, cid)
            st.rerun()
        position = 1
        if can_reply:
            if cols[1].button("↩ Reply", key=f"{key}-reply-{cid}", type="tertiary"):
                st.session_state[f"{key}-replying"] = cid
                st.rerun()
            position = 2
        if comment["mine"]:
            if cols[position].button("\U0001F5D1 Delete", key=f"{key}-delete-{cid}", type="tertiary"):
                service.delete_comment(user, cid)
                st.rerun()
        elif not comment["flagged"]:
            if cols[position].button("⚑ Report", key=f"{key}-flag-{cid}", type="tertiary",
                                     help="Report spam, abuse, or personal details"):
                hidden = service.flag_comment(user, cid, "reported from the community page")
                st.session_state["toast"] = ("Thanks - the comment is hidden until an administrator reviews it."
                                             if hidden else "Thanks - an administrator will review it.")
                st.rerun()
        else:
            cols[position].caption("Reported")
    except (service.ValidationError, service.RateLimited, PermissionDenied) as exc:
        st.error(str(exc))


def comment_thread(entry: dict[str, Any], user, key: str) -> None:
    """Comments under an entry: opened on demand, so long lists stay fast."""
    open_key = f"{key}-open-{entry['id']}"
    label = f"\U0001F4AC Comments ({entry.get('comments', 0)})"
    if not st.session_state.get(open_key):
        if st.button(label, key=f"{key}-show-{entry['id']}", type="tertiary"):
            st.session_state[open_key] = True
            st.rerun()
        return
    if st.button(f"\U0001F4AC Hide comments ({entry.get('comments', 0)})", key=f"{key}-hide-{entry['id']}",
                 type="tertiary"):
        st.session_state[open_key] = False
        st.rerun()

    thread_key = f"{key}-c{entry['id']}"
    replying = st.session_state.get(f"{thread_key}-replying")
    for comment in service.comments(user, entry["id"]):
        _comment_body(comment)
        _comment_actions(comment, user, thread_key, can_reply=True)
        for reply in comment["replies"]:
            gap, body = st.columns([0.06, 0.94])
            with body:
                _comment_body(reply)
                _comment_actions(reply, user, thread_key, can_reply=False)
        if replying == comment["id"] and user is not None:
            gap, body = st.columns([0.06, 0.94])
            with body, st.form(f"{thread_key}-replyform-{comment['id']}", clear_on_submit=True):
                text = st.text_area(f"Reply to {comment['username']}", max_chars=service.MAX_COMMENT, height=70)
                send, cancel = st.columns(2)
                if send.form_submit_button("Post reply", type="primary", width="stretch"):
                    _post(user, entry["id"], text, parent_id=comment["id"], thread_key=thread_key)
                if cancel.form_submit_button("Cancel", width="stretch"):
                    st.session_state.pop(f"{thread_key}-replying", None)
                    st.rerun()

    if user is None:
        st.caption("Sign in to comment and like.")
        return
    with st.form(f"{thread_key}-new", clear_on_submit=True):
        text = st.text_area("Write a comment", max_chars=service.MAX_COMMENT, height=80,
                            placeholder="Share what happened, what helped, or a question for others...")
        st.caption("Comments are public under your username. Don't share your name, contact details or anything "
                   "that identifies you - emails, phone numbers and links are removed automatically.")
        if st.form_submit_button("Post comment", type="primary"):
            _post(user, entry["id"], text, parent_id=None, thread_key=thread_key)


def _post(user, entry_id: int, text: str, *, parent_id: int | None, thread_key: str) -> None:
    try:
        service.add_comment(user, entry_id, text, parent_id=parent_id)
    except (service.ValidationError, service.RateLimited, PermissionDenied) as exc:
        st.error(str(exc))
        return
    st.session_state.pop(f"{thread_key}-replying", None)
    st.rerun()


def hypothesis_cards(rows: list[dict[str, Any]]) -> None:
    if not rows:
        st.caption("No published research hypotheses for this yet.")
        return
    for row in rows:
        subject = " · ".join(x for x in (row.get("medication"), row.get("other_medication"),
                                              row.get("food"), row.get("symptom")) if x)
        links = "".join(
            f"<a href='{ui.esc(u)}' target='_blank' rel='noopener' style='margin-right:.6rem'>source</a>"
            for u in (row.get("evidence_urls") or "").splitlines() if u.strip()
        )
        st.markdown(
            f"<div class='ds-card' style='--ds-card-accent:var(--ds-serious)'>"
            f"<div class='ds-card-head'><div class='ds-card-title'>{ui.esc(row['title'])}</div>"
            f"{ui.badge('Unverified hypothesis', status='serious')}</div>"
            f"<div class='ds-card-body'>{ui.esc(row['description'])}</div>"
            f"<div class='ds-card-foot'>{ui.esc(subject)} · status: {ui.esc(row['status'].replace('_', ' '))}"
            f" {links}</div></div>",
            unsafe_allow_html=True,
        )


def suggest_category_button(kind: str, key: str) -> None:
    """A visible "suggest a new category" button that opens a small form."""
    noun = "symptom" if kind == "symptom" else "food or drink"
    with st.popover(f"➕ Suggest a new {noun}", width="stretch", key=f"{key}-pop"):
        user = account.current_user()
        if user is None:
            st.caption("Sign in to suggest a category.")
            return
        st.caption(f"Can't find your {noun} in the list? Suggest it - an administrator reviews every suggestion, "
                   "and approved ones appear in the forms for everyone.")
        name = st.text_input(f"Proposed {noun}", key=f"{key}-sname", max_chars=60)
        similar = service.similar_categories(kind, name) if len(name.strip()) >= 2 else []
        if similar:
            st.caption("Already in the list or suggested - is one of these what you mean? " + ", ".join(
                f"**{s['name']}**" + (" (awaiting review)" if s["state"] == "pending" else "") for s in similar))
        description = st.text_area("Optional description", key=f"{key}-sdesc", max_chars=300, height=70)
        if st.button("Submit suggestion", key=f"{key}-ssubmit", type="primary"):
            try:
                service.suggest_category(user, kind, name, description)
                st.success("Thanks - it is waiting for review.")
            except (service.ValidationError, service.RateLimited, PermissionDenied) as exc:
                st.error(str(exc))


# --------------------------------------------------------------------------- #
# Report form
# --------------------------------------------------------------------------- #

def report_form(report_type: str, key: str) -> None:
    user = account.require_sign_in("Reports are tied to an account so they can be moderated and deleted later.")
    if user is None:
        return
    ui.note(EMERGENCY, status="critical", label="Emergency.")

    if report_type == "drug_interaction":
        left, right = st.columns(2, gap="medium")
        with left:
            candidate, typed = medication_picker(key, "First medication")
        with right:
            other_candidate, other_typed = medication_picker(f"{key}-b", "Second medication")
    else:
        candidate, typed = medication_picker(key)
        other_candidate, other_typed = None, ""

    food_rows = service.foods()
    symptom_rows = service.symptoms()
    tools = st.columns(2 if report_type == "interaction" else 1)
    with tools[0]:
        suggest_category_button("symptom", f"{key}-sym")
    if report_type == "interaction":
        with tools[1]:
            suggest_category_button("food", f"{key}-fd")

    with st.form(f"{key}-form", clear_on_submit=False):
        symptom_names = st.multiselect(
            "Symptom(s) you experienced", [s["name"] for s in symptom_rows],
            max_selections=service.MAX_SYMPTOMS, key=f"{key}-symptoms",
            help="Not listed? Use “➕ Suggest a new symptom” above.",
        )
        food_options = [f["name"] for f in food_rows]
        food_name = None
        if report_type == "interaction":
            food_name = st.selectbox("Food or drink involved", food_options, index=None,
                                     placeholder="Choose a food or drink", key=f"{key}-food")
        elif report_type == "side_effect":
            food_name = st.selectbox("Food or drink around the same time (optional)", ["None"] + food_options,
                                     key=f"{key}-food")
            food_name = None if food_name == "None" else food_name
        left, right = st.columns(2)
        with left:
            onset = st.selectbox("Time from taking the medication to the symptom",
                                 list(service.ONSET_CHOICES), format_func=service.ONSET_CHOICES.get,
                                 key=f"{key}-onset")
            frequency = st.radio("How often", list(service.FREQUENCY_CHOICES),
                                 format_func=service.FREQUENCY_CHOICES.get, horizontal=True, key=f"{key}-freq")
        with right:
            severity = st.radio("Severity, as you experienced it", list(service.SEVERITY_CHOICES),
                                format_func=service.SEVERITY_CHOICES.get, key=f"{key}-sev")
        context = st.text_area(
            "Optional context", max_chars=service.MAX_CONTEXT, height=80, key=f"{key}-ctx",
            help="For example dose timing or other relevant circumstances.",
            placeholder="Do not include your name, contact details, or anything that identifies you.",
        )
        st.caption("Privacy: do not include names, contact details, addresses or record numbers. "
                   "Emails, phone numbers and links are removed automatically. Only the administrator "
                   "can read individual reports.")
        consent = st.checkbox(
            "I consent to this report being stored for research. I understand it counts publicly "
            "straight away, only aggregated counts are shown to others (never my notes), and I can "
            "delete it from my Account page.", key=f"{key}-consent",
        )
        submitted = st.form_submit_button("Submit report", type="primary", width="stretch")

    if submitted:
        if not typed or (report_type == "drug_interaction" and not other_typed):
            st.error("Enter the medication" + ("s" if report_type == "drug_interaction" else "") + " first.")
            return
        ids = {s["name"]: s["id"] for s in symptom_rows}
        food_ids = {f["name"]: f["id"] for f in food_rows}
        try:
            result = service.submit_report(
                user, report_type=report_type, medication_typed=typed, medication_candidate=candidate,
                symptom_ids=[ids[n] for n in symptom_names], food_id=food_ids.get(food_name) if food_name else None,
                onset=onset, severity=severity, frequency=frequency, context=context, consent=consent,
                other_medication_typed=other_typed, other_medication_candidate=other_candidate,
            )
        except (service.ValidationError, service.RateLimited, PermissionDenied) as exc:
            st.error(str(exc))
            return
        st.success("Thank you - your report is now public in the Community counts." if result.get("published")
                   else "Thank you - your report is saved and will count publicly once an administrator approves it.")
        if result["redacted"]:
            st.info("Contact details or links in your context were removed before saving.")
        if severity == "severe":
            ui.note(EMERGENCY, status="critical", label="Please read.")


# --------------------------------------------------------------------------- #
# Food <-> drug
# --------------------------------------------------------------------------- #

def interactions_page() -> None:
    _intro("Food ↔ drug", "Food and drink with your medication",
           "What official labels say, what people report, and what is being investigated - kept separate.")
    labels = ["Drug → Food", "Food → Drug", "➕ Report a food interaction"]
    default = labels[2] if st.session_state.pop("open-tab-food", False) else None
    drug_tab, food_tab, report_tab = st.tabs(labels, default=default)

    with drug_tab:
        candidate, typed = medication_picker("d2f")
        if typed and st.button("Look up food information", type="primary", key="d2f-go"):
            st.session_state["d2f-active"] = (candidate, typed)
        active = st.session_state.get("d2f-active")
        if active:
            _drug_to_food(*active)

    with food_tab:
        food_rows = service.foods()
        name = st.selectbox("Food or drink", [f["name"] for f in food_rows], index=None,
                            placeholder="Choose a food or drink", key="f2d-food")
        if name:
            _food_to_drug(next(f for f in food_rows if f["name"] == name))
        suggest_category_button("food", "f2d")

    with report_tab:
        report_form("interaction", "interaction")


def _drug_to_food(candidate: dict[str, Any] | None, typed: str) -> None:
    ingredient = (candidate or {}).get("ingredient") or typed
    ui.section(f"{ingredient}", "what is documented, what people report, what is hypothesised")

    ui.section("What the US label says about food and drink")
    with st.spinner("Reading the prescribing information..."):
        label = sources.label_food_mentions(ingredient, service.foods())
    if _result_note(label, f"No US label was found for {ingredient}."):
        _source_caption(label)
        mentions = label.data.get("mentions") or {}
        if not mentions:
            st.caption("The label found does not mention any of the tracked foods or drinks. That does "
                       "not mean no interaction exists.")
        for food, quotes in mentions.items():
            with st.expander(f"{food} - {len(quotes)} label passage(s)"):
                for item in quotes:
                    st.markdown(f"<div style='font-size:.82rem;color:var(--ds-ink-2);margin-bottom:.45rem'>"
                                f"<b>{ui.esc(item['section'])}:</b> “{ui.esc(item['text'])}”</div>",
                                unsafe_allow_html=True)
    st.caption(US_ONLY)

    med = service.find_medication(ingredient)
    ui.section("Community experiences", "moderated and aggregated - not verified, not a measure of risk")
    entries = service.public_entries(account.current_user(), kind="interaction",
                                     medication_id=med["id"]) if med else []
    entry_cards(entries, key="d2f", empty="No approved community reports for this medication with food yet.")

    ui.section("Research hypotheses", "unverified ideas under investigation")
    hypothesis_cards(service.public_hypotheses(medication_id=med["id"]) if med else [])

    with st.expander("Current label documents (DailyMed)"):
        dailymed = sources.dailymed_labels(ingredient)
        if _result_note(dailymed, "No DailyMed documents found."):
            for row in dailymed.data:
                st.markdown(f"- [{row['title']}]({row['url']}) · {row['published']}")


def _food_to_drug(food: dict[str, Any]) -> None:
    ui.section(food["name"], "medications whose labels mention it, and what people report")
    with st.spinner("Searching US labels..."):
        result = sources.drugs_mentioning_food(food)
    if _result_note(result, f"No US label interaction or patient sections mention {food['name']}."):
        _source_caption(result)
        st.dataframe(
            [{"Medication": r["generic_name"], "Label documents mentioning it": r["label_documents"]}
             for r in result.data],
            hide_index=True, width="stretch",
        )
        st.caption("A label mentioning a food is not the same as a clinically important interaction - "
                   "open the medication on the Drug → Food tab to read the exact wording. The count is "
                   "how many label documents (one per manufacturer or packager) mention it.")
    st.caption(US_ONLY)

    ui.section("Community experiences")
    entry_cards(service.public_entries(account.current_user(), kind="interaction", food_id=food["id"]),
                key="f2d", empty=f"No approved community reports involving {food['name']} yet.")
    ui.section("Research hypotheses")
    hypothesis_cards(service.public_hypotheses(food_id=food["id"]))


# --------------------------------------------------------------------------- #
# Drug <-> drug
# --------------------------------------------------------------------------- #

def drug_interactions_page() -> None:
    _intro("Drug ↔ drug", "Check two medications together",
           "What each official label says about the other, adverse-event reports that list both, and "
           "community experiences.")
    check_tab, report_tab = st.tabs(["Check a combination", "➕ Report a drug interaction"])
    with check_tab:
        left, right = st.columns(2, gap="medium")
        with left:
            first = medication_picker("dd-a", "First medication")
        with right:
            second = medication_picker("dd-b", "Second medication")
        ready = bool(first[1] and second[1])
        if st.button("Check combination", type="primary", key="dd-go", disabled=not ready):
            a, b = _identity(*first), _identity(*second)
            if a["ingredient"] == b["ingredient"]:
                st.error("Choose two different medications.")
            else:
                st.session_state["dd-active"] = (a, b)
        active = st.session_state.get("dd-active")
        if active:
            _drug_pair(*active)
    with report_tab:
        report_form("drug_interaction", "druginteraction")


def _quotes(items: list[dict[str, str]], badge: str, status: str) -> None:
    for item in items:
        tag = ui.badge(badge if "class" not in item else f"Class: {item['class']}", status=status)
        st.markdown(
            f"<div class='ds-card' style='--ds-card-accent:var(--ds-{status})'>"
            f"<div class='ds-card-head'><div style='font-size:.74rem;color:var(--ds-ink-3)'>"
            f"{ui.esc(item['section'])}</div>{tag}</div>"
            f"<div class='ds-card-body'>“{ui.esc(item['text'])}”</div></div>",
            unsafe_allow_html=True,
        )


def _drug_pair(a: dict[str, Any], b: dict[str, Any]) -> None:
    ui.section(f"{a['ingredient']} + {b['ingredient']}", "documented, reported, experienced - kept apart")

    ui.section("What the US labels say")
    with st.spinner("Reading both labels..."):
        labels = sources.label_drug_mentions(a, b)
    if labels.status == "unavailable":
        ui.note(labels.message, status="warning", label="Source unavailable.")
    else:
        if labels.status == "not_found":
            ui.note(
                "Neither US label names the other medication or a drug class it belongs to. That is not "
                "evidence the combination is safe - labels do not list every interaction. Ask a pharmacist.",
                status="warning", label="Nothing found in the labels.",
            )
        columns = st.columns(2, gap="large")
        for column, (side, this, other) in zip(columns, (("first", a, b), ("second", b, a))):
            data = labels.data[side]
            with column:
                st.markdown(f"**{ui.esc(data['title'])} label** → mentions of {ui.esc(other['ingredient'])}")
                if data["status"] != "ok":
                    st.caption(f"No US label found for {this['ingredient']}.")
                    continue
                if data["url"]:
                    st.markdown(f"<div style='font-size:.72rem;margin-bottom:.4rem'>{ui.badge('Official source', status='good')} "
                                f"<a href='{ui.esc(data['url'])}' target='_blank' rel='noopener'>open label</a></div>",
                                unsafe_allow_html=True)
                if not data["direct"] and not data["class"]:
                    st.caption("Not mentioned by name or by class.")
                _quotes(data["direct"], f"Names {other['ingredient']}", "critical")
                if data["class"]:
                    st.caption(f"Class-level mentions - the label names a class {other['ingredient']} belongs to "
                               f"({', '.join(data['classes'][:4])}). Weaker than a direct mention.")
                    _quotes(data["class"], "Class", "warning")
    st.caption(US_ONLY + " Drug classes come from the FDA's structured labels via RxClass.")

    ui.section("Adverse-event reports listing both (openFDA FAERS)")
    with st.spinner("Counting reports..."):
        faers = sources.faers_pair(a["ingredient"], b["ingredient"])
    if _result_note(faers, "No FAERS reports list both medications."):
        _source_caption(faers)
        st.markdown(f"**{faers.data['total']:,}** reports name both medications. Most often reported alongside:")
        st.dataframe([{"Reaction": r["term"], "Reports": r["count"]} for r in faers.data["reactions"]],
                     hide_index=True, width="stretch")
        st.caption("Reporting volume only: no denominator, voluntary reporting, and a report listing two drugs "
                   "does not mean they interacted. Commonly co-prescribed drugs, and drugs involved in "
                   "litigation, appear together often for reasons unrelated to any interaction.")

    ui.section("Community experiences")
    med_a, med_b = service.find_medication(a["ingredient"]), service.find_medication(b["ingredient"])
    entries = (service.public_entries(account.current_user(), kind="drug_interaction",
                                      medication_id=med_a["id"], other_medication_id=med_b["id"])
               if med_a and med_b else [])
    entry_cards(entries, key="dd", empty="No approved community reports for this combination yet.")
    ui.section("Research hypotheses")
    hypothesis_cards([h for h in service.public_hypotheses(medication_id=med_a["id"])
                      if med_b and med_b["name"] in (h.get("medication"), h.get("other_medication"))]
                     if med_a else [])


# --------------------------------------------------------------------------- #
# Report and community pages
# --------------------------------------------------------------------------- #

def report_page() -> None:
    _intro("Report", "Report a side effect", "Free for everyone - your report counts publicly straight away, never shown "
           "individually.")
    report_form("side_effect", "side")


def community_page() -> None:
    _intro("Community", "Community reports", "Approved experiences, aggregated, with community confirmations.")
    add_actions("cm")
    ui.note(
        "“I've experienced this too” counts are self-reported by people who chose to respond. They "
        "are not scientific confirmation, they do not show how common something is, and they never "
        "become a risk score.", status="warning", label="How to read the votes.",
    )
    left, middle, right = st.columns([1.2, 1, 1])
    with left:
        medication = st.text_input("Filter by medication", key="cm-med").strip().lower()
    with middle:
        kind = st.selectbox("Type", ["All", "Side effects", "Food interactions", "Drug interactions"], key="cm-kind")
    with right:
        sort = st.selectbox("Sort by", ["Most confirmations", "Most recent"], key="cm-sort")

    entries = service.public_entries(
        account.current_user(),
        kind={"Side effects": "side_effect", "Food interactions": "interaction",
              "Drug interactions": "drug_interaction"}.get(kind),
        sort="recent" if sort == "Most recent" else "confirmations",
        limit=200,
    )
    if medication:
        entries = [e for e in entries
                   if medication in e["medication"].lower() or medication in (e["other_medication"] or "").lower()]
    st.caption(f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} · "
               f"{sum(e['yes_votes'] + e['no_votes'] for e in entries)} vote(s) on them")
    entry_cards(entries, key="cm", empty="No approved community reports match yet. Be the first to report.")

    if medication and entries:
        _faers_reference([e for e in entries if e["kind"] == "side_effect"][:8])

    ui.section("Published research hypotheses")
    hypothesis_cards(service.public_hypotheses())
    nav.link("insights", "See community insights and voting statistics", ":material/insights:")


def _faers_reference(entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    terms = {s["name"]: s["meddra_term"] for s in service.symptoms()}
    with st.expander("Official database reports (openFDA FAERS) for these pairs"):
        st.caption("Spontaneous reports to the FDA naming the medication and reaction. Reporting volume "
                   "only - no denominator, voluntary, and a report does not establish causation.")
        rows = []
        for entry in entries:
            result = sources.faers_count(entry["medication"], terms.get(entry["symptom"], ""))
            rows.append({
                "Medication": entry["medication"], "Symptom": entry["symptom"],
                "FAERS reports": result.data if result.status == "ok" else
                ("unavailable" if result.status == "unavailable" else 0),
            })
        st.dataframe(rows, hide_index=True, width="stretch")
