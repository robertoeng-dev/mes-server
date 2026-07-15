# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Django dashboard for a factory production line (Salcomp, Manaus) that shows
real-time test/yield data — First Pass Yield, PASS/FAIL counts, hourly UPH
trend, failure pareto, and per-channel/per-carrier failure matrices. It reads
from a Postgres table (`mes_test_results`) that is populated by an external
system; this app is **read-only** against that table (no writes, no models for
it, no migrations for it).

Published on GitHub: **https://github.com/robertoeng-dev/mes-server** (public,
branch `main`). Sibling project (the client that writes `mes_test_results`,
see "Data quality" below) is at **https://github.com/robertoeng-dev/mes-client**.
Local git identity is set per-repo to `Roberto Parente <robertotec.eng3@gmail.com>`
(matches the GitHub account `robertoeng-dev`). `.claude/settings.local.json` and
`.claude/scheduled_tasks.lock` are gitignored (machine-local, not shared config);
`.claude/skills/` is tracked. The factory's real internal IP is deliberately
kept out of every tracked file in this repo (see "Data quality" — it was
present in this file and `start_production.bat` before the first push and was
generalized to "host set in .env").

## Commands

```powershell
# install deps (first time)
venv\Scripts\pip install -r requirements.txt

# run against the FACTORY database (Salcomp corporate network — host set in .env)
start_production.bat
# equivalent manually:
set DJANGO_ENV=production
venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000

# run against your LOCAL/DEV database
start_local.bat
# equivalent manually:
set DJANGO_ENV=local
venv\Scripts\python.exe manage.py runserver

# Django checks (no DB required)
venv\Scripts\python.exe manage.py check
```

There is no test suite (`dashboard/tests.py` is the default empty stub) and no
build/lint step — the frontend is plain HTML/CSS/JS served as Django static
files, no bundler.

## Two environments (production vs. local dev)

`mes_server/settings.py` picks which env file to load based on `DJANGO_ENV`:

| `DJANGO_ENV` | env file | Database |
|---|---|---|
| `production` (default, i.e. unset) | `.env` | Salcomp factory Postgres (host set in `.env`) — **real production data** |
| `local` | `.env.local` | Whatever Postgres you point it at — for development only |

Both `.env` and `.env.local` are gitignored. `.env` already has the factory
credentials filled in — **never edit it for local testing**, use `.env.local`
instead (edit `DB_HOST`/`DB_USER`/`DB_PASSWORD`/`DB_NAME` there to point at
your own Postgres). `DB_ENGINE` is also configurable (defaults to
`postgresql`) if you ever need a quick sqlite smoke test of the page shell —
see the note on Postgres-only SQL below before relying on that for anything
beyond checking the HTML renders.

`start_local.bat` / `start_production.bat` just set `DJANGO_ENV` and launch
`runserver` — use them instead of remembering the env var. Production binds to
`0.0.0.0:8000` (reachable from other machines on the corporate network, e.g.
the shop-floor display); local binds to the default `127.0.0.1:8000`.

This repo appears to be a working copy of a project that also lives at
`C:\MES_Server` on a factory machine (see `.claude/settings.local.json` for
prior session commands referencing that path) — check there for the canonical
factory checkout before assuming this copy is the one to deploy from.

## Architecture

**No ORM models for the actual data.** `dashboard/models.py` is empty.
Everything in `dashboard/services.py` is hand-written SQL via
`django.db.connection.cursor()` against a table named `mes_test_results`
(`TABLE_NAME` constant), because that table's schema isn't owned by this app.

**Schema normalization layer.** The test-station data varies in column naming
across sources (`row_data`/`raw_data` JSON blobs with keys like `Station` vs
`station_id` vs `machine_no`, `TestResult` vs `result` vs `status`, etc.).
`services.get_exprs()` builds a dict of SQL expressions (`station`, `model`,
`result`, `event_time`, `failure`, `channel`, `channel_no`, `carrier`) that
`COALESCE` across every known key variant, wrapped once per request. Every
other function in `services.py` (`summary`, `top_failures`, `hourly_yield`,
`channel_summary`, `failure_channel_matrix`, `carrier_channel_matrix`,
`debug_info`, `distinct_filters`) builds its SQL from these expressions rather
than hardcoding column names — **when adding a query, reuse `get_exprs()`,
don't hardcode a column/JSON key.**

