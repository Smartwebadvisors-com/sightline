"""Small robots.txt parser focused on what Sightline needs:
does a given user-agent get blocked from a given path?"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse


def request_target(url: str) -> str:
    """RFC 9309 request-target: path + optional '?query'. Robots rules
    match against this, not the origin root. See scan-22 regression:
    every rule used to be evaluated against '/', making path-scoped
    Disallows invisible on any deep-URL scan."""
    p = urlparse(url)
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    return path


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    disallow: list[str] = field(default_factory=list)


@dataclass
class Robots:
    raw: str
    groups: list[Group] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)


def parse(text: str) -> Robots:
    r = Robots(raw=text)
    current: Group | None = None
    last_was_agent = False
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()
        if field_name == "user-agent":
            if not last_was_agent or current is None:
                current = Group()
                r.groups.append(current)
            current.agents.append(value)
            last_was_agent = True
        elif field_name in ("allow", "disallow"):
            last_was_agent = False
            if current is None:
                current = Group(agents=["*"])
                r.groups.append(current)
            (current.allow if field_name == "allow" else current.disallow).append(value)
        elif field_name == "sitemap":
            r.sitemaps.append(value)
    return r


def _match_rule(rule: str, path: str) -> bool:
    """robots.txt rule matching. '$' anchors end. '*' matches any run."""
    if not rule:
        return False
    # Compile rule to a simple regex-free matcher.
    i = j = 0
    while i < len(rule) and j <= len(path):
        c = rule[i]
        if c == "*":
            # Skip past run of '*' and try to match remainder anywhere.
            while i < len(rule) and rule[i] == "*":
                i += 1
            if i == len(rule):
                return True
            # Try each position in path.
            remainder = rule[i:]
            for k in range(j, len(path) + 1):
                if _match_rule(remainder, path[k:]):
                    return True
            return False
        if c == "$":
            return j == len(path) and i == len(rule) - 1
        if j < len(path) and path[j] == c:
            i += 1
            j += 1
            continue
        return False
    return i == len(rule)


def is_blocked(robots: Robots, agent: str, path: str = "/") -> tuple[bool, str]:
    """Return (blocked, matching_rule). Follows the longest-match rule.
    Longer rule beats shorter; on tie, Allow wins (RFC 9309)."""
    matches: list[tuple[int, bool, str]] = []  # (length, is_disallow, rule)
    agent_l = agent.lower()
    # Find groups that apply. Specific agent name > wildcard.
    specific = [g for g in robots.groups
                if any(a.lower() == agent_l for a in g.agents)]
    groups = specific or [g for g in robots.groups if "*" in g.agents]
    for g in groups:
        for rule in g.allow:
            if _match_rule(rule, path):
                matches.append((len(rule), False, rule))
        for rule in g.disallow:
            if _match_rule(rule, path):
                if rule == "":
                    # Explicit "Disallow:" empty means allow all.
                    continue
                matches.append((len(rule), True, rule))
    if not matches:
        return False, ""
    matches.sort(key=lambda m: (-m[0], m[1]))  # longest first, allow beats disallow
    length, is_disallow, rule = matches[0]
    return is_disallow, rule
