"""Shared fixtures: tiny throwaway git repositories with specific histories."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",  # ignore the developer's ~/.gitconfig
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": os.environ.get("HOME", "/tmp"),
}


_CLOCK = [1_700_000_000]


def _tick() -> dict[str, str]:
    """Each commit gets its own second so ordering is deterministic."""
    _CLOCK[0] += 60
    stamp = f"{_CLOCK[0]} +0000"
    return {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


def git(repo: Path, *args: str, check: bool = True) -> str:
    env = {**GIT_ENV, **_tick()}
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def write_commit(repo: Path, name: str, content: str, msg: str) -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[[str], Path]:
    def _make(name: str = "repo") -> Path:
        repo = tmp_path / name
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        return repo

    return _make


@pytest.fixture
def blind_spot_repo(make_repo: Callable[[str], Path]) -> Path:
    """
    main:     A(=0) ── mainline work ──────── M(merge, =0) ── D(unrelated)
                \\                            /
    feature:     B(=object()) ──── C(=0) ───┘

    The line is `= 0` on both sides of the merge, so git's history
    simplification ignores the feature branch entirely.
    """
    repo = make_repo("blind")
    write_commit(repo, "cfg.py", "import os\nRETRIES = 0\n", "A: base config")
    git(repo, "checkout", "-q", "-b", "feature")
    write_commit(repo, "cfg.py", "import os\nRETRIES = object()\n", "B: use sentinel\n\nFixes #42")
    write_commit(repo, "cfg.py", "import os\nRETRIES = 0\n", "C: revert to 0, sentinel broke docs\n\nSee PR #99")
    git(repo, "checkout", "-q", "main")
    write_commit(repo, "other.txt", "hi\n", "mainline work")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge pull request #7 from me/feature", "feature")
    write_commit(repo, "cfg.py", "import os\nRETRIES = 0\n# trailing comment\n", "D: unrelated edit")
    return repo


@pytest.fixture
def evil_merge_repo(make_repo: Callable[[str], Path]) -> Path:
    """
    A(=10) ── C(=15) ── M(conflict resolved to =30) ── D
        \\── B(=20) ────┘
    """
    repo = make_repo("evil")
    write_commit(repo, "cfg.py", "import os\nTIMEOUT = 10\n", "A: base")
    git(repo, "checkout", "-q", "-b", "feature")
    write_commit(repo, "cfg.py", "import os\nTIMEOUT = 20\n", "B: raise timeout to 20 (#5)")
    git(repo, "checkout", "-q", "main")
    write_commit(repo, "cfg.py", "import os\nTIMEOUT = 15\n", "C: timeout 15 on main")
    git(repo, "merge", "feature", check=False)  # conflict
    write_commit(repo, "cfg.py", "import os\nTIMEOUT = 30\n", "M: merge feature, resolved to 30")
    write_commit(repo, "cfg.py", "import os\nTIMEOUT = 30\n# end\n", "D: comment")
    return repo


@pytest.fixture
def merge_drop_repo(make_repo: Callable[[str], Path]) -> Path:
    """
    A line added on a side branch is silently dropped while resolving a merge,
    then re-added later. Neither -L nor the plain pickaxe sees the drop.

    main:  A(no line) ── C(other file) ── M(resolves WITHOUT the line) ── R(re-adds line)
                \\── B(adds line) ─────────┘
    """
    repo = make_repo("drop")
    write_commit(repo, "cfg.py", "import os\n", "A: base")
    git(repo, "checkout", "-q", "-b", "feature")
    write_commit(repo, "cfg.py", "import os\nFEATURE_FLAG = True\n", "B: add feature flag")
    git(repo, "checkout", "-q", "main")
    write_commit(repo, "cfg.py", "import sys\nimport os\n", "C: add sys import")
    git(repo, "merge", "--no-commit", "--no-ff", "feature", check=False)
    (repo / "cfg.py").write_text("import sys\nimport os\n")  # resolution drops the flag
    git(repo, "add", "cfg.py")
    git(repo, "commit", "-q", "-m", "M: merge feature (flag lost)")
    write_commit(repo, "cfg.py", "import sys\nimport os\nFEATURE_FLAG = True\n", "R: re-add feature flag")
    return repo


@pytest.fixture
def function_repo(make_repo: Callable[[str], Path]) -> Path:
    repo = make_repo("funcs")
    write_commit(
        repo,
        "svc.py",
        "class Client:\n"
        "    def get(self, url):\n"
        "        return self._req('GET', url)\n"
        "\n"
        "    def post(self, url):\n"
        "        return self._req('POST', url)\n",
        "A: client",
    )
    write_commit(
        repo,
        "svc.py",
        "class Client:\n"
        "    def get(self, url, params=None):\n"
        "        return self._req('GET', url, params=params)\n"
        "\n"
        "    def post(self, url):\n"
        "        return self._req('POST', url)\n",
        "B: get() accepts params (#12)",
    )
    return repo