This means the SQL is Postgres-specific (`FILTER (WHERE ...)`, `->>` JSON
operators, `regexp_replace`, `TO_TIMESTAMP`, `date_trunc`, `generate_series`,
`::type` casts). It will not run against sqlite — don't try to make sqlite a
real substitute for Postgres here, it's only useful for exercising the HTML
shell with an empty/mocked API layer.

## Data quality: source data has known corruption, handled defensively

`mes_test_results` is written by an external client program,
`D:\MES_Client_Complete` (a separate Tkinter tray app on the factory PC —
tails tester CSVs and batch-inserts; **not** part of this repo). Real ingested
data was found to contain: (1) a couple of trailing rows of binary garbage
from a truncated CSV tail, (2) a systematic bug where the header/label row's
own text (`"Station"`, `"Material"`, `"Model"`, `"PN"`) got stored as the
*value* in every data row for those keys, and (3) an unrelated batch of rows
from a different test schema (BMU testing: `bmu_sn`, `"Station ID"`,
`SerialNumber`) that this app's normalization doesn't recognize (left as
NULL station/model — intentionally out of scope, not a bug to "fix" here).

**Dashboard-side defenses (this repo, already applied):** `get_exprs()`'s
`station`/`model` COALESCE branches use a `json_text_clean()` helper that
`NULLIF`s known bad literal values (e.g. `Station`, `Material`) so the next
COALESCE branch wins instead of surfacing the header text — this is why
`machine_no` (not `Station`) is what actually resolves as the station name.
`distinct_filters()` additionally gates every dropdown value through
`SANE_FILTER_VALUE_RE` (alnum + limited punctuation, ≤64 chars) so binary
garbage can't pollute the station/model/carrier/failure dropdowns — this gate
is applied **only** to the dropdown-listing queries, never to
`summary()`/aggregates, so reported totals stay exact even with a couple of
garbage rows still physically in the table.

**Client-side fix (in `D:\MES_Client_Complete`, outside this repo, applied
2026-07-13):** `parser/cyg_parser.py` now rejects (before insert, with a
logged reason) rows that are a repeated header, have too few populated
fields, have too many columns, or contain binary-garbage characters
(`errors="ignore"` was also changed to `errors="replace"` so corruption
becomes detectable U+FFFD instead of silently vanishing). Verified with a
standalone synthetic test (`tests/test_parser_validation.py`, run via
`python tests\test_parser_validation.py`, no DB needed) — **not yet**
verified against real production CSVs (`tests/regression_real_files.py` is
ready but the real files only exist on the factory PC) and **not yet**
repackaged with PyInstaller (`parser_TE.spec`) — the `dist/` executable
currently deployed still predates this fix. Both remain open TODOs before
this fix reaches the factory floor.

**Views are thin.** `dashboard/views.py` just pulls filters from the request
(`services.get_filters`) and returns `JsonResponse(services.<fn>(filters))`.
All the logic lives in `services.py`.

**API surface** (`dashboard/urls.py`): `/` (the HTML shell),
`/api/summary/`, `/api/top-failures/` (accepts `?limit=` 1-20, default 10),
`/api/channels/`, `/api/hourly-yield/`, `/api/channel-hourly/` (yield per
channel per hour — feeds intermittency detection), `/api/channel-matrix/`,
`/api/carrier-matrix/`, `/api/carrier-cycles/` (carrier lifecycle counter),
`/api/carriers/reset/` and `/api/carriers/limit/` (POST, mutate
`dashboard_carriers` — see below), `/api/debug/` (row counts + first/last
timestamps, used for the online/offline banner), `/api/filters/` (distinct
station/model/carrier/failure values for dropdowns). Filters — `station`,
`model`, `date_from`, `date_to`, `channel`, `carrier`, `failure` — are passed
as query params on every read endpoint and applied via `services.build_where()`.

