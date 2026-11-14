"""--ask: evidence pack, citation validation, model selection, fallbacks, CLI wiring. No network, no model."""

from __future__ import annotations

import io
import json
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

    monkeypatch.setattr(ask_mod, "build", lambda report, budget: _retries_pack())
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
    assert out.splitlines()[2:4] == [
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
    assert out.splitlines()[1:7] == [
        "AI answer (model llama3.2): the gathered evidence does not answer this question",
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
