"""The administrator's private research workspace.

The page is only listed in the navigation for administrators, but that is a
convenience, not the protection: every call below goes through a `service`
function that re-checks the role in the database and raises `PermissionDenied`
otherwise. Reaching this page by URL without the role shows nothing private.
"""

from __future__ import annotations

from datetime import date

import plotly.graph_objects as go
import streamlit as st

from ..community import quota, service
from ..community.auth import PermissionDenied
from . import account, insights, theme
from . import components as ui
from .charts import PLOTLY_CONFIG


def admin_page() -> None:
    account.sidebar_status()
    ui.masthead(meta=[])
    admin = account.current_user()
    try:
        overview = service.admin_overview(admin)
    except PermissionDenied:
        ui.note("This workspace is for the administrator account only.", status="critical", label="No access.")
        return

    st.markdown(
        "<div class='ds-eyebrow'>Admin console</div><div class='ds-headline'>Research workspace</div>"
        "<div class='ds-subhead'>Private - individual reports, moderation, exports, analysis and accounts.</div>",
        unsafe_allow_html=True,
    )

    def evidence() -> None:
        st.caption("Official-source records imported by lookups, kept apart from user submissions.")
        st.dataframe(service.admin_evidence(admin), hide_index=True, width="stretch")

    def log() -> None:
        st.caption("Every administrative change and export, newest first.")
        st.dataframe(service.audit_log(admin), hide_index=True, width="stretch")

    sections = [
        ("Overview", lambda: _overview(overview)),
        ("Insights (EDA)", lambda: _eda(admin)),
        ("Moderation", lambda: _moderation(admin)),
        ("Comments", lambda: _comments(admin)),
        ("Categories", lambda: _categories(admin)),
        ("Data & export", lambda: _data(admin)),
        ("Hypotheses", lambda: _hypotheses(admin)),
        ("Users & plans", lambda: _users(admin)),
        ("Evidence", evidence),
        ("Audit log", log),
    ]
    # Lazy tabs: only the open one queries the database and draws.
    tabs = st.tabs([name for name, _ in sections], key="admin_tab", on_change="rerun")
    for tab, (_, render) in zip(tabs, sections):
        if tab.open:
            with tab:
                render()


def _eda(admin) -> None:
    """Exploratory analysis over every report, not just approved ones."""
    meds = service.admin_medications(admin)
    chosen = st.selectbox("Medication", [None] + [m["id"] for m in meds], key="eda-med",
                          format_func=lambda i: "All medications" if i is None
                          else next(m["name"] for m in meds if m["id"] == i))
    data = service.insights(admin, scope="admin", medication_id=chosen)
    if not data["reports"] and not data["votes"]["total"]:
        st.caption("No reports or votes yet.")
        return
    status = data["by_status"]
    ui.kpi_row([
        {"label": "Approved", "value": status.get("approved", 0), "status": "good", "sub": "visible publicly"},
        {"label": "Pending", "value": status.get("pending", 0), "status": "warning", "sub": "awaiting review"},
        {"label": "Flagged", "value": status.get("flagged", 0), "status": "serious", "sub": "need a closer look"},
        {"label": "Rejected", "value": status.get("rejected", 0), "slot": 6, "sub": "not published"},
    ])
    insights.render(data, key="adm", admin=True)
    left, right = st.columns(2, gap="large")
    with left:
        ui.section("Accounts by plan")
        st.dataframe(data["plan_mix"], hide_index=True, width="stretch")
    with right:
        ui.section("Research credits used", "last 30 days")
        st.dataframe(data["credits_by_day"], hide_index=True, width="stretch")