**`dashboard_carriers` is the one table this app owns and writes to.**
Everything else in this file about read-only/`mes_test_results` still holds —
this is a separate table, created lazily by `services.ensure_carrier_table()`
(`CREATE TABLE IF NOT EXISTS`, no Django migration) to track carrier
lifecycle: `cycle_limit` (per-carrier override of the global setting) and
`baseline_at` (timestamp from which cycles are counted — reset to `NOW()`
when a carrier is physically replaced, via `services.reset_carrier()`).
`api/carriers/reset/` and `api/carriers/limit/` are `@csrf_exempt` (no
session/login on this dashboard) — acceptable since they only ever touch this
one dashboard-owned table, never `mes_test_results`.

**Carrier field: use `barcode`, never `device_name`.** `get_exprs()`'s
`carrier` expression intentionally excludes `device_name` — that key holds
the **fixture's own serial** (one fixed value per physical channel slot,
`PT30...`), not the carrier being tested. The real carrier ID (scanned
barcode, pattern like `A06T10`/`A17RT1` for retests) lives in `row_data->>'barcode'`.
This was a real bug found and fixed in this table — don't reintroduce
`device_name` into the carrier COALESCE.

**Carrier normalization also handles two barcode-scanner data-quality issues
(2026-07-14):** (1) `services.dedupe_doubled_scan()` collapses a
double-triggered scanner read (`'A06T01A06T01'` → `'A06T01'`, detected by
the value being exactly two identical halves concatenated) so the same
physical carrier doesn't fragment into a phantom second "carrier" in the
dropdowns/matrix; (2) rows with real test result data (`result IS NOT NULL`)
but an empty barcode are labeled `'SEM BARCODE'` instead of falling all the
way to the generic `'N/A'` — `carrier_channel_matrix()`'s WHERE clause
excludes `''`/`'N/A'` but **not** `'SEM BARCODE'`, so this failure data stays
visible instead of silently vanishing from the matrix (on the local dev
dataset this surfaced 2,733 previously-hidden failures). **Landmine already
hit once:** `dedupe_doubled_scan()`'s SQL uses the modulo operator (`% 2`)
inline in an f-string that gets passed to `cursor.execute(sql, params)` —
psycopg2 does %-style substitution on the query text whenever a `params`
argument is given, so a literal `%` must be escaped as `%%` or it miscounts
placeholders against `params` and throws `IndexError: list index out of
range` (only reproduces once the surrounding query has other `%s` params —
a bare `cursor.execute(sql)` with no params never triggers it, which is why
an isolated smoke test without params can pass while the real endpoint
500s). Same category of bug as the SPC parameter-ordering landmine below —
check parameter/placeholder alignment carefully whenever composing raw SQL
fragments that end up inside a parameterized `cursor.execute()`.

**"Cycle" = one physical pass of a carrier through the tester**, detected by
session-gapping timestamps per carrier: a burst of ~20 rows arriving within
`CYCLE_GAP_SECONDS` (30s, tuned against real data — 60s over-merged
back-to-back retest passes) of each other is one cycle; a gap larger than
that starts a new one. See `services.carrier_cycles()`.

**Frontend is a single page**, no framework: `dashboard/templates/dashboard/index.html`
+ `static/dashboard/css/style.css` + `static/dashboard/js/dashboard.js`, charts
via Chart.js (CDN) + chartjs-plugin-datalabels. Static assets are cache-busted
with a `?v=YYYYMMDD_NN` query string in `index.html` on **both** the CSS and
JS `<link>`/`<script>` tags — **bump both together whenever you change either
file** (currently `20260715_01`), since the browser/dashboard PC otherwise
caches the old file indefinitely.

