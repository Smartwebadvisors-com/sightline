"""Assistant-visibility sampling. v1 is a passive recorder: you (or a
future scheduler) run a query against a model and hand Sightline the
result. Sightline stores every observation and computes a trend over
sample size on read.

The design principle: one sample is not evidence. Report treats N<10 as
insufficient. See db.write_av_observation / db.av_summary."""

# No code here in v1 — the CLI subcommand `av-sample` in cli.py writes
# observations directly via db.write_av_observation. A future scheduler
# will live in this module and drive Anthropic/OpenAI API calls, but that
# is deliberately out of scope for v1 to keep the scan cost bounded and
# avoid coupling Sightline's release to model-provider auth setup.