def _overview(o: dict) -> None:
    ui.kpi_row([
        {"label": "Reports received", "value": o["reports"], "slot": 0,
         "sub": f"{o['by_status'].get('approved', 0)} approved · {o['by_status'].get('flagged', 0)} flagged"},
        {"label": "Awaiting moderation", "value": o["by_status"].get("pending", 0),
         "status": "warning" if o["by_status"].get("pending") else "good", "sub": "pending reports"},
        {"label": "Category suggestions", "value": o["pending_suggestions"],
         "status": "warning" if o["pending_suggestions"] else "good", "sub": "pending review"},
        {"label": "Community votes", "value": o["yes_votes"] + o["no_votes"], "slot": 2,
         "sub": f"{o['yes_votes']} experienced · {o['no_votes']} not"},
    ])
    ui.kpi_row([
        {"label": "Medications", "value": o["medications"], "slot": 1, "sub": "unique, in reports"},
        {"label": "Symptoms", "value": o["symptoms"], "slot": 3, "sub": "unique, in reports"},
        {"label": "Foods", "value": o["foods"], "slot": 4, "sub": "unique, in reports"},
        {"label": "Interaction pairs", "value": o["pairs"] + o["drug_pairs"], "slot": 5,
         "sub": f"{o['pairs']} food-drug · {o['drug_pairs']} drug-drug · {o['users']} users"},
    ])
    period = f"{(o['first_report'] or '')[:10]} to {(o['last_report'] or '')[:10]}" if o["first_report"] else "no reports yet"
    ui.note(f"Reporting period: {period}. Sample: {o['reports']} self-selected reports. Raw counts are "
            "reporting volume, not incidence, and do not establish causation.", status="warning",
            label="Read counts carefully.")

    ui.section("Reports over time", "last 90 days")
    if o["trend"]:
        t = theme.tokens()
        figure = go.Figure(go.Bar(
            x=[r["day"] for r in o["trend"]], y=[r["reports"] for r in o["trend"]],
            marker={"color": t["series"][0], "cornerradius": 4},
            hovertemplate="<b>%{x}</b><br>%{y} report(s)<extra></extra>",
        ))
        figure.update_layout(**theme.plotly_layout(240))
        st.plotly_chart(figure, width="stretch", config=PLOTLY_CONFIG, key="adm-trend")
    else:
        st.caption("No reports yet.")

    left, right = st.columns(2, gap="large")
    with left:
        ui.section("Frequent drug → symptom pairs")
        st.dataframe(o["top_drug_symptom"], hide_index=True, width="stretch")
    with right:
        ui.section("Frequent food + drug pairs")
        st.dataframe(o["top_food_drug"], hide_index=True, width="stretch")
    ui.section("Frequent drug + drug pairs")
    st.dataframe(o["top_drug_drug"], hide_index=True, width="stretch")


def _moderation(admin) -> None:
    status = st.segmented_control("Show", ["pending", "flagged", "approved", "rejected"],
                                  default="pending", key="mod-status") or "pending"
    reports = service.admin_reports(admin, status=status, limit=50)
    st.caption(f"{len(reports)} {status} report(s) (newest 50).")
    for report in reports:
        title = report["medication"] + (f" + {report['other_medication']}" if report["other_medication"] else "") \
            + (f" + {report['food']}" if report["food"] else "")
        with st.container(border=True):
            st.markdown(
                f"**#{report['id']} · {ui.esc(title)}** → {ui.esc(report['symptoms'] or '')}  \n"
                f"<span style='font-size:.76rem;color:var(--ds-ink-3)'>{report['created_at']} · "
                f"{report['report_type'].replace('_', ' ')} · {report['severity']} · "
                f"{service.ONSET_CHOICES.get(report['onset'], report['onset'])} · {report['frequency']} · "
                f"entered as “{ui.esc(report['medication_as_entered'])}”"
                f"{'' if report['medication_verified'] else ' (unverified medication)'} · "
                f"user #{report['user_id']}</span>",
                unsafe_allow_html=True,
            )
            if report["context"]:
                st.markdown(f"<div class='ds-note' style='margin:.3rem 0'>{ui.esc(report['context'])}</div>",
                            unsafe_allow_html=True)
            columns = st.columns(4)
            for column, (action, label) in zip(columns, [("approved", "Approve"), ("rejected", "Reject"),
                                                          ("flagged", "Flag"), ("delete", "Delete")]):
                if action == status:
                    continue
                if column.button(label, key=f"mod-{action}-{report['id']}", width="stretch",
                                  type="primary" if action == "approved" else "secondary"):
                    if action == "delete":
                        service.admin_delete_report(admin, report["id"])
                    else:
                        service.moderate_report(admin, report["id"], action)
                    st.rerun()


