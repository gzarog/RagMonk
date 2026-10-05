"""Generate RUST-14 Admin UI golden fixtures from the Python reference.

    python rust/compat/tools/gen_ui_golden.py

Indexes the compat ``code-docs-index`` corpus (semantic off) in a
temporary RAGMONK_HOME, builds the reference FastAPI admin app on it and
records, for every request in ``REQUESTS``, the status code, selected
headers and the body normalized by ``normalize``:

* corpus/home paths, the source id, the CSRF token and the version are
  replaced with placeholders; 32-hex ids become ``<ID>``, timestamps
  ``<TS>`` and ``N KB`` sizes ``<N> KB``;
* whitespace runs collapse to one space and vanish between tags.

JSON bodies are recorded parsed. The Rust test applies the same rules.
Writes rust/compat/golden/ui.json.
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
OUT = ROOT / "golden" / "ui.json"
FIXTURES = [
    ("tests/fixtures/languages", "code"),
    ("tests/fixtures/documents/simple.md", "docs/simple.md"),
    ("tests/fixtures/documents/simple.txt", "docs/simple.txt"),
    ("tests/fixtures/documents/simple.csv", "docs/simple.csv"),
    ("tests/fixtures/documents/simple.html", "docs/simple.html"),
    ("tests/fixtures/documents/email_with_attachments.eml", "docs/email_with_attachments.eml"),
]
TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
MTIME = 1_767_225_600  # 2026-01-01T00:00:00Z
KB = re.compile(r"\d+(?:\.\d+)? KB")

# Canned ``logs/ragmonk.log`` content written at the WRITE_LOG step, so
# the Logs page renders the same entries for both implementations.
LOG_LINES = [
    '{"timestamp": "2026-01-01T10:00:00Z", "level": "INFO", "component": "ragmonk.daemon", '
    '"event": "pass started"}',
    "not json at all",
    '{"timestamp": "2026-01-01T10:00:01Z", "level": "WARNING", "component": "ragmonk.index", '
    '"event": "slow file"}',
    '{"timestamp": "2026-01-01T10:00:02Z", "level": "ERROR", "component": "ragmonk.daemon", '
    '"event": "pass failed <boom>"}',
    "",
    '{"level": "critical", "event": "no component"}',
]

# (method, path, form, csrf, extra headers). ``{source}`` and ``{doc}``
# are substituted; csrf True sends the minted token.
REQUESTS: list[tuple[str, str, dict | None, bool, dict]] = [
    ("GET", "/health", None, False, {}),
    ("GET", "/", None, False, {}),
    ("GET", "/sources", None, False, {}),
    ("GET", "/sources?error=something+broke", None, False, {}),
    ("GET", "/sources/{source}", None, False, {}),
    ("GET", "/sources/src_nope", None, False, {}),
    ("GET", "/indexing", None, False, {}),
    ("GET", "/indexing/failed", None, False, {}),
    ("GET", "/documents", None, False, {}),
    ("GET", "/documents?q=simple", None, False, {}),
    ("GET", "/documents?fmt=csv&source_id={source}", None, False, {}),
    ("GET", "/documents?page=9", None, False, {}),
    ("GET", "/documents/{source}/{doc}", None, False, {}),
    ("GET", "/search", None, False, {}),
    ("GET", "/search?q=bark", None, False, {}),
    ("GET", "/search?q=zzqqxx_nothing", None, False, {}),
    ("GET", "/search?q=bark&mode=semantic&limit=5", None, False, {}),
    ("GET", "/search?q=bark&mode=hybrid", None, False, {}),
    ("GET", "/knowledge?q=Dog", None, False, {}),
    ("GET", "/knowledge?q=zzqqxx_nothing", None, False, {}),
    ("GET", "/knowledge/symbol", None, False, {}),
    ("GET", "/knowledge/symbol?name=speak&relation=callers", None, False, {}),
    ("GET", "/knowledge/symbol?name=bark&relation=callees", None, False, {}),
    ("GET", "/knowledge/symbol?name=speak&relation=impact", None, False, {}),
    ("GET", "/knowledge/symbol?name=nope_nothing&relation=impact", None, False, {}),
    ("GET", "/ai", None, False, {}),
    ("GET", "/ai?test=openai", None, False, {}),
    ("GET", "/ai?test=ollama", None, False, {}),
    ("GET", "/ai?test=mystery", None, False, {}),
    ("GET", "/config", None, False, {}),
    ("GET", "/config?saved=1", None, False, {}),
    ("GET", "/daemon", None, False, {}),
    ("GET", "/daemon/panel", None, False, {}),
    ("GET", "/daemon/logs", None, False, {}),
    ("GET", "/daemon/api/status", None, False, {}),
    ("GET", "/health/", None, False, {}),
    ("GET", "/backups", None, False, {}),
    ("GET", "/backups?saved=restored", None, False, {}),
    ("GET", "/backups/download/nope.tar.gz", None, False, {}),
    ("WRITE_LOG", "", None, False, {}),
    ("GET", "/logs", None, False, {}),
    ("GET", "/logs?level=WARNING", None, False, {}),
    ("GET", "/logs?component=ragmonk.daemon&q=pass", None, False, {}),
    ("GET", "/logs?errors_only=true", None, False, {}),
    ("GET", "/system", None, False, {}),
    ("GET", "/no/such/page", None, False, {}),
    ("GET", "/", None, False, {"host": "evil.example"}),
    ("POST", "/sources/{source}/disable", None, False, {}),
    ("POST", "/sources", {"path": "/definitely/not/here"}, True, {}),
    ("POST", "/sources/{source}/disable", None, True, {}),
    ("GET", "/sources", None, False, {}),
    ("POST", "/sources/{source}/enable", None, True, {}),
    ("POST", "/sources/src_nope/enable", None, True, {}),
    ("POST", "/ai/test", {"provider": "ollama"}, True, {}),
    ("POST", "/config", {"search__semantic_top_k": "abc"}, True, {}),
    (
        "POST",
        "/config",
        {"context__max_files": "7", "telemetry__anonymous_usage": "false"},
        True,
        {},
    ),
    ("POST", "/backups/restore", {"name": "../config.yaml"}, True, {}),
]


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
        # A fixed mtime for every file, so search ties break the same way
        # on both sides (the Rust test pins the same value).
        for f in corpus.rglob("*"):
            if f.is_file():
                os.utime(f, (MTIME, MTIME))
        for key in [k for k in os.environ if k.startswith("RAGMONK_")]:
            del os.environ[key]
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            os.environ.pop(key, None)
        os.environ.update(
            {
                "RAGMONK_HOME": str(home),
                "RAGMONK_SEARCH__SEMANTIC": "false",
                "RAGMONK_UPDATES__ENABLED": "false",
            }
        )
        outputs = [
            subprocess.run([str(exe), *args], check=True, capture_output=True, text=True).stdout
            for args in (["init"], ["source", "add", str(corpus)], ["index"])
        ]
        source_id = re.search(r"(src_[0-9a-f]+)", outputs[1]).group(1)

        from fastapi.testclient import TestClient

        from ragmonk import __version__
        from ragmonk.ui.app import create_app

        records = []
        with TestClient(create_app(home=home), base_url="http://127.0.0.1:8765") as client:
            first = client.get("/sources")
            token = first.cookies.get("ragmonk_csrf")
            docs = client.get(f"/documents?source_id={source_id}&q=simple.md").text
            doc_id = re.search(rf"/documents/{source_id}/([0-9a-f]+)", docs).group(1)
            subs = sorted(
                [
                    (str(corpus.resolve()), "<CORPUS>"),
                    (str(corpus), "<CORPUS>"),
                    (str(home.resolve()), "<HOME>"),
                    (str(home), "<HOME>"),
                    (source_id, "<SOURCE>"),
                    (token, "<CSRF>"),
                    (f"v{__version__}", "v<VERSION>"),
                    (__version__, "<VERSION>"),
                ],
                key=lambda s: -len(s[0]),
            )

            def normalize(text: str) -> str:
                for needle, repl in subs:
                    text = text.replace(needle, repl)
                text = TS.sub("<TS>", text)
                text = HEX32.sub("<ID>", text)
                text = KB.sub("<N> KB", text)
                text = re.sub(r"\s+", " ", text)
                return re.sub(r">\s+<", "><", text).strip()

            for method, path, form, csrf, headers in REQUESTS:
                if method == "WRITE_LOG":
                    log = home / "logs" / "ragmonk.log"
                    log.parent.mkdir(parents=True, exist_ok=True)
                    log.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
                    records.append({"method": method, "path": "", "log_lines": LOG_LINES})
                    continue
                url = path.replace("{source}", source_id).replace("{doc}", doc_id)
                hdrs = dict(headers)
                if csrf:
                    hdrs["x-csrf-token"] = token
                if method == "GET":
                    r = client.get(url, headers=hdrs, follow_redirects=False)
                else:
                    r = client.post(url, data=form or {}, headers=hdrs, follow_redirects=False)
                ctype = r.headers.get("content-type", "")
                text = normalize(r.text)
                body = json.loads(text) if "json" in ctype else text
                records.append(
                    {
                        "method": method,
                        "path": path,
                        "form": form,
                        "csrf": csrf,
                        "headers": headers,
                        "status": r.status_code,
                        "content_type": ctype.split(";")[0],
                        "hx_redirect": normalize(r.headers["hx-redirect"])
                        if "hx-redirect" in r.headers
                        else None,
                        "body": body,
                    }
                )
    OUT.write_text(json.dumps(records, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT}: {len(records)} requests")


if __name__ == "__main__":
    main()
