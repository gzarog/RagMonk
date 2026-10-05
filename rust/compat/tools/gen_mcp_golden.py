"""Generate RUST-13 MCP stdio golden fixtures from the Python reference.

    python rust/compat/tools/gen_mcp_golden.py

Indexes the compat ``code-docs-index`` corpus (tests/fixtures/languages
plus the simple Markdown/TXT/CSV/HTML documents and the EML with
attachments), semantic search off, in a temporary RAGMONK_HOME, then
drives ``ragmonk serve --mcp`` (FastMCP) over stdio with the session in
``SESSION`` and records every response.

Tool results are recorded as their ``structuredContent`` (the ``content``
text is the same model as indented JSON; the generator asserts that).
The ``initialize`` server name's version is masked. ``ragmonk_ask`` runs
with no provider configured. Each payload is normalized by ``normalize``: corpus paths become
``<CORPUS>/...``, the home becomes ``<HOME>``, the source id becomes
``<SOURCE>``, opaque ids are dropped and volatile values (timestamps,
pids, sizes) are masked. The Rust test applies the same rules. Writes
rust/compat/golden/mcp.json and the tool catalog
rust/crates/ragmonk-cli/src/mcp_tools.json.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
OUT = ROOT / "golden" / "mcp.json"
# The tool catalog the Rust server embeds verbatim (``tools/list``).
TOOLS_OUT = ROOT.parent / "crates" / "ragmonk-cli" / "src" / "mcp_tools.json"
FIXTURES = [
    ("tests/fixtures/languages", "code"),
    ("tests/fixtures/documents/simple.md", "docs/simple.md"),
    ("tests/fixtures/documents/simple.txt", "docs/simple.txt"),
    ("tests/fixtures/documents/simple.csv", "docs/simple.csv"),
    ("tests/fixtures/documents/simple.html", "docs/simple.html"),
    ("tests/fixtures/documents/email_with_attachments.eml", "docs/email_with_attachments.eml"),
]
INIT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "compat", "version": "1"},
}


def call(name: str, arguments: dict | None = None) -> dict:
    return {"method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}


# (request, unordered array keys). A request without "id" is a notification.
SESSION: list[tuple[dict, list[str]]] = [
    ({"method": "initialize", "params": INIT}, []),
    ({"method": "notifications/initialized"}, []),
    ({"method": "ping"}, []),
    ({"method": "tools/list", "params": {}}, []),
    ({"method": "prompts/list", "params": {}}, []),
    ({"method": "resources/list", "params": {}}, []),
    ({"method": "resources/templates/list", "params": {}}, []),
    ({"method": "no/such/method", "params": {}}, []),
    (call("nope"), []),
    (call("ragmonk_symbol"), []),
    (call("ragmonk_symbol", {"name": 5}), []),
    (call("ragmonk_symbol", {"name": "   "}), []),
    (call("ragmonk_search", {"query": ""}), []),
    (call("ragmonk_symbol", {"name": "Dog"}), []),
    (call("ragmonk_symbol", {"name": "no_such_symbol_anywhere"}), []),
    (call("ragmonk_search", {"query": "bark"}), ["results"]),
    (call("ragmonk_search", {"query": "bark", "limit": 50}), ["results"]),
    (call("ragmonk_callers", {"name": "speak"}), ["edges"]),
    (call("ragmonk_callees", {"name": "bark", "max_depth": 2, "limit": 50}), ["edges"]),
    (call("ragmonk_impact", {"name": "speak"}), []),
    (call("ragmonk_impact", {"name": "no_such_symbol_anywhere"}), []),
    (call("ragmonk_explore", {"query": "Dog"}), []),
    (call("ragmonk_explore", {"query": "Dog", "max_chars": 200, "max_files": 1}), []),
    (call("ragmonk_explore", {"query": " "}), []),
    (call("ragmonk_documents"), []),
    (call("ragmonk_documents", {"source_id": "src_nope"}), []),
    (call("ragmonk_status"), []),
    (call("ragmonk_ask", {"question": "  "}), []),
    (call("ragmonk_ask", {"question": "Dog"}), []),
]

DROP = {
    "entity_id",
    "source_entity_id",
    "target_entity_id",
    "file_id",
    "id",
    "parent_document_id",
}
VOLATILE = {"pid", "hostname", "python", "running_for_seconds", "database_size_bytes"}
VOLATILE_SUFFIXES = ("_at", "_age_seconds", "_ms", "duration_seconds", "elapsed_seconds")
TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


def normalize(value, subs: list[tuple[str, str]], unordered: list[str]):
    """Mirrors ``normalize`` in rust/crates/ragmonk-cli/tests/mcp.rs."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in DROP:
                continue
            if v is not None and (k in VOLATILE or k.endswith(VOLATILE_SUFFIXES)):
                out[k] = "<VOLATILE>"
                continue
            v = normalize(v, subs, unordered)
            if k in unordered and isinstance(v, list):
                v = sorted(
                    v,
                    key=lambda x: json.dumps(
                        x, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                    ),
                )
            out[k] = v
        return out
    if isinstance(value, list):
        return [normalize(v, subs, unordered) for v in value]
    if isinstance(value, str):
        for needle, repl in subs:
            value = value.replace(needle, repl)
        value = value.replace("\\", "/")
        return TS.sub("<TS>", value)
    return value


