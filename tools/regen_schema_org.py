#!/usr/bin/env python3
"""Regenerate the Organization subtype block in sightline/checks/schema_org.py.

Run this after a schema.org release — never at scan time:

    .venv/bin/python tools/regen_schema_org.py

It fetches the published vocabulary, walks rdfs:subClassOf transitively down
from schema:Organization, and rewrites the region between the GENERATED
markers in place, stamping the version and date it read. This script is the
only thing in the repo that talks to schema.org: a scan must not depend on
schema.org being up, so the result is committed as a literal.

    --check   exit 1 if the committed block is stale (for a CI job that has
              network access; the normal test suite must not need it)
    --diff    print what would change without writing
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import textwrap
import urllib.request
from collections import defaultdict
from datetime import date

VOCAB_URL = "https://schema.org/version/latest/schemaorg-current-https.jsonld"
VERSIONS_URL = "https://raw.githubusercontent.com/schemaorg/schemaorg/main/versions.json"
ROOT = "schema:Organization"

TARGET = (pathlib.Path(__file__).resolve().parent.parent
          / "sightline" / "checks" / "schema_org.py")

BEGIN = "# --- BEGIN GENERATED (tools/regen_schema_org.py) ---"
END = "# --- END GENERATED ---"


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _ids(value) -> list[str]:
    """rdfs:subClassOf is a dict, a list of dicts, or absent."""
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, str):
        return [value]
    return [v["@id"] if isinstance(v, dict) else v for v in value]


def organization_subtypes(graph: list[dict]) -> list[str]:
    """Every class transitively under schema:Organization, bare names."""
    children: dict[str, set[str]] = defaultdict(set)
    for node in graph:
        if "rdfs:Class" not in _ids(node.get("@type")):
            continue
        for parent in _ids(node.get("rdfs:subClassOf")):
            children[parent].add(node["@id"])

    found: set[str] = set()
    stack = [ROOT]
    while stack:
        for child in children[stack.pop()]:
            if child not in found:
                found.add(child)
                stack.append(child)
    if not found:
        raise SystemExit(f"no subclasses found under {ROOT}; vocabulary format "
                         "changed — fix this script rather than shipping an "
                         "empty set")
    return sorted(name.split(":", 1)[1] for name in found)


def render_block(names: list[str], version: str, when: str) -> str:
    body = textwrap.indent(
        "\n".join(textwrap.wrap(" ".join(f'"{n}",' for n in names), width=74)),
        "    ")
    return (
        f"{BEGIN}\n"
        f"# schema.org {version}, generated {when} from\n"
        f"# {VOCAB_URL}\n"
        f"# {len(names)} classes transitively under schema:Organization.\n"
        f"_GENERATED_SUBTYPES = frozenset({{\n"
        f"{body}\n"
        f"}})\n"
        f"SCHEMA_ORG_VERSION = {version!r}\n"
        f"SCHEMA_ORG_GENERATED = {when!r}\n"
        f"{END}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--diff", action="store_true")
    args = ap.parse_args()

    version = str(_get(VERSIONS_URL).get("schemaversion") or "unknown")
    names = organization_subtypes(_get(VOCAB_URL)["@graph"])

    source = TARGET.read_text()
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
    if not pattern.search(source):
        raise SystemExit(f"generated markers not found in {TARGET}")

    old = pattern.search(source).group(0)
    # Keep the committed generation date when only the date would move, so a
    # no-op run does not produce a diff.
    old_names = set(re.findall(r'"([A-Za-z]+)",', old))
    if set(names) == old_names and f"SCHEMA_ORG_VERSION = {version!r}" in old:
        print(f"up to date: {len(names)} subtypes, schema.org {version}")
        return 0

    new = render_block(names, version, date.today().isoformat())
    added = sorted(set(names) - old_names)
    removed = sorted(old_names - set(names))
    print(f"schema.org {version}: {len(names)} subtypes "
          f"(+{len(added)} / -{len(removed)})")
    for n in added:
        print(f"  + {n}")
    for n in removed:
        print(f"  - {n}")

    if args.check:
        print("committed block is stale; run without --check to update",
              file=sys.stderr)
        return 1
    if args.diff:
        print("\n" + new)
        return 0
    TARGET.write_text(pattern.sub(lambda _: new, source))
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
