"""Local stand-ins used by the real-agent integration tests (standard library only, 127.0.0.1).

* ``AnthropicStub``: an Anthropic-compatible Messages endpoint with a scripted "model". It records
  the model, the tools offered to the model, the last user text, the header *names* of every request
  and the values of test headers only (``x-pa-*``, values made up by the tests). It never records
  credentials.
* ``ProbeServer``: counts requests (used to prove that nothing fetched a URL).
* ``MCP_STUB``: a stdio MCP server with tools ``ping`` and ``pong`` that logs every call.
"""

from __future__ import annotations

import http.server
import json
import re
import socketserver
import threading
import time
import uuid
from pathlib import Path
from typing import Any

TEST_HEADER_PREFIX = "x-pa-"


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class AnthropicStub:
    """Scripted by keywords in the last user text: ``DO_<TOOL> <arg>`` makes the "model" call a tool,
    anything else is answered with ``ECHO <text>``; tool results are answered with ``TOOLRESULT …``."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                self._json(200, {"data": [], "has_more": False})

            def do_HEAD(self) -> None:
                self.send_response(200)
                self.send_header("content-length", "0")
                self.end_headers()

            def do_POST(self) -> None:
                n = int(self.headers.get("content-length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    body = {}
                if "count_tokens" in self.path:
                    return self._json(200, {"input_tokens": 12})
                if "/v1/messages" not in self.path:
                    return self._json(404, {"error": {"type": "not_found_error", "message": "not found"}})
                stub.handle(self, body)

            def _json(self, code: int, obj: Any) -> None:
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.srv = _Server(("127.0.0.1", 0), Handler)
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def main_requests(self) -> list[dict[str, Any]]:
        """Requests of the main conversation (background calls such as titles offer no tools)."""
        with self.lock:
            return [r for r in self.records if r["tools"]]

    @staticmethod
    def _last_user(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        for m in reversed(body.get("messages") or []):
            if m.get("role") != "user":
                continue
            c = m.get("content")
            if isinstance(c, str):
                return c, []
            texts = [x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"]
            results = [x for x in c if isinstance(x, dict) and x.get("type") == "tool_result"]
            return "\n".join(texts), results
        return "", []

    def handle(self, req: http.server.BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        text, results = self._last_user(body)
        tools = [t.get("name") for t in body.get("tools") or []]
        clean = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S).strip()
        rec = {
            "t": time.time(), "model": body.get("model"), "tools": tools, "user_text": clean[:300],
            "header_names": sorted(k.lower() for k in req.headers.keys()),
            "test_headers": {k.lower(): v for k, v in req.headers.items() if k.lower().startswith(TEST_HEADER_PREFIX)},
            "tool_results": [{"is_error": bool(r.get("is_error")), "content": str(r.get("content"))[:2000]} for r in results],
            # Kept in memory only (task 13.2): the full tool definitions and the system text, which is where a
            # CLI lists tools it loads on demand. Requests carry no credentials (headers: names only above).
            "body": body,
        }
        with self.lock:
            self.records.append(rec)
        if results:
            summary = "; ".join(("ERR " if r["is_error"] else "OK ") + r["content"][:80] for r in rec["tool_results"])
            return self._stream(req, body, [("text", f"TOOLRESULT {summary}")], "end_turn")
        m = re.search(r"\b(DO_[A-Z_]+)(?:\s+(\S+))?", clean)
        if tools and m:
            kw, arg = m.group(1), m.group(2) or ""
            call = {
                "DO_WRITE": ("Write", {"file_path": arg, "content": "hi\n"}),
                "DO_BASH": ("Bash", {"command": f"touch {arg}", "description": "touch a file"}),
                "DO_FETCH": ("WebFetch", {"url": arg, "prompt": "return the body"}),
                "DO_READ": ("Read", {"file_path": arg}),
                "DO_MCP_PING": ("mcp__stub__ping", {}),
                "DO_MCP_PONG": ("mcp__stub__pong", {}),
                # Task 13.2: ask claude's deferred-tool search for a tool by name.
                "DO_TOOLSEARCH": ("ToolSearch", {"query": f"select:{arg}", "max_results": 5}),
                "DO_TOOLSEARCHKW": ("ToolSearch", {"query": arg, "max_results": 10}),
                # Task 13.33: call the non-functional placeholder claude offers next to `--tools`.
                "DO_PLACEHOLDER": ("DeferredToolPlaceholder", {}),
                # Task 14.3: make the "model" run a skill by name.
                "DO_SKILL": ("Skill", {"skill": arg}),
            }.get(kw)
            if call:
                return self._stream(req, body, [("tool_use", call)], "tool_use")
        return self._stream(req, body, [("text", ("ECHO " + clean[-80:]) if tools else "ok")], "end_turn")

    def _stream(self, req: Any, body: dict[str, Any], blocks: list[tuple[str, Any]], stop: str) -> None:
        mid = "msg_" + uuid.uuid4().hex[:20]
        model = body.get("model") or "stub"
        content = [{"type": "text", "text": v} if k == "text" else
                   {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20], "name": v[0], "input": v[1]}
                   for k, v in blocks]
        if not body.get("stream"):
            return req._json(200, {"id": mid, "type": "message", "role": "assistant", "model": model, "content": content,
                                   "stop_reason": stop, "stop_sequence": None,
                                   "usage": {"input_tokens": 10, "output_tokens": 5}})
        req.send_response(200)
        req.send_header("content-type", "text/event-stream")
        req.send_header("cache-control", "no-cache")
        req.send_header("connection", "close")
        req.end_headers()

        def ev(name: str, data: dict[str, Any]) -> None:
            req.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            req.wfile.flush()

        try:
            ev("message_start", {"type": "message_start", "message": {
                "id": mid, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None,
                "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}})
            for i, block in enumerate(content):
                if block["type"] == "text":
                    ev("content_block_start", {"type": "content_block_start", "index": i,
                                               "content_block": {"type": "text", "text": ""}})
                    ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                               "delta": {"type": "text_delta", "text": block["text"]}})
                else:
                    ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                        "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}})
                    ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                        "type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
                ev("content_block_stop", {"type": "content_block_stop", "index": i})
            ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                 "usage": {"output_tokens": 5}})
            ev("message_stop", {"type": "message_stop"})
        except (BrokenPipeError, ConnectionResetError):
            pass


class ProbeServer:
    """Counts every request; answers 200 with a short body."""

    def __init__(self) -> None:
        self.hits = 0
        probe = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                probe.hits += 1
                self.send_response(200)
                self.send_header("content-length", "6")
                self.end_headers()
                self.wfile.write(b"probe\n")

            do_POST = do_HEAD = do_GET

        self.srv = _Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/probe"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


MCP_STUB = r'''
import json, sys
LOG = sys.argv[1]
def send(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    try:
        m = json.loads(line)
    except ValueError:
        continue
    i, meth = m.get("id"), m.get("method")
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": i, "result": {"protocolVersion": m["params"].get("protocolVersion", "2024-11-05"),
              "capabilities": {"tools": {}}, "serverInfo": {"name": "stub", "version": "1"}}})
    elif meth == "tools/list":
        send({"jsonrpc": "2.0", "id": i, "result": {"tools": [
            {"name": n, "description": "returns " + n.upper() + "-7", "inputSchema": {"type": "object", "properties": {}}}
            for n in ("ping", "pong")]}})
    elif meth == "tools/call":
        with open(LOG, "a") as f:
            f.write(m["params"]["name"] + "\n")
        send({"jsonrpc": "2.0", "id": i, "result": {"content": [{"type": "text", "text": m["params"]["name"].upper() + "-7"}]}})
    elif i is not None:
        send({"jsonrpc": "2.0", "id": i, "result": {}})
'''


def write_mcp_stub(directory: Path, python: str) -> tuple[Path, Path]:
    """Write the MCP stub and a claude ``--mcp-config`` file naming it ``stub``; returns (config, call log)."""
    script = directory / "mcp_stub.py"
    script.write_text(MCP_STUB)
    calls = directory / "mcp-calls.log"
    config = directory / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"stub": {"command": python, "args": [str(script), str(calls)]}}}))
    config.chmod(0o600)
    return config, calls
