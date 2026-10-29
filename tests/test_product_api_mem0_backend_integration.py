"""Actual pinned Mem0 + persistent Qdrant/SQLite + loopback embedding, no SDK mocks."""

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from product_api.mem0_adapter import Mem0Adapter, MemoryBackendError, MemoryScope


@contextmanager
def embeddings_fixture(*, summary=None, transform=None, entered=None, release=None):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.headers.get("authorization") == "Bearer synthetic-only"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if entered is not None:
                entered.set()
            if release is not None:
                assert release.wait(10)
            if self.path == "/v1/chat/completions":
                assert summary is not None, "No unconfigured LLM endpoint may be called"
                assert request["model"] == "fixture-summary-v1"
                paragraphs = json.loads(request["messages"][-1]["content"])["paragraphs"]
                selected = summary(paragraphs)
                content = json.dumps({"selected": selected})
                value = {
                    "model": request["model"],
                    "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": len(json.dumps(request).encode()),
                        "completion_tokens": len(content),
                    },
                }
                calls.append(
                    {
                        "inputUnits": len(paragraphs),
                        "bytes": len(json.dumps(request).encode()),
                        "usage": value["usage"]["prompt_tokens"],
                        "purpose": "summarize",
                    }
                )
            else:
                assert self.path == "/v1/embeddings", "No other endpoint may be called"
                assert request["model"] == "fixture-embedding-v1"
                texts = request["input"]
                calls.append(
                    {
                        "inputUnits": len(texts),
                        "bytes": sum(len(t.encode()) for t in texts),
                        "usage": sum(len(t.encode()) for t in texts),
                        "purpose": "embedding",
                    }
                )
                value = {
                    "object": "list",
                    "model": request["model"],
                    "data": [
                        {
                            "object": "embedding",
                            "index": i,
                            "embedding": [
                                (b + 1) / 256 for b in hashlib.sha256(text.encode()).digest()[:16]
                            ],
                        }
                        for i, text in enumerate(texts)
                    ],
                    "usage": {
                        "prompt_tokens": calls[-1]["usage"],
                        "total_tokens": calls[-1]["usage"],
                    },
                }
            if transform:
                value = transform(self.path, value)
            response = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.integration
def test_actual_mem0_publication_reconciliation_isolation_and_full_local_erase(tmp_path):
    with embeddings_fixture() as (endpoint, usage):
        org, user = uuid4(), uuid4()
        scope = MemoryScope(org, user, "case:synthetic-A")
        version, survivor = uuid4(), uuid4()
        deleted = "SYNTHETIC-ERASE-MARKER-" + uuid4().hex
        retained = "Prior process: arrival remains unknown because samples did not pass."
        adapter = Mem0Adapter(tmp_path, scope, embedding_url=endpoint, synthetic=True)
        try:
            result = adapter.publish(version, deleted)
            assert len(result["ids"]) == 1 and not result["reconciled"]
            count = len(usage)
            assert adapter.publish(version, deleted)["reconciled"]
            assert (
                len(usage) == count
            )  # Unknown remote success is reconciled without another embedding.
            adapter.publish(survivor, retained)
            assert any(row["memory"] == deleted for row in adapter.search("prior process"))
            peer = Mem0Adapter(
                tmp_path,
                MemoryScope(org, uuid4(), scope.object_scope),
                embedding_url=endpoint,
                synthetic=True,
            )
            foreign = Mem0Adapter(
                tmp_path,
                MemoryScope(uuid4(), user, scope.object_scope),
                embedding_url=endpoint,
                synthetic=True,
            )
            try:
                assert peer.search("prior process") == []
                assert foreign.search("prior process") == []
            finally:
                peer.close()
                foreign.close()
            count = len(usage)
            with pytest.raises(MemoryBackendError, match="memory_sensitive_content"):
                adapter.publish(uuid4(), "api_key=sk-never-send-credentials")
            assert len(usage) == count
            erased = adapter.erase_and_rebuild([(survivor, retained)])
            assert (
                erased["state"] == "local_stores_erased" and erased["backupStatus"] == "unverified"
            )
            assert adapter.reconcile(version) == []
            assert any(row["memory"] == retained for row in adapter.search("prior process"))
        finally:
            adapter.close()
        history = scope.namespace + "/data/history.sqlite"
        with sqlite3.connect(tmp_path / history) as c:
            assert (
                c.execute(
                    "select count(*) from history where old_memory=? or new_memory=?",
                    (deleted, deleted),
                ).fetchone()[0]
                == 0
            )
            assert (
                c.execute("select count(*) from messages where content=?", (deleted,)).fetchone()[0]
                == 0
            )
        assert not any(
            deleted.encode() in file.read_bytes()
            for file in (tmp_path / scope.namespace / "data").rglob("*")
            if file.is_file()
        )
        assert usage and all(c["usage"] > 0 for c in usage)


def test_real_data_or_external_embedding_cannot_be_enabled_by_adapter_defaults(tmp_path):
    scope = MemoryScope(uuid4(), uuid4(), "case:A")
    with pytest.raises(MemoryBackendError, match="memory_real_data_disabled"):
        Mem0Adapter(tmp_path, scope, embedding_url="http://127.0.0.1:1/v1")
    with pytest.raises(MemoryBackendError, match="memory_unmetered_embedding_denied"):
        Mem0Adapter(tmp_path, scope, embedding_url="https://api.openai.com/v1", synthetic=True)
