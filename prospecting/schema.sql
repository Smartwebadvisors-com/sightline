-- schema.sql -- prospecting tables for the SWA lead engine.
-- Postgres 14+. Safe to re-run.
--
-- Namespaced with the sightline_ prefix to sit alongside Sightline's own
-- tables and the trend tool in the shared database. Sightline owns the audit
-- (sightline_scans etc.); these tables own the prospecting layer on top:
-- who we looked at, what the gate decided, and what went out.
--
-- Design notes:
--   * sightline_prospects is the durable identity; scans are append-only
--     history, so a prospect can be shown what changed since we first looked.
--   * raw probe/site JSON is kept so a report can be re-rendered without
--     re-running Sightline or re-spending probe calls.
--   * the gate verdict is stored per scan, not per prospect -- verdicts change
--     as their site changes and as GateConfig is tuned.
CREATE TABLE IF NOT EXISTS sightline_prospects (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    domain          TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,
    city            TEXT NOT NULL,
    state           TEXT NOT NULL,
    place_id        TEXT,
    phone           TEXT,
    review_count    INTEGER,
    rating          NUMERIC(2,1),
    source          TEXT NOT NULL DEFAULT 'dataforseo',
    suppressed      BOOLEAN NOT NULL DEFAULT FALSE,
    suppressed_note TEXT,
    ghl_contact_id  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sightline_prospects_geo_idx  ON sightline_prospects (state, city, category);
CREATE INDEX IF NOT EXISTS sightline_prospects_supp_idx ON sightline_prospects (suppressed) WHERE NOT suppressed;

CREATE TABLE IF NOT EXISTS sightline_prospect_scans (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    prospect_id         BIGINT NOT NULL REFERENCES sightline_prospects(id) ON DELETE CASCADE,
    scanned_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    visibility_score    SMALLINT,        -- 0-100 from aeo_probe
    unbranded_asked     SMALLINT,
    unbranded_cited     SMALLINT,
    unbranded_mentioned SMALLINT,
    branded_found       BOOLEAN,

    engine              TEXT NOT NULL DEFAULT 'sightline',  -- sightline | builtin
    aeo_score           SMALLINT,        -- 0-100, whichever engine ran
    geo_score           SMALLINT,        -- Sightline only
    seo_score           SMALLINT,        -- Sightline only
    pillar_scores       JSONB,
    psi_performance     SMALLINT,
    psi_seo             SMALLINT,
    findings_failed     SMALLINT,

    gate_status         TEXT NOT NULL,   -- QUALIFIED | REVIEW | SKIP
    gate_rule           TEXT NOT NULL,
    gate_priority       SMALLINT NOT NULL DEFAULT 0,
    gate_hook           TEXT,
    gate_reasons        JSONB,

    probe_raw           JSONB,           -- full ProbeResult
    site_raw            JSONB,           -- full SiteReport
    report_url          TEXT,            -- hosted gap report, once rendered
    scan_errors         JSONB
);

CREATE INDEX IF NOT EXISTS sightline_scans_prospect_idx ON sightline_prospect_scans (prospect_id, scanned_at DESC);
CREATE INDEX IF NOT EXISTS sightline_scans_queue_idx
    ON sightline_prospect_scans (gate_status, gate_priority DESC, scanned_at DESC);

-- Latest scan per prospect: this is the outreach queue.
CREATE OR REPLACE VIEW sightline_outreach_queue AS
SELECT DISTINCT ON (s.prospect_id)
       p.id            AS prospect_id,
       p.name,
       p.domain,
       p.category,
       p.city,
       p.state,
       p.review_count,
       p.rating,
       p.ghl_contact_id,
       s.id            AS scan_id,
       s.scanned_at,
       s.visibility_score,
       s.aeo_score,
       s.gate_status,
       s.gate_rule,
       s.gate_priority,
       s.gate_hook,
       s.engine,
       s.geo_score,
       s.seo_score,
       s.report_url
FROM   sightline_prospect_scans s
JOIN   sightline_prospects p ON p.id = s.prospect_id
WHERE  NOT p.suppressed
ORDER  BY s.prospect_id, s.scanned_at DESC;

-- Outbound handoff log: what actually went to GHL, and when.
CREATE TABLE IF NOT EXISTS sightline_outreach_events (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    prospect_id  BIGINT NOT NULL REFERENCES sightline_prospects(id) ON DELETE CASCADE,
    scan_id      BIGINT REFERENCES sightline_prospect_scans(id) ON DELETE SET NULL,
    event        TEXT NOT NULL,   -- claimed | pushed_to_ghl | send_failed | replied | booked | suppressed
    channel      TEXT,            -- email | sms | manual
    -- The duplicate-send guard. sha256(prospect_id|scan_id|stage), UNIQUE, so
    -- claiming the right to contact someone IS the insert -- there is no gap
    -- between checking and claiming for a concurrent worker to slip through.
    idempotency_key TEXT UNIQUE,
    payload      JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sightline_outreach_events_prospect_idx
    ON sightline_outreach_events (prospect_id, created_at DESC);

-- Sends claimed but never resolved: the process died mid-flight. These
-- prospects were NOT contacted; nothing retries them automatically.
CREATE OR REPLACE VIEW sightline_stuck_sends AS
SELECT c.prospect_id, c.scan_id, c.created_at, p.name, p.domain
FROM   sightline_outreach_events c
JOIN   sightline_prospects p ON p.id = c.prospect_id
WHERE  c.event = 'claimed'
  AND  NOT EXISTS (
         SELECT 1 FROM sightline_outreach_events d
         WHERE d.prospect_id = c.prospect_id
           AND d.event IN ('pushed_to_ghl', 'send_failed')
           AND d.created_at >= c.created_at
       );

-- Work queue. Replaces Redis: this pipeline moves ~25 items a morning, which
-- is not a queueing problem, and Postgres is already a hard dependency.
-- Drained with SELECT ... FOR UPDATE SKIP LOCKED so concurrent workers get
-- different rows instead of blocking on each other.
CREATE TABLE IF NOT EXISTS sightline_queue (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    stage       TEXT NOT NULL,          -- prospect | review
    dedupe_key  TEXT NOT NULL,          -- domain, usually
    payload     JSONB NOT NULL,
    claimed_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (stage, dedupe_key)
);

CREATE INDEX IF NOT EXISTS sightline_queue_ready_idx
    ON sightline_queue (stage, created_at) WHERE claimed_at IS NULL;
