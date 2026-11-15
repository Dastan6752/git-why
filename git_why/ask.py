"""
``--ask``: answer a question about a line **only** from the evidence git-why gathered.

Design rules (see README "Asking questions"):

* The model sees the numbered evidence pack (:mod:`git_why.evidence`) and nothing else.
* Every sentence that states a fact must cite ``[C1]``-style IDs from the pack
  **and** contain a verbatim quote from the cited source. Both are checked
  mechanically: an unknown ID, a missing quote, or a quote that does not occur
  in the cited source removes the sentence — and the removal is reported with
  its reason, never hidden. (This catches the classic failure of citing a
  comment while reversing or embellishing what it says.)
* If the evidence does not answer the question the model must say so with a
  sentence starting with ``Not in the evidence:`` — that sentence needs no citation.
* Only a **local Ollama** server is supported, and only on a loopback address
  unless ``GIT_WHY_ALLOW_REMOTE_MODEL=1`` is set. No cloud provider.
* There is no default model. Pass ``--model`` or set ``GIT_WHY_MODEL``; if exactly
  one model is installed it is used and named in the output.
* Whatever goes wrong, the normal history report has already been produced; the
  answer section then explains what is missing and how to set it up.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .evidence import DEFAULT_BUDGET, MODE_CODE, MODE_HISTORY, EvidencePack, Item, build, format_for_prompt
from .report import Report

DEFAULT_HOST = "http://127.0.0.1:11434"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
INSUFFICIENT_PREFIX = "not in the evidence"
MAX_WORDS = 150

# A transport maps (url, json payload or None for GET) -> (http status, body text).
# It raises OllamaUnreachableError when the server cannot be reached at all.
Transport = Callable[[str, dict[str, Any] | None], tuple[int, str]]


class OllamaUnreachableError(Exception):
    pass


SYSTEM_PROMPT = (
    "You answer one question about a single line of code using ONLY the numbered evidence provided.\n\n"
    "Rules:\n"
    "1. Every sentence that states a fact must (a) contain a short verbatim quote from the evidence in double "
    "quotes - at least three consecutive words copied exactly - and (b) end with the IDs of the evidence the quote "
    "comes from, in square brackets, e.g. [C2] or [C2][P1].\n"
    "2. Use only IDs that appear in the evidence. Never cite anything else. [S1] is the current text of the line: "
    "it can support what the line says now, never when, why or by whom it was added - those need [C..], [P..], "
    "[I..] or [R..].\n"
    "3. A reason may be stated ONLY if the quoted text itself gives it as the reason. A concern, question or "
    "suggestion in a comment is not a decision: report it as what that person said, not as why the value was chosen. "
    "Never turn 'X would be a problem' into 'X was chosen to avoid the problem', and never write "
    "'the reason is' unless the quoted words themselves say because / to fix / so that / broke.\n"
    "4. Do not add values, names, numbers or details that are not inside the evidence text you quote.\n"
    "5. If the evidence does not state the reason for the current value, or only part of it, end with one sentence "
    'that starts with "Not in the evidence:" saying exactly what is missing. '
    "That sentence needs no quote or citation.\n"
    f"6. Be brief: at most {MAX_WORDS} words, plain prose, no headings or bullet points.\n\n"
    "Example of a correct answer (the evidence showed a reviewer worrying about a magic number and the value later "
    "being set to 0 without a stated reason):\n"
    'The PR discussion says that using 0 "will make it a magic number in this case" and considers a sentinel object '
    "[R2]. Not in the evidence: why the final value was set back to 0.\n"
)

SYSTEM_PROMPT_CODE = (
    "You explain what a line of code does using ONLY the numbered evidence provided. The evidence starts with the "
    "source code at HEAD: [S1] is the line itself and [S2] its enclosing block; commits and discussions follow.\n\n"
    "Rules:\n"
    "1. Every sentence that states a fact must (a) quote the code or evidence verbatim - at least three consecutive "
    "words, or a whole line of code - inside backticks or double quotes, and (b) end with the IDs it comes from, "
    "e.g. [S1] or [S2].\n"
    "2. Describe only behaviour you can read in the quoted code: what is called, with which arguments, what is "
    "returned or raised. Do not guess about code you cannot see.\n"
    "3. Do not say when, why or by whom the line was written unless you quote a commit or discussion "
    "([C1], [P1], [R1] ...) that says so. A code-level purpose is not a historical reason.\n"
    "4. Do not add names, values or behaviour that are not in the quoted text.\n"
    "5. If the question asks for something the evidence does not show (callers, runtime values, intent), end with "
    'one sentence that starts with "Not in the evidence:". That sentence needs no quote or citation.\n'
    f"6. Be brief: at most {MAX_WORDS} words, plain prose, no headings or bullet points.\n\n"
    "Example of a correct answer:\n"
    'The line calls `typer.secho(f"error: {message}", fg="red", err=True)`, which prints the message in red to '
    "stderr [S1]. It is the first statement of `def _fail(message: str, code: int, hint: str | None = None)`, "
    "which ends with `raise typer.Exit(code)` [S2]. Not in the evidence: where _fail is called from.\n"
)

# Words that make a question about *history* (why / when / who / what changed). Anything else is a
# code-explanation question ("what does this do", "how does this work", "explain this line").
_HISTORY_QUESTION = re.compile(
    r"\bwhy\b|\bwhen\b|\bwho\b|\bwhom\b|\breasons?\b|\bhistor|\bchang(e|ed|es|ing)\b|\badded\b|\bintroduc"
    r"|\bremov|\brevert|\bcommit|\bpull request|\bPRs?\b|\breview|\bdecid|\boriginal|\bbefore\b|\bused to\b"
    r"|\bpurpose\b|\bmotivat|\bauthor|\bissue\b|\bago\b|\bbecame\b|\bever\b|\bsince\b|\bstill\b|\bchosen\b",
    re.I,
)


def classify_question(question: str) -> str:
    """``history`` if the question asks why/when/who/what changed; otherwise ``code``."""
    return MODE_HISTORY if _HISTORY_QUESTION.search(question) else MODE_CODE


MODE_LABEL = {MODE_HISTORY: "history", MODE_CODE: "code explanation"}

USER_TEMPLATE_CODE = """Question: {question}

