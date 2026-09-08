# Copy rules

Binding on `sightline/findings.py`, which is the only module allowed to write
client-facing sentences. The mechanical rules are enforced by
`tests/test_findings_copy.py`; the judgement ones are enforced by review.

Read this before adding or editing a template.

### 1. No acronym without its meaning on first use in any client-facing view.
"AEO", "SEO", "CWV", "PSI", "NAP" mean nothing to the person paying the
invoice. Either expand it on first use or don't use it.

### 2. Never name a markup language, file format, or spec in `plain`.
No `JSON-LD`, `robots.txt`, `llms.txt`, `schema.org`, `HTML`, `XML sitemap`,
`rel=canonical`. Describe what the thing *does*: "the machine-readable
description of your business", "the instructions your site gives automated
visitors". The client is not going to edit the file; their developer is, and
their developer reads `technical`.

### 3. Every task names its owner — "you do this" or "we do this".
A task with no owner is a task nobody does. `owner: "client"` copy is written
in the imperative to them; `owner: "swa"` copy is written as work we perform,
never as an instruction they'd have to follow.

### 4. Impact and effort are separate labelled fields, never one fused word.
"Quick win" and "high lift" hide the two numbers a client needs to schedule
work. Ship `impact` and `effort_minutes` as discrete values and let the view
label them. Never concatenate them into a phrase, in any renderer or export.

### 5. Every score carries its scale and a peer comparison.
"62" is not a number, it's a mood. "62 out of 100 — mixed" is a number.
Where we have no peer data, say nothing rather than assert a comparison we
can't evidence: the peer line is suppressed below
`findings.MIN_PEER_DOMAINS`, and the scale is printed always.

### 6. No title corrects a belief the reader never held.
"You are not invisible to ChatGPT" answers a question nobody asked and
plants the worry it then denies. Titles name the action:
"Let assistants like ChatGPT read your site".

### 7. State the business consequence, not just the technical gap.
"No structured data" is an observation. "Assistants are left to guess what
you sell, and they guess wrong in ways you never see" is a reason to pay.
Every `plain.why` ends in a consequence to the business.

### 8. One disclaimer, stated once, in one place, in plain words.
Three hedges scattered through a report read as three separate admissions of
doubt. `findings.disclaimer()` is the only disclaimer text, rendered once,
directly under the score block. Nothing else in any view hedges.

---

## Where the rules are checked

| Rule | Enforcement |
|---|---|
| 1, 2 | `test_plain_names_no_format_or_bare_acronym` |
| 3 | `Finding.__post_init__` (owner required + validated) |
| 4 | `test_export_keeps_impact_and_effort_separate` |
| 5 | `test_every_rendered_score_carries_its_scale` |
| 6 | review; `test_plain_titles_are_not_negations` catches the common shapes |
| 7 | review |
| 8 | `test_report_states_one_disclaimer_once` |
