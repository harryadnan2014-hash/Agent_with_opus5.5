"""Sign-in, the account page, plans and upgrades.

The session only remembers *which* account is signed in. Everything else - role,
plan, whether the account is still active - is re-read from the database on every
page load, so a change an administrator makes takes effect on the next click.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from ..community import auth, quota, service
from ..community.auth import AuthError, Principal
from . import components as ui
from . import nav


def google_enabled() -> bool:
    """True when `.streamlit/secrets.toml` has an [auth.google] section."""
    try:
        return "google" in st.secrets.get("auth", {})
    except Exception:  # noqa: BLE001 - no secrets file at all
        return False


def _google_principal() -> Principal | None:
    """The DrugScope account behind a Google sign-in, if the visitor has one."""
    if not google_enabled():
        return None
    try:
        if not st.user.is_logged_in:
            return None
        return auth.login_external("google", str(st.user.get("sub") or ""))
    except AuthError as exc:
        st.error(str(exc))
        return None


COOKIE = "ds_session"


def _cookie_token() -> str | None:
    try:
        value = st.context.cookies.get(COOKIE)
    except Exception:  # noqa: BLE001 - no request context (tests, scripts)
        return None
    return value if isinstance(value, str) and value else None


def current_user() -> Principal | None:
    user_id = st.session_state.get("user_id")
    principal = auth.load_principal(user_id)
    if user_id and principal is None:
        st.session_state.pop("user_id", None)  # deleted or deactivated
    if principal is None and not st.session_state.get("signed_out"):
        # A refreshed page is a new session: restore it from the remembered sign-in.
        token = _cookie_token()
        principal = auth.principal_from_session(token)
        if principal is not None:
            st.session_state.session_token = token
        else:
            # Google's own sign-in cookie lasts 30 days as well.
            principal = _google_principal()
        if principal is not None:
            st.session_state.user_id = principal.id
    return principal


def apply_cookie_changes() -> None:
    """Write or clear the remembered-sign-in cookie in the browser.

    Streamlit can read cookies but not set them, so a tiny script does it from the
    page. It runs on the rerun after signing in or out.
    """
    token = st.session_state.pop("cookie_set", None)
    clear = st.session_state.pop("cookie_clear", False)
    if not token and not clear:
        return
    value, age = (token, auth.SESSION_DAYS * 86400) if token else ("", 0)
    script = (
        "<script>(function(){var c='" + COOKIE + "=" + value + "; Max-Age=" + str(age)
        + "; Path=/; SameSite=Lax';try{window.parent.document.cookie=c;}catch(e){document.cookie=c;}})();</script>"
    )
    import streamlit.components.v1 as components

    with st.sidebar:
        components.html(script, height=0)


def sign_out() -> None:
    auth.end_session(st.session_state.pop("session_token", None) or _cookie_token())
    st.session_state.pop("user_id", None)
    st.session_state["cookie_clear"] = True
    st.session_state["signed_out"] = True
    if google_enabled():
        try:
            if st.user.is_logged_in:
                st.logout()  # clears Google's cookie and reruns
        except Exception:  # noqa: BLE001
            pass
    st.rerun()


def _sign_in(principal: Principal) -> None:
    token = auth.create_session(principal)
    st.session_state.user_id = principal.id
    st.session_state.session_token = token
    st.session_state["cookie_set"] = token
    st.session_state.pop("signed_out", None)
    st.rerun()


def sign_in_panel(key: str = "auth") -> None:
    """Sign in or create an account. Usable inline on any page."""
    if google_enabled():
        if st.button("Continue with Google", key=f"{key}-google", type="primary", width="stretch",
                     icon=":material/account_circle:"):
            st.login("google")
        st.caption("Only Google's account id is kept - not your email or name. Or use a username below.")
    sign_in, create = st.tabs(["Sign in", "Create account"])
    with sign_in:
        with st.form(f"{key}-signin"):
            username = st.text_input("Username", key=f"{key}-u")
            password = st.text_input("Password", type="password", key=f"{key}-p")
            if st.form_submit_button("Sign in", type="primary", width="stretch"):
                try:
                    _sign_in(auth.authenticate(username, password))
                except AuthError as exc:
                    st.error(str(exc))
    with create:
        with st.form(f"{key}-signup"):
            username = st.text_input("Choose a username", key=f"{key}-nu",
                                     help="Do not use your real name - a nickname is fine.")
            password = st.text_input("Choose a password", type="password", key=f"{key}-np",
                                     help=f"At least {auth.MIN_PASSWORD_LENGTH} characters.")
            adult = st.checkbox("I am 18 or older", key=f"{key}-adult")
            agreed = st.checkbox(
                "I understand DrugScope is a research tool, not medical advice, and that "
                "reports I submit are stored for research as described in the privacy notice.",
                key=f"{key}-agree",
            )
            if st.form_submit_button("Create account", type="primary", width="stretch"):
                if not agreed:
                    st.error("Please accept the research and privacy statement.")
                else:
                    try:
                        _sign_in(auth.register(username, password, confirmed_adult=adult))
                    except AuthError as exc:
                        st.error(str(exc))
    st.caption("You stay signed in on this browser for 30 days unless you sign out. "
               "No email, phone number or real name is collected. "
               f"New accounts get {quota.plans()['free'].credits} research credits every "
               f"{quota.window_hours()} hours.")


def require_sign_in(message: str) -> Principal | None:
    """The signed-in user, or a sign-in prompt and None."""
    user = current_user()
    if user is None:
        ui.note(message, status="warning", label="Sign in required.")
        sign_in_panel(key=f"gate-{message[:12]}")
    return user


# --------------------------------------------------------------------------- #
# Credits
# --------------------------------------------------------------------------- #

def credits_meter(status: quota.QuotaStatus) -> None:
    if status.unlimited:
        st.markdown(ui.badge("Administrator - research is never charged", status="good"),
                    unsafe_allow_html=True)
        st.caption("To see credits being used, sign in with a regular test account.")
        return
    share = status.used / status.limit if status.limit else 1.0
    tone = "good" if share < 0.6 else "warning" if share < 1 else "critical"
    reset = f" · next credit back in {status.resets_in_text()}" if status.resets_at else ""
    st.markdown(
        ui.badge(f"{status.plan_label} plan", status=tone)
        + f"<div style='font-size:.78rem;color:var(--ds-ink-2);margin-top:.35rem'>"
          f"<b style='color:var(--ds-ink)'>{status.remaining}</b> of {status.limit} research credits left "
          f"(rolling {status.window_hours}h){ui.esc(reset)}</div>"
          f"<div class='ds-meter' style='margin-top:.35rem'><span style='width:{min(share, 1) * 100:.0f}%;"
          f"background:var(--ds-{tone})'></span></div>",
        unsafe_allow_html=True,
    )


def credit_line(cost: int, depth: str) -> None:
    """One line under the search box: what this run costs and what is left."""
    user = current_user()
    if user is None:
        body = (f"A {depth} run costs <b>{cost}</b> credit{'s' if cost != 1 else ''}. "
                f"Sign in to get <b>{quota.plans()['free'].credits}</b> free credits every "
                f"{quota.window_hours()} hours.")
    else:
        status = quota.status(user)
        if status.unlimited:
            body = f"A {depth} run costs <b>{cost}</b> credit{'s' if cost != 1 else ''} · administrator: never charged."
        else:
            reset = f" · next credit back in {status.resets_in_text()}" if status.resets_at else ""
            body = (f"A {depth} run costs <b>{cost}</b> credit{'s' if cost != 1 else ''} · you have "
                    f"<b>{status.remaining}</b> of {status.limit} left on {ui.esc(status.plan_label)}{ui.esc(reset)}")
    st.markdown(f"<div class='ds-credit-line'>{body}</div>", unsafe_allow_html=True)


def upgrade_prompt(status: quota.QuotaStatus | None, *, key: str = "upgrade") -> None:
    """Shown where credits ran out: the way forward, without leaving the page."""
    left, right = st.columns([1, 1], gap="medium")
    with left:
        if st.button("See plans & upgrade", type="primary", width="stretch", key=f"{key}-plans"):
            st.switch_page(nav.PAGES["plans"])
    with right:
        redeem_form(key)


def redeem_form(key: str) -> None:
    with st.form(f"{key}-redeem"):
        code = st.text_input("Upgrade code", placeholder="DS-XXXX-XXXX-XXXX")
        if st.form_submit_button("Redeem code", width="stretch"):
            user = current_user()
            if user is None:
                st.error("Sign in first.")
            else:
                try:
                    expires = quota.redeem(user, code)
                    st.session_state["toast"] = f"Upgrade active until {expires[:10]}."
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #

def workspace_card() -> None:
    """The foot of the sidebar on every page: who is signed in, on which plan."""
    user = current_user()
    with st.sidebar:
        if user is None:
            who, plan = "Guest", "Sign in"
        else:
            who = user.username
            plan = "Admin" if user.is_admin else quota.status(user).plan_label
        st.markdown(
            f"<div class='ds-workspace'><span class='dot'></span><span class='who'>{ui.esc(who)}</span>"
            f"<span class='plan'>{ui.esc(plan)}</span></div>"
            "<div class='ds-motto'>Evidence over noise.<span>Smarter research. Better treatments.</span></div>",
            unsafe_allow_html=True,
        )


def sidebar_status() -> None:
    """Credits (or a sign-in link) for pages without their own sidebar controls."""
    with st.sidebar:
        user = current_user()
        if user:
            credits_meter(quota.status(user))
        else:
            st.caption("Not signed in.")
            nav.link("account", "Sign in or create an account", ":material/login:")
    workspace_card()


# --------------------------------------------------------------------------- #
# Plans & access
# --------------------------------------------------------------------------- #

def _plan_features(plan: quota.Plan) -> list[str]:
    hours = quota.window_hours()
    deep, standard = quota.DEPTH_COST["Deep"], quota.DEPTH_COST["Standard"]
    if plan.key == "free":
        return [
            f"{plan.credits} research credits every {hours}h",
            "Scan 1 · Standard 2 · Deep 4 credits per run",
            "Community reports, voting and insights",
            "Food ↔ drug and drug ↔ drug checks",
            "Export every report (Markdown, JSON, CSV)",
        ]
    previous = "Free" if plan.key == "pro" else "Pro"
    return [
        f"{plan.credits} research credits every {hours}h",
        f"About {plan.credits // standard} Standard or {plan.credits // deep} Deep runs per {hours}h",
        f"Everything in {previous}",
    ]


def plans_page() -> None:
    sidebar_status()
    user = current_user()
    status = quota.status(user) if user else None
    plans = quota.plans()

    st.markdown(
        "<div class='ds-eyebrow'>Plans &amp; access</div>"
        "<div class='ds-headline'>Choose your research plan</div>"
        "<div class='ds-subhead'>Start free. Upgrade when evidence becomes mission-critical.</div>",
        unsafe_allow_html=True,
    )
    if not quota.checkout_connected():
        st.info("Checkout is not connected yet - no payment is taken in the app. Paid plans are "
                "activated with an upgrade code from the administrator.")

    columns = st.columns(3, gap="medium")
    for column, plan in zip(columns, plans.values()):
        current = status is not None and status.plan == plan.key
        featured = plan.key == "team"
        items = "".join(f"<li>{ui.esc(f)}</li>" for f in _plan_features(plan))
        with column:
            st.markdown(
                f"<div class='ds-plan{' is-featured' if featured else ''}'>"
                f"<div class='ds-plan-name'>{ui.esc(plan.label)}</div>"
                f"<div class='ds-plan-price'>{ui.esc(plan.price)}<span> / month</span></div>"
                f"<div class='ds-plan-blurb'>{ui.esc(plan.blurb)}</div><ul>{items}</ul>"
                + ("<div class='ds-plan-current'>● Your current plan</div>" if current else "")
                + "</div>",
                unsafe_allow_html=True,
            )
            if plan.key == "free" or current:
                continue
            if plan.upgrade_url:
                st.link_button(f"Upgrade to {plan.label}", plan.upgrade_url, type="primary", width="stretch")
            else:
                st.button(f"Upgrade to {plan.label}", disabled=True, width="stretch", key=f"up-{plan.key}",
                          help="Checkout is not connected - ask the administrator for an upgrade code.")

    ui.section("Your usage", "credits return one run at a time as runs leave the window")
    if user is None:
        st.caption("Sign in to see your credits.")
        nav.link("account", "Sign in or create an account", ":material/login:")
    else:
        left, right = st.columns([1.1, 1], gap="large")
        with left:
            credits_meter(status)
            if status.plan_expires_at:
                st.caption(f"{status.plan_label} until {status.plan_expires_at[:10]}.")
            history = quota.usage_history(user)
            if history:
                st.dataframe(
                    [{"When": h["created_at"].replace("T", " ")[:16], "Credits": h["credits"],
                      "Run": h["detail"]} for h in history],
                    hide_index=True, width="stretch",
                )
            else:
                st.caption("No research runs yet.")
        with right:
            st.markdown("**Have an upgrade code?**")
            redeem_form("plans")
    st.caption("Community features - reports, voting, insights, food and drug checks - never use credits.")


# --------------------------------------------------------------------------- #
# Account
# --------------------------------------------------------------------------- #

PRIVACY_NOTICE = """
**What is stored.** A username and a hashed password. Reports store the medication(s),
symptoms, optional food, timing, severity and frequency, and optional free-text
context. Emails, phone numbers and links typed into free text are removed
automatically before saving.

