# Contributing to OpsMender

Thanks for helping! OpsMender is a small, focused project: an AI DevOps companion for Coolify that
stays light on the servers it watches. Contributions that keep it that way are very welcome.

## Ways to help

- **Test against your Coolify version** and report anything that doesn't sync or parse correctly.
  Include your Coolify version and the relevant OpsMender container logs, with secrets removed.
- **Add diagnosis rules** for failures you've hit (`opsmender/diagnosis/rules.py`). A good rule has
  a precise pattern, a plain-language explanation, concrete fix steps, and a test.
- **Improve the UI, docs, or AI prompts.**
- **Add adapters** for other platforms by implementing the `Provider` interface in
  `opsmender/adapters/base.py`.

For anything larger than a bug fix, please open an issue first so we can agree on the approach.

## Development setup

```sh
git clone https://github.com/abiola814/OpsMender.git
cd OpsMender
uv venv && uv pip install -e ".[dev]"    # or: python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
OPSMENDER_DATA_DIR=./data .venv/bin/python -m opsmender   # http://localhost:8000
```

The setup code for the first admin account is printed in the terminal.

To test the server probe without a real VPS, run it in a container:

```sh
docker run --rm -v "$PWD/opsmender/metrics/probe.sh:/p:ro" debian:bookworm-slim sh /p snapshot
```

## Guidelines

- **Lightweight first.** No new always-on agents, heavy dependencies, or background work that scales
  with log volume. Fetch on demand, bound everything (lines, bytes, time), and store summaries rather
  than raw data.
- **Safe by default.** Anything that changes a user's infrastructure must be opt-in, confirmed, and
  audited. Never send unredacted logs to an AI provider.
- **The probe stays POSIX `sh`.** It must work with dash and busybox, stay read-only, and validate
  every input.
- **Tolerate API drift.** Coolify field names change between releases; read them defensively.
- **Match the surrounding code** for style and naming. Keep comments for the non-obvious "why".
- **Add or update tests** for behaviour changes. `pytest` must pass.

## Pull requests

1. Fork and create a branch from `main`.
2. Keep each PR focused on one change, with a clear description of what and why.
3. Make sure `pytest` passes and the Docker image builds (`docker build .`).
4. Note any user-visible change in `CHANGELOG.md` under "Unreleased".

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).

Please follow our [Code of Conduct](CODE_OF_CONDUCT.md).