Layout is a single CSS Grid (`.grid` in `style.css`, now a 10-column grid)
laid out via `grid-template-areas` (`pareto`/`uph`/`channels`/`matrix1`/`matrix2`)
sized to fit one screen with no page scroll (`html, body { overflow: hidden }`)
— only the matrix tables (`.table-wrap`) scroll internally if they have more
rows than fit (matrix `<table>`s use `table-layout: auto` so the first column
auto-sizes to the longest failure/carrier name — don't reintroduce
`table-layout: fixed` there, it was the cause of truncated names like
`DCI...`). If you add a new panel, it needs its own grid-area and a
row-height entry in `.grid`'s `grid-template-rows`, or it'll break the
no-scroll layout. Every `.panel` also gets an auto-injected "⤢ expand"
button (`wirePanelExpand()` in `dashboard.js`) that fullscreens it in place
(Streamlit-style) — no per-panel markup needed for that.

**Client-side config, persisted in the browser's `localStorage`**
(`CONFIG_KEY = "mesDashboardConfig"`, see `DEFAULT_CONFIG` in `dashboard.js`):
`productionGoal`, `uphYMax`, `hotLimit`, `refreshSeconds`,
`onlineThresholdMinutes`, `paretoTopN`, `yieldYellow`/`yieldRed` (channel
health thresholds), `carrierCycleLimit` (global default, overridable per
carrier in `dashboard_carriers`), `tvMode`. Editable via the ⚙ settings modal
(`openSettings()`/`saveSettings()`). This is **per-browser/per-PC** state, not
shared across machines — the shop-floor TV and an engineer's laptop can have
different settings.