def main() -> None:
    exe = Path(sys.executable).with_name("ragmonk")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        corpus = Path(tmp) / "source"
        for src, dst in FIXTURES:
            target = corpus / dst
            if (REPO / src).is_dir():
                shutil.copytree(REPO / src, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(REPO / src, target)
        env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("RAGMONK_")},
            "RAGMONK_HOME": str(home),
            "RAGMONK_SEARCH__SEMANTIC": "false",
            "RAGMONK_UPDATES__ENABLED": "false",
            "NO_COLOR": "1",
        }
        outputs = [
            subprocess.run(
                [str(exe), *args], env=env, check=True, capture_output=True, text=True
            ).stdout
            for args in (["init"], ["source", "add", str(corpus)], ["index"])
        ]
        source_id = re.search(r"(src_[0-9a-f]+)", outputs[1]).group(1)
        subs = sorted(
            [
                (str(corpus.resolve()), "<CORPUS>"),
                (str(corpus), "<CORPUS>"),
                (str(home.resolve()), "<HOME>"),
                (str(home), "<HOME>"),
                (source_id, "<SOURCE>"),
            ],
            key=lambda s: -len(s[0]),
        )

        proc = subprocess.Popen(
            [str(exe), "serve", "--mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
        )
        records = []
        next_id = 1
        for request, unordered in SESSION:
            message = {"jsonrpc": "2.0", **request}
            notification = request["method"].startswith("notifications/")
            if not notification:
                message["id"] = next_id
                next_id += 1
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            if notification:
                records.append({"request": message, "response": None})
                continue
            response = json.loads(proc.stdout.readline())
            result = response.get("result")
            if request["method"] == "initialize":
                # The version is the build's own.
                result["serverInfo"]["name"] = "ragmonk v<VERSION>"
            if request["method"] == "tools/list":
                TOOLS_OUT.write_text(
                    json.dumps(result["tools"], ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8",
                )
                records.append({"request": message, "unordered": [], "response": response})
                continue
            if isinstance(result, dict) and "structuredContent" in result:
                text = result["content"][0]["text"]
                assert json.loads(text) == result["structuredContent"], text
                result["content"] = [{"type": "text", "text": "<structuredContent>"}]
            records.append(
                {
                    "request": message,
                    "unordered": unordered,
                    "response": normalize(response, subs, unordered),
                }
            )
        proc.stdin.close()
        proc.wait(timeout=30)
    OUT.write_text(json.dumps(records, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT}: {len(records)} exchanges")


if __name__ == "__main__":
    main()