def _comments(admin) -> None:
    """Public comments: reported or hidden ones first."""
    flagged_only = st.toggle("Only reported or hidden comments", value=True, key="cm-flagged")
    rows = service.admin_comments(admin, flagged_only=flagged_only, limit=100)
    st.caption(f"{len(rows)} comment(s). A comment is hidden automatically after "
               f"{service.AUTO_HIDE_FLAGS} reports; showing it again clears its reports.")
    for row in rows:
        with st.container(border=True):
            tone = "critical" if row["status"] == "hidden" else "warning" if row["flags"] else "good"
            reply = " · reply" if row["is_reply"] else ""
            st.markdown(
                f"{ui.badge(row['status'].title(), status=tone)} "
                f"{ui.badge(str(row['flags']) + ' report(s)', dot=False)} {ui.badge(str(row['likes']) + ' like(s)', dot=False)}"
                f"<div style='font-size:.76rem;color:var(--ds-ink-3);margin:.3rem 0'>#{row['id']} · "
                f"{ui.esc(row['username'])} (user #{row['user_id']}) on <b>{ui.esc(row['entry'])}</b> · "
                f"{row['created_at'][:16].replace('T', ' ')}{reply}</div>"
                f"<div style='font-size:.86rem;white-space:pre-wrap'>{ui.esc(row['body'])}</div>"
                + (f"<div style='font-size:.74rem;color:var(--ds-ink-3);margin-top:.3rem'>Reasons: "
                   f"{ui.esc(row['reasons'])}</div>" if row["reasons"] else ""),
                unsafe_allow_html=True,
            )
            a, b, c, _ = st.columns([1, 1, 1, 3])
            if row["status"] == "hidden":
                if a.button("Show", key=f"cm-show-{row['id']}", width="stretch"):
                    service.moderate_comment(admin, row["id"], "visible")
                    st.rerun()
            elif b.button("Hide", key=f"cm-hide-{row['id']}", width="stretch"):
                service.moderate_comment(admin, row["id"], "hidden")
                st.rerun()
            if c.button("Delete", key=f"cm-del-{row['id']}", width="stretch"):
                service.moderate_comment(admin, row["id"], "delete")
                st.rerun()


