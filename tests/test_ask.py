"""--ask: evidence pack, citation validation, model selection, fallbacks, CLI wiring. No network, no model."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from git_why import ask as ask_mod
from git_why.ask import (
    Answer,
    OllamaClient,
    OllamaUnreachableError,
    ask,
    resolve_host,
    split_sentences,
    validate,
)
from git_why.core import Options, investigate
from git_why.evidence import build, format_for_prompt
from git_why.render import render_json, render_plain, render_rich
from typer.testing import CliRunner

from tests.test_noise_render import sample_report

# ---------------------------------------------------------------------------
# A fake Ollama: scripted /api/tags and /api/chat responses
# ---------------------------------------------------------------------------


class FakeOllama:
    def __init__(self, models: list[str] | None, reply: str | Exception = "") -> None:
        self.models = models  # None -> server down
        self.reply = reply
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def __call__(self, url: str, payload: dict[str, Any] | None) -> tuple[int, str]:
        self.calls.append((url, payload))
        if self.models is None:
            raise OllamaUnreachableError("[Errno 111] Connection refused")
        if url.endswith("/api/tags"):
            return 200, json.dumps({"models": [{"name": m} for m in self.models]})
        assert url.endswith("/api/chat")
        if isinstance(self.reply, Exception):
            raise self.reply
        if payload is not None and payload["model"] not in self.models:
            return 404, json.dumps({"error": f"model '{payload['model']}' not found"})
        return 200, json.dumps({"message": {"role": "assistant", "content": self.reply}})

    def client(self) -> OllamaClient:
        return OllamaClient("http://127.0.0.1:11434", transport=self)


# ---------------------------------------------------------------------------
# evidence pack
# ---------------------------------------------------------------------------


def test_evidence_pack_numbers_commits_threads_and_comments(tmp_path: Path) -> None:
    pack = build(sample_report(tmp_path))
    ids = [i.id for i in pack.items]
    # commits oldest first, noise ("style: fmt") excluded; threads in the order commits cite them
    # (the origin says "Fixes #42", the later change cites PR #99); one review comment on the PR
    assert ids == ["C1", "C2", "C3", "C4", "C5", "I1", "P1", "R1"]
    assert pack.get("C1") is not None and "created here" in pack.get("C1").text  # type: ignore[union-attr]
    assert "Fixes #42" in pack.get("C1").text  # type: ignore[union-attr]
    p1 = pack.get("P1")
    assert p1 is not None and p1.kind == "pr" and p1.url == "https://github.com/o/r/pull/99"
    assert "We measured p99" in p1.text
    r1 = pack.get("R1")
    assert r1 is not None and "@rev review on P1" in r1.text and "0.3 was tuned empirically" in r1.text
    prompt = format_for_prompt(pack)
    assert prompt.startswith("Target: f.py:3")
    assert "[C2] commit" in prompt and "[R1] @rev" in prompt
    assert any("formatting/move/pass-through" in c for c in pack.caveats)


def test_evidence_pack_respects_budget_and_reports_omissions(tmp_path: Path) -> None:
    pack = build(sample_report(tmp_path), budget=200)
    assert pack.items, "at least one item is always kept"
    assert pack.omitted > 0
    assert "omitted for length" in format_for_prompt(pack)


def test_evidence_pack_caveats_without_github(blind_spot_repo: Path) -> None:
    report = investigate(blind_spot_repo / "cfg.py", 2, 2, Options())
    pack = build(report)
    assert any("--github" in c for c in pack.caveats)
    assert [i.kind for i in pack.items] == ["commit"] * len(pack.items)
    assert any("invisible to git blame" in i.text for i in pack.items)


# ---------------------------------------------------------------------------
# validation: nothing invented, nothing hidden
# ---------------------------------------------------------------------------


def test_split_sentences_handles_citations_and_newlines() -> None:
    text = 'The value changed [C2]. "Quoted" start [P1][R1]!\nNot in the evidence: why 30.'
    assert split_sentences(text) == [
        "The value changed [C2].",
        '"Quoted" start [P1][R1]!',
        "Not in the evidence: why 30.",
    ]


def test_validate_keeps_cited_removes_uncited_and_unknown(tmp_path: Path) -> None:
    pack = build(sample_report(tmp_path))
    raw = (
        "The line was added to fix double charges [C1]. "
        "It was later tuned after a p99 regression [C3][P1]. "
        "This was probably done for performance reasons. "
        "Someone also changed it in 2031 [C42]. "
        "Reviewer noted the value was empirical [R1, P1]. "
        "Not in the evidence: why exactly 0.3 was chosen."
    )
    kept, removed = validate(raw, pack)
    assert [s.text for s in kept] == [
        "The line was added to fix double charges.",
        "It was later tuned after a p99 regression.",
        "Reviewer noted the value was empirical.",
        "Not in the evidence: why exactly 0.3 was chosen.",
    ]
    assert [s.citations for s in kept] == [["C1"], ["C3", "P1"], ["R1", "P1"], []]
    assert removed == [
        "This was probably done for performance reasons.",
        "Someone also changed it in 2031 [C42].",
    ]


# ---------------------------------------------------------------------------
# ask(): every branch of model selection and failure
# ---------------------------------------------------------------------------


def test_ask_when_ollama_is_down_gives_setup_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_WHY_MODEL", raising=False)
    fake = FakeOllama(models=None)
    a = ask(sample_report(tmp_path), "why?", client=fake.client())
    assert a.status == "unavailable"
    assert "Ollama is not running at http://127.0.0.1:11434" in a.message
    assert a.setup[0].startswith("1. Install Ollama")
    assert any("ollama pull" in s for s in a.setup)
    assert any("compare" in s for s in a.setup)
    assert a.sentences == [] and a.model is None
    assert a.pack.items, "the evidence pack is still built"


def test_ask_no_models_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_WHY_MODEL", raising=False)
    a = ask(sample_report(tmp_path), "why?", client=FakeOllama(models=[]).client())
    assert a.status == "unavailable" and "no model is installed" in a.message
    assert a.setup[0].startswith("1. Download a model")


def test_ask_several_models_and_none_selected_lists_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_WHY_MODEL", raising=False)
    fake = FakeOllama(models=["llama3.2:latest", "qwen2.5:7b"], reply="should not be called")
    a = ask(sample_report(tmp_path), "why?", client=fake.client())
    assert a.status == "no_model_selected"
    assert "llama3.2:latest, qwen2.5:7b" in a.message
    assert "--model llama3.2:latest" in a.setup[0]
    assert not any(url.endswith("/api/chat") for url, _ in fake.calls)


def test_ask_uses_the_only_installed_model_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_WHY_MODEL", raising=False)
    fake = FakeOllama(models=["qwen2.5:7b"], reply="The line was added to fix double charges [C1].")
    a = ask(sample_report(tmp_path), "why?", client=fake.client())
    assert a.status == "answered" and a.model == "qwen2.5:7b"
    assert a.note == "using qwen2.5:7b, the only installed model"


def test_ask_requested_model_not_installed(tmp_path: Path) -> None:
    fake = FakeOllama(models=["qwen2.5:7b"])
    a = ask(sample_report(tmp_path), "why?", model="llama3.2", client=fake.client())
    assert a.status == "unavailable"
    assert "model 'llama3.2' is not installed (installed: qwen2.5:7b)" in a.message
    assert a.setup[0] == "ollama pull llama3.2"


def test_ask_model_name_matches_latest_tag(tmp_path: Path) -> None:
    fake = FakeOllama(models=["llama3.2:latest"], reply="Added to fix double charges [C1].")
    a = ask(sample_report(tmp_path), "why?", model="llama3.2", client=fake.client())
    assert a.status == "answered" and a.model == "llama3.2:latest"


def test_ask_env_model_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_WHY_MODEL", "qwen2.5:7b")
    fake = FakeOllama(models=["qwen2.5:7b", "other"], reply="Added to fix double charges [C1].")
    a = ask(sample_report(tmp_path), "why?", client=fake.client())
    assert a.status == "answered" and a.model == "qwen2.5:7b"


def test_ask_full_answer_sources_and_removed(tmp_path: Path) -> None:
    fake = FakeOllama(
        models=["m"],
        reply=(
            "The line was created to fix double charges [C1]. It was tuned after a p99 regression [C3][P1]. "
            "This is clearly a performance optimisation. Not in the evidence: who approved the value."
        ),
    )
    a = ask(sample_report(tmp_path), "why 0.4?", model="m", client=fake.client())
    assert a.status == "answered"
    assert [i.id for i in a.sources] == ["C1", "C3", "P1"]
    assert a.sources[2].url == "https://github.com/o/r/pull/99"
    assert a.removed == ["This is clearly a performance optimisation."]
    assert a.raw.startswith("The line was created")
    # what the model was given
    url, payload = fake.calls[-1]
    assert url.endswith("/api/chat") and payload is not None
    assert payload["options"] == {"temperature": 0} and payload["stream"] is False
    assert "[C1] commit" in payload["messages"][1]["content"]
    assert "Question: why 0.4?" in payload["messages"][1]["content"]
    assert "ONLY the numbered evidence" in payload["messages"][0]["content"]


def test_ask_only_insufficient_sentence(tmp_path: Path) -> None:
    fake = FakeOllama(models=["m"], reply="Not in the evidence: the reason for this value.")
    a = ask(sample_report(tmp_path), "why?", model="m", client=fake.client())
    assert a.status == "insufficient_evidence"
    assert a.sentences[0].text.startswith("Not in the evidence")
    assert a.sources == [] and a.removed == []


def test_ask_everything_uncited(tmp_path: Path) -> None:
    fake = FakeOllama(models=["m"], reply="It is for performance. Trust me.")
    a = ask(sample_report(tmp_path), "why?", model="m", client=fake.client())
    assert a.status == "insufficient_evidence"
    assert a.sentences == [] and a.removed == ["It is for performance.", "Trust me."]
    assert "no sentence with a valid citation" in a.message


def test_ask_chat_failure_is_reported(tmp_path: Path) -> None:
    fake = FakeOllama(models=["m"], reply=OllamaUnreachableError("model request failed (HTTP 500): boom"))
    a = ask(sample_report(tmp_path), "why?", model="m", client=fake.client())
    assert a.status == "unavailable" and "boom" in a.message and a.model == "m"


# ---------------------------------------------------------------------------
# privacy: loopback only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "http://127.0.0.1:11434"),
        ("", "http://127.0.0.1:11434"),
        ("localhost:11434", "http://localhost:11434"),
        ("http://127.0.0.1", "http://127.0.0.1:11434"),
        ("http://[::1]:11434", "http://::1:11434"),
    ],
)
def test_resolve_host_accepts_loopback(raw: str | None, expected: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    url, err = resolve_host(raw)
    assert err is None and url == expected


def test_resolve_host_refuses_remote_unless_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_WHY_ALLOW_REMOTE_MODEL", raising=False)
    url, err = resolve_host("http://gpu-box.internal:11434")
    assert err is not None and "not this machine" in err and "GIT_WHY_ALLOW_REMOTE_MODEL" in err
    monkeypatch.setenv("GIT_WHY_ALLOW_REMOTE_MODEL", "1")
    assert resolve_host("http://gpu-box.internal:11434") == ("http://gpu-box.internal:11434", None)


def test_ask_remote_host_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "http://gpu-box.internal:11434")
    monkeypatch.delenv("GIT_WHY_ALLOW_REMOTE_MODEL", raising=False)
    fake = FakeOllama(models=["m"], reply="x [C1].")
    a = ask(sample_report(tmp_path), "why?", model="m", client=fake.client())
    assert a.status == "unavailable" and "not this machine" in a.message
    assert fake.calls == []


def test_urllib_transport_rejects_odd_schemes() -> None:
    with pytest.raises(OllamaUnreachableError):
        ask_mod.urllib_transport("file:///etc/passwd", None)


def test_client_handles_bad_server_responses() -> None:
    def weird(url: str, payload: dict[str, Any] | None) -> tuple[int, str]:
        return (200, "not json") if url.endswith("/api/tags") else (500, "oops")

    c = OllamaClient("http://127.0.0.1:11434", transport=weird)
    with pytest.raises(OllamaUnreachableError):
        c.list_models()
    with pytest.raises(OllamaUnreachableError, match="HTTP 500"):
        c.chat("m", "s", "u")


# ---------------------------------------------------------------------------
# rendering: predictable place, nothing hidden
# ---------------------------------------------------------------------------


def _answered(tmp_path: Path) -> tuple[Any, Answer]:
    report = sample_report(tmp_path)
    fake = FakeOllama(
        models=["m"], reply="Created to fix double charges [C1]. It is fast. Not in the evidence: why 0.4."
    )
    return report, ask(report, "why 0.4?", model="m", client=fake.client())


def test_plain_render_puts_answer_last_with_sources_and_removed(tmp_path: Path) -> None:
    report, answer = _answered(tmp_path)
    buf = io.StringIO()
    render_plain(report, out=buf, answer=answer)
    out = buf.getvalue()
    assert out.isascii()
    summary_at, question_at = out.index("Summary (inferred)"), out.index("Question: why 0.4?")
    assert summary_at < question_at, "the normal report comes first, untouched"
    tail = out[question_at:]
    assert "AI answer (model m via local Ollama at http://127.0.0.1:11434; generated text - check the sources)" in tail
    assert "  Created to fix double charges. [C1]" in tail
    assert "  Not in the evidence: why 0.4." in tail
    assert "  Sources\n    [C1] aaaaaaa" in tail and "https://github.com/o/r/commit/" in tail
    assert "Removed 1 sentence that had no valid citation (not shown as fact):\n    - It is fast." in tail


def test_rich_render_contains_answer_section(tmp_path: Path) -> None:
    report, answer = _answered(tmp_path)
    buf = io.StringIO()
    render_rich(report, out=buf, color=False, answer=answer)
    out = buf.getvalue()
    assert "Question: why 0.4?" in out and "[C1]" in out and "Removed 1 sentence" in out and "- It is fast." in out


def test_plain_render_unavailable_shows_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_WHY_MODEL", raising=False)
    report = sample_report(tmp_path)
    answer = ask(report, "why?", client=FakeOllama(models=None).client())
    buf = io.StringIO()
    render_plain(report, out=buf, answer=answer)
    out = buf.getvalue()
    assert out.isascii()
    assert "AI answer: unavailable" in out
    assert "The Git evidence above is complete without AI. To get answers:" in out
    assert "1. Install Ollama" in out


def test_json_render_includes_ask_block(tmp_path: Path) -> None:
    report, answer = _answered(tmp_path)
    buf = io.StringIO()
    render_json(report, out=buf, answer=answer)
    data = json.loads(buf.getvalue())
    assert data["schema_version"] == 1  # additive: the report part is unchanged
    block = data["ask"]
    assert block["status"] == "answered" and block["model"] == "m"
    assert block["sentences"][0] == {"text": "Created to fix double charges.", "citations": ["C1"]}
    assert block["sources"][0]["id"] == "C1"
    assert block["removed_uncited"] == ["It is fast."]
    assert block["evidence"]["items"][0]["id"] == "C1"
    assert block["raw"].startswith("Created to fix")


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _cli(repo: Path, *args: str, fake: FakeOllama, monkeypatch: pytest.MonkeyPatch) -> tuple[int, str, str]:
    import os

    from git_why import cli

    monkeypatch.setattr(cli, "OllamaClient", lambda host: OllamaClient(host, transport=fake))
    monkeypatch.delenv("GIT_WHY_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    old = os.getcwd()
    os.chdir(repo)
    try:
        r = CliRunner().invoke(cli.app, list(args))
    finally:
        os.chdir(old)
    return r.exit_code, r.stdout, r.stderr


def test_cli_ask_answers_from_real_repo_evidence(blind_spot_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeOllama(
        models=["m"],
        reply="The sentinel was reverted because it broke docs [C3]. Probably also slow. Not in the evidence: who decided.",
    )
    code, out, _ = _cli(
        blind_spot_repo,
        "cfg.py:2",
        "--ask",
        "Why is RETRIES 0?",
        "--model",
        "m",
        "--format",
        "plain",
        fake=fake,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert "RETRIES = 0" in out and "Summary (inferred)" in out  # the normal report is intact
    assert "Question: Why is RETRIES 0?" in out
    assert "The sentinel was reverted because it broke docs. [C3]" in out
    assert "Removed 1 sentence that had no valid citation" in out and "- Probably also slow." in out
    # the model only ever saw git evidence from this repo
    _, payload = fake.calls[-1]
    assert payload is not None and "C: revert to 0, sentinel broke docs" in payload["messages"][1]["content"]


def test_cli_ask_without_ollama_still_exits_zero(blind_spot_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, _ = _cli(
        blind_spot_repo,
        "cfg.py:2",
        "--ask",
        "why?",
        "--format",
        "plain",
        fake=FakeOllama(models=None),
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert "AI answer: unavailable" in out and "1. Install Ollama" in out
    assert "Summary (inferred)" in out


def test_cli_ask_json(blind_spot_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeOllama(models=["m"], reply="Reverted to 0 [C3].")
    code, out, _ = _cli(
        blind_spot_repo, "cfg.py:2", "--ask", "why?", "--model", "m", "--json", fake=fake, monkeypatch=monkeypatch
    )
    assert code == 0
    data = json.loads(out)
    assert data["ask"]["status"] == "answered" and data["ask"]["sentences"][0]["citations"] == ["C3"]


@pytest.mark.parametrize(
    "args",
    [
        ("cfg.py:1-2", "--ask", "why?"),
        ("cfg.py", "--function", "f", "--ask", "why?"),
        ("cfg.py", "--text", "RETRIES = 0", "--ask", "why?"),
        ("cfg.py:2", "--ask", "   "),
    ],
)
def test_cli_ask_rejects_unsupported_targets(
    blind_spot_repo: Path, monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]
) -> None:
    code, _, err = _cli(blind_spot_repo, *args, fake=FakeOllama(models=["m"]), monkeypatch=monkeypatch)
    assert code == 1 and "--ask" in err


def test_cli_without_ask_is_unchanged(blind_spot_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeOllama(models=None)
    code, out, _ = _cli(blind_spot_repo, "cfg.py:2", "--format", "plain", fake=fake, monkeypatch=monkeypatch)
    assert code == 0 and "AI answer" not in out and "Question:" not in out and fake.calls == []
