"""Bounded UTF-8/base64 framing, independent of token budgets and business scope."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from uuid import UUID

from supplyagent.service.persistence.context_store import ContextError


def valid_identity(request_id, model_call_key):
    return (
        isinstance(request_id, str)
        and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", request_id) is not None
        and isinstance(model_call_key, str)
        and re.fullmatch(r"[a-f0-9-]{36}-[1-9][0-9]{0,3}", model_call_key) is not None
    )


def valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def frame(body, request_id, model_call_key, direction, *, snapshot_id=None, limits=None):
    limits = limits or {"maxTransferBytes": 1048576, "chunkBytes": 8192, "maxChunks": 128}
    if direction not in {"input", "output", "final"} or not valid_identity(
        request_id, model_call_key
    ):
        raise ContextError("context_frame_invalid")
    if (direction == "output") != (snapshot_id is not None):
        raise ContextError("context_frame_invalid")
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    size = limits["chunkBytes"]
    count = (len(data) + size - 1) // size
    if len(data) > limits["maxTransferBytes"] or count > limits["maxChunks"]:
        raise ContextError("context_transfer_limit")
    common = {"requestId": request_id}
    sha = hashlib.sha256(data).hexdigest()
    yield {
        "type": f"context.{direction}.begin",
        **common,
        "modelCallKey": model_call_key,
        "contextProtocol": 1,
        "schemaVersion": 1,
        "bytes": len(data),
        "chunkCount": count,
        "sha256": sha,
        **({"snapshotId": str(snapshot_id)} if snapshot_id else {}),
    }
    for i in range(count):
        yield {
            "type": f"context.{direction}.chunk",
            **common,
            "index": i,
            "data": base64.b64encode(data[i * size : (i + 1) * size]).decode(),
        }
    yield {"type": f"context.{direction}.end", **common, "bytes": len(data), "sha256": sha}


class FrameReceiver:
    def __init__(self, limits=None, direction="input"):
        self.limits = limits or {"maxTransferBytes": 1048576, "chunkBytes": 8192, "maxChunks": 128}
        if direction not in {"input", "output", "final"}:
            raise ValueError("invalid context direction")
        self.direction = direction
        self.active = None

    def accept(self, message):
        event = {k: v for k, v in message.items() if k not in {"protocolVersion", "seq", "turnId"}}
        kind = event.get("type")
        prefix = f"context.{self.direction}."
        if kind == prefix + "begin":
            expected = {
                "type",
                "requestId",
                "modelCallKey",
                "contextProtocol",
                "schemaVersion",
                "bytes",
                "chunkCount",
                "sha256",
            } | ({"snapshotId"} if self.direction == "output" else set())
            if (
                self.active is not None
                or set(event) != expected
                or not valid_identity(event["requestId"], event["modelCallKey"])
                or not valid_hash(event["sha256"])
                or type(event["contextProtocol"]) is not int
                or type(event["schemaVersion"]) is not int
                or event["contextProtocol"] != 1
                or event["schemaVersion"] != 1
                or type(event["bytes"]) is not int
                or not 1 <= event["bytes"] <= self.limits["maxTransferBytes"]
                or type(event["chunkCount"]) is not int
                or not 1 <= event["chunkCount"] <= self.limits["maxChunks"]
            ):
                raise ContextError("context_frame_invalid")
            if self.direction == "output":
                try:
                    UUID(event["snapshotId"])
                except (ValueError, TypeError, AttributeError):
                    raise ContextError("context_frame_invalid") from None
            if (
                not event["chunkCount"]
                <= event["bytes"]
                <= event["chunkCount"] * self.limits["chunkBytes"]
            ):
                raise ContextError("context_transfer_limit")
            self.active = {"header": event, "chunks": [], "size": 0}
            return None
        if self.active is None or event.get("requestId") != self.active["header"]["requestId"]:
            raise ContextError("context_frame_invalid")
        state = self.active
        header = state["header"]
        if kind == prefix + "chunk":
            if (
                set(event) != {"type", "requestId", "index", "data"}
                or type(event["index"]) is not int
                or event["index"] != len(state["chunks"])
                or len(state["chunks"]) >= header["chunkCount"]
            ):
                raise ContextError("context_frame_invalid")
            if not isinstance(event["data"], str) or len(event["data"]) > 4 * (
                (self.limits["chunkBytes"] + 2) // 3
            ):
                raise ContextError("context_transfer_limit")
            try:
                data = base64.b64decode(event["data"], validate=True)
            except (ValueError, TypeError):
                raise ContextError("context_frame_invalid") from None
            state["size"] += len(data)
            if not 1 <= len(data) <= self.limits["chunkBytes"] or state["size"] > header["bytes"]:
                raise ContextError("context_transfer_limit")
            state["chunks"].append(data)
            return None
        if (
            kind != prefix + "end"
            or set(event) != {"type", "requestId", "bytes", "sha256"}
            or len(state["chunks"]) != header["chunkCount"]
            or state["size"] != header["bytes"]
            or type(event["bytes"]) is not int
            or event["bytes"] != header["bytes"]
            or event["sha256"] != header["sha256"]
        ):
            raise ContextError("context_frame_invalid")
        data = b"".join(state["chunks"])
        if hashlib.sha256(data).hexdigest() != header["sha256"]:
            raise ContextError("context_frame_invalid")
        try:
            body = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ContextError("context_frame_invalid") from None
        self.active = None
        return header, body
