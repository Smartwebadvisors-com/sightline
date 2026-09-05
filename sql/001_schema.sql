-- Sightline: single-URL AEO/GEO/SEO diagnostic.
-- Shares the trendsignal database. Tables prefixed sightline_ to stay out of
-- trendsignal's way.

CREATE TABLE IF NOT EXISTS sightline_scans (
    id            BIGSERIAL PRIMARY KEY,
    url           TEXT NOT NULL,
    domain        TEXT NOT NULL,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at  TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'running',
    error         TEXT,
    meta          JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_sightline_scans_domain
    ON sightline_scans (domain, requested_at DESC);

-- Raw check output. Never numeric. One row per (scan, check_id, item_key).
-- item_key differentiates multi-finding checks (e.g. per-schema-type findings
-- from structured_data). Single-finding checks use item_key = ''.
CREATE TABLE IF NOT EXISTS sightline_findings (
    id            BIGSERIAL PRIMARY KEY,
    scan_id       BIGINT NOT NULL REFERENCES sightline_scans(id) ON DELETE CASCADE,
    check_id      TEXT NOT NULL,
    item_key      TEXT NOT NULL DEFAULT '',
    severity      TEXT NOT NULL,
    examined      TEXT NOT NULL,
    observed      TEXT NOT NULL,
    remediation   TEXT NOT NULL DEFAULT '',
    evidence      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (scan_id, check_id, item_key)
);
CREATE INDEX IF NOT EXISTS idx_sightline_findings_scan
    ON sightline_findings (scan_id);
CREATE INDEX IF NOT EXISTS idx_sightline_findings_check
    ON sightline_findings (check_id, severity);

-- Snapshot of the weights that produced a given batch of scores. When
-- scoring/weights.py changes, insert a new version row and use it to
-- rescore history. Historical scores stay interpretable because the
-- weights that produced them are stored here.
CREATE TABLE IF NOT EXISTS sightline_weight_versions (
    id            SERIAL PRIMARY KEY,
    version       TEXT NOT NULL UNIQUE,
    description   TEXT NOT NULL DEFAULT '',
    weights       JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One row per (finding, weight_version). scan total = SUM(deduction)
-- grouped by (scan_id, weight_version_id), clamped to MAX_SCORE.
CREATE TABLE IF NOT EXISTS sightline_scores (
    id                  BIGSERIAL PRIMARY KEY,
    scan_id             BIGINT NOT NULL REFERENCES sightline_scans(id) ON DELETE CASCADE,
    finding_id          BIGINT NOT NULL REFERENCES sightline_findings(id) ON DELETE CASCADE,
    weight_version_id   INTEGER NOT NULL REFERENCES sightline_weight_versions(id),
    deduction           DOUBLE PRECISION NOT NULL,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (finding_id, weight_version_id)
);
CREATE INDEX IF NOT EXISTS idx_sightline_scores_scan_ver
    ON sightline_scores (scan_id, weight_version_id);

-- Assistant-visibility sampling. Every sample stored raw. Trend and sample
-- size are computed on read. A single sample is a measurement with real
-- variance, not a rank.
CREATE TABLE IF NOT EXISTS sightline_av_observations (
    id               BIGSERIAL PRIMARY KEY,
    domain           TEXT NOT NULL,
    scan_id          BIGINT REFERENCES sightline_scans(id) ON DELETE SET NULL,
    model            TEXT NOT NULL,
    query            TEXT NOT NULL,
    brand_mentioned  BOOLEAN NOT NULL,
    response_excerpt TEXT NOT NULL DEFAULT '',
    sampled_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    meta             JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_sightline_av_domain
    ON sightline_av_observations (domain, sampled_at DESC);
