"""Provider-agnostic interface for Phase 9's ``ragmonk ask``.

Deliberately minimal, per the blueprint's own framing: a provider is a
*consumer* of evidence Phase 5 already assembled (``retrieval/
context_builder.py`` via ``cli/explore.py``'s ``_run``), never a second
knowledge-model owner -- nothing here reads a database, resolves a
symbol, or ranks anything. Every concrete provider
(``ai/openai.py``/``anthropic.py``/``ollama.py``/``openai_compatible.py``)
implements exactly one method: turn an ``AiRequest`` into an ``AiAnswer``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ragmonk.core.errors import (
    EXIT_GENERIC_FAILURE,
    ConfigError,
    RagMonkError,
    SecurityViolationError,
)


class AiNotConfiguredError(ConfigError):
    """No usable provider: ``ai.provider`` is ``"none"``/unset, an
    unknown provider name, or a selected provider is missing something
    it needs to run (a model name, a required API key, a required
    ``base_url``). A configuration problem, not a runtime one, so it
    reuses ``ConfigError``'s exit code (3) rather than a new one.
    """


class AiPrivacyBlockedError(SecurityViolationError):
    """A provider that would send data to a network endpoint outside
    this machine was requested while ``privacy.external_ai_allowed`` is
    ``False`` (the default) -- see ``ai/factory.py`` for exactly which
    providers/hosts this applies to, including the Ollama exemption.
    """


class AiProviderError(RagMonkError):
    """The configured provider itself failed once actually called: a
    network error, a non-2xx response (bad API key, rate limit, model not
    found, ...), or a malformed response. A runtime failure rather than a
    configuration mistake -- it reuses the generic-failure exit code (1),
    the same one any other unclassified ``cli_command``-boundary
    exception already gets.
    """

    exit_code = EXIT_GENERIC_FAILURE


@dataclass(frozen=True)
class AiRequest:
    """What ``ragmonk ask`` hands a provider: the question plus the
    exact evidence package ``cli/explore.py``'s deterministic retrieval
    already assembled for it -- ``evidence``/``graph_paths`` are
    ``context_builder.EvidenceItem.to_dict()``/``GraphPath.to_dict()``
    shapes, not re-derived here.
    """

    question: str
    summary: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    graph_paths: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class AiUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class AiAnswer:
    text: str
    provider: str
    model: str
    usage: AiUsage = field(default_factory=AiUsage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
            },
        }


class AiProvider(Protocol):
    def answer(self, request: AiRequest) -> AiAnswer: ...


_SYSTEM_PROMPT = (
    "You are RagMonk's evidence-grounded assistant. Answer the user's "
    "question using ONLY the evidence and call-graph paths provided below "
    "-- they were retrieved deterministically from the user's own locally "
    "indexed code and documents, not written by you. If the evidence does "
    "not answer the question, say so plainly rather than guessing. Cite "
    "the relevant file paths from the evidence when you use them."
)


def build_prompt(request: AiRequest) -> str:
    """Renders one provider-agnostic evidence prompt so every provider
    grounds its answer in identical context -- the actual wording is not
    the interesting part of Phase 9, keeping it in one shared place is:
    a provider module has no way to accidentally omit or reorder the
    evidence it was given.
    """
    lines = [f"Question: {request.question}", "", f"Retrieval summary: {request.summary}", ""]
    if request.evidence:
        lines.append("Evidence:")
        for item in request.evidence:
            location = item.get("location") or {}
            where = ", ".join(f"{k}={v}" for k, v in location.items() if v is not None)
            lines.append(
                f"- [{item.get('confidence')}] {item.get('entity')} ({item.get('path')}"
                f"{', ' + where if where else ''}): {item.get('snippet', '')}"
            )
        lines.append("")
    if request.graph_paths:
        lines.append("Call graph:")
        for edge in request.graph_paths:
            lines.append(
                f"- {edge.get('source')} -[{edge.get('relationship')}]-> {edge.get('target')}"
            )
        lines.append("")
    if not request.evidence and not request.graph_paths:
        lines.append("No evidence was retrieved for this question.")
    return "\n".join(lines)


def build_messages(request: AiRequest) -> list[dict[str, str]]:
    """The same prompt as ``build_prompt``, shaped as a chat-style
    ``[{"role", "content"}, ...]`` list -- what every provider here
    (OpenAI/Anthropic/Ollama/OpenAI-compatible chat endpoints) actually
    accepts.
    """
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(request)},
    ]
