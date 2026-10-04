# DrugScope

**Evidence-grade pharmaceutical research intelligence.** Ask a question about a drug,
a disease or a treatment comparison. DrugScope queries the primary research
databases, appraises every record it finds, and returns a structured,
source-backed report with a full citation trail.

---

## The one thing that makes this different

**The model never supplies a reference.**

Evidence is retrieved by deterministic HTTP calls to nine research APIs. Each
retrieved record is minted a citation handle — `[S4]`, `[T12]`, `[R2]` — before any
analysis happens. The model can only ever *cite* a handle that retrieval created, and
after synthesis a **citation audit** verifies every handle against the corpus and
strips any that does not resolve, reporting each removal in the run diagnostics.

A hallucinated PMID cannot reach the report, because the report's references are not
generated — they are the retrieval log.

The same principle governs the numbers. Every KPI, index and chart series is computed
arithmetically in `analysis/metrics.py` from the structured appraisals. The model
classifies and explains; code counts. Two runs over the same corpus produce the same
Evidence Quality Index.

---

## Providers

DrugScope is provider-agnostic. Set one key in `.env` and it picks that provider
automatically; the sidebar switches model without a restart.

| Provider | Key | Notes |
|---|---|---|
| **OpenRouter** | `OPENROUTER_API_KEY` | Gateway to many models including free ones. The sidebar reads OpenRouter's live free-model list, because free models are retired every few months |
| **Anthropic / Claude** | `ANTHROPIC_API_KEY` | Full capability: one appraisal call per record, adaptive thinking, server-side web search. Claude Opus 5.5 by default |
| **OpenAI / GPT** | `OPENAI_API_KEY` | `gpt-4o-mini` by default |

A key in the project's `.env` wins over one inherited from the machine environment,
and `DRUGSCOPE_PROVIDER` forces a choice when several keys exist.

The pipeline never branches on provider &mdash; it asks the backend what it
supports and adapts. On a rate-limited free model that means records are appraised
in **batches** rather than one call each (a daily quota would otherwise be spent on
a single run), JSON schema moves into the prompt, concurrency drops, and the web
sweep switches off with a note explaining why. Adding a fourth provider is a row in
`GATEWAYS`, not a new code path.

## Your own documents (RAG)

Upload PDFs, text, Markdown or CSV in the sidebar under **Your documents**. Each
file is chunked with overlap, ranked against your question with BM25-style lexical
scoring, and folded into the same corpus as everything retrieved &mdash; appraised
record by record and citable by handle (`[D3]`).

There is deliberately **no vector database**. At this scale retrieval quality is
decided by chunking and ranking, not embedding sophistication, and a lexical score
is instant, has no cold start, and can be explained to a reviewer. Uploaded
excerpts are graded as unreviewed evidence, so the report never implies a user's
memo carries the weight of a published trial. `sources/documents.py` is the one
module to swap if document volume ever outgrows this.

## Is this an agent? Is it LangGraph?

It is an agent in the sense that matters &mdash; it plans, retrieves, appraises and
synthesises autonomously &mdash; and it **runs on LangGraph**: `pipeline.build_graph()`
compiles the stages into a `StateGraph` with typed state, and the run streams
progress out of each node as it works. During a run the UI shows the graph's nodes
lighting up in turn.

```
START -> plan -> retrieve -+-> appraise --+-> measure -> synthesise -> audit -> END
                           +-> sweep -----+   (sweep only when enabled)
```

**Accuracy checks on every report.** Drug names in the plan are resolved to their
RxNorm active ingredient before any database is queried, so a brand name or a typo
in a free-form question ("metformn", "Ozempic") finds the same records as the
generic name. After synthesis, every citation is verified against the corpus, and
every figure in a key finding is checked against the text of the sources it cites:
a figure not found there (often a fair derivation, such as a relative reduction
computed from a hazard ratio) is flagged on the finding.

**Speed.** On OpenRouter, hidden model reasoning is matched to the job — off for
bulk appraisal, higher for synthesis (one appraisal measured 24.7 s with default
reasoning and 4.9 s without) — and four calls run at once. A Scan typically
completes in 3–4 minutes on a free model; the run panel shows the sources found as
soon as retrieval finishes, plus a rough time remaining.