Evidence:
{evidence}

Answer (quote the code verbatim and cite [S1]/[S2] after each factual sentence; \
say "Not in the evidence:" for anything the code and history do not show):"""

USER_TEMPLATE = """Question: {question}

Evidence:
{evidence}

Answer (quote the evidence verbatim and cite IDs after each factual sentence; \
say "Not in the evidence:" for anything the evidence does not state, especially the reason):"""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class Sentence:
    text: str
    citations: list[str] = field(default_factory=list)
    claims_reason: bool = False  # the sentence presents something as *why* (allowed only if the quote states it)
    is_insufficient: bool = False  # a "Not in the evidence:" sentence
    added_by_git_why: bool = False  # written by the validator, not by the model

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "citations": list(self.citations),
            "claims_reason": self.claims_reason,
            "is_insufficient": self.is_insufficient,
            "added_by_git_why": self.added_by_git_why,
        }


@dataclass
class Removed:
    """A model sentence that did not pass the checks, with the reason it was removed."""

    text: str
    reason: str  # "no citation" | "unknown citation [X]" | "no verbatim quote" | "quote not found in [X]"

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "reason": self.reason}


@dataclass
class Fallback:
    """Shown when no factual sentence survived validation: the single most relevant
    comment, quoted exactly, so the user still gets the best evidence with a link."""

    item: Item
    quote: str  # the single most relevant sentence, exactly as written
    full: str = ""  # the whole comment text, for JSON / auditing

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.item.id,
            "label": self.item.label,
            "url": self.item.url,
            "quote": self.quote,
            "full": self.full or self.quote,
        }


NO_DECISION = "The evidence does not explain the final decision."
NO_EXPLANATION = "The evidence shows the code, but the model produced no verified explanation of it."


def closing_line(mode: str) -> str:
    return NO_EXPLANATION if mode == MODE_CODE else NO_DECISION


@dataclass
class Answer:
    question: str
    # answered | insufficient_evidence | unavailable | no_model_selected
    status: str
    pack: EvidencePack
    mode: str = MODE_HISTORY  # history | code (see classify_question)
    model: str | None = None
    host: str = DEFAULT_HOST
    sentences: list[Sentence] = field(default_factory=list)
    removed: list[Removed] = field(default_factory=list)  # sentences that failed the checks (count always shown)
    fallback: Fallback | None = None  # best exact quote when nothing factual survived
    message: str = ""  # one-line explanation for non-answered states
    setup: list[str] = field(default_factory=list)  # short, explicit next steps
    note: str = ""  # e.g. "using the only installed model"
    raw: str = ""  # untouched model output (JSON only; useful for debugging)

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.sentences)

    @property
    def sources(self) -> list[Item]:
        cited: list[str] = []
        for s in self.sentences:
            for c in s.citations:
                if c not in cited:
                    cited.append(c)
        return [item for c in cited if (item := self.pack.get(c)) is not None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "mode": self.mode,
            "status": self.status,
            "model": self.model,
            "host": self.host,
            "text": self.text or None,
            "sentences": [s.to_dict() for s in self.sentences],
            "sources": [i.to_dict() for i in self.sources],
            "removed": [r.to_dict() for r in self.removed],
            "fallback": self.fallback.to_dict() if self.fallback else None,
            "message": self.message or None,
            "setup": list(self.setup),
            "note": self.note or None,
            "evidence": self.pack.to_dict(),
            "raw": self.raw or None,
        }


# ---------------------------------------------------------------------------
# Ollama client (local only)
# ---------------------------------------------------------------------------


def urllib_transport(url: str, payload: dict[str, Any] | None) -> tuple[int, str]:
    data = json.dumps(payload).encode() if payload is not None else None
    if not url.startswith(("http://", "https://")):
        raise OllamaUnreachableError(f"unsupported URL scheme in {url!r}")
    req = urllib.request.Request(  # noqa: S310 - scheme checked above; host restricted to loopback in resolve_host
        url, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise OllamaUnreachableError(str(getattr(exc, "reason", exc))) from exc


def resolve_host(raw: str | None = None, *, allow_remote: bool | None = None) -> tuple[str, str | None]:
    """
    Normalise OLLAMA_HOST and enforce loopback. Returns (host_url, error).
    Ollama accepts forms like ``0.0.0.0:11434`` or ``http://localhost:11434``.
    """
    raw = (raw if raw is not None else os.environ.get("OLLAMA_HOST", "")).strip() or DEFAULT_HOST
    if "://" not in raw:
        raw = "http://" + raw
    parts = urllib.parse.urlsplit(raw)
    hostname = (parts.hostname or "").lower()
    port = parts.port or 11434
    url = f"{parts.scheme}://{hostname}:{port}"
    if hostname in LOOPBACK_HOSTS:
        return url, None
    if allow_remote is None:
        allow_remote = os.environ.get("GIT_WHY_ALLOW_REMOTE_MODEL") == "1"
    if allow_remote:
        return url, None
    return url, (
        f"OLLAMA_HOST points at {hostname}, which is not this machine; git-why only sends history to a local "
        "model unless GIT_WHY_ALLOW_REMOTE_MODEL=1 is set"
    )


class OllamaClient:
    def __init__(self, host: str = DEFAULT_HOST, *, transport: Transport | None = None) -> None:
        self.host = host.rstrip("/")
        self.transport = transport or urllib_transport

    def list_models(self) -> list[str]:
        status, body = self.transport(f"{self.host}/api/tags", None)
        if status != 200:
            raise OllamaUnreachableError(f"GET /api/tags returned HTTP {status}")
        try:
            data = json.loads(body)
            return [str(m["name"]) for m in data.get("models", []) if "name" in m]
        except (ValueError, TypeError, AttributeError) as exc:
            raise OllamaUnreachableError("unexpected response from /api/tags") from exc

    def chat(self, model: str, system: str, user: str) -> str:
        payload = {
            "model": model,
            "stream": False,
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        status, body = self.transport(f"{self.host}/api/chat", payload)
        if status != 200:
            detail = ""
            try:
                detail = str(json.loads(body).get("error", ""))
            except (ValueError, AttributeError):
                detail = body[:200]
            raise OllamaUnreachableError(f"model request failed (HTTP {status}): {detail}".strip())
        try:
            content = json.loads(body)["message"]["content"]
        except (ValueError, KeyError, TypeError) as exc:
            raise OllamaUnreachableError("unexpected response from /api/chat") from exc
        return str(content)


# ---------------------------------------------------------------------------
# Model selection — never guess
# ---------------------------------------------------------------------------


def _installed_match(requested: str, installed: list[str]) -> str | None:
    """``llama3.2`` matches ``llama3.2:latest``; exact tags match exactly."""
    if requested in installed:
        return requested
    if ":" not in requested and f"{requested}:latest" in installed:
        return f"{requested}:latest"
    return None


def setup_steps(target: str, question: str, installed: list[str] | None = None) -> list[str]:
    q = question.replace('"', "'")
    steps = []
    if installed is None:
        steps += [
            "1. Install Ollama (local, free): https://ollama.com/download - then start it.",
            "2. Download a model, e.g.  ollama pull llama3.2   or   ollama pull qwen2.5:7b",
            f'3. Run:  git why {target} --ask "{q}" --model llama3.2',
        ]
    elif not installed:
        steps += [
            "1. Download a model, e.g.  ollama pull llama3.2   or   ollama pull qwen2.5:7b",
            f'2. Run:  git why {target} --ask "{q}" --model llama3.2',
        ]
    else:
        steps += [f'Pick one with --model, e.g.  git why {target} --ask "{q}" --model {installed[0]}']
    steps.append(
        "To choose a default for your machine: run the same question once per model and compare how many "
        "sentences survive with valid citations (shown below each answer); then set GIT_WHY_MODEL=<name>."
    )
    return steps


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_CITE = re.compile(r"\[([A-Z]\d+(?:\s*,\s*[A-Z]\d+)*)\]")
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])|\n+")
# double quotes, curly double quotes, and curly single-quote PAIRS (a plain apostrophe is not a quote mark)
_QUOTE = re.compile(
    r"`(\S(?:[^`]*?\S)?)`"  # code spans: `like this` (no leading/trailing blank, so a stray pair is not a span)
    r"|[\"\u201c\u201d]([^\"\u201c\u201d]{3,}?)[\"\u201c\u201d]"
    r"|\u2018([^\u2018\u2019]{3,}?)\u2019"
)
_NORM = re.compile(r"[^a-z0-9 ]+")
MIN_QUOTE_WORDS = 3


def split_sentences(text: str) -> list[str]:
    text = " ".join(text.strip().split("\n\n"))
    # keep sentence breaks out of quoted spans: temporarily protect ". " inside quotes
    protected = re.sub(_QUOTE, lambda m: m.group(0).replace(". ", ".\x00"), text)
    parts = [p.replace("\x00", " ") for p in _SPLIT.split(protected)]
    return [p.strip() for p in parts if p and p.strip()]


def normalise(text: str) -> str:
    """Lower-case, drop punctuation/markup, collapse whitespace - the same on both sides of a quote check."""
    text = text.lower().replace("`", " ").replace("\u2019", "'").replace("'", "")
    return " ".join(_NORM.sub(" ", text).split())


def quote_spans(sentence: str) -> list[str]:
    """Every quoted span, longest first when nested (a backtick span may contain double quotes)."""
    spans: list[str] = []
    for m in _QUOTE.finditer(sentence):
        spans.append(next(g for g in m.groups() if g).strip())
    return spans


def quotes_in(sentence: str, sources: list[Item] | None = None) -> list[str]:
    """
    Quotes that count: at least ``MIN_QUOTE_WORDS`` words (after normalisation, so
    ``sys.exit(code)`` is three), or a whole line of a cited code item.
    """
    code_lines = {normalise(line) for item in sources or [] if item.kind == "code" for line in item.quote.splitlines()}
    code_lines.discard("")
    out = []
    for q in quote_spans(sentence):
        n = normalise(q)
        if len(n.split()) >= MIN_QUOTE_WORDS or n in code_lines:
            out.append(q)
    return out


# --- reason claims -----------------------------------------------------------
# The model may say *why* only if the quoted source states a reason itself. A
# warning ("will make it a magic number"), question, or suggestion is not one.
_CLAIMS_REASON = re.compile(
    r"\bthe reason\b|\breasons? (for|why|is|was|being)\b|\bbecause\b|\bin order to\b|\bso that\b|\bto avoid\b"
    r"|\bto prevent\b|\bdue to\b|\b(this|that) is why\b|\bexplains? why\b|\bwhy\b[^.]{0,60}\bis that\b"
    r"|\bmotivat|\bintended to\b|\bpurpose\b|\bwas (chosen|picked|selected) to\b"
    r"|\b(chosen|set|changed|added|introduced|reverted|kept|used|done|made)\b[^.\"]{0,40}"
    r"\b(to avoid|to prevent|to fix|to make|to keep|to ensure|to be consistent|in order to|so that)\b",
    re.I,
)
_REASON_IN_SOURCE = re.compile(
    r"\bbecause\b|\bsince\b|\bso that\b|\bin order to\b|\bto avoid\b|\bto prevent\b|\bto fix\b|\bfix(es|ed)?\b"
    r"|\breason\b|\bdue to\b|\botherwise\b|\bbroke\b|\bbreaks\b|\bbroken\b|\bneeded\b|\bneeds\b|\brequired\b"
    r"|\bconsistent with\b|\bregress(ed|ion)\b|\bworkaround\b|\bbug\b",
    re.I,
)
_HEDGE_IN_SOURCE = re.compile(
    r"\bwould\b|\bwill make\b|\bmight\b|\bcould\b|\bperhaps\b|\bmaybe\b|\bshould we\b|\bwhat if\b|\bI think\b"
    r"|\bI wonder\b|\bnot sure\b|\bconsider\b|\bsuggest\b|\?",
    re.I,
)
REASON_NOT_IN_SOURCE = "states a reason, but the quoted source only raises a concern, question or suggestion"
REASON_CONTRADICTED = 'states a reason while the answer also says "Not in the evidence" - the cautious sentence is kept'


# Source code can show what a line does, never when, why or by whom it was written.
_CLAIMS_HISTORY = re.compile(
    # any past tense ("was already present", "has been there", "got added", "used to be")
    r"\b(was|were|had|has been|have been|had been|got|used to)\b"
    # simple past of change verbs ("someone added", "it existed", "the value remained")
    r"|\b(added|introduced|changed|removed|reverted|renamed|moved|created|modified|updated|fixed|merged|replaced|"
    r"tuned|rewrote|rewritten|existed|remained|appeared|landed|became|started|began|chose|decided)\b"
    # time vocabulary that only history can support
    r"|\balready\b|\boriginally\b|\bpreviously\b|\binitially\b|\bformerly\b|\bearlier\b|\blater\b"
    r"|\bsince\b|\bago\b|\balways\b|\bnever\b|\bstill\b|\bunchanged\b|\bremains\b|\bin (19|20)\d\d\b"
    # sources the code cannot be
    r"|\bcommit(s|ted)?\b|\bpull request|\bPRs?\b|\breview(er|ers|ed)?\b|\bauthor(s|ed)?\b|\bhistor(y|ical)"
    r"|\bissue #?\d+|\bbug #?\d+|\bwritten by\b|\bwrote\b",
    re.I,
)
HISTORY_FROM_CODE = "historical claim cited to source code, not to a commit or discussion"


def claims_history(sentence: str) -> bool:
    """Does the model's own wording (outside the quotes) say when/why/by whom the code was written?"""
    outside = _QUOTE.sub(" ", sentence)
    return bool(_CLAIMS_HISTORY.search(outside))


