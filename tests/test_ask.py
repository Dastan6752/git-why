"""--ask: evidence pack, citation validation, model selection, fallbacks, CLI wiring. No network, no model."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

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
    assert ids == ["C1", "C2", "C3", "C4", "C5", "I1", "P1", "R1", "S1"]  # the line itself comes last
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
    assert [i.kind for i in pack.items] == ["commit"] * (len(pack.items) - 1) + ["code"]
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
        'The line was added with the message "Fixes #42: double charges" [C1]. '
        'It was later tuned; the commit says "p99 regressed" and the PR says "We measured p99" [C3][P1]. '
        "This was probably done for performance reasons. "
        'Someone also changed it in 2031 because "p99 regressed" [C42]. '
        "The reviewer said the value was tuned empirically [R1]. "
        'The reviewer said "0.3 was chosen at random" [R1, P1]. '
        "Not in the evidence: why exactly 0.3 was chosen."
    )
    kept, removed = validate(raw, pack)
    assert [s.text for s in kept] == [
        'The line was added with the message "Fixes #42: double charges".',
        'It was later tuned; the commit says "p99 regressed" and the PR says "We measured p99".',
        "Not in the evidence: why exactly 0.3 was chosen.",
    ]
    assert [s.citations for s in kept] == [["C1"], ["C3", "P1"], []]
    assert [(r.text, r.reason) for r in removed] == [
        ("This was probably done for performance reasons.", "no citation"),
        ('Someone also changed it in 2031 because "p99 regressed" [C42].', "unknown citation [C42]"),
        ("The reviewer said the value was tuned empirically [R1].", "no verbatim quote from the cited source"),
        (
            'The reviewer said "0.3 was chosen at random" [R1, P1].',
            'quote "0.3 was chosen at random" not found in [R1, P1]',
        ),
    ]


def _retries_pack() -> Any:
    """The psf/requests DEFAULT_RETRIES evidence, as the model saw it when it reversed a comment's meaning."""
    from git_why.evidence import EvidencePack, Item

    return EvidencePack(
        target="src/requests/adapters.py:81",
        line="DEFAULT_RETRIES = 0",
        items=[
            Item(
                "C3",
                "commit",
                "2eb7e3c8",
                "commit 2eb7e3c8: Removed: DEFAULT_RETRIES = object(). Added: DEFAULT_RETRIES = 0.",
            ),
            Item(
                "P1",
                "pr",
                'PR #2216 "Retries"',
                'PR #2216 "Retries" (closed) opened by @someone. Description: Adds max_retries handling.',
                url="https://example.invalid/pull/2216",
                quote="Adds max_retries handling.",
            ),
            Item(
                "R1",
                "comment",
                "@someone (comment) on PR #2216",
                "@someone comment on P1: LGTM, will merge after CI.",
                url="https://example.invalid/pull/2216#c1",
                quote="LGTM, will merge after CI.",
            ),
            Item(
                "R2",
                "comment",
                "@sigmavirus24 (review comment) on PR #2216",
                "@sigmavirus24 review comment on P1: I realize that using `0` will make it a magic number in this case. "
                "Perhaps on the sentinel object we set a `value`, e.g.,",
                url="https://example.invalid/pull/2216#r2",
                quote="I realize that using `0` will make it a magic number in this case. "
                "Perhaps on the sentinel object we set a `value`, e.g.,",
            ),
        ],
    )


def test_validate_rejects_reversed_meaning_and_invented_detail() -> None:
    """A real model answer: cited [R2] but inverted it and added a detail R2 never mentions."""
    raw = (
        "The value 0 was chosen to avoid a magic number, with DEFAULT_RETRIES.value = 0 on the sentinel [R2]. "
        "The value was set back to 0 in October 2014 [C3]."
    )
    kept, removed = validate(raw, _retries_pack())
    assert kept == []
    assert [r.reason for r in removed] == ["no verbatim quote from the cited source"] * 2


def test_validate_rejects_a_quote_the_source_never_said() -> None:
    kept, removed = validate('The reviewer said 0 "avoids a magic number" [R2].', _retries_pack())
    assert kept == []
    assert removed[0].reason == 'quote "avoids a magic number" not found in [R2]'