def _categories(admin) -> None:
    pending = service.admin_suggestions(admin, "pending")
    st.caption(f"{len(pending)} suggestion(s) awaiting review. The submitted wording is kept for the audit "
               "trail; the approved name is what the forms and analysis use.")
    symptoms, foods = service.symptoms(), service.foods()
    for suggestion in pending:
        existing = symptoms if suggestion["kind"] == "symptom" else foods
        similar = service.similar_categories(suggestion["kind"], suggestion["submitted_name"])
        with st.container(border=True):
            st.markdown(f"**{ui.esc(suggestion['submitted_name'])}** · {suggestion['kind']} · "
                        f"{suggestion['created_at'][:10]}")
            if suggestion["description"]:
                st.caption(suggestion["description"])
            if similar:
                st.caption("Similar: " + ", ".join(s["name"] for s in similar if s["name"] != suggestion["submitted_name"]))
            key = f"sg-{suggestion['id']}"
            final = st.text_input("Approved name", value=suggestion["submitted_name"].strip().capitalize(), key=f"{key}-n")
            merge = st.selectbox("Or merge into", [None] + [e["id"] for e in existing], key=f"{key}-m",
                                 format_func=lambda i: "-" if i is None else next(e["name"] for e in existing if e["id"] == i))
            a, b, c = st.columns(3)
            try:
                if a.button("Approve", key=f"{key}-a", type="primary", width="stretch"):
                    service.resolve_suggestion(admin, suggestion["id"], "approve", final_name=final)
                    st.rerun()
                if b.button("Merge", key=f"{key}-mg", width="stretch", disabled=merge is None):
                    service.resolve_suggestion(admin, suggestion["id"], "merge", merge_into_id=merge)
                    st.rerun()
                if c.button("Reject", key=f"{key}-r", width="stretch"):
                    service.resolve_suggestion(admin, suggestion["id"], "reject")
                    st.rerun()
            except service.ValidationError as exc:
                st.error(str(exc))

    with st.expander("Remove a category from the forms"):
        kind = st.radio("Type", ["symptom", "food"], horizontal=True, key="retire-kind")
        rows = symptoms if kind == "symptom" else foods
        target = st.selectbox("Category", [r["id"] for r in rows], key="retire-id",
                              format_func=lambda i: next(r["name"] for r in rows if r["id"] == i))
        if st.button("Remove from forms", key="retire-go"):
            service.retire_category(admin, kind, target)
            st.rerun()
        st.caption("Past reports keep their category; it just stops being offered.")

    with st.expander("Decided suggestions"):
        st.dataframe([s for s in service.admin_suggestions(admin, "") if s["status"] != "pending"],
                     hide_index=True, width="stretch")


def _data(admin) -> None:
    c1, c2, c3 = st.columns(3)
    medication = c1.text_input("Medication", key="dx-med")
    symptom = c2.text_input("Symptom", key="dx-sym")
    food = c3.text_input("Food", key="dx-food")
    c4, c5, c6, c7 = st.columns(4)
    status = c4.selectbox("Status", ["", *service.REPORT_STATUSES], key="dx-status",
                          format_func=lambda s: s or "any")
    report_type = c5.selectbox("Type", ["", "side_effect", "interaction"], key="dx-type",
                               format_func=lambda s: s.replace("_", " ") if s else "any")
    date_from = c6.date_input("From", value=None, key="dx-from")
    date_to = c7.date_input("To", value=None, key="dx-to")
    filters = {
        "medication": medication, "symptom": symptom, "food": food, "status": status,
        "report_type": report_type,
        "date_from": date_from.isoformat() if isinstance(date_from, date) else "",
        "date_to": date_to.isoformat() if isinstance(date_to, date) else "",
    }
    rows = service.admin_reports(admin, **filters)
    st.caption(f"{len(rows)} report(s) match (up to 500 shown).")
    st.dataframe(rows, hide_index=True, width="stretch")

    ui.section("CSV export", "audit-logged; report filters above apply to the reports dataset")
    dataset = st.selectbox("Dataset", list(service.EXPORT_DATASETS), format_func=service.EXPORT_DATASETS.get,
                           key="dx-dataset")
    if st.button("Prepare export", key="dx-prepare"):
        st.session_state["dx-csv"] = (dataset, service.export_csv(
            admin, dataset, **(filters if dataset == "reports" else {})))
    prepared = st.session_state.get("dx-csv")
    if prepared:
        name, text = prepared
        st.download_button(f"Download {name}.csv", text, file_name=f"drugscope-{name}-{date.today()}.csv",
                           mime="text/csv", type="primary")