def claims_reason(sentence: str) -> bool:
    """Does the model's own wording (outside the quotes) present something as the reason?"""
    outside = _QUOTE.sub(" ", sentence)
    return bool(_CLAIMS_REASON.search(outside))


def quotes_state_reason(quotes: list[str]) -> bool:
    """Every quote must itself contain reason language and no hedging/concern language."""
    return all(_REASON_IN_SOURCE.search(q) and not _HEDGE_IN_SOURCE.search(q) for q in quotes)


def quote_supported(quote: str, items: list[Item]) -> bool:
    needle = normalise(quote)
    return bool(needle) and any(needle in normalise(item.text) for item in items)


def validate(raw: str, pack: EvidencePack) -> tuple[list[Sentence], list[Removed]]:
    """
    Keep a sentence only if it is a "Not in the evidence:" statement, or it cites
    known IDs *and* every quote it contains occurs in one of the cited sources
    *and*, if it presents something as the reason, the quoted text itself states
    a reason rather than a concern, question or suggestion. A reason claim is
    also removed when the answer contains "Not in the evidence" (they contradict
    each other; the cautious sentence wins). Everything removed is returned with
    its reason. Nothing is silently dropped.
    """
    kept: list[Sentence] = []
    removed: list[Removed] = []
    for sentence in split_sentences(raw):
        ids: list[str] = []
        for m in _CITE.finditer(sentence):
            ids += [p.strip() for p in m.group(1).split(",")]
        ids = list(dict.fromkeys(ids))
        clean = _CITE.sub("", sentence).strip()
        clean = re.sub(r"\s+([.,;:])", r"\1", clean)
        if not clean:
            continue
        if clean.lower().startswith(INSUFFICIENT_PREFIX):
            kept.append(Sentence(clean, [], is_insufficient=True))
            continue
        if not ids:
            removed.append(Removed(sentence.strip(), "no citation"))
            continue
        unknown = [i for i in ids if i not in pack.ids]
        if unknown:
            removed.append(Removed(sentence.strip(), f"unknown citation [{', '.join(unknown)}]"))
            continue
        sources = [item for i in ids if (item := pack.get(i)) is not None]
        quotes = quotes_in(clean, sources)
        if not quotes:
            removed.append(Removed(sentence.strip(), "no verbatim quote from the cited source"))
            continue
        bad = [q for q in quotes if not quote_supported(q, sources)]
        if bad:
            removed.append(Removed(sentence.strip(), f'quote "{bad[0]}" not found in [{", ".join(ids)}]'))
            continue
        if all(item.kind == "code" for item in sources) and claims_history(clean):
            removed.append(Removed(sentence.strip(), HISTORY_FROM_CODE))
            continue
        is_reason = claims_reason(clean)
        if is_reason and not quotes_state_reason(quotes):
            removed.append(Removed(sentence.strip(), REASON_NOT_IN_SOURCE))
            continue
        kept.append(Sentence(clean, ids, claims_reason=is_reason))

    # A reason claim and "Not in the evidence" cannot both stand for the same question.
    if any(s.is_insufficient for s in kept) and any(s.claims_reason for s in kept):
        for s in [s for s in kept if s.claims_reason]:
            kept.remove(s)
            removed.append(Removed(s.text, REASON_CONTRADICTED))
    return kept, removed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def ask(
    report: Report,
    question: str,
    *,
    model: str | None = None,
    client: OllamaClient | None = None,
    client_factory: Callable[[str], OllamaClient] | None = None,
    host: str | None = None,
    budget: int = DEFAULT_BUDGET,
) -> Answer:
    mode = classify_question(question)
    pack = build(report, budget=budget, mode=mode)
    target = report.target.label
    host_url, host_error = resolve_host(host)
    answer = Answer(question=question, status="unavailable", pack=pack, mode=mode, host=host_url)

    if host_error:
        answer.message = host_error
        answer.setup = ["Unset OLLAMA_HOST (or point it at localhost) to use a model running on this machine."]
        return answer

    client = client or (client_factory or OllamaClient)(host_url)
    try:
        installed = client.list_models()
    except OllamaUnreachableError as exc:
        answer.message = f"Ollama is not running at {host_url} ({exc})"
        answer.setup = setup_steps(target, question, None)
        return answer

    requested = model or os.environ.get("GIT_WHY_MODEL", "").strip() or None
    if requested is None:
        if not installed:
            answer.message = f"Ollama is running at {host_url} but no model is installed"
            answer.setup = setup_steps(target, question, [])
            return answer
        if len(installed) > 1:
            answer.status = "no_model_selected"
            answer.message = "several models are installed and none was selected: " + ", ".join(installed)
            answer.setup = setup_steps(target, question, installed)
            return answer
        requested = installed[0]
        answer.note = f"using {requested}, the only installed model"

    chosen = _installed_match(requested, installed)
    if chosen is None:
        answer.message = f"model {requested!r} is not installed" + (
            f" (installed: {', '.join(installed)})" if installed else ""
        )
        answer.setup = [f"ollama pull {requested}", *setup_steps(target, question, installed)[-1:]]
        return answer
    answer.model = chosen

    try:
        raw = client.chat(
            chosen,
            SYSTEM_PROMPT_CODE if mode == MODE_CODE else SYSTEM_PROMPT,
            (USER_TEMPLATE_CODE if mode == MODE_CODE else USER_TEMPLATE).format(
                question=question, evidence=format_for_prompt(pack)
            ),
        )
    except OllamaUnreachableError as exc:
        answer.message = f"the model request failed: {exc}"
        answer.setup = ["Check `ollama ps` / `ollama serve` logs and try again; the evidence above is unaffected."]
        return answer

    answer.raw = raw
    answer.sentences, answer.removed = validate(raw, pack)
    answer.status = "answered" if has_facts(answer.sentences, pack, mode) else "insufficient_evidence"
    if answer.status != "answered":
        if mode == MODE_HISTORY and not any(s.is_insufficient for s in answer.sentences):
            # the model neither grounded a historical claim nor admitted the gap: say it ourselves
            answer.sentences.append(Sentence(NOT_IN_HISTORY, [], is_insufficient=True, added_by_git_why=True))
        answer.fallback = most_relevant_comment(pack, question, answer.removed, mode=mode)
        if not answer.sentences and not answer.fallback:
            answer.message = (
                "the model produced no sentence that quotes and cites the evidence; the evidence may not answer this"
            )
    return answer


