# Contributing to Punk Records

Thanks for helping. Bug reports, fixes and new memory-file formats are all welcome.

## Setup

You need Python 3.9 or newer and git. There is nothing else to install.

```bash
git clone https://github.com/gablooge/punk-records
cd punk-records
python -m unittest discover -s tests -v   # run the tests
pip install -e .                          # optional: puts `punk` on your PATH
```

Linting uses [ruff](https://docs.astral.sh/ruff/), with the settings in `pyproject.toml`. It is a
development tool only:

```bash
pipx run --spec ruff==0.17.5 ruff check .   # or: pip install ruff==0.17.5 && ruff check .
```

CI runs the tests on Linux, macOS and Windows with Python 3.9 and 3.13, and ruff on Linux.

## Rules

- **Zero runtime dependencies.** Standard library only; git is the one external program. A pull
  request that adds a runtime dependency will not be merged. Optional extras can be discussed in
  an issue first.
- **Python 3.9 syntax.** Start modules with `from __future__ import annotations`. No `match`, and
  no `X | Y` types outside annotations.
- **Tests use `unittest`**, not pytest. Every change in behaviour comes with a test. Tests must
  isolate `HOME`, `USERPROFILE`, `PUNK_HOME` and git config; copy the `Base` class in
  `tests/test_punk.py` rather than touching the real home directory.
- **Bytes stay bytes.** Memory files are hashed byte for byte. Read and write them as bytes, and
  never write in text mode (a test fails if product code calls `write_text` or `open(..., "w")`).
- **Keep the security invariants.** Read [SECURITY.md](SECURITY.md). Any new code path that writes
  files needs tests showing it cannot write outside memory paths.
- **Offline first.** Only `punk init <remote>` (clone) and `punk sync` (also the MCP `sync` tool)
  may touch the network, plus whatever you run through `punk git`. Everything else, including
  `punk import` of a bundle file, works offline.

## Pull requests

Keep each pull request to one change. Describe what it fixes, how you reproduced the problem, and
how you tested it; the pull request template has the sections. Make sure the tests and
`ruff check .` pass before you ask for review.

## Reporting bugs

Open an issue with the bug report form. Run `punk scan` or `punk status` and include the output,
with any private paths or memory text removed. Security problems go through
[SECURITY.md](SECURITY.md), not public issues.
