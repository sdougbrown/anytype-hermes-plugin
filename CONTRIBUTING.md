# Contributing

Issues and PRs welcome. A few things that'll make a PR easier to review:

- Run `pytest` before opening a PR — see [README.md](README.md#development) for setup.
- If you're touching `tools.py` or `anytype_client.py`, add a test alongside
  the change rather than relying on manual verification (see
  [docs/design.md Section 7](docs/design.md#7-testing) for what's already covered
  and why real-instance integration tests aren't part of CI).
- Read [docs/design.md](docs/design.md) first if your change touches
  architecture or scope — Section 2 lists decisions that were deliberately made
  over alternatives, so re-litigating them needs a real reason, not just a
  different preference.
- Keep the plugin schema-agnostic. Nothing in `tools.py` or
  `anytype_client.py` should assume a particular set of type/property names
  — the whole point is that it works against anyone's custom Anytype setup
  via live introspection.

No CLA, no formal process. If something's unclear, open an issue and ask.
