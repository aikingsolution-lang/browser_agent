"""
Reusable local HTTP test fixture server.

Starts a ThreadingHTTPServer on 127.0.0.1 on an ephemeral port (port 0)
serving a deterministic HTML page containing:
- <a href="/next" id="a">Next Link</a>
- <input type="text" id="q" name="q">
- <button id="b" onclick="document.title = 'clicked'">Click Me</button>
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

FIXTURE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Fixture Initial</title>
</head>
<body>
    <h1>Local Test Fixture</h1>
    <a href="/next" id="a">Next Link</a>
    <input type="text" id="q" name="q" placeholder="Search query">
    <button id="b" onclick="document.title = 'clicked'">Click Me</button>
</body>
</html>
"""


class _DeterministicFixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = FIXTURE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Suppress noisy HTTP request logging in test outputs
        pass


class LocalFixtureServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.requested_port = port
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.port: int = 0

    def start(self) -> str:
        self.server = ThreadingHTTPServer((self.host, self.requested_port), _DeterministicFixtureHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.url

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=2.0)
            self.thread = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
