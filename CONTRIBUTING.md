# Contributing to git-why

Thanks for your interest! This document explains how to set up a development
environment, what we expect from changes, and how releases work.

## Development setup

Requirements: Python 3.10+ and git 2.30+ (2.36+ to exercise `--merges` fully).

```bash
git clone https://github.com/Dastan6752/git-why
cd git-why
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Run everything CI runs:

```bash
ruff check git_why tests
ruff format --check git_why tests
mypy && mypy tests
python -m pytest --cov
python -m build && twine check --strict dist/*
```

## Project layout

| Module | Responsibility |
|---|---|
| `git_why/history.py` | All `git` invocations and the shared log/diff parser (`Step`, `Hunk`) |
| `git_why/pickaxe.py` | The pickaxe walk (`git log -G` through every text version) and timeline merging |
| `git_why/merges.py` | Landing-merge discovery, merge-commit diffs, `--merges` scan, topological ordering |
| `git_why/functions.py` | `--function`: locating headers, ambiguity, language → git diff driver |
| `git_why/noise.py` | Formatting / move / blank-line classification |
| `git_why/refs.py` | Issue / PR / ticket references in commit messages |
| `git_why/github.py` | Read-only GitHub client (stdlib `urllib`, injectable transport) |
| `git_why/report.py` | The assembled report: evidence vs. inference, confidence, JSON schema |
| `git_why/render.py` | rich / plain / JSON output |
| `git_why/core.py` | The pipeline; `cli.py` is a thin typer wrapper around it |

Renderers never compute; `report.py` never prints. Keep it that way.

## Tests

- **Unit tests** for parsers, regexes and heuristics need no git.
- **Integration tests** build throwaway repositories in `tmp_path` using the
  helpers in `tests/conftest.py` (`make_repo`, `write_commit`, `git`). Each
  commit gets a distinct timestamp so ordering is deterministic.
- **GitHub tests** use `FakeTransport` in `tests/test_github.py`. Tests must
  never touch the network or require credentials.
- When you fix a bug, add a regression test that fails without the fix. If git
  itself behaves surprisingly, document that behaviour in a test too (see
  `test_git_log_L_alone_misses_side_branch`).

## Pull requests

- Keep changes focused; one topic per PR.
- Add or update tests and documentation with the change.
- Update `CHANGELOG.md` under *Unreleased*.
- Explain *why* in the commit message — this tool exists because that matters.

## Releasing (maintainers)

1. Update the version in `pyproject.toml` and `git_why/__init__.py`, move the
   *Unreleased* section of `CHANGELOG.md` under the new version.
2. Commit, tag `vX.Y.Z`, push the tag.
3. Optionally run the *Publish* workflow manually to upload to **TestPyPI**.
4. Create a GitHub Release for the tag. The *Publish* workflow builds, checks
   the tag against the package version and uploads to PyPI through Trusted
   Publishing — after approval in the `pypi` environment.

Nothing is uploaded automatically from a plain push.