**Two operating modes** — `appMode`, persisted in `localStorage` under
`mesDashboardMode` (not the config blob): **ONLINE** (default; sidebar
hidden, date range auto-set to 05:00→now via `onlineAutoDates()`, anchors to
last-received data if the line's been offline past `onlineThresholdMinutes`)
and **ANÁLISE** (sidebar open, all filters live — station/model/channel/
carrier/step/dates — nothing auto-resets). Toggle via the ONLINE/ANÁLISE
buttons in `.status-strip`; `switchMode()` clears the investigation-only
filters (channel/carrier/step) when returning to ONLINE. The two failure
matrices additionally have their own independent view toggle in ONLINE mode
only (`matrixView`: `"hour"` = last 60 real minutes anchored to
`lastDataTime`, vs `"period"` = same range as the rest of the dashboard) —
see `getMatrixQuery()`.

**Status strip** (`.status-strip`, top of `.main`, added alongside the
original banner — both still exist): `refreshDebugStatus()` runs on every
`loadDashboard()` cycle (not just once), polling `/api/debug/` and updating
an always-visible online/offline pill + "last updated" text live. The
original `checkOnlineStatus()`/`#statusBanner` still runs too, but only once
on page load — it shows a dismissible popup banner (auto-hides after 12s if
online) and is what drives `setDefaultDates()`'s initial offline-anchoring;
the two are complementary, not a replacement of one by the other.

**Channel health chips** (`#channelHealth`, above the CHANNELS chart):
one chip per channel 1-20, colored by `/api/channels/`'s yield vs
`yieldYellow`/`yieldRed` — green/amber/red, gray if no production in range.
**Intermittent detection** overlays a striped amber/red chip when a channel
alternates between critical and normal hours rather than staying uniformly
bad: `computeIntermittency()` in `dashboard.js` pulls `/api/channel-hourly/`,
buckets each hour ≥`MIN_TESTS_PER_HOUR` (5) samples as critical/normal
against `yieldRed`, and flags intermittent when there are ≥3 qualifying
hours, ≥2 transitions, and bad hours are a **minority** (≤70%) — a channel
bad the whole period is "degraded" (solid red), not intermittent. Priority
when rendering a chip: critical > intermittent > degrading > healthy. Tuned
against real per-channel-per-hour data before landing on these thresholds —
don't casually change them without re-checking against a real dataset.

**Carrier cycle counter + auto-alert**: `.carrier-alert` banner (pulses red
at/over the limit, amber at ≥90%) appears above the topbar whenever any
carrier crosses its effective limit (`effectiveCycleLimit()` = carrier's own
`cycle_limit` if set in `dashboard_carriers`, else the global
`carrierCycleLimit` config). Each carrier row in the CARRIER matrix also gets
a small cycle-count badge. Click the alert or the "CICLOS" button on the
carrier panel to open the carrier manager modal (`openCarrierManager()`),
where you can set a per-carrier limit or **"Zerar"** (reset — call this the
moment a carrier is physically replaced; it stamps `baseline_at = NOW()` in
`dashboard_carriers` so old cycles stop counting toward the new physical unit).

## EEData / SPC (parametric distribution / Cp-Cpk)

Ported 2026-07-14 from an earlier, superseded local checkout of this project
(was fully built there, never carried over when the working copy moved).
`services.parametric_distribution(filters, step, usl=None, lsl=None, n_bins=30)`
pulls up to 50k numeric values for an arbitrary JSON key (`step`, e.g. `PACK`,
`DCIR`, `Temperature` — the sidebar's "Step Paramétrico" dropdown), computes a
histogram + mean/std/percentiles + Cp/Cpk via `numpy`/`pandas` (both already
in `requirements.txt`), auto-deriving USL/LSL as μ±3σ when not supplied.
Exposed at `/api/spc/distribution/?step=...&usl=...&lsl=...` (`views.api_spc_distribution`,
400s if `step` is missing), plus the usual `station`/`model`/`date_from`/
`date_to`/`channel`/`carrier`/`failure` filters via `get_filters`/`build_where`
— **an improvement over the ported original**, which predated those extra
filters. Rendered as a fourth modal (`#spcOverlay`, same `.settings-overlay`
pattern as settings/carrier-manager) opened by the sidebar's "Analisar Step"
button (`openSpcPanel()` in `dashboard.js`) — deliberately **not** a grid panel
like the old version's `display:none`-toggled `.panel.full`, since this
dashboard's grid is a strict fixed-height no-scroll layout that a dynamically-shown
panel would have broken. Chart is a `Chart.js` histogram with `chartjs-plugin-annotation`
(new CDN `<script>` tag) drawing USL/LSL/mean lines.

**Landmine already hit once, don't reintroduce it:** `parametric_distribution()`'s
SQL has the JSON-key placeholder (`step`) appearing *before* `where_sql`'s own
placeholders (station/model/dates/etc.) in the query text — `cursor.execute()`
binds positionally in left-to-right order, so the params list must be
`[step] + params + [step, step, numeric_re]`, **not** `params + [step, step, step, numeric_re]`.
The latter (what the original ported code actually had) only "works" when
`where_sql` is empty — i.e. no filters active — and throws a Postgres
`DataError` (a filter value like a date bound to where a JSON key was
expected) the instant any filter is set, which in this dashboard is always
(date range defaults on every load). Verified via Playwright end-to-end
(histogram, annotation lines, Cp/Cpk stats, badge all render) after the fix.

## Carrier normalization: doubled-scan dedup + "SEM BARCODE" (2026-07-15)

Two data-quality fixes layered onto `get_exprs()['carrier']` (in
`services.py`), both aimed at not silently losing real statistics:

1. **`dedupe_doubled_scan(expr)`** collapses a double-triggered barcode
   scanner read — the value being exactly two identical halves concatenated
   (`'A06T01A06T01'` → `'A06T01'`) — so the same physical carrier doesn't
   fragment into a phantom second "carrier" everywhere carrier is grouped
   (dropdowns, matrix, cycle counter). Applied to the raw `barcode` value
   before the rest of the COALESCE chain.
2. Rows with real test result data (`result IS NOT NULL`) but an **empty**
   barcode are labeled `'SEM BARCODE'` instead of falling through to the
   generic `'N/A'` — `carrier_channel_matrix()`'s WHERE clause excludes
   `''`/`'N/A'` but not `'SEM BARCODE'`, so this failure data stays visible
   in the matrix (surfaced 2,733 previously-hidden failures on the local dev
   dataset) instead of vanishing silently.

**Landmine hit AGAIN, same root cause as the SPC one above, different
symptom:** `dedupe_doubled_scan()`'s SQL uses the modulo operator (`% 2`)
inline, which must be escaped as `%%` since psycopg2 does %-style
substitution on the query text whenever `cursor.execute(sql, params)` is
called with a `params` argument. **But** `carrier_cycles()` — which also
embeds `get_exprs()['carrier']` — called `cursor.execute(sql)` with **no**
params argument at all, and psycopg2 only does %-substitution when a params
arg is *provided* (even an empty list/tuple). So the `%%` correctly collapses
to `%` everywhere params are passed, but arrives at Postgres as a literal,
invalid `%%` in `carrier_cycles()`, throwing `ProgrammingError: operador não
existe: integer %% integer`. Fixed by changing that call to
`cursor.execute(sql, [])` — always pass a params arg (even empty) on any
query built from `get_exprs()`, for exactly this reason. Before adding a new
`%` (modulo, LIKE-wildcard, anything) into a SQL fragment shared via
`get_exprs()`, grep every `cursor.execute(sql)` call for whether it embeds
that same expression *and* is missing a params argument.

## EEData / SPC: Cp/Cpk overview of ALL parametric steps at once (2026-07-15)

Added alongside the original one-step-at-a-time `parametric_distribution()`
modal (which still exists, now called "Detalhar Step Selecionado"): a full
table of Cp/Cpk for every parametric step that has real numeric data in the
current filter, mirroring the carrier-cycle-manager UX pattern (auto-detected
defaults, per-row manual override, "clear to restore auto").

**Where the limits come from — `mes_csv_schemas`, not a hardcoded list.**
The MES Client already captures the CSV's row-2 spec line (PCM/CYG format:
`'Nome(unidade)[lsl-usl]'`, e.g. `'Sleep Power(μA)[0.01-2]'`) into
`mes_csv_schemas.{upper_limits_json,lower_limits_json,units_json,columns_json}`
per `model_name` — this dashboard reads that table directly rather than
re-parsing CSVs or hardcoding limits:
- `services.discover_schema_specs(model_name=None)` — steps with a known
  numeric LSL **and** USL (skips non-numeric/null entries and implausible
  sentinel placeholders, `abs() > 1_000_000`, seen in some diagnostic-only
  columns of other models).
- `services.discover_step_candidates(model_name=None)` — the **full** column
  list from the schema (`columns_json`) minus a `NON_PARAMETRIC_KEYS`
  denylist (station/model/result/channel/carrier/timestamp/ID fields already
  normalized elsewhere) — this is what makes steps *without* a defined limit
  (e.g. `Temperature`) still show up (falls back to μ±3σ, marked "auto"),
  instead of only showing steps `discover_schema_specs()` already knows about.
  Any candidate that never has real numeric data in `row_data` simply produces
  zero rows in the aggregate query and is dropped — no explicit filtering
  needed for that case.
- Both prefer the model's own schema (`ORDER BY last_seen DESC LIMIT 1` for
  that `model_name`); without a model filter, the single most-recently-seen
  schema across all models is used. On a multi-model line this is a known
  simplification — `filters['model']` should usually be set when using this
  feature if more than one product is active.