def test_validate_accepts_the_honest_answer() -> None:
    raw = (
        'The PR discussion says that using 0 "will make it a magic number in this case" and considers a sentinel '
        "object [R2]. Not in the evidence: why the final value was set back to 0."
    )
    kept, removed = validate(raw, _retries_pack())
    assert removed == []
    assert [s.citations for s in kept] == [["R2"], []]
    assert kept[1].text == "Not in the evidence: why the final value was set back to 0."


def test_validate_rejects_concern_recast_as_the_reason_and_keeps_the_honest_sentences() -> None:
    """
    Exact failure from a real run: the quote is genuinely in R2, but R2 raises a
    concern ("will make it a magic number"); it does not say this is why the final
    value was set to 0. Only the descriptive sentence and "Not in the evidence" may stay.
    """
    raw = (
        'The reason for setting DEFAULT_RETRIES to 0 is that using 0 "will make it a magic number" [R2]. '
        "The PR discussion says using 0 \u2018will make it a magic number in this case\u2019 and considers a "
        "sentinel object [R2]. "
        "Not in the evidence: why the final value was set back to 0."
    )
    kept, removed = validate(raw, _retries_pack())
    assert [s.text for s in kept] == [
        "The PR discussion says using 0 \u2018will make it a magic number in this case\u2019 and considers a "
        "sentinel object.",
        "Not in the evidence: why the final value was set back to 0.",
    ]
    assert [s.citations for s in kept] == [["R2"], []]
    assert not kept[0].claims_reason and kept[1].is_insufficient
    assert len(removed) == 1
    assert removed[0].text.startswith("The reason for setting DEFAULT_RETRIES to 0")
    assert removed[0].reason == "states a reason, but the quoted source only raises a concern, question or suggestion"


def test_validate_rejects_concern_as_reason_even_without_a_contradiction() -> None:
    raw = 'The reason for setting DEFAULT_RETRIES to 0 is that using 0 "will make it a magic number" [R2].'
    kept, removed = validate(raw, _retries_pack())
    assert kept == [] and removed[0].reason.startswith("states a reason, but the quoted source")


@pytest.mark.parametrize(
    "framing",
    [
        'It was set to 0 to avoid a situation where "using 0 will make it a magic number" [R2].',
        'Because "using 0 will make it a magic number", the sentinel was dropped [R2].',
        'This is why "using 0 will make it a magic number" led to the change [R2].',
        'The value was chosen so that "using 0 will make it a magic number" would not matter [R2].',
    ],
)
def test_validate_rejects_other_causal_framings_of_a_concern(framing: str) -> None:
    kept, removed = validate(framing, _retries_pack())
    assert kept == [], framing
    assert removed[0].reason.startswith("states a reason, but the quoted source")


def test_validate_accepts_a_reason_the_source_actually_states() -> None:
    from git_why.evidence import EvidencePack, Item

    pack = EvidencePack(
        "f:1",
        "x",
        [Item("C3", "commit", "c", 'commit c2bc2db: "C: revert to 0, sentinel broke docs". Added: RETRIES = 0.')],
    )
    kept, removed = validate('It was reverted to 0 because the "sentinel broke docs" [C3].', pack)
    assert removed == [] and kept[0].claims_reason is True


def test_validate_never_keeps_a_reason_next_to_not_in_the_evidence() -> None:
    from git_why.evidence import EvidencePack, Item

    pack = EvidencePack(
        "f:1",
        "x",
        [Item("C3", "commit", "c", 'commit c2bc2db: "C: revert to 0, sentinel broke docs". Added: RETRIES = 0.')],
    )
    raw = 'It was reverted to 0 because the "sentinel broke docs" [C3]. Not in the evidence: why 0 exactly.'
    kept, removed = validate(raw, pack)
    assert [s.text for s in kept] == ["Not in the evidence: why 0 exactly."]
    assert removed[0].reason.startswith('states a reason while the answer also says "Not in the evidence"')


LLAMA32_OBSERVED = (
    'The reason for setting DEFAULT_RETRIES to 0 is that using 0 "will make it a magic number" [R2]. '
    "The PR discussion says using 0 \u2018will make it a magic number in this case\u2019 and considers a "
    "sentinel object [R2]. "
    "Not in the evidence: why the final value was set back to 0."
)
HONEST_LINE = (
    "The PR discussion says using 0 \u2018will make it a magic number in this case\u2019 and considers a "
    "sentinel object. [R2]"
)


