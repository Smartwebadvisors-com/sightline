-- Per-scan overall score, cached.
--
-- Overall is a straight mean of the dimension scores, which are computed in
-- Python from stored deductions plus the weight-version snapshot (see
-- scoring/report.py). Reproducing that arithmetic in SQL would mean two
-- implementations of the same score, so this table stores the number
-- scoring/apply.py already computed instead of deriving it again.
--
-- It exists because peer comparison needed the median across every other
-- domain, and computing that live meant one compute_report() per peer
-- domain on every report render -- ~120 round trips, ~3.5s, growing
-- linearly with scan history. With this table the median is one aggregate.
--
-- Keyed by weight version because overall is only meaningful under the
-- weights that produced it. A new weights version starts empty and fills as
-- scans are scored under it, which is what `sightline rescore all
-- --version <new>` already does for every complete scan.
CREATE TABLE IF NOT EXISTS sightline_scan_overall (
    scan_id            BIGINT NOT NULL REFERENCES sightline_scans(id) ON DELETE CASCADE,
    weight_version_id  INTEGER NOT NULL REFERENCES sightline_weight_versions(id),
    overall            DOUBLE PRECISION,   -- NULL = no scored dimension
    computed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scan_id, weight_version_id)
);

-- The peer query orders by weight version and reads overall; the partial
-- index keeps it off the rows that carry no score.
CREATE INDEX IF NOT EXISTS idx_sightline_scan_overall_version
    ON sightline_scan_overall (weight_version_id)
    WHERE overall IS NOT NULL;
