-- Tasks, not severities.
--
-- `severity` stays on the row: it is the scoring input (scoring/weights.py
-- keys deductions off it) and it never appears in a client-facing view. What
-- a client sees is impact, effort and owner, as three separate values, so
-- they cannot be fused back into one word downstream. See COPY.md rule 4.
--
-- Nullable so this migration is safe on existing rows; `sightline
-- migrate-findings` fills them from findings.TASK_GAP, after which every
-- new row is written with all three populated.
ALTER TABLE sightline_findings
    ADD COLUMN IF NOT EXISTS impact         TEXT,
    ADD COLUMN IF NOT EXISTS effort_minutes INTEGER,
    ADD COLUMN IF NOT EXISTS owner          TEXT;

-- db.init_schema() replays every file on every run, so constraints are
-- dropped before being added.
ALTER TABLE sightline_findings
    DROP CONSTRAINT IF EXISTS sightline_findings_impact_chk;
ALTER TABLE sightline_findings
    ADD  CONSTRAINT sightline_findings_impact_chk
         CHECK (impact IS NULL OR impact IN ('high', 'medium', 'low'));

ALTER TABLE sightline_findings
    DROP CONSTRAINT IF EXISTS sightline_findings_owner_chk;
ALTER TABLE sightline_findings
    ADD  CONSTRAINT sightline_findings_owner_chk
         CHECK (owner IS NULL OR owner IN ('client', 'swa'));

-- The report renders swa-owned findings in their own section.
CREATE INDEX IF NOT EXISTS idx_sightline_findings_owner
    ON sightline_findings (scan_id, owner);
