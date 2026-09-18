"""Exact Tokenizer plan, Phase 5: ``doctor``/``status``/``search --explain``
surface the pinned tokenizer identity, and ``doctor`` proves the active
index's embedding payloads never exceed the model limit (truncation 0)
across a mixed English/Greek/code/table corpus.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.tokenization import model_identity

_MIXED_DOC = """# Retrieval Manual

## Overview

The retrieval engine budgets every chunk against the exact tokenizer of the
embedding model so no payload is silently truncated at inference time.

## Ελληνικά

Η ανάκτηση πληροφορίας βασίζεται στη σημασιολογική ομοιότητα μεταξύ του
ερωτήματος και των εγγράφων που έχουν ευρετηριαστεί από το σύστημα.

## Code

```csharp
public async Task<IReadOnlyList<int>> GetCountsAsync(CancellationToken ct)
    => await _repository.Query(x => x.IsActive).Select(x => x.Id).ToListAsync(ct);
```

## Fleet

| Server | CPU | RAM | Status |
| --- | --- | --- | --- |
| api-01 | 40% | 8GB | healthy |
| api-02 | 85% | 16GB | degraded |
"""


@pytest.fixture
def indexed(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> CliRunner:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    # UTF-8 explicitly: the corpus contains Greek, and Windows' default
    # cp1252 encoding cannot encode it.
    (source_dir / "manual.md").write_text(_MIXED_DOC, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(source_dir)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0
    return runner


def _section(payload: dict, name: str) -> dict:
    return next(s for s in payload["sections"] if s["name"] == name)


def test_doctor_reports_tokenizer_identity_and_zero_truncation(indexed: CliRunner) -> None:
    result = indexed.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]

    tokenizer = _section(payload, "Tokenizer")
    checks = {c["name"]: c for c in tokenizer["checks"]}
    assert model_identity.EMBEDDING_MODEL_ID in checks["model"]["detail"]
    assert model_identity.TOKENIZER_REVISION[:12] in checks["revision"]["detail"]

    payloads = checks["payloads"]
    # A real index was built, and nothing was truncated.
    assert payloads["status"] == "ok"
    assert "truncated 0" in payloads["detail"]


def test_status_json_includes_tokenizer_block(indexed: CliRunner) -> None:
    payload = json.loads(indexed.invoke(app, ["status", "--json"]).output)["data"]
    tokenizer = payload["tokenizer"]
    assert tokenizer["model_id"] == model_identity.EMBEDDING_MODEL_ID
    assert tokenizer["revision"] == model_identity.TOKENIZER_REVISION
    assert tokenizer["fingerprint"] == model_identity.tokenizer_fingerprint()
    assert tokenizer["max_sequence_tokens"] == model_identity.MAX_SEQUENCE_TOKENS
    assert tokenizer["chunk_ceiling"] == model_identity.MAX_SEQUENCE_TOKENS


def test_search_explain_json_includes_tokenizer(indexed: CliRunner) -> None:
    result = indexed.invoke(app, ["search", "retrieval", "--json", "--explain"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    tokenizer = payload["explain"]["tokenizer"]
    assert tokenizer["model_id"] == model_identity.EMBEDDING_MODEL_ID
    assert tokenizer["revision"] == model_identity.TOKENIZER_REVISION
