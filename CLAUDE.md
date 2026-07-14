# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Django dashboard for a factory production line (Salcomp, Manaus) that shows
real-time test/yield data — First Pass Yield, PASS/FAIL counts, hourly UPH
trend, failure pareto, and per-channel/per-carrier failure matrices. It reads
from a Postgres table (`mes_test_results`) that is populated by an external
system; this app is **read-only** against that table (no writes, no models for
it, no migrations for it).

Not a git repository yet (no `.git`). A `.gitignore` has been added in
anticipation of initializing one at the factory.

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
file** (currently at `_06`; next change → `_07`), since the browser/dashboard
PC otherwise caches the old file indefinitely.

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

## Current status / open TODOs (as of 2026-07-13)

Everything above is implemented, manually verified end-to-end against the
local dev DB (server up, every endpoint hit, page loaded, filters/reset/limit
round-tripped), and left in a working state. Remaining open items, in rough
priority order:

1. **Deploy the client-side parser fix to the factory.** `D:\MES_Client_Complete`'s
   validation gate is code-complete and unit-tested but not yet run against
   real factory CSVs (needs the factory PC — see `tests/regression_real_files.py`)
   nor repackaged with PyInstaller. Until repackaged, the factory is still
   running the old client binary (pre-fix) — new corrupted rows can still
   land in `mes_test_results`, which is exactly what the dashboard-side
   defenses above exist to tolerate in the meantime.
2. **BMU-schema rows** (~1,093 rows in the local dev sample) are deliberately
   left unmapped/NULL — revisit only if BMU test data needs to appear in this
   dashboard, per an earlier explicit user decision.
3. No outstanding bugs known in the dashboard itself as of this session.

**If you're picking this up fresh:** read the "Data quality" section above
first — several of the SQL COALESCE choices here look arbitrary unless you
know they're working around specific corruption found in production data.
