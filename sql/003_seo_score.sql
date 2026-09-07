-- SEO score: an absolute measurement of organic search presence, sourced
-- from DataForSEO and scored independently of the AEO composite.
--
-- Stored on the scan row rather than derived from sightline_scores because
-- it is not a function of findings at all: no deduction feeds it. See
-- sightline/scoring/seo.py.
--
-- seo_metrics holds the RAW measured values plus per-component subscores,
-- the covered weight, and the scoring version. Keeping the raws means a
-- future seo-v2 can rescore every historical row without re-buying the
-- API calls -- the same property sightline_weight_versions gives the AEO
-- side.
ALTER TABLE sightline_scans
    ADD COLUMN IF NOT EXISTS seo_score DOUBLE PRECISION;
ALTER TABLE sightline_scans
    ADD COLUMN IF NOT EXISTS seo_metrics JSONB NOT NULL DEFAULT '{}'::jsonb;

-- NULL seo_score is meaningful (not measured), so the partial index keeps
-- the backfill scan cheap as history grows.
CREATE INDEX IF NOT EXISTS idx_sightline_scans_seo_missing
    ON sightline_scans (domain)
    WHERE seo_score IS NULL;
