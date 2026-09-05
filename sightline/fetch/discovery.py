"""HTML parsing helpers: JSON-LD blocks, meta tags, headings, text extraction."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup


@dataclass
class Page:
    url: str
    html: str
    soup: BeautifulSoup
    title: str = ""
    meta_description: str = ""
    meta_robots: str = ""
    h1s: list[str] = field(default_factory=list)
    jsonld: list[Any] = field(default_factory=list)  # each: dict or list
    canonical: str = ""
    lang: str = ""


def parse_page(url: str, html: str) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    p = Page(url=url, html=html, soup=soup)
    if soup.title and soup.title.string:
        p.title = soup.title.string.strip()
    md = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if md and md.get("content"):
        p.meta_description = md["content"].strip()
    mr = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
    if mr and mr.get("content"):
        p.meta_robots = mr["content"].strip()
    p.h1s = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    canon = soup.find("link", rel=lambda v: v and "canonical" in v)
    if canon and canon.get("href"):
        p.canonical = canon["href"].strip()
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        p.lang = html_tag["lang"].strip()
    for tag in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            p.jsonld.append(json.loads(raw))
        except json.JSONDecodeError:
            p.jsonld.append({"_sightline_parse_error": True, "_raw_excerpt": raw[:200]})
    return p


def flatten_jsonld(blocks: list[Any]) -> list[dict]:
    """@graph and top-level arrays exploded into a flat list of node dicts."""
    out: list[dict] = []
    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            if "@graph" in node and isinstance(node["@graph"], list):
                for n in node["@graph"]:
                    walk(n)
            else:
                out.append(node)
    for b in blocks:
        walk(b)
    return out


def types_of(node: dict) -> list[str]:
    t = node.get("@type")
    if isinstance(t, str):
        return [t]
    if isinstance(t, list):
        return [str(x) for x in t]
    return []


BOILERPLATE_TAGS = ("script", "style", "noscript", "template", "svg",
                    "nav", "header", "footer", "aside")
BOILERPLATE_ROLES = ("navigation", "banner", "contentinfo", "search",
                     "complementary")
BOILERPLATE_SELECTORS = (".skip-link", ".skiplink", ".skip-nav",
                         ".screen-reader-text", ".visually-hidden",
                         ".sr-only", "a[href^='#main']", "a[href^='#skip']",
                         "a[href^='#content']")


def main_content_soup(soup: BeautifulSoup) -> BeautifulSoup:
    """Return a fresh soup with nav / header / footer / aside / skip-links
    removed, so a content-focused check reads real body copy, not menus.

    Non-mutating. The caller's soup is untouched — earlier checks
    (structured_data reads <script> tags in <head>, entity_consistency
    reads sameAs URLs anywhere) still see the raw tree."""
    clone = BeautifulSoup(str(soup), "html.parser")
    for tag_name in BOILERPLATE_TAGS:
        for t in clone.find_all(tag_name):
            t.decompose()
    for role in BOILERPLATE_ROLES:
        for t in clone.find_all(attrs={"role": role}):
            t.decompose()
    for sel in BOILERPLATE_SELECTORS:
        for t in clone.select(sel):
            t.decompose()
    return clone


def visible_text(soup: BeautifulSoup, limit: int = 4000) -> str:
    """Approximate main-content text. Non-mutating."""
    cleaned = main_content_soup(soup)
    main = cleaned.find("main") or cleaned.find("article") or cleaned.body or cleaned
    text = main.get_text(" ", strip=True) if main else ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]
