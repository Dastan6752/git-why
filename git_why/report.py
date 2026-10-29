"""
report.py — the assembled answer, independent of how it is displayed.

A ``Report`` holds the timeline plus everything needed to judge it:

* **evidence** — facts taken verbatim from git or GitHub (commit messages,
  diffs, issue titles, review comments). Always attributed to a source.
* **inference** — things the tool worked out heuristically (which commits are
  noise, which removed line is the predecessor of an added one, the one-line
  summary). Always labelled as such.
* **confidence** — how complete we believe the history is, with the reasons.

Renderers (rich / plain / json) only format what is here; they never compute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .github import GitHubStatus, Thread
from .history import RepoInfo, Step
from .refs import Ref, refs_for

SCHEMA_VERSION = 1


@dataclass
class Target:
    file: str  # path relative to the repo root
    start: int
    end: int
    lines: list[str]  # content at HEAD
    function: str | None = None
    text: str | None = None  # when tracing a line by text instead of number

    @property
    def label(self) -> str:
        if self.text is not None:
            return f'{self.file} (text "{self.text}")'
        if self.function:
            return f"{self.file} --function {self.function} (lines {self.start}-{self.end})"
        return f"{self.file}:{self.start}" if self.start == self.end else f"{self.file}:{self.start}-{self.end}"


@dataclass
class Confidence:
    level: str  # "high" | "medium" | "low"
    reasons: list[str] = field(default_factory=list)


@dataclass
class Report:
    target: Target
    repo: RepoInfo
    steps: list[Step]  # newest first
    lineage: set[str] = field(default_factory=set)
    lineage_complete: bool = False
    pickaxe_used: bool = False
    merges_scanned: bool = False
    github: GitHubStatus = field(default_factory=GitHubStatus)
    threads: dict[int, Thread] = field(default_factory=dict)  # number -> thread
    commit_prs: dict[str, list[int]] = field(default_factory=dict)  # sha -> PRs (GitHub association)
    warnings: list[str] = field(default_factory=list)
    web_base: str | None = None  # https://github.com/owner/repo

    # -- derived ---------------------------------------------------------------

    @property
    def hidden_count(self) -> int:
        """Commits only the pickaxe found. In --text mode there is no -L baseline, so 0."""
        if self.target.text is not None:
            return 0
        return sum(1 for s in self.steps if s.found_by == "pickaxe")

    @property
    def meaningful(self) -> list[Step]:
        return [s for s in self.steps if not s.is_noise]

    @property
    def origin(self) -> Step | None:
        return self.meaningful[-1] if self.meaningful else None

    def refs(self, step: Step) -> list[Ref]:
        refs = refs_for(step)
        for number in self.commit_prs.get(step.sha, []):
            if all(r.number != number for r in refs):
                refs.append(Ref("pr", str(number), "github association"))
        return refs

    def threads_for(self, step: Step) -> list[Thread]:
        out: list[Thread] = []
        for ref in self.refs(step):
            n = ref.number
            if n is not None and n in self.threads and self.threads[n] not in out:
                out.append(self.threads[n])
        return out

    def commit_url(self, sha: str) -> str | None:
        return f"{self.web_base}/commit/{sha}" if self.web_base else None

    def confidence(self) -> Confidence:
        reasons: list[str] = []
        level = "high"

        def downgrade(to: str, why: str) -> None:
            nonlocal level
            order = {"high": 0, "medium": 1, "low": 2}
            if order[to] > order[level]:
                level = to
            reasons.append(why)

        if not self.steps:
            return Confidence("low", ["no history found for this target"])
        if self.repo.shallow:
            downgrade(
                "low" if any(s.shallow_boundary for s in self.steps) else "medium",
                "shallow clone: history may be truncated (run `git fetch --unshallow`)",
            )
        if any(s.shallow_boundary for s in self.steps):
            downgrade("low", "the oldest commit shown is a shallow-clone boundary, not the true origin")
        if self.pickaxe_used and not self.lineage_complete:
            downgrade(
                "medium",
                "pickaxe walk stopped before reaching the line's birth "
                "(generic or duplicated text); older history relies on git log -L alone",
            )
        if not self.pickaxe_used:
            downgrade("medium", "side-branch changes were not cross-checked (pickaxe disabled or range too large)")
        if not self.merges_scanned:
            reasons.append("edits made inside merge commits were not inspected (use --merges)")
        if self.origin is not None and not self.origin.is_origin:
            downgrade(
                "medium",
                "the oldest commit modifies the line rather than creating it; "
                "an earlier version may exist beyond what git could follow",
            )
        if self.hidden_count and not self.repo.shallow:
            reasons.append("includes commits that git blame / git log -L cannot see (recovered via pickaxe)")
        return Confidence(level, reasons)

    def evidence_strength(self, step: Step) -> str:
        """How much *explanation* exists for this commit: strong / medium / weak."""
        has_thread = any(t.body or t.comments for t in self.threads_for(step))
        has_body = len(step.body.strip()) >= 20
        has_ref = bool(self.refs(step))
        if has_thread or (has_body and has_ref):
            return "strong"
        if has_body or has_ref:
            return "medium"
        return "weak"

    def summary(self) -> str:
        """Plain-English recap built only from facts in the report (no guessing)."""
        real = self.meaningful
        if not real:
            return "No meaningful history found."
        origin = real[-1]
        changes = real[:-1]
        noise = len(self.steps) - len(real)
        hidden = self.hidden_count
        merge_edits = sum(1 for s in self.steps if s.found_by == "merge")

        verb = "Introduced" if origin.is_origin else "Oldest known change"
        parts = [f"{verb} on {origin.day} by {origin.author} ({_q(origin.subject)})"]
        if origin.shallow_boundary:
            parts[-1] += " [shallow boundary]"
        refs = self.refs(origin)
        if refs:
            parts[-1] += f", linked to {', '.join(str(r) for r in refs)}"
        if changes:
            parts.append(
                f"changed meaningfully {len(changes)} time{'s' if len(changes) != 1 else ''} since, "
                f"most recently on {changes[0].day} by {changes[0].author}"
            )
        if noise:
            parts.append(f"{noise} formatting/move commit{'s' if noise != 1 else ''} ignored")
        if merge_edits:
            parts.append(f"{merge_edits} change{'s' if merge_edits != 1 else ''} made inside merge commits")
        if hidden:
            parts.append(
                f"{hidden} commit{'s' if hidden != 1 else ''} recovered that git blame / git log -L cannot see"
            )
        return "; ".join(parts) + "."

    # -- serialisation -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        conf = self.confidence()
        return {
            "schema_version": SCHEMA_VERSION,
            "generator": {"name": "git-why", "version": __version__},
            "target": {
                "file": self.target.file,
                "start": self.target.start,
                "end": self.target.end,
                "function": self.target.function,
                "text": self.target.text,
                "lines": self.target.lines,
            },
            "repo": {
                "root": str(self.repo.root),
                "head": self.repo.head,
                "shallow": self.repo.shallow,
                "web_url": self.web_base,
            },
            "strategies": {
                "range_tracking": True,
                "pickaxe": self.pickaxe_used,
                "lineage_complete": self.lineage_complete,
                "merge_scan": self.merges_scanned,
                "github": self.github.enabled,
            },
            "confidence": {"level": conf.level, "reasons": conf.reasons},
            "summary": {"text": self.summary(), "source": "inferred from the evidence below"},
            "steps": [self._step_dict(s) for s in reversed(self.steps)],  # oldest first
            "github": self.github.to_dict(),
            "warnings": list(self.warnings),
        }

    def _step_dict(self, step: Step) -> dict[str, Any]:
        return {
            "sha": step.sha,
            "short_sha": step.short_sha,
            "url": self.commit_url(step.sha),
            "date": step.date,
            "author": step.author,
            "author_email": step.email,
            "subject": step.subject,
            "body": step.body,
            "kind": step.kind,
            "found_by": step.found_by,
            "is_merge": step.is_merge,
            "noise_reason": step.noise_reason or None,
            "shallow_boundary": step.shallow_boundary,
            "file_before": step.file_before,
            "file_after": step.file_after,
            "removed": step.removed,
            "added": step.added,
            "references": [
                {"kind": r.kind, "id": r.id, "how": r.verb or None, "repo": r.repo} for r in self.refs(step)
            ],
            "landed_via": (
                {
                    "sha": step.landed_via.sha,
                    "short_sha": step.landed_via.short_sha,
                    "subject": step.landed_via.subject,
                    "date": step.landed_via.date,
                    "url": self.commit_url(step.landed_via.sha),
                }
                if step.landed_via
                else None
            ),
            "evidence_strength": self.evidence_strength(step),
            "github_threads": [t.to_dict() for t in self.threads_for(step)],
        }


def _q(s: str) -> str:
    return f'"{s.strip()}"'
