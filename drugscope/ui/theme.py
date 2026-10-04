"""Design tokens, the stylesheet, and the motion system.

This is the whole design system in one file: swap the values and the app is
rebranded without touching a component or a chart.

The palette is a modern, saturated set tuned per mode - the dark column is stepped
for the dark surface rather than being an inverted copy of the light one, so neither
mode looks like a washed-out version of the other. Two rules the components still
follow, for legibility rather than compliance:

* **Direct value labels on bars, plus a table view.** Reading a value off a bar is
  guesswork; `charts.chart_with_table` gives every chart both.
* **Status colours are reserved** for state (good / warning / serious / critical) and
  never double as a series colour, so a red bar never means "series 8".

Surfaces are layered into four elevations - page, surface, raised, inset - so cards
read as objects with depth rather than as outlined rectangles.
"""

from __future__ import annotations

from typing import Any, Literal

import streamlit as st

ThemeName = Literal["light", "dark"]

# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #

PALETTES: dict[ThemeName, dict[str, Any]] = {
    "light": {
        "page": "#f5f7fc",
        "surface": "#ffffff",
        "surface_raised": "#ffffff",
        "surface_sunken": "#eef2fa",
        "surface_inset": "#e4eaf6",
        "border": "#dce4f2",
        "border_strong": "#c2cee6",
        "text_primary": "#0a1226",
        "text_secondary": "#47557a",
        "text_muted": "#6e7ea4",
        "grid": "#e9eef8",
        "accent": "#2563eb",
        "accent_alt": "#7c3aed",
        "shadow": "0 1px 2px rgba(10,18,38,.05), 0 10px 28px -14px rgba(10,18,38,.14)",
        "shadow_lift": "0 2px 6px rgba(10,18,38,.07), 0 22px 46px -18px rgba(10,18,38,.22)",
        "glow": "0 0 0 1px rgba(37,99,235,.20), 0 10px 32px -10px rgba(37,99,235,.35)",
        "series": ["#2563eb", "#7c3aed", "#059669", "#d97706", "#db2777", "#0891b2", "#dc2626", "#65a30d"],
        "low_contrast_series": [],
        "sequential": ["#dbeafe", "#bfdbfe", "#93c5fd", "#60a5fa", "#3b82f6", "#2563eb", "#1d4ed8", "#1e3a8a"],
        "ordinal": ["#93c5fd", "#60a5fa", "#3b82f6", "#2563eb", "#1d4ed8", "#1e40af", "#1e3a8a"],
        "diverging_low": "#2563eb",
        "diverging_mid": "#e4eaf6",
        "diverging_high": "#dc2626",
        "status": {"good": "#059669", "warning": "#d97706", "serious": "#ea580c", "critical": "#dc2626"},
    },
    "dark": {
        "page": "#060b16",
        "surface": "#0b1426",
        "surface_raised": "#0f1a2e",
        "surface_sunken": "#0a1222",
        "surface_inset": "#16233d",
        "border": "#1d2b47",
        "border_strong": "#2b3d61",
        "text_primary": "#eef3fb",
        "text_secondary": "#9aabc7",
        "text_muted": "#64769a",
        "grid": "#152238",
        "accent": "#3b82f6",
        "accent_alt": "#8b5cf6",
        "shadow": "0 1px 2px rgba(0,0,0,.4), 0 12px 32px -16px rgba(0,0,0,.8)",
        "shadow_lift": "0 3px 8px rgba(0,0,0,.45), 0 26px 54px -20px rgba(0,0,0,.9)",
        "glow": "0 0 0 1px rgba(59,130,246,.35), 0 12px 38px -10px rgba(59,130,246,.45)",
        "series": ["#3b82f6", "#8b5cf6", "#10b981", "#f59e0b", "#ec4899", "#06b6d4", "#ef4444", "#a3e635"],
        "low_contrast_series": [],
        "sequential": ["#10203d", "#16305c", "#1c4080", "#2563eb", "#3b82f6", "#60a5fa", "#93c5fd", "#bfdbfe"],
        "ordinal": ["#bfdbfe", "#93c5fd", "#60a5fa", "#3b82f6", "#2563eb", "#1d4ed8", "#1e40af"],
        "diverging_low": "#3b82f6",
        "diverging_mid": "#233a55",
        "diverging_high": "#ef4444",
        "status": {"good": "#10b981", "warning": "#f59e0b", "serious": "#fb923c", "critical": "#ef4444"},
    },
}

# How many options the comparison chart will colour before folding the rest into the
# table view. Beyond this a grouped bar chart stops being readable at any palette.
ALL_PAIRS_SAFE_SLOTS = 6

TIER_COLOR_SLOT = {"Tier 1": 0, "Tier 2": 2, "Tier 3": 3, "Tier 4": 4}

STRENGTH_STATUS = {
    "strong": "good", "moderate": "warning",
    "limited": "serious", "preliminary": "critical",
}
SEVERITY_STATUS = {
    "decision_changing": "critical", "material": "serious", "minor": "warning",
    "critical": "critical", "serious": "serious", "moderate": "warning",
    "mild": "good", "high": "serious",
}
CONFIDENCE_STATUS = {"high": "good", "moderate": "warning", "low": "serious"}

FONT_STACK = (
    '"Inter var", "Inter", ui-sans-serif, -apple-system, BlinkMacSystemFont, '
    '"Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
)
MONO_STACK = '"JetBrains Mono", "SF Mono", ui-monospace, Menlo, Consolas, monospace'


def active_theme() -> ThemeName:
    """Resolve the theme the app is actually painted in.

    Order matters. `theme.base` in `.streamlit/config.toml` is what Streamlit paints
    its own chrome with, so it wins - reading the *browser* preference first is what
    produced white cards on a dark page. `st.context.theme` is the fallback for when
    no base is configured, and dark is the final default.
    """
    try:
        configured = st.get_option("theme.base")
        if configured in ("light", "dark"):
            return configured  # type: ignore[return-value]
    except Exception:  # pragma: no cover - option table unavailable in bare mode
        pass
    try:
        detected = getattr(st.context.theme, "type", None)
        if detected in ("light", "dark"):
            return detected  # type: ignore[return-value]
    except Exception:  # pragma: no cover - older runtimes
        pass
    return "dark"


