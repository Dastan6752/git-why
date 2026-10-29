"""CLI behaviour: output formats, exit codes, edge cases. Uses typer's CliRunner."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from git_why import __version__
from git_why.cli import app, parse_target
from typer.testing import CliRunner

from tests.conftest import git, write_commit

runner = CliRunner()


# Rich reads COLUMNS/TERM when it renders, so pin them: at very narrow widths
# the help table word-breaks long option names ("--github-r" / "epo"), and
# TERM=dumb disables colour even when a terminal is forced.
HELP_ENV = {"COLUMNS": "100", "TERM": "xterm-256color", "NO_COLOR": None}


def run(*args: str, cwd: Path | None = None, env: dict[str, str | None] | None = None) -> tuple[int, str, str]:
    old = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        result = runner.invoke(app, list(args), env=env)
    finally:
        os.chdir(old)
    return result.exit_code, result.stdout, result.stderr


# ---------- target parsing ----------


def test_parse_target_forms() -> None:
    assert parse_target("src/app.py:42") == ("src/app.py", 42, 42)
    assert parse_target("app.py:9-3") == ("app.py", 3, 9)
    assert parse_target("a:b/c.py:7") == ("a:b/c.py", 7, 7)
    assert parse_target("plain.py") == ("plain.py", None, None)


def test_version() -> None:
    code, out, _ = run("--version")
    assert code == 0 and out.strip() == f"git-why {__version__}"


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def plain(text: str) -> str:
    """Drop ANSI escapes and join wrapped lines so substrings can be asserted on."""
    return " ".join(ANSI.sub("", text).split())


def declared_options() -> set[str]:
    """Every long option the command really declares, taken from Click itself."""
    from typer.main import get_command

    params = get_command(app).params
    return {o for p in params if p.param_type_name == "option" for o in p.opts if o.startswith("--")}


def options_panel_entries(help_output: str) -> set[str]:
    """
    Long options that head a row of the "Options" panel. Only the panel counts:
    an option mentioned in an example or another option's description does not
    document it.
    """
    lines = ANSI.sub("", help_output).splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("╭─ Options")]
    assert len(starts) == 1, "expected exactly one Options panel"
    found: set[str] = set()
    for line in lines[starts[0] + 1 :]:
        if line.startswith("╰"):
            break
        cells = line.strip("│ ").split()
        if cells and cells[0].startswith("--"):
            found.add(cells[0])
    return found


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_options_panel_lists_every_declared_option(flag: str) -> None:
    code, out, _ = run(flag, env=HELP_ENV)
    assert code == 0
    declared = declared_options()
    assert {"--function", "--text", "--github", "--github-repo", "--merges", "--json", "--format"} <= declared
    listed = options_panel_entries(out)
    assert declared <= listed, f"options missing from the Options panel: {sorted(declared - listed)}"


def test_help_shows_description_and_examples() -> None:
    code, out, _ = run("--help", env=HELP_ENV)
    text = plain(out)
    assert code == 0
    assert "Usage: git-why [OPTIONS] TARGET" in text.replace("{TARGET}", "TARGET")
    assert "why is this line here?" in text
    assert "Examples" in text
    for example in (
        "git why src/app.py:42",
        "git why src/app.py --function retry",
        "git why src/app.py:42 --github",
        "git why src/app.py:42 --merges",
        "git why src/app.py --text 'TIMEOUT = 30'",
    ):
        assert example in text, example
    assert "GITHUB_TOKEN" in text
    assert "Exit codes: 0 ok, 1 usage error, 2 git error, 3 no history found" in text


def test_help_with_forced_colour_still_documents_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Regression: typer forces a colour terminal when GITHUB_ACTIONS / FORCE_COLOR
    is set (decided at import time), and the Rich help styles the two dashes of
    an option separately, so the raw text no longer contains "--function".
    The options must still be there once escapes are stripped.
    """
    import typer.rich_utils as ru

    monkeypatch.setattr(ru, "FORCE_TERMINAL", True)
    code, out, _ = run("--help", env=HELP_ENV)
    assert code == 0
    assert "\x1b[" in out, "expected styled output under a forced terminal"
    assert {"--function", "--github", "--merges"} <= options_panel_entries(out)
    assert "Examples" in plain(out)


# ---------- formats ----------


