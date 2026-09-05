# Prospecting layer — Sightline as the audit engine

Turns a raw list of local businesses into a ranked outreach queue, where every
qualified row carries a specific, evidence-backed reason to email them.

**Sightline does the auditing.** This is the prospecting layer on top of it:
the part Sightline doesn't have. Two measurements per prospect:

1. **AI visibility** (`aeo_probe.py`) — asks Perplexity the questions a buyer
   actually asks (`best plumber in Allentown, PA`) and records whether the
   prospect is cited, merely named, or absent, plus which competitors and
   directories get cited instead. Sightline audits a site; this measures
   whether the market can find it. Different question, and it's the one that
   makes a cold email land.
2. **Site audit** (`sightline_adapter.py` → Sightline) — one URL in, scored
   AEO/GEO/SEO out, with findings. Same engine, same numbers a paying client
   sees. `aeo_score.py` is kept only as a fallback for when Sightline is
   unreachable or has no scan on record.

A deterministic gate (`aeo_gate.py`) combines them into `QUALIFIED / REVIEW /
SKIP` with a priority score. No LLM decides anything — that stays true to the
gated sense-reason-act pattern the rest of the loop uses.

## Failing safely

Every external dependency here can break: Perplexity, Sightline, DataForSEO,
the prospect's own website, Postgres, GHL. The loud failures are easy — retry
or skip. The one that costs you is a dependency returning **HTTP 200 with a
shape you didn't expect**, which reads as "no citations found", which reads as
"this business is invisible", which qualifies everyone and sends two hundred
people a claim that's false.

Four guards, and one rule: **the pipeline fails closed.** Everything before
sending is free to redo. Sending is the only irreversible act. So uncertainty
anywhere upstream degrades toward *not* contacting someone.

| Guard | Catches |
|---|---|
| `validate_probe_payload` | Shape drift — a 200 with renamed or missing fields becomes an ERROR, never a zero score |
| Canary preflight | Both directions: a known-visible business reported absent (probe broken), a nonexistent business reported cited (matching broken). Either one halts the batch |
| Quorum (`AEO_PROBE_QUORUM`, default 3 of 4) | Partial outages. Fewer than 3 answered queries → `trustworthy=False` → gate returns REVIEW, not QUALIFIED |
| `outbox.claim_send` | Duplicate sends. `sha256(prospect_id\|scan_id\|stage)` with a UNIQUE constraint — the check and the claim are one atomic insert |

Circuit breakers stop hammering a dead dependency (5 consecutive failures →
5-minute pause, per dependency). Budget caps stop runaway spend and are never
retried around. Retries use backoff with jitter, and only on 408/425/429/5xx —
a 401 fails immediately rather than burning three attempts on a bad key.

Run before every batch:

```bash
python3 preflight.py          # exit 0 = GO, 1 = DEGRADED, 2 = HALT
```

Point the positive canary at a business you're certain shows up:

```bash
export CANARY_NAME="Rita's Italian Ice"
export CANARY_DOMAIN=ritasice.com
export CANARY_CATEGORY="italian ice shop"
export CANARY_CITY=Bethlehem
export CANARY_STATE=PA
```

### The send path

`claim_send` inserts the claim **before** calling GHL, not after. If the
process dies mid-send you get a claimed row for a message that may never have
gone out — a prospect you never contact — rather than an unclaimed row for one
that did. Those show up in `sightline_stuck_sends` for you to decide about;
nothing retries automatically, because a failed send may or may not have
reached the inbox and there's no way to know from here.

A failed send does **not** free the prospect for a retry. That's deliberate.

## Read this first

The adapter is written against a *tolerant* guess at Sightline's output shape,
not the real one. `FIELD_ALIASES` and `FINDING_ALIASES` at the top of
`sightline_adapter.py` are the entire surface between the two systems — every
assumption lives there, and 33 tests cover flat, nested, and degraded shapes.

Run `bash discover.sh > sightline-surface.txt` on the VPS and send it back;
pinning those two dicts to reality is a ten-minute change. Until then the
adapter will probably work and will fall back to the built-in audit, loudly,
when it doesn't.

## Files

