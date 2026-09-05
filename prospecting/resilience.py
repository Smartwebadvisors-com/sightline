"""
resilience.py -- guards for every call that leaves this machine.

The governing rule: THE PIPELINE FAILS CLOSED. Every stage before sending is
retryable and cheap to redo. Sending is the only irreversible act. So any
uncertainty anywhere upstream must degrade toward *not* contacting someone,
never toward contacting them on data we are not sure about.

That rule matters because the dangerous failure here is not a timeout. It is a
dependency that returns HTTP 200 with a shape we did not expect, which silently
becomes "this business is invisible in AI search", which qualifies everyone,
which sends two hundred people a claim that is false. A stack trace costs an
hour. That costs the domain.

Four guards, in the order they engage:

    Budget          hard ceiling on calls and spend per run. Stops the loop.
    CircuitBreaker  N consecutive failures on a dependency -> stop calling it.
    RetryPolicy     backoff with jitter, only on genuinely retryable errors.
    validate        shape assertions, so a wrong-shaped 200 is an ERROR and
                    never a zero.

All four are process-local and dependency-free on purpose: no Redis, no extra
service, nothing new that can itself go down.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, TypeVar

import requests

LOG = logging.getLogger("aeo.resilience")

T = TypeVar("T")


# ---------------------------------------------------------------------------
# error classification
# ---------------------------------------------------------------------------

class DependencyError(RuntimeError):
    """A call to an external system failed. Carries whether retrying is sane."""

    def __init__(self, dependency: str, message: str, retryable: bool = False,
                 status: int | None = None):
        super().__init__(f"[{dependency}] {message}")
        self.dependency = dependency
        self.message = message
        self.retryable = retryable
        self.status = status


class BudgetExhausted(RuntimeError):
    """A hard cap was hit. Never retried, never worked around."""


class CircuitOpen(DependencyError):
    """The breaker is open for this dependency; the call was not attempted."""

    def __init__(self, dependency: str, until: float):
        super().__init__(dependency, f"circuit open for {until - time.time():.0f}s more",
                         retryable=False)
        self.until = until


class ShapeError(DependencyError):
    """A 200 response that does not contain what we need.

    Deliberately its own class: this is the failure that silently corrupts
    scores, and it must never be mistaken for 'the business was absent'.
    """

    def __init__(self, dependency: str, message: str):
        super().__init__(dependency, f"unexpected response shape: {message}",
                         retryable=False)


RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 522, 524}


def classify(dependency: str, exc: Exception) -> DependencyError:
    """Turn any exception into a DependencyError with a retryable verdict."""
    if isinstance(exc, DependencyError):
        return exc

    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        return DependencyError(
            dependency, f"HTTP {status}", retryable=status in RETRYABLE_STATUS,
            status=status,
        )
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return DependencyError(dependency, f"{type(exc).__name__}", retryable=True)
    if isinstance(exc, ValueError):        # includes JSONDecodeError
        return DependencyError(dependency, f"bad payload: {exc}", retryable=False)
    return DependencyError(dependency, f"{type(exc).__name__}: {exc}", retryable=False)


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------

@dataclass
class Budget:
    """Hard ceilings for one run. Hitting one stops the loop; it never degrades.

    A runaway retry loop against a metered API is the failure mode that shows
    up on a bill rather than in a log, so the cap is checked before every call
    and raising is the only outcome.
    """
    max_calls: dict[str, int] = field(default_factory=dict)   # per dependency
    max_spend_usd: float | None = None
    cost_per_call: dict[str, float] = field(default_factory=dict)

    calls: dict[str, int] = field(default_factory=dict)
    spend_usd: float = 0.0

    def check(self, dependency: str) -> None:
        limit = self.max_calls.get(dependency)
        used = self.calls.get(dependency, 0)
        if limit is not None and used >= limit:
            raise BudgetExhausted(
                f"{dependency}: call cap reached ({used}/{limit})")
        if self.max_spend_usd is not None:
            next_cost = self.cost_per_call.get(dependency, 0.0)
            if self.spend_usd + next_cost > self.max_spend_usd:
                raise BudgetExhausted(
                    f"spend cap reached (${self.spend_usd:.2f} + "
                    f"${next_cost:.2f} > ${self.max_spend_usd:.2f})")

    def record(self, dependency: str) -> None:
        self.calls[dependency] = self.calls.get(dependency, 0) + 1
        self.spend_usd += self.cost_per_call.get(dependency, 0.0)

    def summary(self) -> str:
        parts = [f"{k}={v}" for k, v in sorted(self.calls.items())]
        return f"calls({', '.join(parts) or 'none'}) spend=${self.spend_usd:.2f}"


# ---------------------------------------------------------------------------
# circuit breaker
# ---------------------------------------------------------------------------

@dataclass
class CircuitBreaker:
    """Stops hammering a dependency that is clearly down.

    Consecutive failures only: a single blip among successes is noise, five in
    a row is an outage. While open, calls raise CircuitOpen immediately -- the
    caller records the prospect as unknown and moves on, rather than waiting
    out fifty timeouts.
    """
    threshold: int = 5
    cooldown_s: float = 300.0

    _failures: dict[str, int] = field(default_factory=dict)
    _open_until: dict[str, float] = field(default_factory=dict)

    def is_open(self, dependency: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        until = self._open_until.get(dependency)
        if until is None:
            return False
        if now >= until:
            # half-open: clear it and let one call through to test the water
            self._open_until.pop(dependency, None)
            self._failures[dependency] = 0
            return False
        return True

    def guard(self, dependency: str, now: float | None = None) -> None:
        if self.is_open(dependency, now):
            raise CircuitOpen(dependency, self._open_until[dependency])

    def record_success(self, dependency: str) -> None:
        self._failures[dependency] = 0

    def record_failure(self, dependency: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        count = self._failures.get(dependency, 0) + 1
        self._failures[dependency] = count
        if count >= self.threshold:
            self._open_until[dependency] = now + self.cooldown_s
            LOG.error("circuit OPEN for %s after %d consecutive failures; "
                      "pausing %.0fs", dependency, count, self.cooldown_s)

    def state(self) -> dict[str, str]:
        now = time.time()
        out = {}
        for dep in set(self._failures) | set(self._open_until):
            if self.is_open(dep, now):
                out[dep] = f"OPEN ({self._open_until[dep] - now:.0f}s left)"
            else:
                out[dep] = f"closed ({self._failures.get(dep, 0)} recent failures)"
        return out


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------

@dataclass
class RetryPolicy:
    """Exponential backoff with full jitter.

    Jitter matters more than it looks: without it, a batch of prospects that
    all hit a rate limit retries in lockstep and hits it again together.
    """
    attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter: bool = True

    def delay_for(self, attempt: int) -> float:
        raw = min(self.base_delay_s * (2 ** attempt), self.max_delay_s)
        return random.uniform(0, raw) if self.jitter else raw


@dataclass
class Guard:
    """One object wiring budget + breaker + retry around any callable.

        guard = Guard()
        payload = guard.call("perplexity", lambda: post(...))

    Raises DependencyError (or BudgetExhausted) instead of returning junk.
    Callers treat that as UNKNOWN -- never as a zero score.
    """
    budget: Budget = field(default_factory=Budget)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Callable[[float], None] = time.sleep

    def call(self, dependency: str, fn: Callable[[], T],
             validate: Callable[[T], None] | None = None) -> T:
        self.breaker.guard(dependency)

        last: DependencyError | None = None
        for attempt in range(self.retry.attempts):
            self.budget.check(dependency)      # raises BudgetExhausted, never retried
            try:
                result = fn()
                self.budget.record(dependency)
                if validate is not None:
                    validate(result)           # ShapeError is not retryable
                self.breaker.record_success(dependency)
                return result
            except BudgetExhausted:
                raise
            except Exception as exc:
                err = classify(dependency, exc)
                if isinstance(exc, requests.RequestException):
                    self.budget.record(dependency)   # a failed call still costs
                last = err
                self.breaker.record_failure(dependency)
                if not err.retryable or attempt == self.retry.attempts - 1:
                    raise err
                wait = self.retry.delay_for(attempt)
                LOG.warning("%s failed (%s); retry %d/%d in %.1fs",
                            dependency, err.message, attempt + 1,
                            self.retry.attempts - 1, wait)
                self.sleep(wait)

        raise last or DependencyError(dependency, "exhausted retries")


# ---------------------------------------------------------------------------
# shape validation
# ---------------------------------------------------------------------------

def require(condition: bool, dependency: str, message: str) -> None:
    if not condition:
        raise ShapeError(dependency, message)


def validate_probe_payload(payload: Any) -> None:
    """A Sonar response we can actually score.

    This is the guard that matters most. When Perplexity moves an endpoint or
    renames a field, the request still returns 200 and the body still parses
    as JSON -- it just has no citations. Without this check, that reads as
    'nobody cites this business', every prospect scores 0, every prospect
    qualifies, and the batch is confidently wrong. It has to be an error.
    """
    dep = "perplexity"
    require(isinstance(payload, dict), dep, f"not an object ({type(payload).__name__})")

    choices = payload.get("choices")
    require(isinstance(choices, list) and bool(choices), dep, "no choices[]")

    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    require(isinstance(content, str) and content.strip() != "", dep, "empty content")

    # Either key may be absent on its own, but a grounded answer with neither
    # means we are not looking at the response we think we are.
    has_citations = isinstance(payload.get("citations"), list)
    has_results = isinstance(payload.get("search_results"), list)
    require(has_citations or has_results, dep,
            "neither citations[] nor search_results[] present -- "
            "endpoint or response format has probably changed")