def _hypotheses(admin) -> None:
    rows = service.admin_hypotheses(admin)
    meds = service.admin_medications(admin)
    foods, symptoms = service.foods(), service.symptoms()
    options = {None: "New hypothesis", **{r["id"]: f"#{r['id']} {r['title']}" for r in rows}}
    chosen = st.selectbox("Edit", list(options), format_func=options.get, key="hy-pick")
    current = next((r for r in rows if r["id"] == chosen), {})

    def pick(label, items, field, key):
        ids = [None] + [i["id"] for i in items]
        return st.selectbox(label, ids, index=ids.index(current.get(field)) if current.get(field) in ids else 0,
                            key=f"{key}-{chosen}",
                            format_func=lambda i: "-" if i is None else next(x["name"] for x in items if x["id"] == i))

    with st.form(f"hy-form-{chosen}"):
        title = st.text_input("Title", value=current.get("title", ""))
        description = st.text_area("Description and current evidence", value=current.get("description", ""), height=110)
        a, b, c, d = st.columns(4)
        with a:
            medication_id = pick("Medication", meds, "medication_id", "hy-med")
        with b:
            other_medication_id = pick("Second medication", meds, "other_medication_id", "hy-med2")
        with c:
            food_id = pick("Food", foods, "food_id", "hy-food")
        with d:
            symptom_id = pick("Symptom", symptoms, "symptom_id", "hy-sym")
        statuses = ["proposed", "investigating", "supported", "not_supported"]
        status = st.selectbox("Status", statuses, index=statuses.index(current.get("status", "proposed")),
                              format_func=lambda s: s.replace("_", " "))
        urls = st.text_area("Evidence URLs (one per line)", value=current.get("evidence_urls", ""), height=70)
        public = st.checkbox("Publish to community pages (shown labelled as an unverified hypothesis)",
                             value=bool(current.get("is_public")))
        if st.form_submit_button("Save hypothesis", type="primary"):
            try:
                service.save_hypothesis(admin, hypothesis_id=chosen, title=title, description=description,
                                        medication_id=medication_id, food_id=food_id, symptom_id=symptom_id,
                                        status=status, is_public=public, evidence_urls=urls,
                                        other_medication_id=other_medication_id)
                st.success("Saved.")
                st.rerun()
            except service.ValidationError as exc:
                st.error(str(exc))
    st.dataframe(rows, hide_index=True, width="stretch")


def _users(admin) -> None:
    users = service.admin_users(admin)
    st.dataframe(users, hide_index=True, width="stretch")
    regular = [u for u in users if u["role"] == "user"]

    left, right = st.columns(2, gap="large")
    with left:
        ui.section("Change a plan")
        if regular:
            target = st.selectbox("User", [u["id"] for u in regular], key="pl-user",
                                  format_func=lambda i: next(u["username"] for u in regular if u["id"] == i))
            plans = quota.plans()
            plan = st.selectbox("Plan", list(plans), key="pl-plan", format_func=lambda k: plans[k].label)
            days = st.number_input("For how many days (0 = no expiry)", 0, 3660, 30, key="pl-days",
                                   disabled=plan == "free")
            a, b = st.columns(2)
            if a.button("Apply plan", key="pl-apply", type="primary", width="stretch"):
                quota.set_plan(admin, target, plan=plan, days=(int(days) or None) if plan != "free" else None)
                st.session_state["toast"] = f"Plan set to {plans[plan].label}."
                st.rerun()
            active = next(u["is_active"] for u in regular if u["id"] == target)
            if b.button("Deactivate" if active else "Reactivate", key="pl-active", width="stretch"):
                service.set_user_active(admin, target, not active)
                st.rerun()
        else:
            st.caption("No user accounts yet.")
    with right:
        ui.section("Issue upgrade codes", "give one to each paying customer")
        with st.form("codes"):
            plans = quota.plans()
            code_plan = st.selectbox("Plan", list(quota.PAID_PLANS), format_func=lambda k: plans[k].label)
            count = st.number_input("How many", 1, 100, 1)
            days = st.number_input("Days per code", 1, 3660, 30)
            note = st.text_input("Note (e.g. order or invoice number)")
            if st.form_submit_button("Generate", type="primary"):
                st.session_state["fresh-codes"] = quota.generate_codes(
                    admin, count=int(count), days=int(days), note=note, plan=code_plan)
        if st.session_state.get("fresh-codes"):
            st.warning("Copy these now - they are stored only as hashes and cannot be shown again.")
            st.code("\n".join(st.session_state["fresh-codes"]), language=None)