| File | What it is |
|---|---|
| `sourcing.py` | **SENSE stage.** DataForSEO listings by category + map radius, filtered to real candidates |
| `aeo_probe.py` | Perplexity visibility probe; pure `evaluate_response()` is unit-tested |
| `sightline_adapter.py` | **Sightline → SiteReport.** CLI and Postgres backends; all mapping assumptions live at the top |
| `aeo_gate.py` | Deterministic qualification rules + priority scoring |
| `aeo_types.py` | `Finding` / `SiteReport` — shared so the gate doesn't care which engine ran |
| `aeo_score.py` | Built-in audit, now a fallback. Also exports `check_ai_crawlers()` |
| `scan_prospect.py` | Orchestrator + CLI + Postgres persistence |
| `loop_hook.py` | Redis in/out wiring for `lead_engine_loop.py`, with the act-stage cap |
| `schema.sql` | `sightline_prospects`, `sightline_prospect_scans`, `sightline_outreach_events`, queue view |
| `discover.sh` | Read-only VPS probe that pins the Sightline mapping |
| `resilience.py` | Retry, circuit breaker, budget caps, response-shape validation |
| `preflight.py` | Canary checks that catch silently-wrong data before a batch runs |
| `outbox.py` | At-most-once send guarantee, enforced by a UNIQUE constraint |
| `status.py` | Daily view: queue depth, verdicts by priority, sends, errors |
| `queue_pg.py` | Postgres work queue (SKIP LOCKED). No Redis. |
| `selftest.py` / `test_sightline.py` / `test_failures.py` / `test_sourcing.py` / `test_schema.py` / `test_autoscan.py` | Offline suites — safe any time |
| `test_queue_pg.py` / `test_outbox_pg.py` | **Destructive.** Drop and recreate tables; refuse to run if the database holds real data |

## Install

```bash
cd /root/sightline/prospecting          # alongside Sightline, shared DB
pip install requests beautifulsoup4 psycopg[binary] redis
psql "$DATABASE_URL" -f schema.sql      # sightline_-prefixed, coexists cleanly
python3 selftest.py && python3 test_sightline.py
```

Environment:

```bash
export PERPLEXITY_API_KEY=...        # required unless --no-probe
export DATABASE_URL=postgres://...   # the same one Sightline uses
export REDIS_URL=redis://127.0.0.1:6379/0

# Sightline wiring -- adjust to match discover.sh output
export SIGHTLINE_DIR=/root/sightline
export SIGHTLINE_CMD='python3 -m sightline scan --json {url}'
export SIGHTLINE_REPORT_BASE=https://sightline.smartwebadvisors.com/r/
export AEO_ENGINE=sightline-cli       # or sightline-db, or builtin

export PSI_API_KEY=...               # only used by the builtin fallback
```

### Which engine

| `--engine` | What it does | When |
|---|---|---|
| `sightline-cli` | Shells out to Sightline per prospect | Default. Fresh scan each time. |
| `sightline-db` | Reads the latest `sightline_scans` row | When Sightline already scans on a schedule — prospecting then costs nothing extra. Refuses scans older than 30 days. |
| `builtin` | The bundled audit | Fallback. Automatic when Sightline errors, with the reason recorded on the scan row. |

## Run

```bash
# one prospect, JSON to stdout
python3 scan_prospect.py --name "Otter Squad Plumbing" --domain ottersquad.com \
    --category plumber --city Allentown --state PA --reviews 64 --rating 4.8

# a batch, persisted
python3 scan_prospect.py --file prospects.json --persist --out scans.json

# technical audit only, zero Perplexity spend
python3 scan_prospect.py --file prospects.json --no-probe

# reuse Sightline scans that already ran
python3 scan_prospect.py --file prospects.json --engine sightline-db
```

`prospects.json` is a list of objects with `name, domain, category, city,
state` and optionally `review_count, rating, place_id`.

## Sourcing Lehigh Valley automatically

```bash
export DATAFORSEO_LOGIN=...
export DATAFORSEO_PASSWORD=...

python3 sourcing.py --dry-run                 # see what it would queue
python3 sourcing.py --enqueue                 # push to the sense queue
python3 sourcing.py --from-file saved.json    # parse a saved response, no spend
```

**Geography.** One coordinate + radius, not a city list: `40.6300,-75.3700`
with a 25 km radius is centered between Allentown and Easton and covers the
whole ABE corridor plus Emmaus, Whitehall, Nazareth, Hellertown and Macungie.
Widen to 35 km to pull in Quakertown and Kutztown. Radius is in kilometers.

```bash
export LV_CENTER="40.6300,-75.3700"
export LV_RADIUS_KM=25
```

**Categories** default to ten local service verticals that actually buy
marketing — plumber, roofing, electrician, HVAC, general contractor,
landscaper, dentist, chiropractor, personal injury attorney, auto repair.
Override with `--categories`. Ten per run is the DataForSEO cap.

**What gets filtered out before it costs a scan.** Every prospect that reaches
the scan stage costs a Sightline run plus five Perplexity calls, so rejection
is done on listing data first:

| Dropped | Why |
|---|---|
| `no_website` | Nothing to audit and nothing to put in a gap report. A real prospect for a different offer. |
| `social_page_only` | Their "website" is a Facebook page. Same story. |
| `chain` | One domain at 3+ locations is a franchise. Marketing isn't decided in the Lehigh Valley. |
| `already_known` | Already in `sightline_prospects`. |
| `suppressed` | Replied, bounced, opted out, or is a client. |
| `duplicate_in_batch` | Two listings, one business. |
| `too_few_reviews` | No demand to be losing. |

