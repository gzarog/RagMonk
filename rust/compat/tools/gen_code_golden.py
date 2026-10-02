"""Generate RUST-05 code-intelligence golden fixtures from the Python reference.

    python rust/compat/tools/gen_code_golden.py

Runs the reference parser/extractor/resolver/framework rules over every file
in rust/compat/fixtures/code (one source root) and records, per file, the
parse outcome, extracted entities and the relationships the reference
processor builds. Cross-file references are resolved against every other
file of the corpus (the whole-build semantics the V2 Rust indexer uses).
Writes rust/compat/golden/code.json.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from ragmonk.code import processor
from ragmonk.code.extractor import default_namespace_for_path, extract
from ragmonk.code.parser import detect_language, parse
from ragmonk.core.models import Entity

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "code"
OUT = Path(__file__).resolve().parents[1] / "golden" / "code.json"


def key(rel: str, local: int) -> str:
    return f"{rel}#{local}"


def main() -> None:
    files = sorted(p for p in ROOT.rglob("*") if p.is_file())
    parsed: dict[str, dict] = {}
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        language = detect_language(path)
        entry: dict = {"language": language}
        parsed[rel] = entry
        if language is None:
            continue
        source = path.read_bytes()
        if parse(source, language).root_node.has_error:
            entry["error"] = "parse_error"
            continue
        name, qualified = default_namespace_for_path(PurePosixPath(rel))
        extraction = extract(
            source,
            language,
            default_namespace_name=name,
            default_namespace_qualified_name=qualified,
        )
        entry["extraction"] = extraction
        entry["entities"] = [
            Entity(
                id=key(rel, e.local_id),
                source_id="s",
                file_id=rel,
                kind=e.kind,
                name=e.name,
                qualified_name=e.qualified_name,
                language=language,
                parent_id=None if e.parent_local_id is None else key(rel, e.parent_local_id),
                signature=e.signature,
                start_line=e.start_line,
                end_line=e.end_line,
                start_col=e.start_col,
                end_col=e.end_col,
                generation=1,
                created_at="t",
                updated_at="t",
            )
            for e in extraction.entities
        ]

    all_entities = [e for v in parsed.values() for e in v.get("entities", [])]
    out_files = {}
    for rel, entry in parsed.items():
        record: dict = {"language": entry["language"]}
        out_files[rel] = record
        if "error" in entry:
            record["error"] = entry["error"]
        if "entities" not in entry:
            continue
        entities = entry["entities"]
        extraction = entry["extraction"]
        ns_local = next(i for i, e in enumerate(extraction.entities) if e.kind.value == "namespace")

        def qualified_lookup(text: str, rel: str = rel) -> list[Entity]:
            return [e for e in all_entities if e.file_id != rel and e.qualified_name == text]

        def name_lookup(name: str, rel: str = rel) -> list[Entity]:
            return [e for e in all_entities if e.file_id != rel and e.name == name]

        ctx = SimpleNamespace(file_id=rel, next_generation=1, path=rel)
        rels = processor._build_relationships(
            ctx=ctx,
            extraction=extraction,
            entities=entities,
            namespace_local_id=ns_local,
            language=entry["language"],
            now="t",
            qualified_lookup=qualified_lookup,
            name_lookup=name_lookup,
        )
        record["entities"] = [
            {
                "key": e.id,
                "kind": e.kind.value,
                "name": e.name,
                "qualified_name": e.qualified_name,
                "parent": e.parent_id,
                "signature": e.signature,
                "range": [e.start_line, e.start_col, e.end_line, e.end_col],
            }
            for e in entities
        ]
        record["relationships"] = sorted(
            (
                {
                    "type": r.relationship_type.value,
                    "source": r.source_entity_id,
                    "target": r.target_entity_id,
                    "symbol": r.target_symbol,
                    "resolver": r.resolver,
                    "confidence": r.confidence.value,
                    "location": r.source_location,
                    "evidence": r.evidence,
                }
                for r in rels
            ),
            key=lambda d: json.dumps(d, sort_keys=True),
        )
    OUT.write_text(json.dumps({"files": out_files}, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
