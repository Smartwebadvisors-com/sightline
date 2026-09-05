"""
test_failures.py -- simulate the ways external systems break.

No network. Every dependency is a fake that fails on purpose. The question
each test answers is not "did it survive" but "did it fail in the safe
direction" -- toward not contacting someone.

    python3 test_failures.py
"""

from __future__ import annotations

import sys

import requests

from aeo_gate import evaluate
from aeo_probe import Prospect, ProbeResult, probe
from aeo_types import Finding, SiteReport
from resilience import (Budget, BudgetExhausted, CircuitBreaker, CircuitOpen,
                        DependencyError, Guard, RetryPolicy, ShapeError,
                        classify, validate_probe_payload)

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


def http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(response=resp)


NO_SLEEP = lambda _: None

# --------------------------------------------------------------------------
print("\nerror classification")
check("429 is retryable", classify("x", http_error(429)).retryable, True)
check("503 is retryable", classify("x", http_error(503)).retryable, True)
check("401 is not retryable", classify("x", http_error(401)).retryable, False)
check("404 is not retryable", classify("x", http_error(404)).retryable, False)
check("timeout is retryable", classify("x", requests.Timeout()).retryable, True)
check("bad JSON is not retryable",
      classify("x", ValueError("Expecting value")).retryable, False)

# --------------------------------------------------------------------------
print("\nretry behaviour")
calls = {"n": 0}


def flaky_then_ok():
    calls["n"] += 1
    if calls["n"] < 3:
        raise http_error(503)
    return {"ok": True}


g = Guard(retry=RetryPolicy(attempts=4, base_delay_s=0, jitter=False), sleep=NO_SLEEP)
check("recovers from transient failures", g.call("dep", flaky_then_ok), {"ok": True})
check("stopped as soon as it succeeded", calls["n"], 3)

calls["n"] = 0


def always_401():
    calls["n"] += 1
    raise http_error(401)


g2 = Guard(retry=RetryPolicy(attempts=4, base_delay_s=0, jitter=False), sleep=NO_SLEEP)
try:
    g2.call("dep", always_401)
    check("bad credentials raise", "no error", "DependencyError")
except DependencyError:
    check("bad credentials raise", "DependencyError", "DependencyError")
check("bad credentials are not retried", calls["n"], 1)

# --------------------------------------------------------------------------
print("\ncircuit breaker")
breaker = CircuitBreaker(threshold=3, cooldown_s=60)
g3 = Guard(breaker=breaker, retry=RetryPolicy(attempts=1), sleep=NO_SLEEP)


def always_down():
    raise requests.ConnectionError("refused")


for _ in range(3):
    try:
        g3.call("perplexity", always_down)
    except DependencyError:
        pass

check("opens after threshold", breaker.is_open("perplexity"), True)
check("other dependencies unaffected", breaker.is_open("dataforseo"), False)

blocked = {"n": 0}


def should_not_run():
    blocked["n"] += 1
    return {}


try:
    g3.call("perplexity", should_not_run)
    check("open circuit raises", "no error", "CircuitOpen")
except CircuitOpen:
    check("open circuit raises", "CircuitOpen", "CircuitOpen")
check("open circuit does not call the dependency", blocked["n"], 0)

breaker._open_until["perplexity"] = 0        # simulate cooldown elapsed
check("half-opens after cooldown", breaker.is_open("perplexity"), False)

# --------------------------------------------------------------------------
print("\nbudget caps")
budget = Budget(max_calls={"perplexity": 2})
g4 = Guard(budget=budget, retry=RetryPolicy(attempts=1), sleep=NO_SLEEP)
for _ in range(2):
    g4.call("perplexity", lambda: {"ok": True})
try:
    g4.call("perplexity", lambda: {"ok": True})
    check("call cap stops the run", "no error", "BudgetExhausted")
except BudgetExhausted:
    check("call cap stops the run", "BudgetExhausted", "BudgetExhausted")

spend = Budget(max_spend_usd=0.05, cost_per_call={"perplexity": 0.02})
g5 = Guard(budget=spend, retry=RetryPolicy(attempts=1), sleep=NO_SLEEP)
for _ in range(2):
    g5.call("perplexity", lambda: {"ok": True})
try:
    g5.call("perplexity", lambda: {"ok": True})
    check("spend cap stops the run", "no error", "BudgetExhausted")
except BudgetExhausted:
    check("spend cap stops the run", "BudgetExhausted", "BudgetExhausted")
check("spend tracked", round(spend.spend_usd, 2), 0.04)

retry_budget = Budget(max_calls={"dep": 10})
g6 = Guard(budget=retry_budget,
           retry=RetryPolicy(attempts=3, base_delay_s=0, jitter=False),
           sleep=NO_SLEEP)
try:
    g6.call("dep", lambda: (_ for _ in ()).throw(http_error(503)))
except DependencyError:
    pass
check("failed attempts count against budget", retry_budget.calls["dep"], 3)

# --------------------------------------------------------------------------
print("\nshape drift -- the silent one")
GOOD = {"choices": [{"message": {"content": "Try Beaver Brigade."}}],
        "citations": ["https://beaverbrigade.com"]}
validate_probe_payload(GOOD)
print("  ok   a valid payload passes")