def test_json_output_schema(blind_spot_repo: Path) -> None:
    code, out, _ = run("cfg.py:2", "--json", cwd=blind_spot_repo)
    assert code == 0
    data = json.loads(out)
    assert data["schema_version"] == 1
    assert data["generator"]["version"] == __version__
    assert data["target"] == {
        "file": "cfg.py",
        "start": 2,
        "end": 2,
        "function": None,
        "text": None,
        "lines": ["RETRIES = 0"],
    }
    assert data["strategies"]["pickaxe"] is True and data["strategies"]["lineage_complete"] is True
    assert data["confidence"]["level"] == "high"
    steps = data["steps"]
    assert [s["subject"][:1] for s in steps] == ["A", "B", "C"]  # oldest first
    assert steps[0]["kind"] == "origin" and steps[1]["kind"] == "change"
    assert steps[1]["found_by"] == "pickaxe" and steps[0]["found_by"] == "range"
    assert steps[1]["removed"] == ["RETRIES = 0"] and steps[1]["added"] == ["RETRIES = object()"]
    assert {"kind": "issue", "id": "42", "how": "fixes", "repo": None} in steps[1]["references"]
    assert steps[1]["landed_via"]["subject"].startswith("Merge pull request #7")
    assert steps[1]["evidence_strength"] in {"medium", "strong"}
    assert re.fullmatch(r"[0-9a-f]{40}", steps[0]["sha"])
    assert data["github"]["enabled"] is False
    assert "inferred" in data["summary"]["source"]


def test_plain_output_is_ascii_and_complete(blind_spot_repo: Path) -> None:
    code, out, _ = run("cfg.py:2", "--format", "plain", cwd=blind_spot_repo)
    assert code == 0
    assert out.isascii()
    assert "line created here" in out
    assert "[invisible to git blame]" in out
    assert "Summary (inferred):" in out and "Confidence: high" in out
    assert "-> fixes #42" in out


def test_rich_output_no_color(blind_spot_repo: Path) -> None:
    code, out, _ = run("cfg.py:2", "--format", "rich", "--no-color", cwd=blind_spot_repo)
    assert code == 0
    assert "\x1b[" not in out
    assert "invisible to git blame" in out and "Confidence:" in out


def test_auto_format_is_plain_when_piped(blind_spot_repo: Path) -> None:
    code, out, _ = run("cfg.py:2", cwd=blind_spot_repo)
    assert code == 0 and out.startswith("cfg.py:2\n")


def test_all_flag_disables_noise(make_repo) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo()
    write_commit(repo, "a.py", "x=1\n", "A")
    write_commit(repo, "a.py", "x = 1\n", "style: fmt")
    code, out, _ = run("a.py:1", "--json", cwd=repo)
    assert json.loads(out)["steps"][1]["kind"] == "noise"
    code, out, _ = run("a.py:1", "--json", "--all", cwd=repo)
    assert json.loads(out)["steps"][1]["kind"] == "change"


def test_no_pickaxe_mode_is_honest_about_confidence(blind_spot_repo: Path) -> None:
    code, out, _ = run("cfg.py:2", "--json", "--no-pickaxe", cwd=blind_spot_repo)
    data = json.loads(out)
    assert [s["subject"][:1] for s in data["steps"]] == ["A"]  # git's blind spot
    assert data["confidence"]["level"] == "medium"
    assert any("not cross-checked" in r for r in data["confidence"]["reasons"])


# ---------- --text, --function, --merges via CLI ----------


def test_text_mode_traces_deleted_line(blind_spot_repo: Path) -> None:
    code, out, _ = run("cfg.py", "--text", "RETRIES = object()", "--json", cwd=blind_spot_repo)
    assert code == 0
    data = json.loads(out)
    assert data["target"]["text"] == "RETRIES = object()"
    assert [s["subject"][:1] for s in data["steps"]] == ["A", "B", "C"]
    assert all(s["found_by"] == "pickaxe" for s in data["steps"])
    code, plain, _ = run("cfg.py", "--text", "RETRIES = object()", "--format", "plain", cwd=blind_spot_repo)
    assert "invisible to git blame" not in plain  # no -L baseline in text mode


def test_text_mode_for_deleted_file(make_repo) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo()
    write_commit(repo, "gone.py", "SECRET_FLAG = 1\n", "A: add")
    git(repo, "rm", "-q", "gone.py")
    git(repo, "commit", "-q", "-m", "B: remove file")
    code, out, _ = run("gone.py", "--text", "SECRET_FLAG = 1", "--json", cwd=repo)
    assert code == 0
    steps = json.loads(out)["steps"]
    assert [(s["subject"][:1], s["kind"]) for s in steps] == [("A", "origin"), ("B", "deletion")]


def test_function_cli(function_repo: Path) -> None:
    code, out, _ = run("svc.py", "--function", "get", "--format", "plain", cwd=function_repo)
    assert code == 0
    assert "--function get (lines 2-3)" in out
    assert "function created here" in out


def test_merges_cli(merge_drop_repo: Path) -> None:
    code, out, _ = run("cfg.py:3", "--merges", "--format", "plain", cwd=merge_drop_repo)
    assert code == 0
    assert "[changed by the merge itself]" in out
    assert "line deleted here" in out


# ---------- errors & exit codes ----------


def test_bad_target_syntax(blind_spot_repo: Path) -> None:
    code, _, err = run("cfg.py", cwd=blind_spot_repo)
    assert code == 1 and "expected FILE:LINE" in err