What it deliberately is *not* is a model-driven control loop. The graph's edges are
fixed in code; its one branch (whether the web sweep runs alongside appraisal) is
decided by configuration, never by the model; and no node hands the model a
retrieval tool. That is the decision the product rests on: an agent that chooses its
own retrieval cannot guarantee that a citation corresponds to a document that was
actually fetched. Here the model can only ever *cite* a handle an HTTP call minted,
and the `audit` node strips anything it invents. You also get honest progress
percentages and reproducible indices.

## Quick start

**Windows: double-click `run.bat`.** The first time, it creates the Python
environment and installs everything; after that it starts straight away and opens
your browser at http://localhost:8501.

Or by hand:

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

copy .env.example .env          # cp on macOS/Linux — then add your key
streamlit run app.py
```

You need **one** provider key in `.env` (see the table above). Every research
database is open and keyless. To use the moderation and admin tools, also set
`DRUGSCOPE_ADMIN_USERNAME` and `DRUGSCOPE_ADMIN_PASSWORD` (see *Accounts* below).

`NCBI_API_KEY` is **not required** — it only lifts the PubMed rate limit from ~3 to
~10 requests/second. Without it PubMed still works, and Europe PMC covers the
literature stream regardless.

---

## What it produces

| Section | What you get |
|---|---|
| **Overview** | A committed one-sentence verdict, a 3–5 paragraph briefing, and 5–9 key findings each graded for evidence strength and individually cited |
| **Evidence** | Publication volume over time, evidence-tier and study-design mix, and a coverage map showing where the corpus has nothing to say |
| **Trials** | Phase distribution, recruitment status, research momentum, and the full trial register with enrolment, sponsor and results status |
| **Regulatory & safety** | Approval history, boxed warnings, discrete safety signals, and FAERS reporting — carrying the caveat that those counts are reporting volume, not incidence |
| **Comparison** | A head-to-head matrix across the criteria a formulary committee would ask about, with `Not established` wherever the corpus is silent |
| **Conflicts** | Contested topics found by arithmetic, then adjudicated: which side the stronger evidence favours, and the methodological reason the results diverge |
| **Gaps** | Unanswered questions, each with the study that would close it — design, population, endpoint, duration |
| **Sources** | Every record, citable and linked, with its per-record appraisal |
| **Export** | Markdown, structured JSON, and a CSV source register — all three carrying the methodology note |

### Three analytics worth understanding

**Evidence Quality Index (0–100).** A weighted composite over the retrieved papers and
trials: best available design (30), depth of top-tier evidence (25), human
non-preclinical evidence (15), recency (15), corroboration (15). It rates the evidence
*base*, not any single study, and it saturates — the sixth randomised trial adds less
than the second. The Overview tab shows the per-component breakdown, so the score is
arguable rather than oracular.

**Consensus Index (0–100).** For every topic carrying at least two directional claims,
the share held by the majority direction, weighted by claim volume. Topics under 70%
agreement are flagged contested and handed to the synthesis stage as conflicts it must
address — so the model cannot quietly fail to notice an inconvenient disagreement.

**Coverage map.** Claims cross-tabulated by dimension against evidence tier. The empty
cells are the finding: gap analysis starts from arithmetic, not from a model's
impression of what might be missing.

---

## Community research

Alongside the evidence reports, DrugScope collects structured, consented experiences
from its own users. Four kinds of information appear on these pages and are always
labelled separately: **official label information** (quoted from US prescribing
information), **official database reports** (FAERS counts), **community
experiences**, and **unverified research hypotheses**.

Navigation is a sidebar workspace: **Research, Insights, Community, Food ↔ drug,
Drug ↔ drug, Report a side effect**, then **Plans & access, Account** and — for the
administrator only — **Admin console**.

| Page | What it does |
|---|---|
| **Insights** | Exploratory analysis of approved reports: how many people voted and how they split, most reported symptoms and medications, severity, time to onset, once vs repeatedly, report types, weekly activity — filterable by medication |
| **Community** | Approved drug → symptom, drug + food and drug + drug entries, aggregated. "I've experienced this too" / "I haven't" with counts and the respondent split, sorting, FAERS volume for comparison, and **➕ Report** buttons |
| **Food ↔ drug** | *Drug → Food*: quotes what the US label says about each tracked food. *Food → Drug*: medications whose US labels mention that food. Community reports, hypotheses, and **➕ Report a food interaction** |
| **Drug ↔ drug** | Two medications: what **each** US label says about the other — named directly, or by a class it belongs to (classes from the FDA's structured labels via RxClass) — FAERS reports listing both with their most common reactions, community reports and voting, and **➕ Report a drug interaction** |
| **Report a side effect** | Medication (brand names resolve to the ingredient through RxNorm, or **➕ add** an unlisted one), symptoms (**➕ Suggest a new symptom** if missing, with near-duplicates shown first), timing, severity, frequency, optional food and context |
| **Plans & access** | Free / Pro / Team pricing cards, your credits and usage history, upgrade buttons, and upgrade-code redemption |
| **Account** | Sign in or sign up, your own reports (with delete), change password, delete account |
| **Admin console** | Administrators only — see below |

**Privacy by design.** No email, phone or real name is collected; emails, phone
numbers and links typed into free text are stripped before saving. New reports are
`pending` until an administrator approves them, and the public only ever sees
aggregated counts per drug-symptom or drug-food pair — never an individual report or
its free text. Votes attach to those aggregated entries, one per account
(`UNIQUE(user_id, entry_id)` in the database). Users can delete any report or their
whole account. Free-text context is cleared after `DRUGSCOPE_CONTEXT_RETENTION_DAYS`.

**Counts are not risk.** Reports and votes come from self-selected people. They are
never shown as rates and never turned into a risk score, and every page says so.

### Accounts, roles and security

* Passwords are salted **scrypt** hashes; five failed sign-ins lock a username for 15 minutes.
* The **administrator** is created from `.env` (`DRUGSCOPE_ADMIN_USERNAME` /
  `DRUGSCOPE_ADMIN_PASSWORD`) every time the app starts — nothing is hard-coded, and
  that username cannot be claimed through sign-up.
* **Permissions are enforced in the backend**, not by hiding pages. Every function in
  `community/service.py` and `community/quota.py` re-reads the caller's role from the
  database; the browser never touches the database. Demoting or deactivating an
  account takes effect on its next click.
* All SQL is parameterised; CSV exports defuse spreadsheet formulas; report,
  suggestion and vote actions are rate-limited per account; every administrative
  action and export is written to an audit log.

### Quotas and upgrades

Research runs cost credits — **Scan 1, Standard 2, Deep 4** — from an allowance that
refills over a **rolling window**: Free 5, Pro 60 and Team 200 per 24 hours (all
configurable, including e.g. a 5-hour window, and the displayed prices). The cost is
on the Run button and the remaining balance under the search box. When credits run
out the run is blocked server-side, the page says exactly when the next credit comes
back, and offers the upgrade. A run that fails or is interrupted is refunded.
Administrators are never charged — use a regular test account to see the quota work.
Community features are always free.

Upgrades are fulfilled with **single-use upgrade codes**: the administrator issues
them in *Admin → Users & plans* (stored only as hashes) and a user redeems one on the
Account page, which grants or extends Pro. Set `DRUGSCOPE_UPGRADE_URL` to a payment
link (for example a Stripe Payment Link) and the *Upgrade to Pro* button sends people
there; after payment you send them a code. Fully automatic card billing needs your
own payment-provider account and a webhook endpoint, which Streamlit cannot host.

### Admin workspace

Overview KPIs (reports, pending moderation, unique medications/foods/symptoms/pairs,
votes, users), reports over time, frequent drug-symptom, food-drug and drug-drug
pairs — always with sample size and reporting period. **Insights (EDA)** — the same
analysis as the public page but over every report, plus moderation status, accounts
by plan and credits used per day. **Moderation** (approve, reject, flag,
delete, with the raw report), **Categories** (approve, merge or reject suggestions;
the submitted wording is kept for audit), **Data & export** (filter by medication,
symptom, food, status, type and date; CSV export), **Hypotheses** (draft, track and
publish), **Users & plans** (grant Pro, issue codes, deactivate), **Evidence**
(imported official records with source URL and retrieval time) and the **Audit log**.

### Official sources used by the community pages

openFDA drug labels (food mentions in interaction and patient sections), openFDA
FAERS (drug + reaction report counts), DailyMed (current label documents) and RxNorm
(brand → ingredient). Results are cached in `evidence_sources` for seven days with
their source URL and retrieval time, kept apart from community data, and never
overwrite it. An outage or rate limit shows a notice and the rest of the page keeps
working. **Coverage is US labelling** — products sold in the UAE or elsewhere can be
labelled differently.

### Before collecting real users' data

`run.bat` starts the app on `localhost` only, so nobody else on your network can reach it. Before
opening it to other people: deploy behind HTTPS, set `client.showErrorDetails` to
`"none"`, review the privacy and health-data law that applies where you operate
(in the UAE that includes the federal personal-data and health-data laws), publish a
privacy notice, and keep the service adults-only. Sign-in lasts for the browser
session; a page refresh signs you out.

---

## How a run works

```
1. PLAN        1 call    Question -> boolean PubMed queries, entities, aliases, outcomes
2. RETRIEVE    0 calls   Every query against every enabled stream, concurrently
3. APPRAISE    N calls   One call per record, in parallel, + the web sweep alongside
4. MEASURE     0 calls   Deterministic analytics over the appraised corpus
5. SYNTHESISE  1 call    One pass over the structured digest
6. AUDIT       0 calls   Strip unresolvable citations, assemble the report
```

See *Is this an agent?* above for why the control flow is fixed rather than
model-driven. Stage 3's call count depends on the provider: one per record on
Anthropic, batched on a rate-limited gateway.

**Why one call per record** rather than one call over the whole corpus: a 60-paper
corpus in a single prompt gets skimmed, and effect sizes go missing. One abstract per
call gets read. It also means each claim is attributable to its record by
construction, and a glowing industry-sponsored trial cannot colour the reading of the
next abstract.

### Research depth

| Depth | Papers | Trials | Effort | Web sweep | Typical |
|---|---|---|---|---|---|
| Scan | 12 | 15 | medium | off | 1–2 min |
| Standard | 28 | 40 | high | on | 3–5 min |
| Deep | 60 | 80 | max | on | 8–15 min |

The sidebar shows the live token and cost meter for every run.

---

## Data sources

| Source | What it contributes |
|---|---|
| **Europe PMC** | Literature, including preprints and European journals |
| **PubMed** (NCBI E-utilities) | Literature with MeSH indexing and publication types |
| **ClinicalTrials.gov** (v2) | Trial phase, status, enrolment, sponsor, dates, results |
| **Drugs@FDA** | Approval record and full submission history |
| **FDA labels** | Approved indications, boxed warnings, mechanism |
| **openFDA FAERS** | Aggregate spontaneous adverse-event reporting |
| **FDA enforcement** | Recalls and their classification |
| **ChEMBL** | Curated mechanism of action, target, development stage |
| **RxNorm / RxClass** | Drug normalisation and ATC/pharmacologic class |
| **Web search** | Recent developments, restricted to ~30 trusted domains |

Both literature indexes are queried because their coverage differs at the edges, and
results are reconciled on DOI, then PMID, then normalised title.

---

## Architecture

```
run.bat                    Double-click launcher (Windows)
app.py                     Streamlit front end and page navigation — owns no analysis
drugscope/
  config.py                Models, depth profiles, rate limits, credentials
  models.py                Typed contracts. Retrieval types are dataclasses built
                           by code; analysis types are Pydantic schemas the model fills
  llm.py                   Async Messages API wrapper: structured output, streaming,
                           adaptive thinking, cost accounting, capability degradation
  providers.py             OpenRouter / OpenAI backend and the live free-model catalogue
  pipeline.py              The stages as a LangGraph StateGraph, streamed as progress events
  community/
    db.py                  SQLite schema, versioned migrations, seed vocabulary
    auth.py                Accounts, scrypt passwords, lockout, admin bootstrap
    quota.py               Plans, credits per rolling window, upgrade codes
    service.py             Reports, aggregates, votes, moderation, export, audit log
    sources.py             openFDA labels/FAERS, DailyMed, RxNorm — cached, fail-soft
  export.py                Markdown / JSON / CSV, each with the methodology note
  sources/
    base.py                Per-host throttling, retry, response cache, text cleaning
    literature.py          Europe PMC + PubMed, with deterministic design classification
    trials.py              ClinicalTrials.gov v2, with importance-weighted ranking
    regulatory.py          Drugs@FDA, labels, FAERS, enforcement
    pharmacology.py        ChEMBL + RxNorm
  analysis/
    prompts.py             The shared persona and the eight non-negotiable rules
    plan.py                Stage 1 — query construction
    extract.py             Stage 2 — per-record appraisal
    synthesize.py          Stage 3 — synthesis + the citation audit
    metrics.py             All deterministic analytics
    websweep.py            The recent-developments sweep
  ui/
    theme.py               Design tokens and the stylesheet — the whole design system
    charts.py              Plotly charts
    components.py          KPI cards, badges, evidence cards, timeline, conflict panels
    account.py             Sign-in, account page, credits meter, upgrade panel
    community.py           Food ↔ drug, report, and community pages
    admin.py               The administrator's private workspace
