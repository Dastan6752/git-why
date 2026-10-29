"""--function: locating functions, ambiguity, git-native tracking."""

from __future__ import annotations

from pathlib import Path

import pytest
from git_why import functions
from git_why.core import Options, UserError, investigate

from tests.conftest import write_commit

PY = """\
import os

def helper(x):
    return x

class Client:
    @property
    def timeout(self):
        return 3

    async def fetch(
        self,
        url,
    ) -> bytes:
        return b""

    def get(self, url):
        return url

def get():
    pass
"""

JS = """\
function add(a, b) {
  return a + b;
}
const mul = (a, b) => {
  if (a) {
    return a * b;
  }
  return 0;
};
class K {
  sub(a, b) {
    return a - b;
  }
}
"""

C = """\
static int
helper(int x)
{
    return x;
}

int main(int argc, char **argv) {
    return helper(argc);
}
"""


def test_find_python_functions() -> None:
    assert [m.line for m in functions.find_functions(PY, "helper")] == [3]
    assert [m.line for m in functions.find_functions(PY, "timeout")] == [8]
    assert [m.line for m in functions.find_functions(PY, "fetch")] == [11]
    assert [m.line for m in functions.find_functions(PY, "get")] == [17, 20]  # ambiguous
    assert functions.find_functions(PY, "nothing") == []


def test_find_does_not_match_calls_or_prefixes() -> None:
    src = "def widget():\n    return get()\nx = get(1)\n"
    assert functions.find_functions(src, "get") == []


def test_find_js_and_c_functions() -> None:
    assert [m.line for m in functions.find_functions(JS, "add")] == [1]
    assert [m.line for m in functions.find_functions(JS, "mul")] == [4]
    assert [m.line for m in functions.find_functions(JS, "sub")] == [11]
    assert [m.line for m in functions.find_functions(C, "main")] == [7]
    # C: return type on its own line -> header is the name line
    assert [m.line for m in functions.find_functions(C, "helper")] == [2]


def test_extent_python_multiline_signature() -> None:
    assert functions.function_extent(PY, 11) == (11, 15)
    assert functions.function_extent(PY, 3) == (3, 4)
    assert functions.function_extent(PY, 17) == (17, 18)
    assert functions.function_extent(PY, 20) == (20, 21)


def test_extent_brace_languages() -> None:
    assert functions.function_extent(JS, 1) == (1, 3)
    assert functions.function_extent(JS, 4) == (4, 9)
    assert functions.function_extent(JS, 11) == (11, 13)
    assert functions.function_extent(C, 7) == (7, 9)


@pytest.mark.parametrize(
    "path,driver",
    [("a/b.py", "python"), ("x.RS", "rust"), ("main.go", "golang"), ("f.txt", None), ("noext", None)],
)
def test_diff_driver_mapping(path: str, driver: str | None) -> None:
    assert functions.diff_driver_for(path) == driver


# ---------- integration ----------


def test_function_history_tracks_indented_method(function_repo: Path) -> None:
    report = investigate(function_repo / "svc.py", None, None, Options(function="get"))
    assert report.target.function == "get"
    assert report.target.start == 2 and report.target.end == 3
    subjects = [s.subject[:1] for s in reversed(report.steps)]
    assert subjects == ["A", "B"]
    b = report.steps[0]
    assert "params" in "".join(b.added)
    assert any(r.id == "12" for r in report.refs(b))


def test_function_history_untouched_sibling_has_single_commit(function_repo: Path) -> None:
    report = investigate(function_repo / "svc.py", None, None, Options(function="post"))
    assert [s.subject[:1] for s in report.steps] == ["A"]


def test_ambiguous_function_errors_with_ranges(function_repo: Path) -> None:
    write_commit(
        function_repo,
        "svc.py",
        (function_repo / "svc.py").read_text() + "\n\ndef get():\n    return 1\n",
        "C: module-level get",
    )
    with pytest.raises(UserError) as exc:
        investigate(function_repo / "svc.py", None, None, Options(function="get"))
    assert exc.value.exit_code == 1
    assert "2 times" in str(exc.value)
    assert exc.value.hint and "svc.py:2-3" in exc.value.hint and "svc.py:9-10" in exc.value.hint


def test_missing_function_errors(function_repo: Path) -> None:
    with pytest.raises(UserError) as exc:
        investigate(function_repo / "svc.py", None, None, Options(function="delete"))
    assert exc.value.exit_code == 1


def test_function_falls_back_to_line_range_when_git_cannot_match(make_repo) -> None:  # type: ignore[no-untyped-def]
    """A language git has no diff driver for: we compute the range and warn."""
    repo = make_repo("nodriver")
    write_commit(
        repo, "lib.zig", 'const std = @import("std");\n\npub fn add(a: i32, b: i32) i32 {\n    return a + b;\n}\n', "A"
    )
    write_commit(
        repo,
        "lib.zig",
        'const std = @import("std");\n\npub fn add(a: i32, b: i32) i32 {\n    return a + b + 0;\n}\n',
        "B",
    )
    report = investigate(repo / "lib.zig", None, None, Options(function="add"))
    # Git's default driver treats the column-0 `pub fn add` line as a funcname line,
    # so either path is acceptable; what matters is that both commits are found.
    assert [s.subject for s in reversed(report.steps)] == ["A", "B"]
    assert report.target.start == 3 and report.target.end == 5
