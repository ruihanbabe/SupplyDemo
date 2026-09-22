"""Minimal MCP client over stdio.

No SDK: the transport is newline-delimited JSON-RPC 2.0, and writing it out keeps the
dependency surface flat while making the protocol itself visible.

The part that matters for safety is `child_environment`. A subprocess started with
os.environ inherits every credential the operator happened to export — database
passwords, model API keys, cloud tokens — none of which a supplier integration has any
business seeing. The child gets an explicit allow-list instead, plus whatever the server
declares it needs.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

logger = logging.getLogger("supplyagent.mcp")

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "supplyagent", "version": "0.1.0"}

#: The only variables a child process inherits unless its own config asks for more.
#: PATH so the interpreter resolves, the locale so text decodes, and nothing else.
BASE_ENVIRONMENT = ("PATH", "LANG", "LC_ALL", "PYTHONPATH", "PYTHONUNBUFFERED")


class MCPError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _configuration() -> dict[str, str]:
    """Same precedence as everywhere else in this project: .env first, real environment
    on top.

    Reading .env here is not optional. Credentials live in that file, not in the shell
    the server was started from, and a client that only consulted os.environ would
    silently fall back to recorded data on a machine that is fully configured — which is
    exactly the failure that looks like everything working.
    """
    from dotenv import dotenv_values

    root = Path(__file__).resolve().parents[2]
    loaded = {key: value for key, value in dotenv_values(root / ".env").items() if value}
    return {**loaded, **os.environ}


def child_environment(passthrough: tuple[str, ...] = (),
                      source: dict[str, str] | None = None) -> dict[str, str]:
    """Build the child's environment from an allow-list.

    Declared rather than inherited: a server that needs a credential names it, and the
    name shows up in config review. Inheriting the parent's environment would put every
    secret in the process by default and make its absence the thing nobody notices.
    """
    origin = _configuration() if source is None else source
    allowed = {*BASE_ENVIRONMENT, *passthrough}
    return {key: value for key, value in origin.items() if key in allowed}


@dataclass
class MCPServerSpec:
    name: str
    command: list[str]
    #: Environment variable names this server is allowed to receive, e.g. its API key.
    passthrough: tuple[str, ...] = ()
    cwd: Path | None = None
    startup_timeout: float = 10.0
    call_timeout: float = 20.0
    env_overrides: dict[str, str] = field(default_factory=dict)


class MCPClient:
    """One client, one child process, one server."""

    def __init__(self, spec: MCPServerSpec) -> None:
        self.spec = spec
        self._process: subprocess.Popen | None = None
        self._next_id = 0
        self._lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------------

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def start(self) -> dict[str, Any]:
        environment = {**child_environment(self.spec.passthrough),
                       **self.spec.env_overrides}
        self._process = subprocess.Popen(
            self.spec.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=environment,
            cwd=str(self.spec.cwd) if self.spec.cwd else None)
        handshake = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO}, timeout=self.spec.startup_timeout)
        self._notify("notifications/initialized", {})
        return handshake

    def stop(self) -> None:
        if self._process is None:
            return
        try:
            if self._process.stdin:
                self._process.stdin.close()
            self._process.wait(timeout=5)
        except Exception:  # noqa: BLE001 - shutdown must not raise over a live error
            self._process.kill()
        finally:
            self._process = None

    # ---- protocol --------------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        return self._request("tools/list", {})["tools"]

    def call_tool(self, name: str, arguments: dict[str, Any],
                  *, timeout: float | None = None) -> dict[str, Any]:
        """Call one tool and return its decoded payload.

        The server answers in MCP's content envelope; this unwraps the single text part
        back into the object the tool actually produced. `isError` is raised rather than
        returned, so a server-side fault cannot be mistaken for data.
        """
        result = self._request("tools/call", {"name": name, "arguments": arguments},
                               timeout=timeout or self.spec.call_timeout)
        parts = result.get("content") or []
        text = "".join(part.get("text", "") for part in parts if part.get("type") == "text")
        if result.get("isError"):
            raise MCPError("tool_failed", text or "server reported an error")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MCPError("malformed_response",
                           f"server returned non-JSON content: {exc}") from None

    # ---- transport -------------------------------------------------------------

    def _write(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise MCPError("transport_error", "server is not running")
        self._process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._process.stdin.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any],
                 *, timeout: float | None = None) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._write({"jsonrpc": "2.0", "id": request_id,
                         "method": method, "params": params})
            line = self._read_line(timeout or self.spec.call_timeout)
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MCPError("malformed_response", f"server sent non-JSON: {exc}") from None
        if "error" in message:
            error = message["error"]
            raise MCPError("server_error", f"{error.get('code')}: {error.get('message')}")
        return message.get("result") or {}

    def _read_line(self, timeout: float) -> str:
        """Read one reply, giving up rather than blocking forever.

        readline() on a pipe has no timeout of its own, so a server that hangs would hang
        the caller with it. The read happens on a worker thread that is abandoned on
        timeout: we stop waiting, we do not pretend to have stopped the server — hence
        the process is killed, which is the only way to actually end it.
        """
        if self._process is None or self._process.stdout is None:
            raise MCPError("transport_error", "server is not running")
        stdout = self._process.stdout
        box: dict[str, str] = {}

        def read() -> None:
            box["line"] = stdout.readline()

        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            self._process.kill()
            self._process = None
            raise MCPError("timeout", f"server did not answer within {timeout}s")
        line = box.get("line", "")
        if not line:
            stderr = ""
            if self._process and self._process.stderr:
                stderr = self._process.stderr.read()[:300]
            raise MCPError("transport_error", f"server closed the connection. {stderr}")
        return line


#: Named one by one rather than matched by prefix. A prefix rule quietly widens the
#: moment someone adds a variable that happens to start the same way, and the whole
#: point of the allow-list is that widening it is a visible edit.
SUPPLIER_CREDENTIALS = (
    "SUPPLYAGENT_SUPPLIER_MOUSER_API_KEY",
    "SUPPLYAGENT_SUPPLIER_ELEMENT14_API_KEY",
    "SUPPLYAGENT_SUPPLIER_ELEMENT14_STORE",
    "SUPPLYAGENT_SUPPLIER_DIGIKEY_CLIENT_ID",
    "SUPPLYAGENT_SUPPLIER_DIGIKEY_CLIENT_SECRET",
    "SUPPLYAGENT_SUPPLIER_DIGIKEY_BASE_URL",
    "SUPPLYAGENT_SUPPLIER_REGION",
    "SUPPLYAGENT_SUPPLIER_CURRENCY",
)

SUPPLIER_SERVER = MCPServerSpec(
    name="supplier",
    command=[sys.executable, "-u",
             str(Path(__file__).resolve().parents[2] / "mcp_servers" / "supplier" / "server.py")],
    passthrough=SUPPLIER_CREDENTIALS,
    # Three providers, each a network round trip plus an OAuth exchange for one of them.
    call_timeout=25.0,
)