def expect_shape_error(label: str, payload) -> None:
    try:
        validate_probe_payload(payload)
        check(label, "accepted", "ShapeError")
    except ShapeError:
        check(label, "ShapeError", "ShapeError")


expect_shape_error("empty object rejected", {})
expect_shape_error("no choices rejected", {"citations": []})
expect_shape_error("empty content rejected",
                   {"choices": [{"message": {"content": "  "}}],
                    "citations": []})
expect_shape_error("renamed citation fields rejected",
                   {"choices": [{"message": {"content": "Try Beaver Brigade."}}],
                    "sources_v2": ["https://beaverbrigade.com"]})
expect_shape_error("html error page rejected", "<html>502 Bad Gateway</html>")

# --------------------------------------------------------------------------
print("\nthe probe fails closed")


class FakeSession:
    """Answers the first `ok_count` queries, then fails."""

    def __init__(self, ok_count: int, payload=None):
        self.ok_count = ok_count
        self.calls = 0
        self.payload = payload or GOOD

    def post(self, *a, **kw):
        self.calls += 1
        if self.calls <= self.ok_count:
            resp = requests.Response()
            resp.status_code = 200
            resp._content = __import__("json").dumps(self.payload).encode()
            resp.headers["Content-Type"] = "application/json"
            return resp
        raise requests.ConnectionError("network gone")

    def close(self):
        pass


import aeo_probe
aeo_probe.PAUSE_BETWEEN_QUERIES = 0

quiet = Guard(retry=RetryPolicy(attempts=1), sleep=NO_SLEEP)
partial = probe(Prospect("Otter Squad Plumbing", "ottersquad.com", "plumber",
                         "Allentown", "PA", review_count=64, rating=4.8),
                api_key="test", session=FakeSession(ok_count=1), guard=quiet)
check("partial outage is marked untrustworthy", partial.trustworthy, False)
check("only successful queries counted", partial.unbranded_asked, 1)

quiet2 = Guard(retry=RetryPolicy(attempts=1), sleep=NO_SLEEP)
full = probe(Prospect("Otter Squad Plumbing", "ottersquad.com", "plumber",
                      "Allentown", "PA", review_count=64, rating=4.8),
             api_key="test", session=FakeSession(ok_count=5), guard=quiet2)
check("a complete run is trustworthy", full.trustworthy, True)
check("all four unbranded counted", full.unbranded_asked, 4)

# --------------------------------------------------------------------------
print("\nthe gate refuses unreliable data")


def site(aeo=30, n=5, errors=None) -> SiteReport:
    return SiteReport(
        domain="ottersquad.com", url="https://ottersquad.com", reachable=True,
        status_code=200, aeo_score=aeo,
        findings=[Finding(f"f{i}", "answerability", f"c{i}", False, "medium", 3,
                          f"gap {i}") for i in range(n)],
        errors=errors or [],
    )


prospect = Prospect("Otter Squad Plumbing", "ottersquad.com", "plumber",
                    "Allentown", "PA", review_count=64, rating=4.8)

v = evaluate(prospect, partial, site())
check("untrustworthy probe -> REVIEW, not QUALIFIED", v.status, "REVIEW")
check("and it says why", v.rule, "PROBE_UNRELIABLE")

trusted_invisible = ProbeResult(
    domain="ottersquad.com", name="Otter Squad Plumbing", visibility_score=10,
    unbranded_asked=4, unbranded_cited=0, unbranded_mentioned=0,
    branded_found=True, top_competitors=[("beaverbrigade.com", 3)])
check("a trustworthy absence still qualifies",
      evaluate(prospect, trusted_invisible, site()).status, "QUALIFIED")

check("fallback audit -> REVIEW",
      evaluate(prospect, trusted_invisible,
               site(errors=["sightline_fallback: connection refused"])).rule,
      "AUDIT_ENGINE_DEGRADED")

# The scenario this whole file exists for.
print("\n  scenario: Perplexity renames a field, still returns HTTP 200")
drifted = FakeSession(ok_count=5, payload={
    "choices": [{"message": {"content": "Try Beaver Brigade Plumbing."}}],
    "sources_v2": ["https://beaverbrigade.com"],     # renamed
})
quiet3 = Guard(retry=RetryPolicy(attempts=1), sleep=NO_SLEEP)
drift_result = probe(prospect, api_key="test", session=drifted, guard=quiet3)
check("  drift does not become a zero score", drift_result.trustworthy, False)
check("  and the gate will not send",
      evaluate(prospect, drift_result, site()).status, "REVIEW")
print("  (without the shape check this reads as 'invisible' and qualifies "
      "every prospect in the batch)")

# --------------------------------------------------------------------------
print("\nidempotency key")
from outbox import idempotency_key

k1 = idempotency_key(42, 7, "first_touch")
check("deterministic across workers", k1, idempotency_key(42, 7, "first_touch"))
check("differs per prospect", k1 == idempotency_key(43, 7, "first_touch"), False)
check("differs per stage", k1 == idempotency_key(42, 7, "followup_1"), False)
check("differs per scan", k1 == idempotency_key(42, 8, "first_touch"), False)

# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all failure-mode checks passed")
