#!/usr/bin/env python3
"""flaky_http_server.py — serves a fixed payload over HTTP/1.1, honoring
Range requests correctly, but DELIBERATELY drops the connection partway
through the first request (advertises the full Content-Length, writes only
a fraction of the body, then hard-closes the socket) — reproducing the exact
class of failure a real macOS stage-runtime.sh run hit against the 888 MiB
machine image: "curl: (18) transfer closed with N bytes remaining to read".

Every request AFTER the first (including the Range-resumed retry
fetch_verified's curl -C - issues) is served in full and correctly.

Usage: flaky_http_server.py <payload-file> [--always-truncate]
Prints exactly one line, "PORT <n>", once listening (the caller reads this
to learn which ephemeral port got bound) and one "REQUEST <method> <path>
range=<value-or->" line per request handled, both flushed immediately.

--always-truncate: cut EVERY request, not just the first — for testing the
bounded-retries-then-fail path (a connection that never recovers).
"""
import http.server
import socketserver
import sys
import threading

PAYLOAD_PATH = sys.argv[1]
ALWAYS_TRUNCATE = "--always-truncate" in sys.argv[2:]

with open(PAYLOAD_PATH, "rb") as f:
    PAYLOAD = f.read()

_lock = threading.Lock()
_first_request_done = [False]


class FlakyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        range_header = self.headers.get("Range", "-")
        print(f"REQUEST {self.command} {self.path} range={range_header}", flush=True)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        total = len(PAYLOAD)
        range_header = self.headers.get("Range")
        start = 0
        if range_header and range_header.startswith("bytes="):
            start = int(range_header.split("=", 1)[1].split("-", 1)[0])

        with _lock:
            should_cut = ALWAYS_TRUNCATE or not _first_request_done[0]
            _first_request_done[0] = True

        body = PAYLOAD[start:]
        if range_header:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

        if should_cut:
            # Advertise the full length, deliver roughly a third of it, then
            # hard-close — a short body against a promised Content-Length is
            # exactly what makes curl report "transfer closed with N bytes
            # remaining to read" instead of a clean, if wrong-sized, response.
            cut_at = max(1, len(body) // 3)
            try:
                self.wfile.write(body[:cut_at])
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            try:
                self.connection.shutdown(1)  # SHUT_WR
            except OSError:
                pass
            return

        self.wfile.write(body)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    srv = Server(("127.0.0.1", 0), FlakyHandler)
    print(f"PORT {srv.server_address[1]}", flush=True)
    srv.serve_forever()
