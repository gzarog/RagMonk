"""``ai/base.py``'s shared prompt-building -- the one piece of logic every
provider reuses, so it is worth proving once here rather than duplicated
per-provider.
"""

from __future__ import annotations

from ragmonk.ai.base import AiRequest, build_messages, build_prompt


def _request(**overrides: object) -> AiRequest:
    defaults: dict[str, object] = {
        "question": "What does AnimalService do?",
        "summary": "Found 1 symbol(s) and 0 document(s).",
        "evidence": [
            {
                "source": "lexical:entity",
                "path": "services/animal_service.py",
                "location": {"line_start": 1, "line_end": 3, "page": None, "section": None},
                "entity": "AnimalService.bark_loudly",
                "relationship": "match",
                "confidence": "exact",
                "snippet": "def bark_loudly(self): ...",
            }
        ],
        "graph_paths": [
            {
                "source": "DogConsumer.handle",
                "relationship": "calls",
                "target": "AnimalService.bark_loudly",
                "confidence": "high",
            }
        ],
    }
    defaults.update(overrides)
    return AiRequest(**defaults)  # type: ignore[arg-type]


def test_build_prompt_includes_question_evidence_and_graph_paths() -> None:
    prompt = build_prompt(_request())
    assert "What does AnimalService do?" in prompt
    assert "AnimalService.bark_loudly" in prompt
    assert "services/animal_service.py" in prompt
    assert "DogConsumer.handle" in prompt
    assert "calls" in prompt


def test_build_prompt_says_so_when_no_evidence_was_retrieved() -> None:
    prompt = build_prompt(_request(evidence=[], graph_paths=[]))
    assert "No evidence was retrieved" in prompt


def test_build_messages_has_a_system_and_a_user_message() -> None:
    messages = build_messages(_request())
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "evidence" in messages[0]["content"].lower()
    assert "What does AnimalService do?" in messages[1]["content"]
