"""Generate RUST-14 AI provider golden fixtures from the Python reference.

    python rust/compat/tools/gen_ai_golden.py

Drives ``ai.factory.create_provider`` for every HTTP provider against a
local mock server with canned responses, and records for each case the
exact request the provider sent (method, path, selected headers, JSON
body) and the outcome: ``AiAnswer.to_dict()`` or the error's class name,
exit code and message. Also records the factory's configuration and
privacy errors, and ``build_prompt`` for a fixed evidence package.

The mock's port appears in messages and is written as ``<PORT>``.
Writes rust/compat/golden/ai.json.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ragmonk.ai import factory
from ragmonk.ai.base import AiProviderError, AiRequest, build_prompt
from ragmonk.core.config import AiConfig, PrivacyConfig
from ragmonk.core.errors import RagMonkError

OUT = Path(__file__).resolve().parents[1] / "golden" / "ai.json"
HEADERS = ("content-type", "authorization", "x-api-key", "anthropic-version", "accept")

REQUEST = AiRequest(
    question="who calls bark?",
    summary="Found 1 symbol(s) and 0 document(s) matching 'bark'.",
    evidence=[
        {
            "source": "code",
            "path": "/src/animals.py",
            "location": {"line_start": 17, "line_end": 18, "section": None},
            "entity": "animals.Dog.bark",
            "relationship": "defines",
            "confidence": "exact",
            "snippet": "def bark(self):\n    return 'woof'",
        },
        {"path": "doc.md", "entity": "Λογαριασμός", "confidence": "high", "location": {}},
    ],
    graph_paths=[
        {"source": "animals.Dog.speak", "relationship": "calls", "target": "animals.Dog.bark"}
    ],
)
EMPTY = AiRequest(question="q", summary="nothing")

OPENAI_OK = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "gpt-4o-mini-2024-07-18",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Dog.speak calls bark."},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127},
}
OPENAI_BARE = {"id": "x", "object": "chat.completion", "created": 1, "model": "", "choices": []}
ANTHROPIC_OK = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-3-5-haiku-20241022",
    "content": [
        {"type": "text", "text": "Dog.speak "},
        {"type": "tool_use", "id": "t", "name": "x", "input": {}},
        {"type": "text", "text": "calls bark."},
    ],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 99, "output_tokens": 5},
}
OLLAMA_OK = {
    "model": "llama3.2:latest",
    "message": {"role": "assistant", "content": "Dog.speak calls bark."},
    "done": True,
    "prompt_eval_count": 80,
    "eval_count": 6,
}
ERR_JSON = {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}}
ANTHROPIC_ERR = {
    "type": "error",
    "error": {"type": "authentication_error", "message": "invalid x-api-key"},
}

# (status, content-type, body) by case name.
RESPONSES: dict[str, tuple[int, str, bytes]] = {}
CAPTURED: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401 - silence
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        CAPTURED.append(
            {
                "method": "POST",
                "path": self.path,
                "headers": {k: self.headers[k] for k in HEADERS if self.headers.get(k) is not None},
                "body": json.loads(raw) if raw else None,
            }
        )
        status, ctype, body = RESPONSES["current"]
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def j(obj) -> bytes:
    return json.dumps(obj).encode()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def error(exc: RagMonkError) -> dict:
    return {"type": type(exc).__name__, "exit_code": exc.exit_code, "message": str(exc)}


def outcome(fn) -> dict:
    """Like ``cli/ask._run``: a non-RagMonk failure becomes AiProviderError."""
    try:
        return {"answer": fn().to_dict()}
    except RagMonkError as exc:
        return {"error": error(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": error(AiProviderError(f"ai provider call failed: {exc}"))}


class ScriptedStream:
    """A ``ByteStream`` answering each request with the next scripted
    reply (``{"result": ...}`` or ``{"error": ...}``); records every
    message the client wrote.
    """

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.sent: list[dict] = []
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def read(self) -> bytes:
        return await self.queue.get()

    async def write(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            message = json.loads(line)
            self.sent.append(message)
            if "id" in message:
                reply = self.replies.pop(0) if self.replies else {"result": {}}
                out = {"jsonrpc": "2.0", "id": message["id"], **reply}
                # Noise the client must tolerate: a notification and a
                # reply to an unknown id, then the real reply split in two.
                raw = json.dumps(out).encode() + b"\n"
                await self.queue.put(b'{"jsonrpc":"2.0","method":"progress"}\n')
                await self.queue.put(b'{"jsonrpc":"2.0","id":999,"result":{}}\n')
                await self.queue.put(raw[:7])
                await self.queue.put(raw[7:])

    async def aclose(self) -> None:
        pass


INIT_OK = {"result": {"serverInfo": {"name": "codex"}}}
CODEX_SCRIPTS = {
    "status_signed_in": (
        "status",
        [
            INIT_OK,
            {
                "result": {
                    "authenticated": True,
                    "authMode": "chatgpt",
                    "account": {"email": "me@example.com"},
                    "runtimeVersion": "0.40.0",
                }
            },
        ],
    ),
    "status_signed_out": ("status", [INIT_OK, {"result": {"authenticated": False}}]),
    "status_api_key_mode": (
        "status",
        [INIT_OK, {"result": {"authenticated": True, "authMode": "apikey"}}],
    ),
    "login": ("login", [INIT_OK, {"result": {"authenticated": True, "account": "me"}}]),
    "logout": ("logout", [INIT_OK, {"result": {}}]),
    "models": (
        "models",
        [INIT_OK, {"result": {"models": [{"id": "gpt-5"}, {"name": "o4"}, "raw", 5, {}]}}],
    ),
    "answer_ok": (
        "answer",
        [
            INIT_OK,
            {"result": {"threadId": "t1"}},
            {
                "result": {
                    "status": "completed",
                    "model": "gpt-5",
                    "message": {
                        "content": [
                            {"type": "text", "text": "A"},
                            {"type": "image"},
                            {"type": "text", "text": "B"},
                        ]
                    },
                    "usage": {"inputTokens": 10, "outputTokens": "x"},
                }
            },
        ],
    ),
    "answer_text_field": (
        "answer",
        [INIT_OK, {"result": {"id": "t2"}}, {"result": {"text": "plain"}}],
    ),
    "answer_failed": (
        "answer",
        [INIT_OK, {"result": {"threadId": "t1"}}, {"result": {"status": "failed"}}],
    ),
    "answer_empty": (
        "answer",
        [INIT_OK, {"result": {"threadId": "t1"}}, {"result": {"message": {}}}],
    ),
    "answer_no_thread": ("answer", [INIT_OK, {"result": {}}]),
    "answer_quota": (
        "answer",
        [
            INIT_OK,
            {"result": {"threadId": "t1"}},
            {"error": {"code": 429, "message": "usage limit reached"}},
        ],
    ),
    "answer_login": (
        "answer",
        [INIT_OK, {"error": {"code": "unauthenticated", "message": "please sign in"}}],
    ),
    "answer_policy": (
        "answer",
        [INIT_OK, {"error": {"code": 403, "message": "workspace disabled"}}],
    ),
    "answer_other": ("answer", [INIT_OK, {"error": {"code": -32000, "message": "boom"}}]),
    "answer_string_error": ("answer", [INIT_OK, {"error": "bad"}]),
}


async def codex_sessions() -> list[dict]:
    from ragmonk.ai._transport import JsonRpcClient
    from ragmonk.ai.codex import CodexSession

    out = []
    for name, (op, replies) in CODEX_SCRIPTS.items():
        for model in [""] if op != "answer" else ["", "gpt-5-mini"]:
            if model and name != "answer_ok":
                continue
            stream = ScriptedStream(replies)
            client = JsonRpcClient(stream)
            session = CodexSession(client, timeout=5.0)
            try:
                if op == "status":
                    result = (await session.account_status()).to_dict()
                elif op == "login":
                    result = (await session.login()).to_dict()
                elif op == "logout":
                    result = await session.logout()
                elif op == "models":
                    result = await session.models()
                else:
                    text, used, usage = await session.answer("SYS", "USER", model=model)
                    result = {
                        "text": text,
                        "model": used,
                        "usage": [usage.input_tokens, usage.output_tokens],
                    }
                outcome_ = {"result": result}
            except RagMonkError as exc:
                outcome_ = {"error": error(exc)}
            finally:
                await client.aclose()
            out.append(
                {
                    "case": name + (f"/{model}" if model else ""),
                    "op": op,
                    "model": model,
                    "replies": replies,
                    "sent": stream.sent,
                    **outcome_,
                }
            )
    return out


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    dead = f"http://127.0.0.1:{free_port()}"
    allowed = PrivacyConfig(external_ai_allowed=True)
    os.environ.update(
        {"OPENAI_API_KEY": "sk-test", "ANTHROPIC_API_KEY": "ak-test", "RAGMONK_AI_API_KEY": "ck"}
    )

    providers = {
        "openai": {"provider": "openai", "base_url": base + "/v1"},
        "openai_model": {"provider": "openai", "base_url": base + "/v1", "model": "gpt-x"},
        "openai_compatible": {
            "provider": "openai_compatible",
            "base_url": base + "/v1",
            "model": "local-model",
        },
        "anthropic": {"provider": "anthropic", "base_url": base},
        "ollama": {"provider": "ollama", "base_url": base + "/"},
    }
    ok = {
        "openai": OPENAI_OK,
        "openai_model": OPENAI_OK,
        "openai_compatible": OPENAI_OK,
        "anthropic": ANTHROPIC_OK,
        "ollama": OLLAMA_OK,
    }
    cases = []
    for name, cfg in providers.items():
        variants = [
            ("ok", REQUEST, (200, "application/json", j(ok[name]))),
            ("empty_evidence", EMPTY, (200, "application/json", j(ok[name]))),
            (
                "http_401_json",
                REQUEST,
                (401, "application/json", j(ANTHROPIC_ERR if name == "anthropic" else ERR_JSON)),
            ),
            ("http_500_text", REQUEST, (500, "text/plain", b"upstream exploded")),
            ("not_json", REQUEST, (200, "application/json", b"<html>nope</html>")),
        ]
        if name.startswith("openai"):
            variants.append(("bare", REQUEST, (200, "application/json", j(OPENAI_BARE))))
        if name == "ollama":
            variants.append(("bare", REQUEST, (200, "application/json", j({}))))
        for variant, request, response in variants:
            RESPONSES["current"] = response
            CAPTURED.clear()
            provider = factory.create_provider(ai=AiConfig(**cfg), privacy=allowed)
            result = outcome(lambda p=provider, r=request: p.answer(r))
            cases.append(
                {
                    "case": f"{name}/{variant}",
                    "config": cfg,
                    "request": "full" if request is REQUEST else "empty",
                    "response": {
                        "status": response[0],
                        "content_type": response[1],
                        "body": response[2].decode(),
                    },
                    "sent": CAPTURED[0] if CAPTURED else None,
                    **result,
                }
            )
        # Unreachable endpoint.
        dead_cfg = {**cfg, "base_url": cfg["base_url"].replace(base, dead)}
        provider = factory.create_provider(ai=AiConfig(**dead_cfg), privacy=allowed)
        cases.append(
            {
                "case": f"{name}/connection_refused",
                "config": dead_cfg,
                "request": "full",
                **outcome(lambda p=provider: p.answer(REQUEST)),
            }
        )

    # Factory configuration and privacy errors (no request is sent).
    factory_cases = []
    env_sets = {
        "all": {"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "k"},
        "none": {},
    }
    configs = [
        ({}, "all", False),
        ({"provider": "  NONE "}, "all", False),
        ({"provider": "mystery"}, "all", True),
        ({"provider": "openai"}, "all", False),
        ({"provider": "openai"}, "none", True),
        ({"provider": "anthropic"}, "none", True),
        ({"provider": "anthropic"}, "all", False),
        ({"provider": "openai_compatible"}, "all", True),
        ({"provider": "openai_compatible", "base_url": "http://x"}, "all", True),
        ({"provider": "openai_compatible"}, "all", False),
        ({"provider": "ollama"}, "none", False),
        ({"provider": "ollama", "base_url": "http://[::1]:11434"}, "none", False),
        ({"provider": "ollama", "base_url": "http://gpu-box:11434"}, "none", False),
        ({"provider": "ollama", "base_url": "http://gpu-box:11434"}, "none", True),
        ({"provider": "codex"}, "none", False),
        ({"provider": "github_copilot"}, "none", False),
    ]
    for cfg, env_name, external in configs:
        saved = {k: os.environ.pop(k, None) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")}
        os.environ.update(env_sets[env_name])
        try:
            factory.create_provider(
                ai=AiConfig(**cfg), privacy=PrivacyConfig(external_ai_allowed=external)
            )
            result = {"ok": True}
        except RagMonkError as exc:
            result = {
                "error": {
                    "type": type(exc).__name__,
                    "exit_code": exc.exit_code,
                    "message": str(exc),
                }
            }
        finally:
            for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
                os.environ.pop(k, None)
            os.environ.update({k: v for k, v in saved.items() if v is not None})
        factory_cases.append(
            {"config": cfg, "env": env_name, "external_ai_allowed": external, **result}
        )
    server.shutdown()
    codex_cases = asyncio.run(codex_sessions())

    payload = {
        "requests": {
            name: {
                "question": r.question,
                "summary": r.summary,
                "evidence": r.evidence,
                "graph_paths": r.graph_paths,
            }
            for name, r in (("full", REQUEST), ("empty", EMPTY))
        },
        "prompt": build_prompt(REQUEST),
        "prompt_empty": build_prompt(EMPTY),
        "providers": cases,
        "factory": factory_cases,
        "codex": codex_cases,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    text = text.replace(str(port), "<PORT>").replace(dead.rsplit(":", 1)[1], "<DEAD>")
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}: {len(cases)} provider cases, {len(factory_cases)} factory cases")


if __name__ == "__main__":
    main()