tests/                     pytest: community backend, UI (AppTest), LangGraph pipeline, sources
```

Run the tests with `.venv\Scripts\python -m pip install -r requirements-dev.txt`, then
`.venv\Scripts\python -m pytest tests` (58 tests, under a minute, no API keys needed).

`analysis/`, `sources/` and `pipeline.py` never import anything under `ui/`. The engine
is headless, so the same pipeline drives a CLI, an API endpoint or a scheduled report —
Streamlit is one front end, not the product.

### Design system

`ui/theme.py` holds the complete palette, and it is **validated rather than chosen by
eye**: the categorical slots were checked for colour-blind separation and
surface contrast in both modes. Two constraints from that validation are enforced in
code, not left to discipline:

- Comparison views cap at **three** categorical hues, because only the first three
  slots clear colour-blind separation when every pair can appear together. A fourth
  option goes to the table view rather than getting a fourth colour.
- The slots below 3:1 contrast on the light surface carry the **relief rule** —
  visible direct value labels *and* a table view. Every chart goes through
  `charts.chart_with_table`, which is how that obligation is met.

Dark mode is a *selected* set of steps for the dark surface, not an inverted light
palette. To rebrand: replace the token values in `theme.py` and re-run the palette
validator. Nothing else changes.

---

## Limitations — read these

DrugScope is **research intelligence for qualified professionals**. It is not medical
advice, not a treatment recommendation, and not a substitute for the approved product
label or a prescriber's judgement.

- **Retrieval is a sample, not a systematic review.** Query wording changes what is
  found. A record absent from a corpus is not evidence of absence.
- **Appraisal reads abstracts and registry records, not full texts.** Methodological
  flaws visible only in a full paper are missed.
- **Publication bias is not corrected for.** Negative and null results are
  systematically under-represented in the literature these corpora are drawn from.
- **FAERS counts are reporting volume, not incidence.** No exposure denominator,
  voluntary and uneven reporting, and a report is not a finding of causation. They
  cannot be compared between drugs of different market size.
- **Regulatory coverage is US FDA only.** EMA, MHRA and PMDA are not queried.
- **A high Consensus Index is not proof of correctness.** It can equally mean the
  corpus is too small or too homogeneous to disagree with itself — which is why the
  report says so when it finds no conflicts.

Every export carries this list. That is deliberate: a report that travels without its
limitations is a liability.

---

## Configuration

On Anthropic, planning and synthesis run on **Claude Opus 5.5**. Appraisal is one
call per retrieved record, so it is the one stage where a cheaper worker model is a
defensible trade — it is exposed in the sidebar and via `DRUGSCOPE_EXTRACTION_MODEL`
rather than chosen for you. On OpenRouter set `OPENROUTER_MODEL`, or pick a model in
the sidebar. See `.env.example` for every variable, including the admin account,
quotas, upgrade link, retention period and database location.