_STOPWORDS = frozenset(
    {"the", "a", "an", "is", "are", "was", "were", "be", "been", "to", "of", "in", "on", "at", "for", "and", "or"}
    | {"not", "this", "that", "it", "its", "why", "what", "how", "does", "did", "do", "set", "with", "from", "by"}
    | {"as", "into", "than", "then", "there", "here", "have", "has", "had"}
)


def _tokens(text: str) -> set[str]:
    return {t for t in _NORM.sub(" ", text.lower().replace("`", "")).split() if len(t) > 1 and t not in _STOPWORDS}


NOT_IN_HISTORY = "Not in the evidence: the commits and discussions gathered do not say why this line was added."


def has_facts(sentences: list[Sentence], pack: EvidencePack, mode: str) -> bool:
    """
    Did the answer establish anything? For code questions any cited sentence counts.
    For history questions only a citation to history evidence (C/P/I/R) counts: the
    current source [S1] can show what the line says, never when or why it got there.
    """
    for s in sentences:
        kinds = {item.kind for c in s.citations if (item := pack.get(c)) is not None}
        if kinds and (mode == MODE_CODE or kinds - {"code"}):
            return True
    return False


def most_relevant_comment(
    pack: EvidencePack, question: str, removed: list[Removed], *, mode: str = MODE_HISTORY
) -> Fallback | None:
    """
    Pick the evidence to show when nothing factual survived: for history questions the
    comment (else PR/issue description) that best matches the question and whatever the
    model tried to quote; for code questions the code itself. Deterministic: score, then pack order.
    """
    if mode == MODE_CODE:
        candidates = [i for i in pack.items if i.kind == "code"]
    else:
        candidates = [i for i in pack.items if i.kind == "comment"] or [
            i for i in pack.items if i.kind in ("pr", "issue")
        ]
    if not candidates:
        return None
    wanted = _tokens(question)
    pointed_at: set[str] = set()
    for r in removed:
        wanted |= _tokens(" ".join(quote_spans(r.text)))
        pointed_at |= {c for group in _CITE.findall(r.text) for c in re.split(r"\s*,\s*", group)}

    def score(item: Item) -> tuple[int, int]:
        return (len(wanted & _tokens(item.text)), 1 if item.id in pointed_at else 0)

    best = max(candidates, key=score)  # max() keeps the first of equal scores
    full = best.quote or best.text
    return Fallback(best, most_relevant_sentence(full, wanted), full)


def most_relevant_sentence(text: str, wanted: set[str]) -> str:
    """The sentence of ``text`` sharing the most words with ``wanted`` (first wins ties); whole text if unsplittable."""
    sentences = [ln.strip() for ln in text.splitlines() if ln.strip()] if "\n" in text else split_sentences(text)
    if len(sentences) <= 1:
        return text.strip()
    return max(sentences, key=lambda sent: len(wanted & _tokens(sent)))
