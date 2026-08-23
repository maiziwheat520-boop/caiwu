from __future__ import annotations

import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


SITE_ROOT = Path(os.environ.get("SITE_ROOT", "/site")).resolve()
PORT = int(os.environ.get("PORT", "8080"))


class PreviewHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(SITE_ROOT), **kwargs)

    def _use_spa_fallback(self) -> None:
        request_path = urlsplit(self.path).path
        if request_path == "/healthz":
            return
        relative_path = request_path.lstrip("/")
        target = (SITE_ROOT / relative_path).resolve()
        if SITE_ROOT not in target.parents and target != SITE_ROOT:
            return
        if not target.exists() and "." not in Path(relative_path).name:
            self.path = "/index.html"

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/healthz":
            payload = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._use_spa_fallback()
        super().do_GET()

    def do_HEAD(self) -> None:
        self._use_spa_fallback()
        super().do_HEAD()

    def end_headers(self) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if urlsplit(self.path).path.startswith("/assets/"):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} {format % args}", flush=True)


class PreviewServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    if not (SITE_ROOT / "index.html").is_file():
        raise SystemExit(f"Missing built site: {SITE_ROOT / 'index.html'}")
    with PreviewServer(("0.0.0.0", PORT), PreviewHandler) as server:
        print(f"Serving {SITE_ROOT} on port {PORT}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
