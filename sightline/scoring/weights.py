"""Sightline scoring weights and dimension mapping.

Two layers:
  1. DEDUCTIONS[check_id][severity] -> raw per-finding point value
  2. DIMENSIONS[dim_id] -> list of check_ids that feed that dimension

Dimension scoring is coverage-normalized: each dimension =
100 * (points earned / points available), where 'available' is the sum of
the check's 'critical' weight over every finding that ACTUALLY RAN
(severity in pass/low/medium/high/critical). Unavailable findings are
excluded from BOTH numerator and denominator, so a check that couldn't
run doesn't lift or lower the site's dimension score.

Per-finding deductions are the value in the weight table exactly — no
per-check cap scaling. The dimension ratio is naturally 0..100 so caps
aren't needed, and diluted individual deductions confused the report
('why does the weight table say -8 but the finding shows -6.2?').

When you reweight:
  1. Change values in this file.
  2. Bump WEIGHTS_VERSION.
  3. Run `sightline rescore all --version <new>`
"""
from __future__ import annotations

WEIGHTS_VERSION = "v2"
WEIGHTS_DESCRIPTION = (
    "Per-dimension coverage-normalized scoring. Reweighted from v1 after "
    "six real scans revealed dilution under per-check caps and a free-pass "
    "problem with unavailable PSI checks. llms.txt demoted to informational."
)

MAX_SCORE = 100.0

# The six scored dimensions. Each is 0..100 in the report; overall is the
# straight mean of dimensions that had at least one scored finding.
DIMENSIONS: dict[str, list[str]] = {
    "ai_accessibility":   ["ai_crawler"],
    "structured_data":    ["structured_data"],
    "entity_consistency": ["entity_consistency"],
    "answer_first":       ["answer_first"],
    "performance":        ["pagespeed"],
    "authority":          ["rank"],
}

DIMENSION_LABELS: dict[str, str] = {
    "ai_accessibility":   "AI Accessibility",
    "structured_data":    "Structured Data",
    "entity_consistency": "Entity Consistency",
    "answer_first":       "Answer-First",
    "performance":        "Performance",
    "authority":          "Authority",
}

# Order for the dimension headline grid.
DIMENSION_ORDER = ["ai_accessibility", "structured_data", "entity_consistency",
                   "answer_first", "performance", "authority"]

# Deduction per (check_id, severity). Missing keys default to 0. Severities
# 'pass', 'info', and 'unavailable' are 0 (listed for readability).
DEDUCTIONS: dict[str, dict[str, float]] = {
    # AI Accessibility ─ blocked assistant crawlers ARE the product story.
    # A fully-blocked site must score near zero on this dimension, so
    # blocked crawlers emit severity='critical' in ai_crawler.py.
    "ai_crawler": {
        "critical": 30, "high": 15, "medium": 8, "low": 3,
        "info": 0, "pass": 0, "unavailable": 0,
    },
    # Structured Data ─ no JSON-LD at all is 'critical' and substantially
    # outweighs any single missing property.
    "structured_data": {
        "critical": 40, "high": 15, "medium": 6, "low": 2,
        "info": 0, "pass": 0, "unavailable": 0,
    },
    "entity_consistency": {
        "critical": 25, "high": 12, "medium": 5, "low": 2,
        "info": 0, "pass": 0, "unavailable": 0,
    },
    "answer_first": {
        "critical": 20, "high": 10, "medium": 4, "low": 2,
        "info": 0, "pass": 0, "unavailable": 0,
    },
    "pagespeed": {
        "critical": 30, "high": 12, "medium": 6, "low": 2,
        "info": 0, "pass": 0, "unavailable": 0,
    },
    "rank": {
        "critical": 30, "high": 12, "medium": 6, "low": 2,
        "info": 0, "pass": 0, "unavailable": 0,
    },

    # llms.txt is informational only. It is not a documented ranking
    # signal and, across six real scans, deducted -1 uniformly (no signal).
    # Findings still appear in the report under an informational section,
    # outside the six scored dimensions.
    "llms_txt": {
        "critical": 0, "high": 0, "medium": 0, "low": 0,
        "info": 0, "pass": 0, "unavailable": 0,
    },
}


def snapshot() -> dict:
    """Blob written to sightline_weight_versions.weights."""
    return {
        "version": WEIGHTS_VERSION,
        "description": WEIGHTS_DESCRIPTION,
        "max_score": MAX_SCORE,
        "dimensions": DIMENSIONS,
        "dimension_labels": DIMENSION_LABELS,
        "dimension_order": DIMENSION_ORDER,
        "deductions": DEDUCTIONS,
    }