def _ask_with_pack(raw: str, monkeypatch: pytest.MonkeyPatch, question: str = "Why is DEFAULT_RETRIES 0?") -> Any:
    """Run the full ask() pipeline (FakeOllama -> validate -> fallback) against the requests evidence pack."""
    import git_why.ask as ask_mod

    monkeypatch.setattr(ask_mod, "build", lambda report, budget, mode: _retries_pack())
    fake = FakeOllama(models=["llama3.2"], reply=raw)
    report = SimpleNamespace(target=SimpleNamespace(label="src/requests/adapters.py:81"))
    return ask(cast(Any, report), question, model="llama3.2", client=fake.client())


def test_regression_llama32_observed_output_rendered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The llama3.2 answer observed on psf/requests: only the two honest sentences may reach the terminal."""
    answer = _ask_with_pack(LLAMA32_OBSERVED, monkeypatch)
    assert answer.status == "answered" and answer.fallback is None
    buf = io.StringIO()
    render_plain(sample_report(tmp_path), out=buf, answer=answer)
    out = buf.getvalue()[buf.getvalue().index("Question:") :]
    assert out.splitlines()[2] == "  question type: history"
    assert out.splitlines()[3:5] == [
        f"  {HONEST_LINE}",
        "  Not in the evidence: why the final value was set back to 0.",
    ]
    assert "The reason for setting" not in out and 'magic number" [R2]' not in out
    assert "[R2] @sigmavirus24 (review comment) on PR #2216  https://example.invalid/pull/2216#r2" in out
    assert "Removed 1 generated sentence the evidence does not support (--show-removed or --json lists them)." in out
    assert "reason:" not in out  # the detailed list is not part of normal output


def test_regression_llama32_when_only_the_false_reason_is_produced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same run without the honest sentence: validation removes every factual sentence, so the user
    gets the most relevant comment as an exact quote with its link, then the no-decision line."""
    raw = (
        'The reason for setting DEFAULT_RETRIES to 0 is that using 0 "will make it a magic number" [R2]. '
        "Not in the evidence: why the final value was set back to 0."
    )
    answer = _ask_with_pack(raw, monkeypatch)
    assert answer.status == "insufficient_evidence"
    assert answer.fallback is not None and answer.fallback.item.id == "R2"
    assert [s.text for s in answer.sentences] == ["Not in the evidence: why the final value was set back to 0."]
    buf = io.StringIO()
    render_plain(sample_report(tmp_path), out=buf, answer=answer)
    out = buf.getvalue()[buf.getvalue().index("Question:") :]
    assert out.splitlines()[1:8] == [
        "AI answer (model llama3.2): the gathered evidence does not answer this question",
        "  question type: history",
        "  Most relevant comment in the evidence (exact quote) [R2]:",
        '    "I realize that using `0` will make it a magic number in this case."',
        "    - @sigmavirus24 (review comment) on PR #2216  https://example.invalid/pull/2216#r2",
        "  The evidence does not explain the final decision.",
        "  Not in the evidence: why the final value was set back to 0.",
    ]
    assert "The reason for setting" not in out
    assert "Removed 1 generated sentence" in out and "reason:" not in out
    # JSON keeps the full audit trail
    assert answer.to_dict()["fallback"]["url"] == "https://example.invalid/pull/2216#r2"
    assert answer.to_dict()["fallback"]["full"].endswith("we set a `value`, e.g.,")
    assert answer.to_dict()["removed"][0]["reason"].startswith("states a reason, but the quoted source")


def test_fallback_picks_the_comment_matching_question_and_attempted_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    # nothing in the question matches R2, but the model tried to quote it -> R2 still wins over R1
    answer = _ask_with_pack('It was "a magic number" obviously [R1].', monkeypatch)
    assert answer.fallback is not None and answer.fallback.item.id == "R2"
    # with no hint at all, the question decides; "merge" matches R1
    a = _ask_with_pack("No idea.", monkeypatch, question="When will this merge after CI?")
    assert a.fallback is not None and a.fallback.item.id == "R1"


