"""Assistant prompt testing — the one piece of work in the report that we
perform rather than hand over.

This replaces the two client-facing prompt-testing tasks, merged into one
standing finding with owner="swa". They were two instructions telling a
client to go and ask ChatGPT about themselves, which is (a) work we are
being paid for and (b) something no client does twice, let alone on the
schedule that makes the numbers mean anything.

It emits severity='info' so it is never scored, and it is deliberately
absent from weights.DIMENSIONS: it measures nothing about the fetched page,
so it must not move a dimension score in either direction. Its state lives
in sightline_av_observations (see av/sample.py, db.av_summary)."""
from __future__ import annotations

from .. import db
from .base import Finding, ScanContext

CHECK_ID = "prompt_testing"


def run(ctx: ScanContext) -> list[Finding]:
    av = db.av_summary(ctx.domain)
    n = av["n_samples"] or 0
    if n == 0:
        observed = (
            "No assistant-visibility samples recorded for this domain. "
            "Scheduled prompt testing has not started."
        )
    elif n < 10:
        observed = (
            f"{n} sample(s) recorded in the last {av['window_days']} days. "
            "Below the 10-sample floor for a trend claim; testing continues."
        )
    else:
        observed = (
            f"{n} samples in the last {av['window_days']} days at a "
            f"{av['visibility_rate']:.0%} mention rate. Sampled measurement "
            "with real variance, not a rank."
        )
    return [Finding(
        check_id=CHECK_ID, item_key="",
        severity="info",
        examined="Scheduled assistant prompt testing",
        observed=observed,
        remediation="",              # nothing for the client to do; owner=swa
        evidence={"n_samples": n, "window_days": av["window_days"]},
    )]