**Who can see it.** Individual reports and votes are visible only to the
administrator. Everyone else sees approved, aggregated counts - never an individual
report or its free text.

**Retention.** Free-text context is cleared automatically after the retention
period set by the operator. You can delete any of your reports, or your whole
account, at any time from this page.

**Before collecting real users' health information**, the operator must review the
privacy and health-data rules that apply where the service runs (in the UAE this
includes the federal personal-data and health-data laws), and must not collect data
from children.
"""


def account_page() -> None:
    sidebar_status()
    ui.masthead(meta=[])
    user = current_user()

    if user is None:
        ui.section("Your account", "sign in to run research, report experiences and vote")
        left, right = st.columns([1.2, 1], gap="large")
        with left:
            sign_in_panel("account")
        with right:
            with st.expander("Privacy notice", expanded=True):
                st.markdown(PRIVACY_NOTICE)
        return

    status = quota.status(user)
    ui.section(f"Signed in as {user.username}",
               "administrator" if user.is_admin else f"{status.plan_label} plan")
    left, right = st.columns([1.3, 1], gap="large")
    with left:
        ui.section("Plan and credits")
        credits_meter(status)
        nav.link("plans", "Plans, usage history and upgrade codes", ":material/workspace_premium:")

        ui.section("My reports", "only you and the administrator can see these")
        reports = service.my_reports(user)
        if not reports:
            st.caption("You have not submitted any reports yet.")
            nav.link("report", "Report a side effect", ":material/add_circle:")
        for report in reports:
            title = report["medication"] + (f" + {report['other_medication']}" if report["other_medication"] else "") \
                + (f" + {report['food']}" if report["food"] else "")
            with st.container(border=True):
                st.markdown(
                    f"**{ui.esc(title)}** → {ui.esc(report['symptoms'] or '')}  \n"
                    f"<span style='font-size:.75rem;color:var(--ds-ink-3)'>"
                    f"{report['created_at'][:10]} · {report['severity']} · "
                    f"{service.ONSET_CHOICES.get(report['onset'], report['onset'])}</span>",
                    unsafe_allow_html=True,
                )
                st.markdown(ui.badge(report["status"].title(),
                                     status={"approved": "good", "pending": "warning"}.get(report["status"], "serious")),
                            unsafe_allow_html=True)
                if st.button("Delete this report", key=f"del-{report['id']}"):
                    service.delete_my_report(user, report["id"])
                    st.rerun()

    with right:
        external = auth.is_external(user)
        with st.expander("Privacy notice", expanded=False):
            st.markdown(PRIVACY_NOTICE)
        if not user.is_admin:
            with st.expander("Change username", expanded=user.username.startswith("member-")):
                st.caption("Your username is shown on your public comments.")
                with st.form("change-username"):
                    new_name = st.text_input("New username", value=user.username)
                    if st.form_submit_button("Save username"):
                        try:
                            auth.change_username(user, new_name)
                            st.rerun()
                        except AuthError as exc:
                            st.error(str(exc))
        if external:
            st.caption("You sign in with Google, so there is no DrugScope password.")
        else:
            with st.expander("Change password"):
                with st.form("change-password"):
                    current = st.text_input("Current password", type="password")
                    new = st.text_input("New password", type="password")
                    if st.form_submit_button("Change password"):
                        try:
                            auth.change_password(user, current, new)
                            st.success("Password changed.")
                        except AuthError as exc:
                            st.error(str(exc))
        if not user.is_admin:
            with st.expander("Delete my account"):
                st.caption("Deletes your account, every report, vote, comment and project you made, and your "
                           "usage history. This cannot be undone.")
                with st.form("delete-account"):
                    password = st.text_input("Type your username to confirm" if external else "Password",
                                             type="default" if external else "password")
                    sure = st.checkbox("I understand this cannot be undone")
                    if st.form_submit_button("Delete everything", type="primary"):
                        if not sure:
                            st.error("Tick the confirmation first.")
                        else:
                            try:
                                auth.delete_account(user, password)
                                sign_out()
                            except AuthError as exc:
                                st.error(str(exc))
        if st.button("Sign out", width="stretch"):
            sign_out()


# --------------------------------------------------------------------------- #
# Voting
# --------------------------------------------------------------------------- #

def entry_vote_buttons(entry: dict[str, Any], user: Principal | None, key: str) -> None:
    """The two community buttons for one aggregated entry, plus the response split."""
    yes, no = st.columns(2)
    mine = entry.get("my_vote")
    yes_label = f"✔ I've experienced this too ({entry['yes_votes']})"
    no_label = f"I haven't experienced this ({entry['no_votes']})"
    disabled = user is None
    with yes:
        if st.button(yes_label, key=f"{key}-y-{entry['id']}", width="stretch", disabled=disabled,
                     type="primary" if mine == 1 else "secondary"):
            _vote(user, entry["id"], 1, mine)
    with no:
        if st.button(no_label, key=f"{key}-n-{entry['id']}", width="stretch", disabled=disabled,
                     type="primary" if mine == -1 else "secondary"):
            _vote(user, entry["id"], -1, mine)
    responses = entry["yes_votes"] + entry["no_votes"]
    if responses:
        share = 100 * entry["yes_votes"] / responses
        st.caption(f"{responses} people responded · {share:.0f}% said they experienced it too "
                   "(self-selected respondents, not a measure of how common it is)")


def _vote(user: Principal | None, entry_id: int, value: int, mine: int | None) -> None:
    try:
        if mine == value:
            service.remove_vote(user, entry_id)  # clicking your own vote again removes it
        else:
            service.cast_vote(user, entry_id, value)
        st.rerun()
    except (service.ValidationError, service.RateLimited, auth.PermissionDenied) as exc:
        st.error(str(exc))