- `services.step_cpk_overview(filters)` computes count/mean/stddev for
  **every** candidate step in **one** query (`jsonb_each_text` unpivot +
  `GROUP BY`, restricted to `WHERE j.key = ANY(%s)`) rather than one query per
  step — this is the only reason showing 30-40 steps at once is cheap; don't
  regress to a per-step loop calling `parametric_distribution()` 40 times.
- **`dashboard_step_specs`** is a second dashboard-owned table (alongside
  `dashboard_carriers`) for manual LSL/USL/unit overrides — same
  lazy-`CREATE TABLE IF NOT EXISTS` pattern, same "any field can be cleared
  back to auto by sending `null`" semantics via `services.set_step_spec()`.
- A step whose schema has `usl <= lsl` (a real data-quality issue seen in the
  wild — `R1T` in the local dev A06 schema has USL=5 lower than LSL=15,
  apparently swapped at the source) shows `limits_valid: false` and a
  "LIMITES INVERTIDOS NO SCHEMA" status instead of a nonsensical negative-then-positive
  Cp/Cpk — **never** compute Cp/Cpk when `usl <= lsl`.
- Clicking "Detalhar" on an overview row carries that row's *effective*
  usl/lsl (schema or override) into the sidebar's `spcUsl`/`spcLsl` inputs
  before opening the single-step modal — without this, the single-step view
  falls back to its own independent μ±3σ auto-limit and shows a **different**
  Cpk than what the user just saw in the overview for the same step, which
  reads as a bug even though both numbers are individually "correct."
