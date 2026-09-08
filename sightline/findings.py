"""The findings module: the one place client-facing copy is written.

Every Finding carries two blocks, produced by the same call:

    technical  {title, detail}           Sightline's own UI. Current wording.
    plain      {title, why, do, payoff}  Cited. Written for a reader who has
                                         never heard the acronym.

`build()` returns both or raises. Nothing downstream may author, edit, or
substitute either block — the renderer and the JSON export read what this
module returned. That is the whole reason the module exists: plain copy
written next to the view that shows it drifts from the copy rules inside one
sprint, and then two surfaces describe the same finding differently.

The rules both blocks must satisfy live in COPY.md at the repo root.
tests/test_findings_copy.py enforces the mechanical ones. Read COPY.md before
adding or editing a template below.

Copy is DERIVED, not stored. A finding row keeps its raw observation plus
impact/effort_minutes/owner; the prose is rebuilt on read from the templates
here and the scan's brand/category profile. Storing prose would freeze a
client's report at the wording that shipped the day it was scanned, so fixing
a sentence in COPY.md would fix nothing already sold. Deriving means one copy
fix improves every historical report.

`severity` is not in either block. It is a scoring input (see
scoring/weights.py, which keys deductions off it) and never reaches a
client-facing view. What a client sees is impact, effort and owner, as three
separate values — COPY.md rule 4.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

# Raw check output. Aliased because `Finding` in this module is the
# client-facing schema; see checks/base.py for the distinction.
from .checks.base import Finding as CheckOutput

IMPACTS = ("high", "medium", "low")
OWNERS = ("client", "swa")

# What the copy has to distinguish, which is not the same axis as severity.
GAP = "gap"
PASS = "pass"
UNMEASURED = "unmeasured"

_OUTCOME = {
    "critical": GAP, "high": GAP, "medium": GAP, "low": GAP,
    "pass": PASS, "info": PASS, "unavailable": UNMEASURED,
}


@dataclass(frozen=True)
class Technical:
    title: str
    detail: str


@dataclass(frozen=True)
class Plain:
    title: str      # names an action (rule 6)
    why: str        # ends in a business consequence (rule 7)
    do: str
    payoff: str


@dataclass(frozen=True)
class ClientProfile:
    """Who the report is about, in the words the client would use. Interpolated
    into plain.why so it cannot read like a mail-merge."""
    brand: str = "this business"
    category: str = "business"


@dataclass(frozen=True)
class Copy:
    title: str
    why: str
    do: str
    payoff: str


@dataclass(frozen=True)
class Task:
    impact: str
    effort_minutes: int
    owner: str


# --------------------------------------------------------------------------
# brand + category
# --------------------------------------------------------------------------

# Plain words for the schema types we detect. The client says "plumbing
# company"; nobody outside this repo says "HomeAndConstructionBusiness".
CATEGORY_WORDS = {
    "plumber": "plumbing company",
    "electrician": "electrical contractor",
    "roofingcontractor": "roofing contractor",
    "hvacbusiness": "heating and cooling company",
    "generalcontractor": "contractor",
    "homeandconstructionbusiness": "home services company",
    "dentist": "dental practice",
    "physician": "medical practice",
    "medicalbusiness": "medical practice",
    "attorney": "law firm",
    "legalservice": "law firm",
    "realestateagent": "real estate agency",
    "restaurant": "restaurant",
    "foodestablishment": "restaurant",
    "store": "shop",
    "autorepair": "auto repair shop",
    "autodealer": "car dealership",
    "professionalservice": "professional services firm",
    "localbusiness": "local business",
    "organization": "business",
}

_TITLE_SPLIT = re.compile(r"\s*[|–—·]\s*|\s+-\s+")


def _possessive(name: str) -> str:
    """"Smart Web Advisors" -> "Smart Web Advisors'", not "Advisors's".
    Templates interpolate {brand_s} wherever they need the possessive."""
    return name + ("'" if name.endswith(("s", "S")) else "'s")


def _brand_from_domain(domain: str) -> str:
    stem = (domain or "").lower().replace("www.", "").split(".")[0]
    return stem.replace("-", " ").title() or "this business"


def profile_from_page(page: Any, domain: str) -> ClientProfile:
    """Best available brand + category. Falls back through page title to the
    domain stem, so plain.why is never left holding a placeholder."""
    from .fetch.discovery import flatten_jsonld, types_of

    brand = ""
    category = ""
    nodes = flatten_jsonld(page.jsonld) if (page and page.jsonld) else []
    seen = {t.lower() for n in nodes for t in types_of(n)}
    for key, word in CATEGORY_WORDS.items():
        if key in seen:
            category = word
            break
    for n in nodes:
        if {t.lower() for t in types_of(n)} & set(CATEGORY_WORDS):
            brand = str(n.get("name") or "").strip()
            if brand:
                break
    if not brand and page and page.title:
        brand = _TITLE_SPLIT.split(page.title.strip())[0].strip()
    return ClientProfile(brand=brand or _brand_from_domain(domain),
                         category=category or "business")


def profile_from_meta(meta: dict | None) -> ClientProfile:
    p = (meta or {}).get("profile") or {}
    return ClientProfile(brand=p.get("brand") or "this business",
                         category=p.get("category") or "business")


def profile_from_rows(rows: Iterable[dict], domain: str) -> ClientProfile:
    """Recover a profile from already-stored findings, for the backfill of
    scans that ran before profiles were persisted."""
    brand, category = "", ""
    seen: set[str] = set()
    for r in rows:
        ev = r.get("evidence") or {}
        if not brand and r.get("check_id") == "entity_consistency":
            brand = str(ev.get("name") or "").strip()
        seen.update(str(t).lower() for t in (ev.get("types") or []))
    # Resolve in CATEGORY_WORDS order, which runs specific -> generic, the
    # same way profile_from_page does. Iterating the stored `types` list
    # instead picks whatever sorts first, and since that list is sorted
    # alphabetically 'Organization' beat 'ProfessionalService' on every
    # site that declares both -- so a backfilled scan said "a business"
    # where a live scan of the same page said "a professional services
    # firm". Both paths must produce identical copy for identical input.
    for key, word in CATEGORY_WORDS.items():
        if key in seen:
            category = word
            break
    return ClientProfile(brand=brand or _brand_from_domain(domain),
                         category=category or "business")


# --------------------------------------------------------------------------
# the copy
# --------------------------------------------------------------------------

# The noun phrase each check is about, in plain words. Used by the shared
# pass/unmeasured copy so a passing check still reads as a sentence.
SUBJECT = {
    "ai_crawler":         "the access assistants have to your site",
    "structured_data":    "your machine-readable business description",
    "entity_consistency": "your business details",
    "answer_first":       "your question-and-answer pages",
    "llms_txt":           "your assistant summary page",
    "pagespeed":          "your page speed",
    "rank":               "your search footprint",
    "prompt_testing":     "our visibility testing",
}

PASS_COPY = Copy(
    title="Keep {subject} as it is",
    why=("{brand} already has this right. It is one of the things an assistant "
         "checks before it will describe a {category} with any confidence."),
    do="Nothing to change. Keep it this way the next time the site is rebuilt.",
    payoff="Nothing here is holding {brand} back.",
)

UNMEASURED_COPY = Copy(
    title="We re-run this measurement",
    why=("We could not get a reading on this for {brand} during this scan, so "
         "we will not claim it is either good or bad."),
    do="We re-run the measurement and tell you what it says. Nothing for you to do.",
    payoff="You get a number you can rely on instead of a guess.",
)

# One gap entry per check. Add a "check_id:item_key" key only where the
# generic wording would be wrong for a specific item.
PLAIN_GAP: dict[str, Copy] = {
    "ai_crawler": Copy(
        title="Let assistants like ChatGPT read your site",
        why=("Right now {brand_s} site turns away the automated readers that "
             "ChatGPT, Claude, Perplexity and Google's answer features use to "
             "look things up. When someone asks one of them for a {category}, "
             "you are not among the sources it is allowed to see."),
        do=("Ask whoever manages your website to stop turning these readers "
            "away. It is a one-line change and it takes effect immediately."),
        payoff=("{brand} becomes eligible to be named when a customer asks an "
                "assistant for a {category}."),
    ),
    "structured_data": Copy(
        title="Describe your business so machines can read it",
        why=("Nothing on {brand_s} site states in machine-readable terms that "
             "you are a {category}, where you work, or how to reach you. "
             "Assistants are left to guess from your marketing copy, and they "
             "guess wrong in ways you never see."),
        do=("Have whoever maintains your site add a hidden, machine-readable "
            "description of the business — legal name, address, phone, hours "
            "and the services you sell — to every page template."),
        payoff=("Assistants can state what {brand} does and where, instead of "
                "skipping you for a competitor they can describe in one line."),
    ),
    "entity_consistency": Copy(
        title="Make your business details agree everywhere",
        why=("{brand_s} name, phone number and address do not agree from one "
             "part of your own site to another, so an assistant comparing "
             "{category} options has no way to tell which version is current — "
             "and tends to drop the ones it cannot pin down."),
        do=("Decide which name, address and phone are correct, then have them "
            "corrected everywhere they appear on the site."),
        payoff="One consistent record is easier to trust and easier to repeat back.",
    ),
    "answer_first": Copy(
        title="Answer your customers' questions on the page",
        why=("{brand_s} pages are written as marketing statements rather than "
             "answers. When someone asks an assistant a real question about "
             "{category} work, there is no passage on your site it can quote, "
             "so it quotes somebody else."),
        do=("Under a heading phrased as the question a customer actually asks, "
            "put the direct answer in the first two sentences. Detail after."),
        payoff="Assistants quote whoever answered the question first and plainest.",
    ),
    "llms_txt": Copy(
        title="Publish a short summary for assistants",
        why=("{brand} does not publish the short, plain summary some assistants "
             "look for when they want a quick statement of what a {category} "
             "offers and where — so they build that summary themselves, from "
             "whatever they happen to find."),
        do=("Ask your developer to publish a one-page summary of your services, "
            "service area and contact details at a fixed address on the site."),
        payoff="Cheap to add, and still rare among local competitors.",
    ),
    "pagespeed": Copy(
        title="Speed up your pages on phones",
        why=("{brand_s} pages are slow to load on a phone. Slow {category} "
             "sites get abandoned by customers before the page paints, and get "
             "read less often by the automated readers that gather sources."),
        do=("Have your developer compress the images and remove unused code on "
            "your slowest pages, starting with the homepage."),
        payoff="Faster pages get seen by more customers and read more often by assistants.",
    ),
    "rank": Copy(
        title="Widen the searches you show up for",
        why=("{brand} shows up for very few of the searches a {category} "
             "customer actually types, and very few other sites link to you. "
             "Assistants that lean on search results have little reason to "
             "reach your pages at all."),
        do=("Publish a page for each service and each town you actually serve, "
            "and ask suppliers, trade associations and local press to link to you."),
        payoff="More ways to be found is more chances to be the one named.",
    ),
    # owner="swa": written as work we perform, never as an instruction.
    "prompt_testing": Copy(
        title="We test how assistants answer questions about you",
        why=("Nobody currently knows whether ChatGPT, Claude, Perplexity or "
             "Google's answer features name {brand} when someone asks for a "
             "{category} — not you and not us — until somebody asks them "
             "repeatedly and writes down what comes back."),
        do=("We run a fixed set of buyer questions against each assistant on a "
            "schedule and log every answer, so your visibility is measured "
            "instead of assumed."),
        payoff=("You find out whether you are being named, how often, and "
                "whether that changes after the work above."),
    ),
}

TASK_GAP: dict[str, Task] = {
    "ai_crawler":         Task("high",    15, "client"),
    "structured_data":    Task("high",   120, "client"),
    "entity_consistency": Task("medium",  45, "client"),
    "answer_first":       Task("high",   180, "client"),
    "llms_txt":           Task("low",     30, "client"),
    "pagespeed":          Task("medium", 240, "client"),
    "rank":               Task("medium", 480, "client"),
    "prompt_testing":     Task("high",    90, "swa"),
}

TASK_PASS = Task("low", 0, "client")
TASK_UNMEASURED = Task("low", 15, "swa")   # we re-measure; client does nothing


@dataclass
class Finding:
    check_id: str
    item_key: str
    severity: str                  # scoring input only; never client-facing
    technical: Technical
    plain: Plain
    impact: str
    effort_minutes: int
    owner: str
    remediation: str = ""          # technical-side fix text, Sightline UI only
    evidence: dict[str, Any] = field(default_factory=dict)
    deduction: float | None = None

    def __post_init__(self) -> None:
        if self.impact not in IMPACTS:
            raise ValueError(f"invalid impact {self.impact!r} for {self.check_id}")
        if self.owner not in OWNERS:
            raise ValueError(f"invalid owner {self.owner!r} for {self.check_id}")
        if int(self.effort_minutes) < 0:
            raise ValueError(f"negative effort for {self.check_id}")
        if not (self.technical.title and self.technical.detail):
            raise ValueError(f"empty technical block for {self.check_id}")
        for name in ("title", "why", "do", "payoff"):
            if not getattr(self.plain, name):
                raise ValueError(f"empty plain.{name} for {self.check_id}")

    @property
    def outcome(self) -> str:
        return _OUTCOME.get(self.severity, GAP)


def _fill(copy: Copy, profile: ClientProfile, subject: str) -> Plain:
    def f(s: str) -> str:
        return s.format(brand=profile.brand,
                        brand_s=_possessive(profile.brand),
                        category=profile.category, subject=subject)
    return Plain(title=f(copy.title), why=f(copy.why), do=f(copy.do),
                 payoff=f(copy.payoff))


def _copy_and_task(check_id: str, item_key: str,
                   outcome: str) -> tuple[Copy, Task]:
    key = f"{check_id}:{item_key}"
    copy = PLAIN_GAP.get(key) or PLAIN_GAP.get(check_id)
    task = TASK_GAP.get(key) or TASK_GAP.get(check_id)
    # Work we perform is ours whatever the measurement said. A standing task
    # does not become a client instruction because this week's sample came
    # back fine, so ownership is a property of the check and is resolved
    # before outcome. Outcome-driven copy applies to client-owned checks
    # only. (Without this, prompt_testing's severity='info' fell through to
    # the generic pass copy and shipped as owner='client'.)
    if task is not None and task.owner == "swa":
        return copy, task
    if outcome == PASS:
        return PASS_COPY, TASK_PASS
    if outcome == UNMEASURED:
        return UNMEASURED_COPY, TASK_UNMEASURED
    if copy is None or task is None:
        raise KeyError(
            f"no plain copy for check {check_id!r}: every finding needs both "
            "blocks. Add an entry to PLAIN_GAP/TASK_GAP and read COPY.md."
        )
    return copy, task


def build(obs: CheckOutput, profile: ClientProfile) -> Finding:
    """The single seam. technical and plain are produced here, together, or
    not at all."""
    outcome = _OUTCOME.get(obs.severity, GAP)
    copy, task = _copy_and_task(obs.check_id, obs.item_key, outcome)
    subject = SUBJECT.get(obs.check_id, "this")
    return Finding(
        check_id=obs.check_id,
        item_key=obs.item_key,
        severity=obs.severity,
        technical=Technical(title=obs.examined, detail=obs.observed),
        plain=_fill(copy, profile, subject),
        impact=task.impact,
        effort_minutes=task.effort_minutes,
        owner=task.owner,
        remediation=obs.remediation,
        evidence=obs.evidence,
    )


def build_all(observations: Iterable[CheckOutput],
              profile: ClientProfile) -> list[Finding]:
    return [build(o, profile) for o in observations]


def from_row(row: dict, profile: ClientProfile) -> Finding:
    """Rebuild a Finding from a stored row, so the renderer and the export
    read the same two blocks from the same templates."""
    obs = CheckOutput(
        check_id=row["check_id"], severity=row["severity"],
        examined=row["examined"], observed=row["observed"],
        remediation=row.get("remediation") or "",
        item_key=row.get("item_key") or "", evidence=row.get("evidence") or {},
    )
    f = build(obs, profile)
    # Stored task fields win over the tables: a finding sold at 15 minutes
    # stays 15 minutes even if we later re-estimate the check.
    if row.get("impact") in IMPACTS:
        f = replace(f, impact=row["impact"])
    if row.get("effort_minutes") is not None:
        f = replace(f, effort_minutes=int(row["effort_minutes"]))
    if row.get("owner") in OWNERS:
        f = replace(f, owner=row["owner"])
    f.deduction = row.get("deduction")
    return f


# --------------------------------------------------------------------------
# scores and the one disclaimer (COPY.md rules 5 and 8)
# --------------------------------------------------------------------------

SCALE = 100
BANDS = ((90, "strong"), (75, "solid"), (60, "mixed"), (40, "weak"),
         (0, "critical gaps"))

# Below this, a "typical site scores X" line is an assertion, not evidence.
MIN_PEER_DOMAINS = 5


def band(score: float | None) -> str:
    if score is None:
        return "not measured"
    for floor, label in BANDS:
        if score >= floor:
            return label
    return "critical gaps"


def score_phrase(score: float | None) -> str:
    """Rule 5, first half: the scale travels with the number, always."""
    if score is None:
        return "not measured"
    return f"{score:.0f} out of {SCALE} — {band(score)}"


def peer_sentence(score: float | None, peers: dict | None) -> str:
    """Rule 5, second half. Empty string when we cannot evidence a
    comparison — an honest silence beats a peer claim built on three sites."""
    if score is None or not peers:
        return ""
    n, median = peers.get("n_domains") or 0, peers.get("median")
    if n < MIN_PEER_DOMAINS or median is None:
        return ""
    side = ("above" if score > median else
            "below" if score < median else "level with")
    return (f"That is {side} the median of {median:.0f} for the {n} other "
            f"sites we have scanned.")


def disclaimer(profile: ClientProfile) -> str:
    """Rule 8. The only disclaimer text in the product. Rendered once,
    directly under the score block. Nothing else in any view hedges."""
    return (
        f"How to read these numbers. Every score here runs from 0 to {SCALE}, "
        f"where {SCALE} is the best we can measure and 0 the worst. They "
        f"describe what we could see on the pages we fetched, on the date "
        f"shown. They are not a promise about where {profile.brand} will "
        f"appear in Google or in any assistant — those are other "
        f"companies' systems and nobody controls them. Two scores here can "
        f"disagree without either being wrong: one looks at these pages, the "
        f"other at how the whole site stands in search. And any visibility "
        f"figure we sample moves up and down between measurements, so read it "
        f"across several samples rather than one."
    )