Review count and rating floors are also applied server-side in the request, so
you aren't billed for pages of businesses the gate would reject anyway.

Two locations on one domain is **not** a chain — the second is a duplicate.
The threshold is `chain_threshold`, default 3.

**A note on failing closed here too:** if the dedupe query against Postgres
fails, sourcing raises rather than continuing. Sourcing with an empty
known-domains set would re-queue everyone you have already emailed, and
sourcing nothing is much cheaper than that.

## Wire it into the loop

```bash
# sense: refill the queue weekly (categories rotate slowly; no need for daily)
0 6 * * 1     cd /root/sightline/prospecting && python3 sourcing.py --enqueue

# reason: drain the sense queue, scan, persist
*/30 * * * *  cd /root/sightline/prospecting && python3 loop_hook.py --limit 25

# act: release cleared rows (to review by default, not straight to send)
0 9 * * 1-5   cd /root/sightline/prospecting && python3 loop_hook.py --act --limit 15
```

The act stage is capped and gated by env, not by code changes:

```bash
AEO_MIN_PRIORITY=55      # priority floor to release
AEO_DAILY_CAP=30         # hard ceiling on pushes per day
AEO_REQUIRE_APPROVAL=1   # 1 = land in aeo:outreach:review for your eyes first
```

Leave `AEO_REQUIRE_APPROVAL=1` until the copy is proven. Reviewed rows move to
`aeo:outreach:pending` for the actor to render the report and push to GHL.

## How the scores work

**Visibility (0–100).** Four unbranded buyer queries carry 80%; one branded
control carries 20%. Cited in the sources = full credit, named in the answer
text only = half. So: invisible everywhere = 0, findable only when you already
know the name = 20, cited in every answer = 100.

**Site score (0–100).** Sightline's AEO score, with GEO and SEO carried along
on the scan row. If a payload has no AEO score the adapter falls back to the
overall score, then to the mean of whatever layers came back, so a partial
result still ranks sensibly instead of scoring zero. The built-in fallback
computes its own from four weighted pillars (structured data 30, answerability
30, crawlability 25, performance 15).

**Priority (0–100).** Demand they have (reviews 40, rating 10) against demand
they're losing (invisibility 30, fixable gaps 20). With `--no-probe` the
invisibility points move to the technical side rather than scoring an unprobed
business as if it were invisible.

## The rules that qualify

Checked in order; first match wins.

| Rule | Fires when | Why it's the pitch |
|---|---|---|
| `AI_CRAWLERS_BLOCKED` | robots.txt disallows GPTBot / PerplexityBot / ClaudeBot / Google-Extended | One file has opted them out of AI search entirely. Undeniable, and a same-day fix. |
| `STRONG_REPUTATION_INVISIBLE` | ≥40 reviews and visibility ≤35 | They earned the demand and are handing it to whoever the answer engine names instead. |
| `INVISIBLE_AND_FIXABLE` | visibility ≤35 and AEO ≤70 | Invisible, with named technical reasons behind it. |
| `TECHNICAL_GAPS_ONLY` | `--no-probe` runs with AEO ≤70 | Audit-only evidence; probe before sending. |

Skips are recorded with a reason (`INSUFFICIENT_DEMAND`,
`REPUTATION_PROBLEM`, `NO_REACHABLE_SITE`, `NO_CLEAR_GAP`, `SUPPRESSED`) so the
source list can be tuned instead of guessed at. Everything is tunable through
`GateConfig` — start loose, tighten once you've read 25 reports.

## What's not built yet

Using Sightline collapses this list. It was three items; it's now closer to one
and a half.

- **~~Sourcing~~.** Built — `sourcing.py`. See below.
- **~~Report rendering~~.** Sightline already renders scored reports. The
  adapter carries `report_url` through to the scan row; point
  `SIGHTLINE_REPORT_BASE` at it and the gap report is done. What's left is
  deciding whether a prospect-facing report shows the same detail a client's
  does.
- **GHL push.** The actor that takes a reviewed row, creates the contact with
  score/hook/report_url as custom fields, and starts the sequence.

## One thing to check in Sightline

Does it flag AI crawlers blocked in robots.txt? Section 7 of `discover.sh`
answers this. If not, `check_ai_crawlers()` in `aeo_score.py` is ~40 lines
worth porting into Sightline itself — it belongs in a client-facing AEO audit
at least as much as in prospecting. Until then the adapter merges it in
automatically (`supplement=True`).

## Before you run it against real prospects

Point it at three sites you already know well — one client, one competitor,
one you've audited by hand — and check the findings match your own read. The
gate thresholds are opinions; only your judgment on the first 25 makes them
right.