- The sidebar's `stepSelect` dropdown is no longer a hardcoded 13-item HTML
  `<option>` list — it's populated from the overview endpoint's actual
  results (`fillStepSelect()` in `dashboard.js`, called after
  `refreshStepSpecsOverview()`), so it only ever offers steps that really
  have data, and grows automatically to the ~38 real ones instead of missing
  most of them (`R1T`, `STC`, `DOCD2`, etc. weren't in the old hardcoded list).

## Deploy — factory rollout planned 2026-07-16

Both repos are being packaged together for a factory deploy the day after
2026-07-15. **SERVER** (this repo): deployed by pulling the latest `main`
onto the factory checkout (see `C:\MES_Server` note above) and restarting
`start_production.bat` — no migrations to run (this app has none;
`dashboard_carriers`/`dashboard_step_specs` self-create via
`CREATE TABLE IF NOT EXISTS` on first request). **CLIENT**: the compiled
installer, `MES_Client_Setup_v1.0.3.exe`, is the deployable artifact — built
from source, not committed to git (`installer/Output/` is gitignored on
purpose; distribute via a GitHub Release attachment on `mes-client`, not the
repo tree). See each repo's README "Deploy em Produção" section for the
step-by-step on the factory floor. Bump the cache-bust `?v=` query string
(see above) before this deploy if `style.css`/`dashboard.js` changed since
the last one shipped, or the shop-floor PC's browser will keep serving a
stale cached copy after the pull.

## Current status / open TODOs (as of 2026-07-15)

Everything above is implemented, manually verified end-to-end against the
local dev DB (server up, every endpoint hit, page loaded, filters/reset/limit
round-tripped, screenshotted via Playwright), and left in a working state.
Both this repo and `mes-client` are pushed to GitHub. Remaining open items,
in rough priority order:

1. **Regression-test the client parser fix against real factory CSVs before/
   during tomorrow's deploy.** `mes-client`'s validation gate is code-complete,
   unit-tested, and repackaged (`MES_Client_Setup_v1.0.3.exe`, Inno Setup
   6.7.3 — turned out to already be installed on this dev machine per-user at
   `%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`, not under Program Files).
   Still **not** run against real factory CSVs (`tests/regression_real_files.py`
   is ready but the real files only exist on the factory PC — run it there
   before/right after installing v1.0.3) and the new installer has **not**
   been run/tested anywhere yet (admin-privileged install to `C:\Utility\MES`
   with Task Scheduler registration — deliberately not tested on this dev
   box; first real run will be at the factory).
2. **BMU-schema rows** (~1,093 rows in the local dev sample) are deliberately
   left unmapped/NULL — revisit only if BMU test data needs to appear in this
   dashboard, per an earlier explicit user decision.
3. No outstanding bugs known in the dashboard itself as of this session (one
   suspected filter-propagation issue during manual testing turned out to be
   a test-script artifact, not a real bug — confirmed by calling
   `loadDashboard()` directly, which worked correctly). The old superseded
   local checkout (`E:\Mes_Server_DashBoard`) has been permanently deleted
   (2026-07-14) — its one valuable piece, SPC/EEData, was already ported.

**If you're picking this up fresh:** read the "Data quality" section above
first — several of the SQL COALESCE choices here look arbitrary unless you
know they're working around specific corruption found in production data.
