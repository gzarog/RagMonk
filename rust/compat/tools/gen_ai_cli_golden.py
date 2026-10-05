"""Generate RUST-14 ``ask``/``ai`` CLI golden fixtures from the Python
reference.

    python rust/compat/tools/gen_ai_cli_golden.py

Indexes the compat ``code-docs-index`` corpus (semantic off) in a
temporary RAGMONK_HOME, points ``ai.provider=ollama`` at a local mock
server, and records each command's exit code, stdout, stderr and (for
``ask``) the chat request the provider sent. ``PATH`` holds no ``codex``
executable, so the subscription lifecycle commands report their runtime
as unavailable. Corpus paths become ``<CORPUS>``, the home ``<HOME>`` and
the mock's port ``<PORT>``. Writes rust/compat/golden/ai_cli.json.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
OUT = ROOT / "golden" / "ai_cli.json"
FIXTURES = [
    ("tests/fixtures/languages", "code"),
    ("tests/fixtures/documents/simple.md", "docs/simple.md"),
    ("tests/fixtures/documents/simple.txt", "docs/simple.txt"),
    ("tests/fixtures/documents/simple.csv", "docs/simple.csv"),
    ("tests/fixtures/documents/simple.html", "docs/simple.html"),
    ("tests/fixtures/documents/email_with_attachments.eml", "docs/email_with_attachments.eml"),
]
OLLAMA_OK = {
    "model": "llama3.2:latest",
    "message": {"role": "assistant", "content": "Dog.speak calls bark."},
    "done": True,
    "prompt_eval_count": 80,
    "eval_count": 6,
}
STATE: dict = {"response": (200, OLLAMA_OK), "sent": []}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        STATE["sent"].append({"path": self.path, "body": json.loads(raw)})
        status, body = STATE["response"]
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# (name, args, extra env, mock response or None)
CASES = [
    ("ask-json", ["ask", "who calls speak", "--json"], {}, (200, OLLAMA_OK)),
    ("ask-text", ["ask", "Dog"], {}, (200, OLLAMA_OK)),
    ("ask-no-evidence", ["ask", "zzqqxx_nothing", "--json"], {}, (200, OLLAMA_OK)),
    ("ask-http-500", ["ask", "Dog"], {}, (500, b"model not loaded")),
    ("ask-not-configured", ["ask", "Dog"], {"RAGMONK_AI__PROVIDER": "none"}, None),
    ("ask-privacy", ["ask", "Dog"], {"RAGMONK_AI__PROVIDER": "openai"}, None),
    ("ai-providers-json", ["ai", "providers", "--json"], {}, None),
    ("ai-providers", ["ai", "providers"], {}, None),
    ("ai-status-api", ["ai", "status", "openai"], {}, None),
    ("ai-status-unknown", ["ai", "status", "nope"], {}, None),
    ("ai-status-privacy", ["ai", "status", "codex", "--json"], {}, None),
    ("ai-login-privacy", ["ai", "login", "codex"], {}, None),
    (
        "ai-status-unavailable",
        ["ai", "status", "codex"],
        {"RAGMONK_PRIVACY__EXTERNAL_AI_ALLOWED": "true"},
        None,
    ),
    ("ai-logout-unavailable", ["ai", "logout", "codex"], {}, None),
    (
        "ai-models-unavailable",
        ["ai", "models", "codex", "--json"],
        {"RAGMONK_PRIVACY__EXTERNAL_AI_ALLOWED": "true"},
        None,
    ),
]


def main() -> None:
    exe = Path(sys.executable).with_name("ragmonk")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
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
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("RAGMONK_")},
            "RAGMONK_HOME": str(home),
            "RAGMONK_SEARCH__SEMANTIC": "false",
            "RAGMONK_UPDATES__ENABLED": "false",
            "RAGMONK_AI__PROVIDER": "ollama",
            "RAGMONK_AI__BASE_URL": f"http://127.0.0.1:{port}",
            "NO_COLOR": "1",
            "TERM": "dumb",
            "COLUMNS": "200",
            # No codex/copilot runtime is reachable.
            "PATH": str(bin_dir),
        }
        for args in (["init"], ["source", "add", str(corpus)], ["index"]):
            subprocess.run([str(exe), *args], env=env, check=True, capture_output=True)

        def norm(text: str) -> str:
            for needle, repl in (
                (str(corpus.resolve()), "<CORPUS>"),
                (str(corpus), "<CORPUS>"),
                (str(home.resolve()), "<HOME>"),
                (str(home), "<HOME>"),
                (str(port), "<PORT>"),
            ):
                text = text.replace(needle, repl)
            return text

        records = []
        for name, args, extra, response in CASES:
            STATE["sent"] = []
            if response is not None:
                STATE["response"] = response
            proc = subprocess.run(
                [str(exe), *args], env={**env, **extra}, capture_output=True, text=True
            )
            records.append(
                {
                    "case": name,
                    "args": args,
                    "env": extra,
                    "response": None
                    if response is None
                    else {
                        "status": response[0],
                        "body": response[1].decode()
                        if isinstance(response[1], bytes)
                        else response[1],
                    },
                    "exit_code": proc.returncode,
                    "stdout": norm(proc.stdout),
                    "stderr": norm(proc.stderr),
                    "sent": json.loads(norm(json.dumps(STATE["sent"], ensure_ascii=False))),
                }
            )
    server.shutdown()
    OUT.write_text(json.dumps(records, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT}: {len(records)} cases")


if __name__ == "__main__":
    main()
