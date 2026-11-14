"""
``--ask``: answer a question about a line **only** from the evidence git-why gathered.

Design rules (see README "Asking questions"):

* The model sees the numbered evidence pack (:mod:`git_why.evidence`) and nothing else.
* Every sentence that states a fact must cite ``[C1]``-style IDs from the pack.
  Sentences without a valid citation are **removed and reported**, never hidden.
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

from .evidence import DEFAULT_BUDGET, EvidencePack, Item, build, format_for_prompt
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
    "1. Every sentence that states a fact must end with the IDs of the evidence that supports it, "
    "in square brackets, e.g. [C2] or [C2][P1].\n"
    "2. Use only IDs that appear in the evidence. Never cite anything else.\n"
    "3. If the evidence does not contain the answer, or only part of it, write one sentence that starts with "
    '"Not in the evidence:" describing what is missing. That sentence needs no citation.\n'
    "4. Do not guess motives, performance reasons, or intentions that are not written in the evidence. "
    "Quoting a commit message or comment is good; paraphrasing it closely is fine.\n"
    f"5. Be brief: at most {MAX_WORDS} words, plain prose, no headings or bullet points.\n"
)

USER_TEMPLATE = """Question: {question}

Evidence:
{evidence}

Answer (cite IDs after each factual sentence; say "Not in the evidence:" for anything the evidence does not show):"""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class Sentence:
    text: str
    citations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "citations": list(self.citations)}


@dataclass
class Answer:
    question: str
    # answered | insufficient_evidence | unavailable | no_model_selected
    status: str
    pack: EvidencePack
    model: str | None = None
    host: str = DEFAULT_HOST
    sentences: list[Sentence] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # sentences dropped for lacking valid citations
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
            "status": self.status,
            "model": self.model,
            "host": self.host,
            "text": self.text or None,
            "sentences": [s.to_dict() for s in self.sentences],
            "sources": [i.to_dict() for i in self.sources],
            "removed_uncited": list(self.removed),
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


def split_sentences(text: str) -> list[str]:
    text = " ".join(text.strip().split("\n\n"))
    return [s.strip() for s in _SPLIT.split(text) if s and s.strip()]


def validate(raw: str, pack: EvidencePack) -> tuple[list[Sentence], list[str]]:
    """Keep sentences whose citations all exist; report the rest. Nothing is silently dropped."""
    kept: list[Sentence] = []
    removed: list[str] = []
    for sentence in split_sentences(raw):
        ids: list[str] = []
        for m in _CITE.finditer(sentence):
            ids += [p.strip() for p in m.group(1).split(",")]
        clean = _CITE.sub("", sentence).strip()
        clean = re.sub(r"\s+([.,;:])", r"\1", clean)
        if not clean:
            continue
        if clean.lower().startswith(INSUFFICIENT_PREFIX):
            kept.append(Sentence(clean, []))
            continue
        if ids and all(i in pack.ids for i in ids):
            kept.append(Sentence(clean, list(dict.fromkeys(ids))))
        else:
            removed.append(sentence.strip())
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
    pack = build(report, budget=budget)
    target = report.target.label
    host_url, host_error = resolve_host(host)
    answer = Answer(question=question, status="unavailable", pack=pack, host=host_url)

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
            chosen, SYSTEM_PROMPT, USER_TEMPLATE.format(question=question, evidence=format_for_prompt(pack))
        )
    except OllamaUnreachableError as exc:
        answer.message = f"the model request failed: {exc}"
        answer.setup = ["Check `ollama ps` / `ollama serve` logs and try again; the evidence above is unaffected."]
        return answer

    answer.raw = raw
    answer.sentences, answer.removed = validate(raw, pack)
    has_fact = any(s.citations for s in answer.sentences)
    answer.status = "answered" if has_fact else "insufficient_evidence"
    if not answer.sentences:
        answer.message = "the model produced no sentence with a valid citation; the evidence may not answer this"
    return answer
