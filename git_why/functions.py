"""
functions.py — find a function by name in a source file.

Used by ``--function``. Git does the actual history tracking (``git log -L
:<regex>:<file>``); this module's job is to

* detect ambiguity *before* asking git (git silently picks the first match),
* work out which built-in git diff driver understands the file's language,
* locate the header line so the output can show it.

The locator is heuristic and language-agnostic on purpose: it is only used to
count candidates and to display the signature, never to decide history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

# File extension -> git built-in diff driver name (see `git help attributes`,
# "Defining a custom hunk-header"). These make git recognise nested/indented
# function headers for the language.
DIFF_DRIVERS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".c": "cpp",
    ".h": "cpp",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".cs": "csharp",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".go": "golang",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".pl": "perl",
    ".pm": "perl",
    ".ex": "elixir",
    ".exs": "elixir",
    ".sh": "bash",
    ".bash": "bash",
    ".m": "objc",
    ".mm": "objc",
    ".css": "css",
    ".scss": "css",
    ".html": "html",
    ".htm": "html",
    ".md": "markdown",
    ".tex": "tex",
    ".f": "fortran",
    ".f90": "fortran",
    ".pas": "pascal",
    ".scm": "scheme",
    ".adb": "ada",
    ".ads": "ada",
    ".dts": "dts",
    ".bib": "bibtex",
}


@dataclass(frozen=True)
class FunctionMatch:
    line: int  # 1-based line number of the header
    text: str  # the header line as written


def diff_driver_for(path: str) -> str | None:
    return DIFF_DRIVERS.get(PurePosixPath(path).suffix.lower())


def _header_regex(name: str) -> re.Pattern[str]:
    n = re.escape(name)
    return re.compile(
        r"^\s*(?:"
        # keyword-style:  def name(   async def name(   fn name<   func (r) name(
        # function name(  sub name    class Name   struct Name   impl Name   object Name
        rf"(?:@?[\w.]+\s+)*(?:async\s+)?(?:def|fn|func(?:\s*\([^)]*\))?|function|sub|proc|"
        rf"class|struct|enum|trait|impl(?:<[^>]*>)?|interface|object|module|type|macro_rules!)\s+{n}\b"
        r"|"
        # C/Java/Go-method-style:  int name(   public static void name(   name :: Type
        rf"(?:[\w:<>,\[\]*&\s]+?\s+)?\*?{n}\s*\("
        r"|"
        # JS / Python assignment-style:  const name = (   name = lambda   name: function
        rf"(?:const|let|var)?\s*{n}\s*[:=]\s*(?:async\s+)?(?:function|\(|lambda|[\w(]+\s*=>)"
        r")"
    )


def find_functions(content: str, name: str) -> list[FunctionMatch]:
    """All plausible header lines defining ``name`` in ``content``."""
    pattern = _header_regex(name)
    matches: list[FunctionMatch] = []
    for i, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "*", "/*")):
            continue
        # skip obvious calls: `name(` preceded by return/await/= without a type
        if re.match(rf"^\s*(?:return|await|yield|if|elif|while|print)\s+{re.escape(name)}\s*\(", line):
            continue
        if pattern.match(line):
            matches.append(FunctionMatch(i, line))
    return matches


def function_extent(content: str, header_line: int) -> tuple[int, int]:
    """
    Best-effort (start, end) line range of the function starting at ``header_line``.

    1. Skip the signature, which may span several lines (balanced parentheses).
    2. Brace languages: balance ``{``/``}`` from there.
    3. Indentation languages: until the next non-blank line indented at or left
       of the header's indentation.

    Only used for display and suggestions, never for history.
    """
    lines = content.splitlines()
    idx = header_line - 1
    header = lines[idx]
    indent = len(header) - len(header.lstrip())
    last = len(lines) - 1

    # 1. signature end: first line (from the header) where parentheses balance
    depth = 0
    sig_end = idx
    for j in range(idx, min(last, idx + 200) + 1):
        depth += lines[j].count("(") - lines[j].count(")")
        if depth <= 0:
            sig_end = j
            break

    # 2. brace style? look at the signature's last line and the next non-blank line
    brace_start = None
    for j in range(sig_end, min(last, sig_end + 2) + 1):
        if "{" in lines[j]:
            brace_start = j
            break
        if lines[j].strip() and j != sig_end:
            break
    if brace_start is not None:
        depth = 0
        for j in range(brace_start, min(last, brace_start + 20000) + 1):
            depth += lines[j].count("{") - lines[j].count("}")
            if depth <= 0:
                return header_line, j + 1
        return header_line, last + 1

    # 3. indentation style
    end = sig_end
    for j in range(sig_end + 1, last + 1):
        line = lines[j]
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        end = j
    return header_line, end + 1
