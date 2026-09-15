"""THROWAWAY stub Anthropic endpoint for the #660 init benchmark.

Delete with the branch. Not a test, not collected by the suite -- a
standalone helper the benchmark runner starts in-process.

Why it exists: the benchmark has to measure a REAL `claude` start, and a real
start makes an API call. Two ways to avoid needing a key, one of which does
not work:

  * A deliberately invalid key. Measured 2026-09-15 on macOS: a 401 costs
    187 seconds of retry backoff before the process exits. Useless as a
    timer, and the backoff is not a constant we control.
  * This: point ANTHROPIC_BASE_URL at a local server that answers the SSE
    shape immediately. The run completes normally, offline, in about a
    second, with no key, no cost and no network variance in the number.

The server records the wall clock of every request it receives, which is the
measurement that matters: time from process launch to first API call covers
everything Claude Code does at startup -- plugin load, SessionStart hook,
context ingestion -- and that is NOT what the hook's own durationMs reports.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _sse(event, data):
    return ("event: " + event + "\ndata: " + json.dumps(data) + "\n\n").encode()


_MESSAGE = {
    "id": "msg_stub",
    "type": "message",
    "role": "assistant",
    "model": "stub",
    "content": [],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


class StubServer:
    """A local Anthropic-shaped endpoint that timestamps what it receives."""

    def __init__(self):
        self.hits = []
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _record(self):
                with stub._lock:
                    stub.hits.append((time.time(), self.command, self.path))

            def do_GET(self):
                self._record()
                body = b"{}"
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self._record()
                length = int(self.headers.get("content-length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("connection", "close")
                self.end_headers()
                w = self.wfile
                w.write(_sse("message_start",
                             {"type": "message_start", "message": _MESSAGE}))
                w.write(_sse("content_block_start",
                             {"type": "content_block_start", "index": 0,
                              "content_block": {"type": "text", "text": ""}}))
                w.write(_sse("content_block_delta",
                             {"type": "content_block_delta", "index": 0,
                              "delta": {"type": "text_delta", "text": "ok"}}))
                w.write(_sse("content_block_stop",
                             {"type": "content_block_stop", "index": 0}))
                w.write(_sse("message_delta",
                             {"type": "message_delta",
                              "delta": {"stop_reason": "end_turn",
                                        "stop_sequence": None},
                              "usage": {"output_tokens": 1}}))
                w.write(_sse("message_stop", {"type": "message_stop"}))
                w.flush()

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()

    def first_hit_after(self, t0):
        """First request received at or after t0, or None.

        Filtering by timestamp rather than clearing a shared log is what keeps
        one run's late background request out of the next run's number -- the
        first draft of this harness truncated a shared file and read a
        +0.01s first-request time that belonged to the previous run.
        """
        with self._lock:
            after = [h for h in self.hits if h[0] >= t0]
        return min(after)[0] if after else None