def test_fallback_quote_is_shortened_to_the_most_relevant_exact_sentence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the fallback used to print the whole comment; only the sentence that matters is shown."""
    raw = 'The reason for setting DEFAULT_RETRIES to 0 is that using 0 "will make it a magic number" [R2].'
    answer = _ask_with_pack(raw, monkeypatch)
    assert answer.fallback is not None
    assert answer.fallback.quote == "I realize that using `0` will make it a magic number in this case."
    assert answer.fallback.full.startswith(answer.fallback.quote) and "Perhaps" in answer.fallback.full
    # the sentence is exact: it occurs verbatim in the evidence the model saw
    assert answer.fallback.quote in answer.pack.get("R2").text
    # the "Not in the evidence" conclusion and the link stay
    buf = io.StringIO()
    render_plain(sample_report(_tmp_dir()), out=buf, answer=answer)
    out = buf.getvalue()
    assert '    "I realize that using `0` will make it a magic number in this case."\n' in out
    assert "Perhaps on the sentinel" not in out
    assert "https://example.invalid/pull/2216#r2" in out and "The evidence does not explain the final decision." in out


def test_most_relevant_sentence_follows_the_question_and_keeps_single_sentences() -> None:
    from git_why.ask import _tokens, most_relevant_sentence

    text = "First we tried a sentinel. The docs build broke with it. So we went back to zero."
    assert most_relevant_sentence(text, _tokens("why did the docs break?")) == "The docs build broke with it."
    assert most_relevant_sentence(text, _tokens("zero?")) == "So we went back to zero."
    assert most_relevant_sentence(text, set()) == "First we tried a sentinel."  # tie -> first sentence
    assert most_relevant_sentence("Only one sentence here", _tokens("anything")) == "Only one sentence here"


def _tmp_dir() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp())


def test_fallback_is_none_without_comments_or_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    from git_why.ask import most_relevant_comment
    from git_why.evidence import EvidencePack, Item

    pack = EvidencePack("f:1", "x", [Item("C1", "commit", "c", "commit c: fix")])
    assert most_relevant_comment(pack, "why?", []) is None


def test_quote_parsing_accepts_curly_single_quote_pairs_but_not_apostrophes() -> None:
    from git_why.ask import quotes_in

    assert quotes_in("says \u2018will make it a magic number\u2019 here") == ["will make it a magic number"]
    assert quotes_in("it doesn\u2019t matter and isn\u2019t quoted") == []


def test_quote_matching_ignores_backticks_case_and_punctuation() -> None:
    kept, _ = validate('It says "using 0 will make it a magic number" [R2].', _retries_pack())
    assert len(kept) == 1
    kept, _ = validate('It notes "we set a `value`" on the sentinel [R2].', _retries_pack())
    assert len(kept) == 1


def test_short_quotes_do_not_count() -> None:
    _, removed = validate('The value is "0" [C3].', _retries_pack())
    assert removed[0].reason == "no verbatim quote from the cited source"


def test_system_prompt_carries_the_reason_rules() -> None:
    from git_why.ask import SYSTEM_PROMPT

    assert "verbatim quote" in SYSTEM_PROMPT
    assert "A concern, question or suggestion in a comment is not a decision" in SYSTEM_PROMPT
    assert "Not in the evidence:" in SYSTEM_PROMPT
    assert "will make it a magic number in this case" in SYSTEM_PROMPT  # the worked example


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
    fake = FakeOllama(models=["qwen2.5:7b"], reply='The line was added with "Fixes #42: double charges" [C1].')
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
    fake = FakeOllama(models=["llama3.2:latest"], reply='Added with "Fixes #42: double charges" [C1].')
    a = ask(sample_report(tmp_path), "why?", model="llama3.2", client=fake.client())
    assert a.status == "answered" and a.model == "llama3.2:latest"


def test_ask_env_model_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_WHY_MODEL", "qwen2.5:7b")
    fake = FakeOllama(models=["qwen2.5:7b", "other"], reply='Added with "Fixes #42: double charges" [C1].')
    a = ask(sample_report(tmp_path), "why?", client=fake.client())
    assert a.status == "answered" and a.model == "qwen2.5:7b"


def test_ask_full_answer_sources_and_removed(tmp_path: Path) -> None:
    fake = FakeOllama(
        models=["m"],
        reply=(
            'The line was created with "Fixes #42: double charges" [C1]. '
            'It was tuned when "p99 regressed" and the PR says "We measured p99" [C3][P1]. '
            "This is clearly a performance optimisation. Not in the evidence: who approved the value."
        ),
    )
    a = ask(sample_report(tmp_path), "why 0.4?", model="m", client=fake.client())
    assert a.status == "answered"
    assert [i.id for i in a.sources] == ["C1", "C3", "P1"]
    assert a.sources[2].url == "https://github.com/o/r/pull/99"
    assert [(r.text, r.reason) for r in a.removed] == [("This is clearly a performance optimisation.", "no citation")]
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
    assert a.sentences == [] and [r.text for r in a.removed] == ["It is for performance.", "Trust me."]
    # nothing factual survived: the best exact quote (here the only review comment) is offered instead
    assert a.fallback is not None and a.fallback.item.id == "R1" and a.message == ""


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
        models=["m"], reply='Created with "Fixes #42: double charges" [C1]. It is fast. Not in the evidence: why 0.4.'
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
    assert '  Created with "Fixes #42: double charges". [C1]' in tail
    assert "  Not in the evidence: why 0.4." in tail
    assert "  Sources\n    [C1] aaaaaaa" in tail and "https://github.com/o/r/commit/" in tail
    # the normal output only counts removed sentences; the list is opt-in
    assert "Removed 1 generated sentence the evidence does not support (--show-removed or --json lists them).\n" in tail
    assert "It is fast." not in tail
    buf = io.StringIO()
    render_plain(report, out=buf, answer=answer, show_removed=True)
    assert (
        "Removed 1 sentence the evidence does not support:\n    - It is fast.\n      reason: no citation\n"
        in buf.getvalue()
    )


def test_rich_render_contains_answer_section(tmp_path: Path) -> None:
    report, answer = _answered(tmp_path)
    buf = io.StringIO()
    render_rich(report, out=buf, color=False, answer=answer)
    out = buf.getvalue()
    assert "Question: why 0.4?" in out and "[C1]" in out and "Removed 1 generated sentence" in out
    assert "It is fast." not in out
    buf = io.StringIO()
    render_rich(report, out=buf, color=False, answer=answer, show_removed=True)
    assert "- It is fast." in buf.getvalue() and "reason: no citation" in buf.getvalue()


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
    assert block["sentences"][0] == {
        "text": 'Created with "Fixes #42: double charges".',
        "citations": ["C1"],
        "claims_reason": False,
        "is_insufficient": False,
    }
    assert block["sources"][0]["id"] == "C1"
    assert block["removed"] == [{"text": "It is fast.", "reason": "no citation"}]
    assert block["evidence"]["items"][0]["id"] == "C1"
    assert block["raw"].startswith("Created with")


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
        reply=(
            'The sentinel was reverted; the commit says "revert to 0, sentinel broke docs" [C3]. '
            "Probably also slow. Not in the evidence: who decided."
        ),
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
    assert 'The sentinel was reverted; the commit says "revert to 0, sentinel broke docs". [C3]' in out
    assert "Removed 1 generated sentence the evidence does not support" in out
    assert "Probably also slow." not in out  # details are behind --show-removed / --json
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
    fake = FakeOllama(models=["m"], reply='Commit C says "revert to 0, sentinel broke docs" [C3].')
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


# ---------------------------------------------------------------------------
# question types: code explanation vs. historical reason (end to end)
# ---------------------------------------------------------------------------

HELPER_FILE = '''import sys


def fail(message, code=1):
    """Print an error and stop."""
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)
'''


def _last_prompt(fake: FakeOllama) -> str:
    payload = fake.calls[-1][1]
    assert payload is not None
    return str(payload["messages"][-1]["content"])


def _system_prompt(fake: FakeOllama) -> str:
    payload = fake.calls[-1][1]
    assert payload is not None
    return str(payload["messages"][0]["content"])


@pytest.fixture
def helper_repo(make_repo: Callable[[str], Path]) -> Path:
    from tests.conftest import write_commit

    repo = make_repo("helper")
    write_commit(repo, "app.py", HELPER_FILE, "add fail helper")
    return repo


def test_e2e_code_question_is_answered_from_the_source_code(helper_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """'What does this line do?' must be grounded in the code at HEAD, not in a commit title."""
    fake = FakeOllama(
        models=["m"],
        reply=(
            'The line calls `print(f"error: {message}", file=sys.stderr)`, which writes to stderr [S1]. '
            'It belongs to the helper "def fail(message, code=1)" which then calls `sys.exit(code)` [S2]. '
            "The `file=sys.stderr` argument was added later by a reviewer [S1]. "
            "Not in the evidence: where fail() is called from."
        ),
    )
    code, out, _ = _cli(
        helper_repo,
        "app.py:6",
        "--ask",
        "What does this line do?",
        "--model",
        "m",
        "--format",
        "plain",
        fake=fake,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    tail = out[out.index("Question: What does this line do?") :]
    assert "question type: code explanation" in tail
    # grounded sentences survive, each cited to source code
    assert 'The line calls `print(f"error: {message}", file=sys.stderr)`, which writes to stderr. [S1]' in tail
    assert "which then calls `sys.exit(code)`. [S2]" in tail
    assert "  Not in the evidence: where fail() is called from." in tail
    # a historical claim cited to source code is rejected (and not silently), even though its quote is real
    assert "added later by a reviewer" not in tail.split("Removed")[0]
    assert "Removed 1 generated sentence the evidence does not support" in tail
    code2, out2, _ = _cli(
        helper_repo,
        "app.py:6",
        "--ask",
        "What does this line do?",
        "--model",
        "m",
        "--format",
        "plain",
        "--show-removed",
        fake=fake,
        monkeypatch=monkeypatch,
    )
    assert "reason: historical claim cited to source code, not to a commit or discussion" in out2
    # sources point at the code at HEAD
    assert "[S1] app.py:6 (HEAD)" in tail and "[S2] app.py lines 4-7 (HEAD)" in tail
    # the model saw the code and the commit, in that order
    prompt = _last_prompt(fake)
    assert prompt.index("[S1]") < prompt.index("[S2]") < prompt.index("[C1]")
    assert 'print(f"error: {message}", file=sys.stderr)' in prompt
    assert _system_prompt(fake).startswith("You explain what a line of code does")


def test_e2e_history_question_keeps_the_current_behaviour(
    blind_spot_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'Why ...' questions keep commits/PRs/comments as the primary evidence and all existing rules."""
    fake = FakeOllama(
        models=["m"],
        reply=(
            'It went back to 0 in the commit "C: revert to 0, sentinel broke docs" [C3]. '
            'The line currently reads "RETRIES = 0" [S1]. '
            "The sentinel was chosen to avoid a magic number [C2]. "
            "Not in the evidence: why 0 rather than another number."
        ),
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
    tail = out[out.index("Question: Why is RETRIES 0?") :]
    assert "question type: history" in tail
    assert 'It went back to 0 in the commit "C: revert to 0, sentinel broke docs". [C3]' in tail
    assert 'The line currently reads "RETRIES = 0". [S1]' in tail
    assert "  Not in the evidence: why 0 rather than another number." in tail
    assert "chosen to avoid" not in tail.split("Removed")[0]
    assert "Removed 1 generated sentence the evidence does not support" in tail
    prompt = _last_prompt(fake)
    assert prompt.index("[C1]") < prompt.index("[C3]") < prompt.index("[S1]")  # history first, code last
    assert "[S2]" not in prompt  # no enclosing block for history questions
    assert _system_prompt(fake).startswith("You answer one question about a single line")


@pytest.mark.parametrize(
    ("question", "mode"),
    [
        ("What does this line do?", "code"),
        ("what does this do", "code"),
        ("How does this work?", "code"),
        ("Explain this line.", "code"),
        ("What is err=True for?", "code"),
        ("What does this return?", "code"),
        ("Why is this timeout set to 30?", "history"),
        ("Why was this line added?", "history"),
        ("When did this change?", "history"),
        ("Who wrote this?", "history"),
        ("What is the purpose of this line?", "history"),  # purpose = intent -> history evidence first
        ("What changed here?", "history"),
        ("Was this ever different?", "history"),
        ("Which PR introduced this?", "history"),
        ("Is there a reason for 0?", "history"),
        ("What did this look like before?", "history"),
    ],
)
def test_classify_question(question: str, mode: str) -> None:
    from git_why.ask import classify_question

    assert classify_question(question) == mode


def test_code_mode_pack_puts_source_first_and_history_mode_puts_it_last(
    helper_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = investigate(helper_repo / "app.py", 6, 6, Options())
    code_pack = build(report, mode="code")
    assert [i.id for i in code_pack.items] == ["S1", "S2", "C1"]
    s1, s2 = code_pack.get("S1"), code_pack.get("S2")
    assert s1 is not None and s2 is not None
    assert s1.kind == s2.kind == "code"
    assert s1.quote == '    print(f"error: {message}", file=sys.stderr)' and s1.label == "app.py:6 (HEAD)"
    assert s2.quote.splitlines()[0] == "def fail(message, code=1):" and s2.label == "app.py lines 4-7 (HEAD)"
    assert s1.url is None and s2.url is None  # no web remote in the fixture repo
    history_pack = build(report, mode="history")
    assert [i.id for i in history_pack.items] == ["C1", "S1"]
    assert "S2" not in history_pack.ids
    # the prompt shows the code verbatim, line by line
    text = format_for_prompt(code_pack)
    assert '[S1] source code, app.py line 6 at HEAD:\n    print(f"error: {message}", file=sys.stderr)' in text


def test_code_items_link_to_the_blob_when_the_remote_is_known(tmp_path: Path) -> None:
    report = sample_report(tmp_path)
    report.target.context_start, report.target.context = 1, ["def f():", "    sleep(0.4)", "    return 1"]
    report.target.start = report.target.end = 2
    pack = build(report, mode="code")
    s1, s2 = pack.get("S1"), pack.get("S2")
    assert s1 is not None and s2 is not None
    assert s1.url == f"https://github.com/o/r/blob/{report.repo.head}/f.py#L2"
    assert s2.url == f"https://github.com/o/r/blob/{report.repo.head}/f.py#L1-L3"


def test_code_items_skipped_for_text_targets(tmp_path: Path) -> None:
    report = sample_report(tmp_path)
    report.target.text = "sleep(0.4)"
    assert all(i.kind != "code" for i in build(report, mode="code").items)


def test_enclosing_block_is_dropped_when_it_equals_the_target(tmp_path: Path) -> None:
    report = sample_report(tmp_path)
    report.target.context_start, report.target.context = report.target.start, list(report.target.lines)
    assert "S2" not in build(report, mode="code").ids


def test_enclosing_block_is_clipped_at_a_line_boundary(tmp_path: Path) -> None:
    from git_why import evidence

    report = sample_report(tmp_path)
    report.target.context_start = 1
    report.target.context = [f"line {n} " + "x" * 60 for n in range(1, 41)]
    pack = build(report, mode="code")
    s2 = pack.get("S2")
    assert s2 is not None and len(s2.quote) <= evidence.MAX_CODE_CHARS + 2 and s2.quote.endswith("\n…")
    assert all(ln == "…" or ln.startswith("line ") for ln in s2.quote.splitlines())


def test_enclosing_block_heuristics() -> None:
    from git_why.functions import enclosing_block

    py = "import os\n\n\nclass A:\n    def m(self):\n        x = 1\n        return x\n\n\ndef g():\n    pass\n"
    assert enclosing_block(py, 6, 6) == (5, ["    def m(self):", "        x = 1", "        return x"])
    assert enclosing_block(py, 1, 1)[0] == 1  # top-level: a window, starting at the file start
    js = "const x = 1;\n\nfunction fail(msg) {\n  console.error(msg);\n  process.exit(1);\n}\n"
    assert enclosing_block(js, 4, 4) == (
        3,
        ["function fail(msg) {", "  console.error(msg);", "  process.exit(1);", "}"],
    )
    assert enclosing_block("", 1, 1) == (0, [])
    assert enclosing_block("a\n", 2, 2) == (0, [])  # out of range
    big = "def huge():\n" + "".join(f"    x{n} = {n}\n" for n in range(100))
    start, lines = enclosing_block(big, 50, 50)
    assert len(lines) == 21 and start == 40 and "    x48 = 48" in lines  # too big -> +-10 window


def test_function_extent_ignores_braces_inside_fstrings() -> None:
    from git_why.functions import function_extent

    src = 'def _fail(message):\n    print(f"error: {message}")\n    raise SystemExit(1)\n\n\ndef other():\n    pass\n'
    assert function_extent(src, 1) == (1, 3)


def test_backtick_quotes_and_whole_line_code_quotes_count() -> None:
    from git_why.ask import quotes_in
    from git_why.evidence import Item

    s1 = Item(
        "S1", "code", "f.py:2 (HEAD)", "source code, f.py line 2 at HEAD:\n    RETRIES = 0", quote="    RETRIES = 0"
    )
    assert quotes_in("the line is `RETRIES = 0` here", [s1]) == ["RETRIES = 0"]  # whole code line, 2 words
    assert quotes_in('the line is "RETRIES = 0" here', [s1]) == ["RETRIES = 0"]
    assert quotes_in("the line is `RETRIES = 0` here", []) == []  # not a whole line of a cited code item
    assert quotes_in("it calls `sys.exit(code)` now", []) == ["sys.exit(code)"]  # three tokens after normalising
    assert quotes_in("a `0` and `x` here", []) == []
    # a stray pair of backticks around prose is not a quote span
    assert quotes_in("use `0` will make it a magic `value`", []) == []


def test_validate_rejects_history_claims_cited_only_to_code(helper_repo: Path) -> None:
    from git_why.ask import HISTORY_FROM_CODE

    report = investigate(helper_repo / "app.py", 6, 6, Options())
    pack = build(report, mode="code")
    for raw in [
        "The `file=sys.stderr` argument was added by a reviewer [S1].",
        "`sys.exit(code)` was introduced in 2021 [S2].",
        'The author wrote `print(f"error: {message}", file=sys.stderr)` to fix issue #12 [S1].',
        "Originally `sys.exit(code)` did not exist [S2].",
    ]:
        kept, removed = validate(raw, pack)
        assert kept == [] and removed[0].reason == HISTORY_FROM_CODE, raw
    # the same claim cited to the commit that actually says so passes the history checks
    kept, removed = validate('The line was created in the commit "add fail helper" [C1].', pack)
    assert removed == [] and kept[0].citations == ["C1"]
    # describing behaviour from the code is fine
    kept, removed = validate("It ends with `sys.exit(code)`, which stops the process [S2].", pack)
    assert removed == [] and kept[0].citations == ["S2"]


def test_reason_rule_still_applies_when_code_and_history_are_cited_together(helper_repo: Path) -> None:
    from git_why.ask import REASON_NOT_IN_SOURCE

    report = investigate(helper_repo / "app.py", 6, 6, Options())
    pack = build(report, mode="code")
    raw = 'The reason for `file=sys.stderr` is the commit "add fail helper" [S1][C1].'
    kept, removed = validate(raw, pack)
    assert kept == [] and removed[0].reason == REASON_NOT_IN_SOURCE


def test_code_mode_fallback_quotes_the_line_itself(helper_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeOllama(models=["m"], reply="It prints something, probably an error. Trust me.")
    code, out, _ = _cli(
        helper_repo,
        "app.py:6",
        "--ask",
        "What does this line do?",
        "--model",
        "m",
        "--format",
        "plain",
        fake=fake,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    tail = out[out.index("Question:") :]
    assert "  Most relevant code in the evidence (exact quote) [S1]:" in tail
    assert '    "print(f"error: {message}", file=sys.stderr)"' in tail
    assert "  The evidence shows the code, but the model produced no verified explanation of it." in tail
    assert "The evidence does not explain the final decision." not in tail
    assert "Removed 2 generated sentences" in tail


def test_json_report_schema_is_unchanged_and_ask_block_carries_mode(
    helper_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOllama(models=["m"], reply="It calls `sys.exit(code)` at the end [S2].")
    code, out, _ = _cli(
        helper_repo,
        "app.py:6",
        "--ask",
        "How does this work?",
        "--model",
        "m",
        "--json",
        fake=fake,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    data = json.loads(out)
    assert data["schema_version"] == 1
    assert set(data["target"]) == {"file", "start", "end", "function", "text", "lines"}  # no new report keys
    assert data["ask"]["mode"] == "code"
    assert [i["id"] for i in data["ask"]["evidence"]["items"]] == ["S1", "S2", "C1"]
    assert data["ask"]["sentences"][0]["citations"] == ["S2"]
    assert data["ask"]["evidence"]["items"][0]["kind"] == "code"


def test_rich_render_shows_question_type_and_code_source(helper_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeOllama(models=["m"], reply="It calls `sys.exit(code)` at the end [S2].")
    code, out, _ = _cli(
        helper_repo,
        "app.py:6",
        "--ask",
        "How does this work?",
        "--model",
        "m",
        "--format",
        "rich",
        "--no-color",
        fake=fake,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert "question type: code explanation" in out and "[S2] app.py lines 4-7 (HEAD)" in out