def test_conflicting_options(blind_spot_repo: Path) -> None:
    assert run("cfg.py", "--function", "a", "--text", "b", cwd=blind_spot_repo)[0] == 1
    assert run("cfg.py:2", "--function", "a", cwd=blind_spot_repo)[0] == 1
    assert run("cfg.py:2", "--format", "yaml", cwd=blind_spot_repo)[0] == 1
    assert run("cfg.py:2", "--github-repo", "nonsense", cwd=blind_spot_repo)[0] == 1


def test_missing_file(blind_spot_repo: Path) -> None:
    code, _, err = run("nope.py:1", cwd=blind_spot_repo)
    assert code == 1 and "does not exist" in err and "--text" in err


def test_directory_target(blind_spot_repo: Path) -> None:
    (blind_spot_repo / "pkg").mkdir()
    code, _, err = run("pkg:1", cwd=blind_spot_repo)
    assert code == 1 and "directory" in err


def test_line_out_of_range(blind_spot_repo: Path) -> None:
    code, _, err = run("cfg.py:500", cwd=blind_spot_repo)
    assert code == 1 and "has 3 lines" in err


def test_not_a_repository(tmp_path: Path) -> None:
    f = tmp_path / "loose.py"
    f.write_text("x = 1\n")
    code, _, err = run(f"{f}:1")
    assert code == 2 and "not inside a git repository" in err


def test_untracked_file(blind_spot_repo: Path) -> None:
    (blind_spot_repo / "new.py").write_text("y = 2\n")
    code, _, err = run("new.py:1", cwd=blind_spot_repo)
    assert code == 2 and "not tracked" in err


def test_binary_file(make_repo) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo()
    (repo / "blob.bin").write_bytes(b"ab\0cd\n")
    git(repo, "add", "blob.bin")
    git(repo, "commit", "-q", "-m", "bin")
    code, _, err = run("blob.bin:1", cwd=repo)
    assert code == 2 and "binary" in err


def test_empty_repository(make_repo) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo()
    (repo / "a.py").write_text("x = 1\n")
    code, _, err = run("a.py:1", cwd=repo)
    assert code == 2 and "no commits" in err


def test_no_history_exit_code(make_repo) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo()
    write_commit(repo, "a.py", "x = 1\n", "A")
    code, out, err = run("a.py", "--text", "NEVER_EXISTED = 1", "--format", "plain", cwd=repo)
    assert code == 3 and "no history found" in err


def test_uncommitted_changes_warn_and_use_head(blind_spot_repo: Path) -> None:
    (blind_spot_repo / "cfg.py").write_text("# new first line\nimport os\nRETRIES = 0\n# trailing comment\n")
    code, out, _ = run("cfg.py:2", "--json", cwd=blind_spot_repo)
    data = json.loads(out)
    assert data["target"]["lines"] == ["RETRIES = 0"]  # HEAD content, not the working tree
    assert any("uncommitted" in w for w in data["warnings"])


def test_shallow_clone_is_flagged(blind_spot_repo: Path, tmp_path: Path) -> None:
    shallow = tmp_path / "shallow"
    git(blind_spot_repo, "clone", "-q", "--depth", "1", f"file://{blind_spot_repo}", str(shallow))
    code, out, _ = run("cfg.py:2", "--json", cwd=shallow)
    assert code == 0
    data = json.loads(out)
    assert data["repo"]["shallow"] is True
    assert data["confidence"]["level"] == "low"
    assert data["steps"][0]["shallow_boundary"] is True
    assert any("shallow" in r for r in data["confidence"]["reasons"])


def test_rename_is_followed(make_repo) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo()
    write_commit(repo, "old/utils.py", "import time\nDELAY = 1.0\n", "A: add delay\n\nFixes #3")
    git(repo, "mv", "old/utils.py", "new.py")
    git(repo, "commit", "-q", "-m", "move")
    write_commit(repo, "new.py", "import time\nDELAY = 0.3\n", "B: tune delay")
    code, out, _ = run("new.py:2", "--json", cwd=repo)
    data = json.loads(out)
    assert [s["subject"][:1] for s in data["steps"]] == ["A", "B"]
    assert data["steps"][0]["file_after"] == "old/utils.py"


def test_large_range_skips_pickaxe(make_repo) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo()
    write_commit(repo, "big.py", "".join(f"v{i} = {i}\n" for i in range(20)), "A")
    code, out, _ = run("big.py:1-10", "--json", cwd=repo)
    data = json.loads(out)
    assert code == 0 and data["strategies"]["pickaxe"] is False
    assert data["confidence"]["level"] == "medium"


@pytest.mark.parametrize("flag", ["--debug"])
def test_debug_never_prints_token(blind_spot_repo: Path, monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecret")
    git(blind_spot_repo, "remote", "add", "origin", "https://github.com/o/r.git")
    # offline transport so no network is touched
    import git_why.github as gh

    monkeypatch.setattr(gh, "urllib_transport", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    code, out, err = run("cfg.py:2", "--github", flag, "--format", "plain", cwd=blind_spot_repo)
    assert code == 0
    assert "ghp_supersecret" not in out + err
    assert "token present" in err