def tokens() -> dict[str, Any]:
    return PALETTES[active_theme()]


def series_colors(n: int, *, all_pairs: bool = False) -> list[str]:
    """`n` categorical colours in fixed slot order.

    With `all_pairs=True` the caller is drawing a grouped or overlaid form, which
    stops being readable past a handful of series, so the count is capped and the
    caller folds the remainder into its table view.
    """
    palette = tokens()["series"]
    cap = ALL_PAIRS_SAFE_SLOTS if all_pairs else len(palette)
    return palette[: min(n, cap)]


def status_color(key: str) -> str:
    palette = tokens()
    return palette["status"].get(key, palette["text_secondary"])


def ordinal_scale(n: int) -> list[str]:
    ramp = tokens()["ordinal"]
    if n <= 1:
        return [ramp[len(ramp) // 2]]
    step = (len(ramp) - 1) / (n - 1)
    return [ramp[int(round(i * step))] for i in range(n)]


def needs_relief(slot: int) -> bool:
    """True when this slot is too low-contrast to carry meaning without a label."""
    return slot in tokens()["low_contrast_series"]


# --------------------------------------------------------------------------- #
# Stylesheet
# --------------------------------------------------------------------------- #

def stylesheet() -> str:
    t = tokens()
    dark = active_theme() == "dark"
    accent = t["accent"]
    accent_alt = t["accent_alt"]
    # A faint tinted wash so the page is not a flat slab of one colour.
    wash = (
        "radial-gradient(1200px 600px at 12% -8%, rgba(57,135,229,.10), transparent 60%),"
        "radial-gradient(900px 500px at 92% 2%, rgba(144,133,233,.07), transparent 55%)"
        if dark else
        "radial-gradient(1200px 600px at 12% -10%, rgba(42,120,214,.07), transparent 60%),"
        "radial-gradient(900px 500px at 92% 0%, rgba(74,58,167,.05), transparent 55%)"
    )

    return f"""<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap');
:root {{
  --ds-page: {t['page']};
  --ds-surface: {t['surface']};
  --ds-raised: {t['surface_raised']};
  --ds-sunken: {t['surface_sunken']};
  --ds-inset: {t['surface_inset']};
  --ds-border: {t['border']};
  --ds-border-strong: {t['border_strong']};
  --ds-ink: {t['text_primary']};
  --ds-ink-2: {t['text_secondary']};
  --ds-ink-3: {t['text_muted']};
  --ds-accent: {accent};
  --ds-good: {t['status']['good']};
  --ds-warning: {t['status']['warning']};
  --ds-serious: {t['status']['serious']};
  --ds-critical: {t['status']['critical']};
  --ds-shadow: {t['shadow']};
  --ds-shadow-lift: {t['shadow_lift']};
  --ds-glow: {t['glow']};
  --ds-font: {FONT_STACK};
  --ds-mono: {MONO_STACK};
  --ds-radius: 14px;
  --ds-radius-sm: 9px;
  --ds-ease: cubic-bezier(.22,.61,.36,1);
}}

html, body, [class*="st-"], .stMarkdown, button, input, textarea, select {{
  font-family: var(--ds-font);
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}}
.stApp {{ background: {t['page']}; background-image: {wash}; background-attachment: fixed; }}
/* Leaves room for Streamlit's header bar so the masthead is never hidden under it. */
.block-container {{ padding-top: 3.4rem; padding-bottom: 5rem; max-width: 1340px; }}

/* ---------- Sidebar workspace navigation ---------- */
[data-testid="stLogo"] {{ height: 2.4rem; max-width: 12rem; }}
[data-testid="stSidebarHeader"] {{ padding-bottom: .4rem; }}
[data-testid="stNavSectionHeader"], [data-testid="stSidebarNav"] header {{
  font-family: var(--ds-mono) !important; font-size: .66rem !important; font-weight: 600 !important;
  letter-spacing: .16em; text-transform: uppercase; color: var(--ds-ink-3) !important;
  margin-top: .9rem;
}}
[data-testid="stSidebarNavLink"] {{
  border-radius: 10px; margin: .08rem 0; padding: .42rem .7rem !important;
  transition: background .18s var(--ds-ease), transform .18s var(--ds-ease);
}}
[data-testid="stSidebarNavLink"]:hover {{ background: color-mix(in srgb, {accent_alt} 10%, transparent) !important; transform: translateX(2px); }}
[data-testid="stSidebarNavLink"][aria-current="page"], [data-testid="stSidebarNavLink"][data-active="true"] {{
  background: color-mix(in srgb, {accent_alt} 20%, var(--ds-raised)) !important;
  box-shadow: inset 0 0 0 1px color-mix(in srgb, {accent_alt} 40%, transparent);
}}
[data-testid="stSidebarNavLink"] span {{ font-size: .9rem; }}
[data-testid="stSidebarNavSeparator"] {{ border-color: var(--ds-border); }}

/* ---------- Workspace card (sidebar foot) ---------- */
.ds-workspace {{
  border: 1px solid var(--ds-border); background: var(--ds-raised);
  border-radius: 12px; padding: .65rem .8rem; margin-top: 1.2rem;
  font-family: var(--ds-mono); font-size: .68rem; letter-spacing: .08em;
  text-transform: uppercase; color: var(--ds-ink-2);
  display: flex; align-items: center; gap: .5rem;
}}
.ds-workspace .dot {{ width: 7px; height: 7px; border-radius: 50%; background: var(--ds-good);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--ds-good) 22%, transparent); flex: none; }}
.ds-workspace .who {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.ds-workspace .plan {{ margin-left: auto; color: {accent_alt}; font-weight: 700; }}
.ds-motto {{ margin: .9rem 0 .2rem; font-size: .82rem; color: var(--ds-ink); font-weight: 600; }}
.ds-motto span {{ display: block; font-weight: 400; color: var(--ds-ink-3); font-size: .76rem; margin-top: .1rem; }}

/* ---------- Page heading (eyebrow + headline) ---------- */
.ds-eyebrow {{
  font-family: var(--ds-mono); font-size: .72rem; letter-spacing: .2em; text-transform: uppercase;
  color: var(--ds-ink-3); margin: .2rem 0 .7rem;
}}
.ds-headline {{
  font-size: 2.35rem; font-weight: 800; letter-spacing: -.045em; line-height: 1.1;
  color: var(--ds-ink); margin: 0 0 .6rem;
}}
.ds-subhead {{ font-size: 1rem; color: var(--ds-ink-2); margin: 0 0 1.3rem; max-width: 62ch; }}

/* ---------- Pricing cards ---------- */
.ds-plan {{
  border: 1px solid var(--ds-border); border-radius: 16px; padding: 1.3rem 1.3rem 1.1rem;
  background: var(--ds-raised); box-shadow: var(--ds-shadow); height: 100%;
  transition: transform .25s var(--ds-ease), box-shadow .25s var(--ds-ease), border-color .25s var(--ds-ease);
  animation: ds-rise .5s var(--ds-ease) backwards;
}}
.ds-plan:hover {{ transform: translateY(-3px); box-shadow: var(--ds-shadow-lift); }}
.ds-plan.is-featured {{
  border-color: color-mix(in srgb, {accent_alt} 55%, var(--ds-border));
  box-shadow: 0 0 0 1px color-mix(in srgb, {accent_alt} 30%, transparent), var(--ds-shadow-lift);
}}
.ds-plan-name {{ font-family: var(--ds-mono); font-size: .74rem; letter-spacing: .16em;
  text-transform: uppercase; color: {accent_alt}; font-weight: 600; }}
.ds-plan-price {{ margin: .7rem 0 .15rem; color: var(--ds-ink); font-size: 2.6rem; font-weight: 800;
  letter-spacing: -.04em; line-height: 1; }}
.ds-plan-price span {{ font-size: .85rem; font-weight: 500; color: var(--ds-ink-3); letter-spacing: 0; }}
.ds-plan-blurb {{ color: var(--ds-ink-2); font-size: .9rem; padding-bottom: .9rem;
  border-bottom: 1px solid var(--ds-border); margin-bottom: .8rem; }}
.ds-plan ul {{ list-style: none; padding: 0; margin: 0; }}
.ds-plan li {{ font-size: .86rem; color: var(--ds-ink-2); padding: .3rem 0 .3rem 1.4rem; position: relative; line-height: 1.45; }}
.ds-plan li::before {{ content: "\\2713"; position: absolute; left: 0; color: var(--ds-good); font-weight: 700; }}
.ds-plan-current {{ display: inline-block; margin-top: .8rem; font-size: .72rem; font-weight: 700;
  letter-spacing: .08em; text-transform: uppercase; color: var(--ds-good); }}

/* ---------- Calls to action ---------- */
.ds-cta-hint {{ font-size: .78rem; color: var(--ds-ink-3); margin: -.3rem 0 .8rem; }}
.ds-credit-line {{ font-size: .8rem; color: var(--ds-ink-2); margin: .1rem 0 .9rem; }}
.ds-credit-line b {{ color: var(--ds-ink); }}

/* ---------- Motion primitives ---------- */
@keyframes ds-rise {{ from {{ opacity: 0; transform: translateY(10px); }} to {{ opacity: 1; transform: none; }} }}
@keyframes ds-fade {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
@keyframes ds-fill {{ from {{ transform: scaleX(0); }} to {{ transform: scaleX(1); }} }}
@keyframes ds-shimmer {{ 0% {{ background-position: -420px 0; }} 100% {{ background-position: 420px 0; }} }}
@keyframes ds-pulse {{ 0%,100% {{ opacity: 1; transform: scale(1); }} 50% {{ opacity: .45; transform: scale(.82); }} }}
@keyframes ds-sweep {{ 0% {{ transform: translateX(-100%); }} 100% {{ transform: translateX(300%); }} }}

.ds-in {{ animation: ds-rise .5s var(--ds-ease) backwards; }}
.ds-in-1 {{ animation-delay: .04s; }} .ds-in-2 {{ animation-delay: .09s; }}
.ds-in-3 {{ animation-delay: .14s; }} .ds-in-4 {{ animation-delay: .19s; }}

@media (prefers-reduced-motion: reduce) {{
  *, *::before, *::after {{
    animation: none !important;
    transition-duration: .001ms !important;
  }}
}}

/* ---------- Masthead ---------- */
.ds-masthead {{
  display: flex; align-items: flex-end; justify-content: space-between;
  gap: 1.5rem; flex-wrap: wrap;
  padding-bottom: 1.1rem; margin-bottom: 1.5rem;
  border-bottom: 1px solid var(--ds-border);
  animation: ds-fade .6s var(--ds-ease) backwards;
}}
.ds-wordmark {{
  display: flex; align-items: center; gap: .6rem;
  font-size: 1.7rem; font-weight: 750; letter-spacing: -.035em;
  color: var(--ds-ink); line-height: 1;
}}
.ds-logo {{
  width: 30px; height: 30px; border-radius: 9px; flex: none;
  display: grid; place-items: center;
  background: linear-gradient(145deg, {accent}, {t["series"][4]});
  box-shadow: var(--ds-glow);
  position: relative; overflow: hidden;
}}
.ds-logo::after {{
  content: ""; position: absolute; inset: 0; width: 30%;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,.45), transparent);
  animation: ds-sweep 4.5s var(--ds-ease) infinite;
}}
.ds-logo svg {{ position: relative; z-index: 1; }}
.ds-tagline {{ color: var(--ds-ink-3); font-size: .8rem; margin-top: .45rem; padding-left: 2.6rem; }}
.ds-masthead-meta {{ display: flex; gap: 1.6rem; align-items: flex-end; }}
.ds-masthead-meta > div {{ text-align: right; }}
.ds-masthead-meta span {{
  display: block; font-size: .64rem; font-weight: 600; letter-spacing: .09em;
  text-transform: uppercase; color: var(--ds-ink-3);
}}
.ds-masthead-meta b {{
  display: block; color: var(--ds-ink); font-size: 1.08rem; font-weight: 680;
  font-variant-numeric: tabular-nums; letter-spacing: -.02em; margin-top: .12rem;
}}

/* ---------- KPI cards ---------- */
.ds-kpi {{
  background: var(--ds-raised);
  border: 1px solid var(--ds-border);
  border-radius: var(--ds-radius);
  padding: 1rem 1.1rem 1.05rem;
  height: 100%; display: flex; flex-direction: column; gap: .45rem;
  position: relative; overflow: hidden;
  box-shadow: var(--ds-shadow);
  transition: transform .28s var(--ds-ease), box-shadow .28s var(--ds-ease),
              border-color .28s var(--ds-ease);
  animation: ds-rise .5s var(--ds-ease) backwards;
}}
.ds-kpi::before {{
  content: ""; position: absolute; inset: 0 0 auto 0; height: 2px;
  background: linear-gradient(90deg, var(--ds-kpi-accent), transparent 85%);
}}
.ds-kpi::after {{
  content: ""; position: absolute; right: -30%; top: -60%;
  width: 180px; height: 180px; border-radius: 50%;
  background: radial-gradient(circle, var(--ds-kpi-accent), transparent 70%);
  opacity: .06; pointer-events: none;
  transition: opacity .3s var(--ds-ease);
}}
.ds-kpi:hover {{
  transform: translateY(-3px);
  box-shadow: var(--ds-shadow-lift);
  border-color: var(--ds-border-strong);
}}
.ds-kpi:hover::after {{ opacity: .13; }}
.ds-kpi-label {{
  font-size: .67rem; font-weight: 650; letter-spacing: .09em;
  text-transform: uppercase; color: var(--ds-ink-3);
  display: flex; align-items: center; gap: .35rem;
}}
.ds-kpi-value {{
  font-size: 2.05rem; font-weight: 720; line-height: 1.02;
  letter-spacing: -.04em; color: var(--ds-ink);
  font-variant-numeric: tabular-nums;
  display: flex; align-items: baseline; gap: .25rem;
}}
.ds-kpi-value .ds-unit {{
  font-size: .8rem; font-weight: 550; color: var(--ds-ink-3); letter-spacing: 0;
}}
.ds-kpi-sub {{ font-size: .74rem; color: var(--ds-ink-2); line-height: 1.42; margin-top: auto; }}
.ds-meter {{
  height: 5px; border-radius: 99px; background: var(--ds-inset);
  overflow: hidden; margin-top: .15rem;
}}
.ds-meter > span {{
  display: block; height: 100%; border-radius: 99px;
  transform-origin: left; animation: ds-fill .85s var(--ds-ease) backwards .15s;
  position: relative;
}}
.ds-meter > span::after {{
  content: ""; position: absolute; inset: 0;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,.35), transparent);
  animation: ds-sweep 2.8s var(--ds-ease) infinite 1s;
}}

/* ---------- Badges ---------- */
.ds-badge {{
  display: inline-flex; align-items: center; gap: .32rem;
  font-size: .68rem; font-weight: 620; letter-spacing: .015em;
  padding: .2rem .52rem; border-radius: 6px;
  border: 1px solid var(--ds-badge-line, var(--ds-border-strong));
  color: var(--ds-badge-ink, var(--ds-ink-2));
  background: var(--ds-badge-bg, transparent);
  white-space: nowrap; vertical-align: middle;
  transition: transform .2s var(--ds-ease);
}}
.ds-badge:hover {{ transform: translateY(-1px); }}
.ds-badge-dot {{
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--ds-badge-ink, currentColor); flex: none;
}}
.ds-badge-live .ds-badge-dot {{ animation: ds-pulse 1.7s ease-in-out infinite; }}

/* ---------- Section headers ---------- */
.ds-section {{
  display: flex; align-items: baseline; gap: .7rem;
  margin: 2.1rem 0 .3rem; padding-bottom: .55rem;
  border-bottom: 1px solid var(--ds-border);
  animation: ds-fade .5s var(--ds-ease) backwards;
}}
.ds-section h3 {{
  margin: 0; font-size: 1.02rem; font-weight: 680;
  letter-spacing: -.02em; color: var(--ds-ink);
  display: flex; align-items: center; gap: .5rem;
}}
.ds-section h3::before {{
  content: ""; width: 3px; height: 15px; border-radius: 2px;
  background: linear-gradient(180deg, var(--ds-accent), transparent);
}}
.ds-section span {{ font-size: .75rem; color: var(--ds-ink-3); }}

/* ---------- Content cards ---------- */
.ds-card {{
  background: var(--ds-raised);
  border: 1px solid var(--ds-border);
  border-left: 3px solid var(--ds-card-accent, var(--ds-border-strong));
  border-radius: var(--ds-radius-sm);
  padding: .95rem 1.1rem; margin-bottom: .75rem;
  box-shadow: var(--ds-shadow);
  transition: transform .25s var(--ds-ease), box-shadow .25s var(--ds-ease),
              border-color .25s var(--ds-ease);
  animation: ds-rise .45s var(--ds-ease) backwards;
}}
.ds-card:hover {{
  transform: translateX(2px);
  box-shadow: var(--ds-shadow-lift);
  border-color: var(--ds-border-strong);
  border-left-color: var(--ds-card-accent, var(--ds-accent));
}}
.ds-card-head {{
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: .8rem; margin-bottom: .45rem;
}}
.ds-card-title {{
  font-size: .94rem; font-weight: 660; color: var(--ds-ink);
  line-height: 1.38; letter-spacing: -.012em;
}}
.ds-card-body {{ font-size: .85rem; color: var(--ds-ink-2); line-height: 1.58; }}
.ds-card-foot {{
  margin-top: .6rem; padding-top: .55rem;
  border-top: 1px dashed var(--ds-border);
  font-size: .75rem; color: var(--ds-ink-3);
  display: flex; gap: .4rem; flex-wrap: wrap; align-items: center;
}}
.ds-sowhat {{
  font-size: .8rem; color: var(--ds-ink); margin-top: .55rem;
  padding: .45rem .7rem; border-radius: 0 7px 7px 0;
  border-left: 2px solid var(--ds-accent);
  background: var(--ds-sunken);
}}

/* ---------- Verdict ---------- */
.ds-verdict {{
  background: linear-gradient(135deg, var(--ds-raised), var(--ds-sunken));
  border: 1px solid var(--ds-border);
  border-radius: var(--ds-radius);
  padding: 1.3rem 1.45rem; margin-bottom: 1.1rem;
  position: relative; overflow: hidden;
  box-shadow: var(--ds-shadow);
  animation: ds-rise .5s var(--ds-ease) backwards;
}}
.ds-verdict::before {{
  content: ""; position: absolute; inset: 0 auto 0 0; width: 3px;
  background: linear-gradient(180deg, var(--ds-accent), transparent);
}}
.ds-verdict-label {{
  font-size: .65rem; font-weight: 700; letter-spacing: .12em;
  text-transform: uppercase; color: var(--ds-ink-3); margin-bottom: .5rem;
}}
.ds-verdict-text {{
  font-size: 1.16rem; font-weight: 570; line-height: 1.48;
  color: var(--ds-ink); letter-spacing: -.017em;
}}

/* ---------- Conflicts ---------- */
.ds-conflict {{
  border: 1px solid var(--ds-border); border-radius: var(--ds-radius-sm);
  overflow: hidden; margin-bottom: .85rem; background: var(--ds-raised);
  box-shadow: var(--ds-shadow);
  animation: ds-rise .45s var(--ds-ease) backwards;
  transition: box-shadow .25s var(--ds-ease);
}}
.ds-conflict:hover {{ box-shadow: var(--ds-shadow-lift); }}
.ds-conflict-head {{
  padding: .65rem 1rem; background: var(--ds-sunken);
  border-bottom: 1px solid var(--ds-border);
  display: flex; align-items: center; justify-content: space-between; gap: .7rem;
  font-weight: 660; font-size: .9rem; color: var(--ds-ink);
}}
.ds-sides {{ display: grid; grid-template-columns: 1fr 1fr; position: relative; }}
.ds-side {{ padding: .85rem 1rem; font-size: .83rem; line-height: 1.55; color: var(--ds-ink-2); }}
.ds-side + .ds-side {{ border-left: 1px solid var(--ds-border); }}
.ds-side-label {{
  font-size: .64rem; font-weight: 700; letter-spacing: .09em;
  text-transform: uppercase; margin-bottom: .4rem;
}}
.ds-adjudication {{
  padding: .8rem 1rem; border-top: 1px solid var(--ds-border);
  background: var(--ds-sunken); font-size: .83rem; line-height: 1.55; color: var(--ds-ink);
}}
@media (max-width: 860px) {{
  .ds-sides {{ grid-template-columns: 1fr; }}
  .ds-side + .ds-side {{ border-left: 0; border-top: 1px solid var(--ds-border); }}
}}

/* ---------- Timeline ---------- */
.ds-timeline {{ position: relative; padding-left: 1.6rem; margin-top: .5rem; }}
.ds-timeline::before {{
  content: ""; position: absolute; left: 5px; top: .5rem; bottom: .5rem; width: 2px;
  background: linear-gradient(180deg, var(--ds-accent), var(--ds-border) 55%, transparent);
  border-radius: 2px;
}}
.ds-event {{
  position: relative; padding: 0 0 1.1rem 0;
  animation: ds-rise .45s var(--ds-ease) backwards;
}}
.ds-event::before {{
  content: ""; position: absolute; left: -1.6rem; top: .3rem;
  width: 12px; height: 12px; border-radius: 50%;
  background: var(--ds-page);
  border: 2.5px solid var(--ds-event-color, var(--ds-accent));
  transition: transform .25s var(--ds-ease), box-shadow .25s var(--ds-ease);
}}
.ds-event:hover::before {{
  transform: scale(1.3);
  box-shadow: 0 0 0 5px color-mix(in srgb, var(--ds-event-color, var(--ds-accent)) 18%, transparent);
}}
.ds-event-date {{
  font-family: var(--ds-mono); font-size: .71rem; color: var(--ds-ink-3);
  font-variant-numeric: tabular-nums; letter-spacing: .01em;
}}
.ds-event-label {{ font-size: .9rem; font-weight: 640; color: var(--ds-ink); margin: .15rem 0 .2rem; }}
.ds-event-detail {{ font-size: .81rem; color: var(--ds-ink-2); line-height: 1.55; }}

/* ---------- Sources ---------- */
.ds-source {{
  padding: .7rem .75rem; border: 1px solid transparent;
  border-bottom-color: var(--ds-border);
  border-radius: var(--ds-radius-sm);
  font-size: .845rem; line-height: 1.5;
  transition: background .2s var(--ds-ease), border-color .2s var(--ds-ease);
}}
.ds-source:hover {{ background: var(--ds-raised); border-color: var(--ds-border); }}
.ds-source a {{ color: var(--ds-accent); text-decoration: none; font-weight: 580; }}
.ds-source a:hover {{ text-decoration: underline; }}
.ds-source-meta {{
  color: var(--ds-ink-3); font-size: .745rem; margin-top: .28rem;
  display: flex; gap: .4rem; flex-wrap: wrap; align-items: center;
}}
.ds-handle {{
  font-family: var(--ds-mono); font-size: .71rem; font-weight: 600;
  color: var(--ds-accent); background: var(--ds-sunken);
  padding: .1rem .36rem; border-radius: 5px; border: 1px solid var(--ds-border);
  transition: background .2s var(--ds-ease), transform .2s var(--ds-ease);
}}
a.ds-handle:hover {{ background: var(--ds-inset); transform: translateY(-1px); }}

/* ---------- Callout ---------- */
.ds-note {{
  border: 1px solid var(--ds-border);
  border-left: 3px solid var(--ds-note-color, var(--ds-warning));
  background: var(--ds-sunken); border-radius: var(--ds-radius-sm);
  padding: .75rem 1rem; font-size: .8rem; line-height: 1.55;
  color: var(--ds-ink-2); margin: .55rem 0 .95rem;
  animation: ds-fade .4s var(--ds-ease) backwards;
}}
.ds-note b {{ color: var(--ds-ink); }}

/* ---------- Run progress ---------- */
.ds-runlog {{
  font-size: .78rem; color: var(--ds-ink-2); line-height: 1.7;
  background: var(--ds-raised); border: 1px solid var(--ds-border);
  border-radius: var(--ds-radius-sm); padding: .85rem 1.05rem;
  box-shadow: var(--ds-shadow);
}}
.ds-runlog .ds-runline {{ animation: ds-rise .35s var(--ds-ease) backwards; }}
.ds-runlog .ds-runline.is-current {{ color: var(--ds-ink); font-weight: 560; }}
.ds-skel {{
  height: 11px; border-radius: 6px; margin: .45rem 0;
  background: linear-gradient(90deg, var(--ds-sunken) 8%, var(--ds-inset) 22%, var(--ds-sunken) 36%);
  background-size: 840px 100%; animation: ds-shimmer 1.5s linear infinite;
}}

/* ---------- Empty state ---------- */
.ds-hero {{ animation: ds-rise .55s var(--ds-ease) backwards; }}
.ds-hero h1 {{
  font-size: 1.75rem; font-weight: 720; letter-spacing: -.035em;
  color: var(--ds-ink); margin: 0 0 .35rem;
}}
.ds-hero p {{ font-size: .88rem; color: var(--ds-ink-3); line-height: 1.6; margin: 0 0 1rem; max-width: 62ch; }}
.ds-pipeline {{
  display: flex; gap: .4rem; flex-wrap: wrap; margin: 1.1rem 0 .3rem;
}}
.ds-step {{
  display: flex; align-items: center; gap: .45rem;
  font-size: .72rem; color: var(--ds-ink-3);
  background: var(--ds-raised); border: 1px solid var(--ds-border);
  border-radius: 99px; padding: .3rem .7rem .3rem .45rem;
  animation: ds-rise .45s var(--ds-ease) backwards;
}}
.ds-step b {{
  width: 17px; height: 17px; border-radius: 50%; flex: none;
  display: grid; place-items: center;
  background: var(--ds-inset); color: var(--ds-ink-2);
  font-size: .62rem; font-weight: 700; font-variant-numeric: tabular-nums;
}}


/* ---------- Brand ---------- */
.ds-brand {{ display: flex; align-items: center; gap: .6rem; }}
.ds-logo {{
  border-radius: 10px; flex: none; display: grid; place-items: center;
  color: #fff; background: linear-gradient(145deg, {accent}, {accent_alt});
  box-shadow: var(--ds-glow); position: relative; overflow: hidden;
}}
.ds-logo::after {{
  content: ""; position: absolute; inset: 0; width: 34%;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,.5), transparent);
  animation: ds-sweep 5s var(--ds-ease) infinite;
}}
.ds-wordtext {{
  font-size: 1.32rem; font-weight: 780; letter-spacing: -.035em; color: var(--ds-ink);
}}
.ds-wordtext span {{
  background: linear-gradient(95deg, {accent}, {accent_alt});
  -webkit-background-clip: text; background-clip: text; color: transparent;
}}
.ds-brand-sub {{
  font-size: .72rem; color: var(--ds-ink-3); line-height: 1.45;
  margin: .6rem 0 1.1rem; padding-left: .12rem;
}}
.ds-brand-foot {{
  display: flex; gap: .55rem; align-items: flex-start;
  margin-top: 1.2rem; padding-top: 1rem; border-top: 1px solid var(--ds-border);
  font-size: .71rem; color: var(--ds-ink-3); line-height: 1.5;
}}
.ds-brand-foot > span {{ color: {accent}; flex: none; }}

/* ---------- Welcome hero ---------- */
.ds-welcome {{
  position: relative; overflow: hidden;
  border: 1px solid var(--ds-border); border-radius: 18px;
  background:
    radial-gradient(900px 420px at 88% 12%, rgba(139,92,246,.22), transparent 62%),
    radial-gradient(700px 400px at 66% 88%, rgba(59,130,246,.20), transparent 60%),
    linear-gradient(120deg, var(--ds-raised), var(--ds-sunken));
  padding: 1.9rem 2rem 2rem;
  box-shadow: var(--ds-shadow);
  animation: ds-rise .55s var(--ds-ease) backwards;
}}
.ds-welcome-art {{
  position: absolute; right: -60px; top: -40px; width: 420px; height: 320px;
  pointer-events: none; opacity: .5;
  background:
    radial-gradient(closest-side, rgba(139,92,246,.42), transparent 72%),
    radial-gradient(closest-side at 62% 58%, rgba(59,130,246,.40), transparent 70%),
    radial-gradient(closest-side at 30% 78%, rgba(16,185,129,.22), transparent 70%);
  filter: blur(6px);
  animation: ds-float 11s ease-in-out infinite;
}}
@keyframes ds-float {{
  0%,100% {{ transform: translate3d(0,0,0) scale(1); }}
  50% {{ transform: translate3d(-16px,14px,0) scale(1.06); }}
}}
.ds-welcome-body {{ position: relative; max-width: 60ch; }}
.ds-welcome-eyebrow {{ font-size: .82rem; color: var(--ds-ink-2); margin-bottom: .1rem; }}
.ds-welcome-mark {{
  font-size: 3rem; font-weight: 800; letter-spacing: -.045em;
  line-height: 1.05; color: var(--ds-ink); margin-bottom: .55rem;
}}
.ds-welcome-mark span {{
  background: linear-gradient(95deg, {accent}, {accent_alt});
  -webkit-background-clip: text; background-clip: text; color: transparent;
}}
.ds-welcome-lead {{ font-size: 1rem; font-weight: 640; color: var(--ds-ink); margin-bottom: .45rem; }}
.ds-welcome-sub {{ font-size: .87rem; color: var(--ds-ink-2); line-height: 1.6; }}

/* ---------- Capability grid ---------- */
.ds-capgrid {{
  display: grid; gap: .75rem; margin-top: .4rem;
  grid-template-columns: repeat(auto-fit, minmax(215px, 1fr));
}}
.ds-cap {{
  position: relative; overflow: hidden;
  background: var(--ds-raised); border: 1px solid var(--ds-border);
  border-radius: 14px; padding: 1.05rem 1.05rem 2.2rem;
  box-shadow: var(--ds-shadow);
  transition: transform .26s var(--ds-ease), box-shadow .26s var(--ds-ease),
              border-color .26s var(--ds-ease);
  animation: ds-rise .5s var(--ds-ease) backwards;
}}
.ds-cap:hover {{
  transform: translateY(-4px); box-shadow: var(--ds-shadow-lift);
  border-color: color-mix(in srgb, var(--cap) 45%, var(--ds-border));
}}
.ds-cap-icon {{
  width: 40px; height: 40px; border-radius: 11px; display: grid; place-items: center;
  color: #fff; margin-bottom: .8rem;
  background: linear-gradient(145deg, var(--cap), color-mix(in srgb, var(--cap) 62%, #000));
  box-shadow: 0 6px 18px -8px var(--cap);
  transition: transform .26s var(--ds-ease);
}}
.ds-cap:hover .ds-cap-icon {{ transform: scale(1.07) rotate(-3deg); }}
.ds-cap-title {{
  font-size: .92rem; font-weight: 660; color: var(--ds-ink);
  line-height: 1.3; letter-spacing: -.015em; margin-bottom: .4rem;
}}
.ds-cap-body {{ font-size: .78rem; color: var(--ds-ink-3); line-height: 1.5; }}
.ds-cap-arrow {{
  position: absolute; right: 1rem; bottom: .9rem; color: var(--cap);
  opacity: .55; transition: transform .26s var(--ds-ease), opacity .26s var(--ds-ease);
}}
.ds-cap:hover .ds-cap-arrow {{ transform: translateX(4px); opacity: 1; }}

/* ---------- Side panels ---------- */
.ds-panel {{
  background: var(--ds-raised); border: 1px solid var(--ds-border);
  border-radius: 14px; overflow: hidden; margin-bottom: .85rem;
  box-shadow: var(--ds-shadow);
  animation: ds-rise .5s var(--ds-ease) backwards;
}}
.ds-panel-head {{
  display: flex; align-items: center; gap: .5rem;
  padding: .8rem 1rem; border-bottom: 1px solid var(--ds-border);
  background: var(--ds-sunken);
  font-size: .88rem; font-weight: 660; color: var(--ds-ink);
}}
.ds-panel-icon {{ color: {accent}; display: grid; place-items: center; }}
.ds-panel-meta {{ margin-left: auto; font-size: .72rem; font-weight: 500; color: var(--ds-ink-3); }}
.ds-panel-body {{ padding: .5rem .7rem .7rem; }}
.ds-prow {{
  display: flex; align-items: center; gap: .6rem;
  padding: .52rem .5rem; border-radius: 9px;
  transition: background .2s var(--ds-ease);
}}
.ds-prow:hover {{ background: var(--ds-sunken); }}
.ds-prow-icon {{
  width: 30px; height: 30px; border-radius: 8px; flex: none;
  display: grid; place-items: center; color: #fff;
  background: linear-gradient(145deg, var(--cap), color-mix(in srgb, var(--cap) 62%, #000));
}}
.ds-prow-label {{ font-size: .81rem; font-weight: 560; color: var(--ds-ink); }}
.ds-prow-meta {{ margin-left: auto; font-size: .71rem; color: var(--ds-ink-3); text-align: right; }}
.ds-step-row {{ display: flex; gap: .65rem; align-items: flex-start; padding: .45rem .5rem; }}
.ds-step-row b {{
  width: 20px; height: 20px; border-radius: 50%; flex: none; margin-top: .1rem;
  display: grid; place-items: center; font-size: .65rem; font-weight: 700;
  color: {accent}; background: color-mix(in srgb, {accent} 16%, transparent);
  border: 1px solid color-mix(in srgb, {accent} 38%, transparent);
}}
.ds-step-row span {{ display: block; font-size: .81rem; font-weight: 600; color: var(--ds-ink); }}
.ds-step-row em {{ display: block; font-size: .73rem; font-style: normal; color: var(--ds-ink-3); line-height: 1.45; }}
.ds-trust {{
  border: 1px solid color-mix(in srgb, {accent} 30%, var(--ds-border));
  background: linear-gradient(135deg, color-mix(in srgb, {accent} 11%, var(--ds-raised)), var(--ds-raised));
  border-radius: 14px; padding: .9rem 1rem;
  font-size: .78rem; line-height: 1.55; color: var(--ds-ink-2);
  animation: ds-rise .5s var(--ds-ease) backwards;
}}
.ds-trust b {{ color: var(--ds-ink); display: block; margin-bottom: .2rem; }}

/* ---------- Streamlit chrome ---------- */
[data-testid="stMetricValue"] {{ font-variant-numeric: tabular-nums; }}
div[data-baseweb="tab-list"] {{
  gap: .1rem; border-bottom: 1px solid var(--ds-border);
  background: transparent !important;
}}
button[data-baseweb="tab"] {{
  font-size: .86rem !important; font-weight: 570 !important;
  transition: color .2s var(--ds-ease);
}}
[data-testid="stSidebar"] {{
  border-right: 1px solid var(--ds-border);
  background: {t['surface_sunken']} !important;
}}
[data-testid="stSidebar"] .block-container {{ padding-top: 1.5rem; }}
hr {{ border-color: var(--ds-border); }}
[data-testid="stDataFrame"], .stDataFrame {{
  border: 1px solid var(--ds-border); border-radius: var(--ds-radius-sm); overflow: hidden;
}}
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{
  border-radius: var(--ds-radius-sm); font-weight: 570;
  transition: transform .18s var(--ds-ease), box-shadow .18s var(--ds-ease),
              border-color .18s var(--ds-ease);
}}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {{
  transform: translateY(-1px); box-shadow: var(--ds-shadow);
}}
.stFormSubmitButton > button[kind="primary"] {{ box-shadow: var(--ds-glow); }}
[data-testid="stExpander"] details {{
  border: 1px solid var(--ds-border) !important;
  border-radius: var(--ds-radius-sm) !important;
  background: var(--ds-raised);
  overflow: hidden;
}}
[data-testid="stExpander"] summary:hover {{ background: var(--ds-sunken); }}
[data-testid="stTextArea"] textarea, [data-testid="stTextInput"] input {{
  border-radius: var(--ds-radius-sm) !important;
  font-size: .92rem !important;
}}
[data-testid="stTextArea"] textarea:focus, [data-testid="stTextInput"] input:focus {{
  box-shadow: var(--ds-glow) !important;
}}
[data-testid="stProgress"] > div > div > div {{
  background: linear-gradient(90deg, var(--ds-accent), {t["series"][4]}) !important;
}}
/* Icons are a ligature font Streamlit ships locally ("Material Symbols Rounded").
   The broad font rule at the top of this sheet matches their `st-` classes too, and
   without this override the browser paints the ligature NAME as text
   ("keyboard_arrow_right") - the font itself was never the problem. */
[data-testid="stIconMaterial"], .material-symbols-rounded {{
  font-family: "Material Symbols Rounded" !important;
  font-weight: normal; font-style: normal; letter-spacing: normal;
  text-transform: none; white-space: nowrap; word-wrap: normal; direction: ltr;
  font-feature-settings: "liga"; -webkit-font-feature-settings: "liga";
}}
/* The same broad rule would also put code blocks in the UI font. */
code, pre, kbd, samp, [data-testid="stCode"] code, [data-testid="stCode"] pre {{
  font-family: var(--ds-mono) !important;
}}
[data-testid="stExpander"] summary {{ color: var(--ds-ink); }}
[data-testid="stSidebar"] [data-testid="stButton"] button p {{
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}

/* ---------- LangGraph node tracker ---------- */
.ds-graph {{
  display: flex; flex-wrap: wrap; align-items: center; gap: .3rem;
  margin: 0 0 .65rem; font-size: .7rem;
}}
.ds-graph-label {{
  font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
  color: var(--ds-accent); margin-right: .35rem;
}}
.ds-node {{
  font-family: var(--ds-mono); padding: .14rem .5rem; border-radius: 99px;
  border: 1px solid var(--ds-border); color: var(--ds-ink-3); background: var(--ds-sunken);
}}
.ds-node.is-done {{ color: var(--ds-good); border-color: color-mix(in srgb, var(--ds-good) 45%, var(--ds-border)); }}
.ds-node.is-live {{
  color: var(--ds-ink); border-color: var(--ds-accent); background: color-mix(in srgb, var(--ds-accent) 16%, var(--ds-raised));
  animation: ds-pulse 1.6s ease-in-out infinite;
}}
.ds-node-par {{ display: inline-flex; flex-direction: column; gap: .2rem; }}
.ds-node-arrow {{ color: var(--ds-ink-3); }}

/* ---------- Prose with inline citations ---------- */
.ds-prose {{ font-size: .9rem; color: var(--ds-ink-2); line-height: 1.68; }}
.ds-prose p {{ margin: 0 0 .85rem; }}
.ds-prose b {{ color: var(--ds-ink); }}
.ds-prose .ds-handle {{ font-size: .68rem; padding: .05rem .3rem; margin: 0 .05rem; }}

/* ---------- Setup card ---------- */
.ds-setup {{
  border: 1px solid color-mix(in srgb, var(--ds-warning) 45%, var(--ds-border));
  background: linear-gradient(135deg, color-mix(in srgb, var(--ds-warning) 9%, var(--ds-raised)), var(--ds-raised));
  border-radius: 14px; padding: 1rem 1.15rem; margin: 1rem 0 .4rem;
  font-size: .84rem; line-height: 1.6; color: var(--ds-ink-2);
  animation: ds-rise .5s var(--ds-ease) backwards;
}}
.ds-setup b {{ color: var(--ds-ink); }}
.ds-setup code {{
  font-size: .78rem; padding: .08rem .35rem; border-radius: 5px;
  background: var(--ds-inset); color: var(--ds-ink);
}}
.ds-setup ol {{ margin: .45rem 0 0; padding-left: 1.2rem; }}
.ds-setup li {{ margin-bottom: .2rem; }}

/* ---------- Small screens ---------- */
@media (max-width: 640px) {{
  .block-container {{ padding-left: 1rem; padding-right: 1rem; }}
  .ds-welcome {{ padding: 1.3rem 1.2rem 1.4rem; }}
  .ds-welcome-mark {{ font-size: 2.3rem; }}
  .ds-masthead-meta {{ gap: 1rem; flex-wrap: wrap; }}
  .ds-masthead-meta > div {{ text-align: left; }}
  .ds-kpi-value {{ font-size: 1.7rem; }}
}}
</style>
"""


def inject() -> None:
    """Install the stylesheet.

    `st.html` injects the element as-is. `st.markdown(..., unsafe_allow_html=True)`
    must not be used here: markdown terminates a raw-HTML block at the first blank
    line, so a multi-section stylesheet leaks onto the page as visible text.
    """
    css = stylesheet()
    try:
        st.html(css)
    except AttributeError:  # pragma: no cover - very old Streamlit
        st.markdown(_collapse_blank_lines(css), unsafe_allow_html=True)


def _collapse_blank_lines(css: str) -> str:
    """Markdown-safe fallback: an HTML block with no blank lines stays one block."""
    return "\n".join(line for line in css.splitlines() if line.strip())


# --------------------------------------------------------------------------- #
# Plotly template
# --------------------------------------------------------------------------- #

def plotly_layout(height: int = 300, *, showlegend: bool = False) -> dict[str, Any]:
    """Shared chart chrome: recessive grid and axes, ink-coloured text."""
    t = tokens()
    family = FONT_STACK.replace('"', "")
    return {
        "height": height,
        "margin": {"l": 8, "r": 18, "t": 30, "b": 8},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {"family": family, "size": 12, "color": t["text_secondary"]},
        "showlegend": showlegend,
        "legend": {
            "orientation": "h", "yanchor": "bottom", "y": 1.03, "x": 0,
            "font": {"size": 11, "color": t["text_secondary"]},
            "bgcolor": "rgba(0,0,0,0)",
        },
        "xaxis": {
            "gridcolor": t["grid"], "linecolor": t["border"], "zerolinecolor": t["border"],
            "tickfont": {"size": 11, "color": t["text_muted"]},
            "title": {"font": {"size": 11, "color": t["text_muted"]}},
        },
        "yaxis": {
            "gridcolor": t["grid"], "linecolor": t["border"], "zerolinecolor": t["border"],
            "tickfont": {"size": 11, "color": t["text_muted"]},
            "title": {"font": {"size": 11, "color": t["text_muted"]}},
        },
        "hoverlabel": {
            "bgcolor": t["surface_raised"], "bordercolor": t["border_strong"],
            "font": {"size": 12, "color": t["text_primary"], "family": family},
        },
        "bargap": 0.32,
        "transition": {"duration": 350, "easing": "cubic-in-out"},
    }
