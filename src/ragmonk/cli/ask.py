"""``ragmonk ask "QUESTION" [--json]`` -- Phase 9's one AI-optional
command: runs the exact same deterministic retrieval ``ragmonk explore``
uses (``retrieval/planner.py`` + ``cli/explore.py``'s own ``_run``, not a
second retrieval implementation) to assemble an evidence package, then
hands the question and that evidence to the configured ``ai:`` provider
for a synthesized answer -- returned alongside the evidence it was based
on, so the answer stays auditable against real, already-indexed sources
(the blueprint's evidence-first principle) rather than trusted blind.

Never required by, and never changes the behavior of, ``explore``/
``search``/``impact``/etc.: this is a new, separate command layered on
top of the same evidence, not a replacement for any of them.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from ragmonk.ai import factory as ai_factory
from ragmonk.ai.base import AiProvider, AiProviderError, AiRequest
from ragmonk.cli import explore as explore_cli
from ragmonk.core.errors import RagMonkError
from ragmonk.core.lifecycle import AppContext
from ragmonk.retrieval import planner

from ._common import cli_command, console, print_json


def _build_provider(ctx: AppContext) -> AiProvider:
    """Its own function (rather than inlined into ``_run``) purely as a
    test seam: integration tests monkeypatch this one call to inject a
    fake provider without needing a real API key or a real network call,
    while still exercising the rest of ``_run`` (real retrieval, real
    evidence assembly) end-to-end.
    """
    return ai_factory.create_provider(ai=ctx.config.ai, privacy=ctx.config.privacy)


def _run(ctx: AppContext, question: str, *, provider: AiProvider | None = None) -> dict[str, Any]:
    question = question.strip()
    query_plan = planner.plan(question, semantic_enabled=ctx.config.search.semantic)
    exploration = explore_cli._run(ctx, query_plan)

    resolved_provider = provider if provider is not None else _build_provider(ctx)

    request = AiRequest(
        question=question,
        summary=exploration["summary"],
        evidence=exploration["evidence"],
        graph_paths=exploration["call_flows"],
    )
    try:
        answer = resolved_provider.answer(request)
    except RagMonkError:
        raise
    except Exception as exc:  # noqa: BLE001 - any provider-library-specific failure -> one type
        raise AiProviderError(f"ai provider call failed: {exc}") from exc

    return {
        "question": question,
        "answer": answer.to_dict(),
        "intent": exploration["intent"],
        "strategies": exploration["strategies"],
        "evidence": exploration["evidence"],
        "graph_paths": exploration["call_flows"],
        "evidence_truncated": exploration["evidence_truncated"],
        "evidence_truncation_reasons": exploration["evidence_truncation_reasons"],
    }


def _render(result: dict[str, Any]) -> None:
    console.print(result["answer"]["text"])
    console.print(
        f"\n[dim]provider={result['answer']['provider']} model={result['answer']['model']} "
        f"evidence={len(result['evidence'])} item(s)"
        f"{' (truncated)' if result['evidence_truncated'] else ''}[/dim]"
    )


@cli_command
def ask(
    question: Annotated[str, typer.Argument(help="Natural-language question to ask.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with AppContext.bootstrap() as ctx:
        result = _run(ctx, question)
        if json_output:
            print_json(result)
            return
        _render(result)
