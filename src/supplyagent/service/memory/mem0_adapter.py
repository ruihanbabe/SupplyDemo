"""F48 pinned OSS backend seam. Local fixture mode only until metered embedding is configured."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

MEM0_COMMIT = "26cdefb46600c0fa5953c4b5ee5003c125539fc4"
MEM0_VERSION = "2.2.1"


class MemoryBackendError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def admit(text):
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 32768:
        raise MemoryBackendError("memory_content_invalid")
    if re.search(
        r"(?i)(?:bearer\s+\S+|sk-[a-z0-9_-]{8,}|(?:password|api[_ -]?key|secret|access[_ -]?token|refresh[_ -]?token|authorization|private[_ -]?key)['\"\\]*\s*[:=]\s*['\"\\]*\S+|[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?<!\d)1[3-9]\d{9}(?!\d))",
        text,
    ):
        raise MemoryBackendError("memory_sensitive_content")
    if re.search(
        r"(?i)(?:ignore\s+(?:all|previous|system)\s+(?:rules|instructions)|忽略(?:所有|之前|系统)(?:规则|指令)|泄露系统提示)",
        text,
    ):
        raise MemoryBackendError("memory_untrusted_instruction")
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class MemoryScope:
    organization_id: UUID
    user_id: UUID
    object_scope: str

    def __post_init__(self):
        if (
            not isinstance(self.organization_id, UUID)
            or not isinstance(self.user_id, UUID)
            or not isinstance(self.object_scope, str)
            or not 1 <= len(self.object_scope) <= 1024
        ):
            raise ValueError("invalid trusted memory scope")

    @property
    def namespace(self):
        return hashlib.sha256(
            json.dumps(
                [str(self.organization_id), str(self.user_id), self.object_scope],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()


class Mem0Adapter:
    """One server-bound private domain, exact publication keys, and whole-domain erase/rebuild."""

    def __init__(self, root, scope, *, embedding_url, synthetic=False):
        if not synthetic or not isinstance(scope, MemoryScope):
            raise MemoryBackendError("memory_real_data_disabled")
        url = urlsplit(embedding_url)
        if (
            url.scheme != "http"
            or url.hostname not in {"127.0.0.1", "::1"}
            or not url.port
            or url.username
            or url.password
        ):
            raise MemoryBackendError("memory_unmetered_embedding_denied")
        self.root = Path(root).resolve()
        self.scope, self.embedding_url = scope, embedding_url
        self.directory = self.root / scope.namespace
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise MemoryBackendError("memory_storage_invalid")
        self.memory = self.client = None
        self.meter = None
        os.environ["MEM0_TELEMETRY"] = "False"
        os.environ.setdefault("MEM0_DIR", str(self.root / "sdk-control"))
        self._open()

    def _open(self):
        distribution = importlib.metadata.distribution("mem0ai")
        source = json.loads(distribution.read_text("direct_url.json") or "{}")
        if (
            MEM0_COMMIT not in source.get("url", "")
            or source.get("archive_info", {}).get("hashes", {}).get("sha256")
            != "4e8aa24e3b71130d0740659e8e8bbfaf36e41a32089ea8d6a84aa8d178ed70d3"
        ):
            raise MemoryBackendError("memory_backend_artifact_mismatch")
        if distribution.version != MEM0_VERSION:
            raise MemoryBackendError("memory_backend_version_mismatch")
        from mem0 import Memory
        from mem0.memory.telemetry import MEM0_TELEMETRY
        from qdrant_client import QdrantClient

        if MEM0_TELEMETRY:
            raise MemoryBackendError("memory_telemetry_not_disabled")
        data = self.directory / "data"
        data.mkdir(mode=0o700, exist_ok=True)
        if data.is_symlink():
            raise MemoryBackendError("memory_storage_invalid")
        self.client = QdrantClient(path=str(data / "vectors"))
        try:
            self.memory = Memory.from_config(
                {
                    "vector_store": {
                        "provider": "qdrant",
                        "config": {
                            "collection_name": "published",
                            "embedding_model_dims": 16,
                            "client": self.client,
                        },
                    },
                    "embedder": {
                        "provider": "openai",
                        "config": {
                            "model": "fixture-embedding-v1",
                            "embedding_dims": 16,
                            "api_key": "synthetic-only",
                            "openai_base_url": self.embedding_url,
                        },
                    },
                    "llm": {
                        "provider": "openai",
                        "config": {
                            "model": "fixture-never-called",
                            "api_key": "synthetic-only",
                            "openai_base_url": self.embedding_url,
                        },
                    },
                    "history_db_path": str(data / "history.sqlite"),
                }
            )
            from supplyagent.service.memory.models import LocalModelPort

            self.memory.embedding_model = LocalModelPort(self.embedding_url, self.meter)
        except Exception:  # noqa: BLE001 - SDK errors cross a private boundary as safe codes.
            self.client.close()
            raise MemoryBackendError("memory_backend_unavailable") from None

    def close(self):
        if self.memory:
            self.memory.close()
        if self.client:
            self.client.close()
        self.memory = self.client = None

    def bind_meter(self, meter):
        self.meter = meter
        self.memory.embedding_model.meter = meter

    @staticmethod
    def _call(operation, *, writing=False):
        try:
            return operation()
        except MemoryBackendError:
            raise
        except Exception:  # noqa: BLE001 - SDK failures must not leak provider payloads.
            raise MemoryBackendError(
                "memory_write_outcome_unknown" if writing else "memory_backend_unavailable"
            ) from None

    def reconcile(self, version_id):
        if not isinstance(version_id, UUID):
            raise TypeError("invalid memory version")
        return self._call(
            lambda: self.memory.get_all(
                filters={"user_id": self.scope.namespace, "publication_key": str(version_id)},
                top_k=1000,
            )
        )["results"]

    def publish(self, version_id, text):
        content_hash = admit(text)
        rows = self.reconcile(version_id)
        if rows:
            if any(
                row["memory"] != text or row.get("metadata", {}).get("content_hash") != content_hash
                for row in rows
            ):
                raise MemoryBackendError("memory_projection_mismatch")
            return {
                "ids": [row["id"] for row in rows],
                "reconciled": True,
                "contentHash": content_hash,
            }
        result = self._call(
            lambda: self.memory.add(
                text,
                user_id=self.scope.namespace,
                metadata={
                    "publication_key": str(version_id),
                    "content_hash": content_hash,
                    "namespace": self.scope.namespace,
                },
                infer=False,
            ),
            writing=True,
        )
        rows = result["results"]
        if len(rows) != 1 or rows[0]["memory"] != text:
            raise MemoryBackendError("memory_write_outcome_unknown")
        return {"ids": [rows[0]["id"]], "reconciled": False, "contentHash": content_hash}

    def search(self, query, *, limit=30):
        admit(query)
        if type(limit) is not int or not 1 <= limit <= 30:
            raise ValueError("invalid memory search limit")
        rows = self._call(
            lambda: self.memory.search(
                query,
                top_k=limit,
                filters={"user_id": self.scope.namespace, "namespace": self.scope.namespace},
                threshold=0.0,
                rerank=False,
            )
        )["results"]
        if any(row.get("metadata", {}).get("namespace") != self.scope.namespace for row in rows):
            raise MemoryBackendError("memory_namespace_mismatch")
        return rows

    def erase_and_rebuild(self, surviving_versions):
        # SQL must deny deleted inputs and hold the domain lease before entering this operation.
        survivors = []
        for version_id, text in surviving_versions:
            if not isinstance(version_id, UUID):
                raise TypeError("invalid survivor version")
            admit(text)
            survivors.append((version_id, text))
        self.close()
        data = self.directory / "data"
        if data.is_symlink() or data.parent != self.directory:
            raise MemoryBackendError("memory_storage_invalid")
        shutil.rmtree(data)
        if data.exists():
            raise MemoryBackendError("memory_erase_incomplete")
        self._open()
        rebuilt = {str(version): self.publish(version, text) for version, text in survivors}
        return {"state": "local_stores_erased", "rebuilt": rebuilt, "backupStatus": "unverified"}
