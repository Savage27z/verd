"""Tiny web server (stdlib, runs in a thread next to the bot) that serves lead website previews."""
import logging
import os
import re
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import mockup, store

log = logging.getLogger("odify.web")

TOKEN_PATH = re.compile(r"^/m/([A-Za-z0-9_-]{6,64})/?$")
# Link-preview fetchers (WhatsApp, Telegram, Facebook, Slack…) and crawlers aren't real people.
BOT_UA = re.compile(r"whatsapp|telegrambot|facebookexternalhit|facebot|twitterbot|slackbot|discordbot|"
                    r"linkedinbot|skypeuripreview|bot\b|crawler|spider|preview", re.IGNORECASE)


def public_base_url() -> str:
    """Where previews are reachable: PUBLIC_URL, else Railway's generated domain."""
    url = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if url:
        return url if "://" in url else f"https://{url}"
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return f"https://{domain}" if domain else ""


def preview_url(token: str) -> str:
    base = public_base_url()
    return f"{base}/m/{token}" if base else ""


def brand() -> tuple[str, str]:
    return os.getenv("MOCKUP_BRAND", "Odify").strip() or "Odify", os.getenv("MOCKUP_BRAND_URL", "").strip()


def render_token(token: str) -> str | None:
    found = store.mockup_by_token(token)
    if not found:
        return None
    lead, search = found
    name, url = brand()
    return mockup.render(lead, search["options"].get("language") or "en", name, url,
                         fallback_city=search.get("location") or "")


def make_handler(on_view: Callable[[dict, int], None] | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "odify"
        sys_version = ""

        def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; frame-src https://maps.google.com "
                             "https://www.google.com; img-src data: https://*.googleusercontent.com; base-uri 'none'; form-action 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/health"):
                return self._send(200, "ok", "text/plain; charset=utf-8")
            if path == "/robots.txt":
                return self._send(200, "User-agent: *\nDisallow: /\n", "text/plain; charset=utf-8")
            m = TOKEN_PATH.match(path)
            page = render_token(m.group(1)) if m else None
            if page is None:
                return self._send(404, "Not found", "text/plain; charset=utf-8")
            self._count_view(m.group(1))
            self._send(200, page)

        def _count_view(self, token: str):
            if self.command == "GET" and not BOT_UA.search(self.headers.get("User-Agent", "")):
                views, due = store.record_view(token)
                if due and on_view:
                    found = store.mockup_by_token(token)
                    if found:
                        try:
                            on_view(found[0], views)
                        except Exception:
                            log.exception("View notification failed")

        do_HEAD = do_GET  # noqa: N815

        def log_message(self, fmt, *args):  # quiet: one line per preview view is logged by on_view
            pass

    return Handler


def start(on_view: Callable[[dict, int], None] | None = None, port: int | None = None) -> ThreadingHTTPServer:
    port = int(os.getenv("PORT", "8080")) if port is None else port
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(on_view))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="odify-web", daemon=True).start()
    log.info("Preview server on port %d (public: %s)", server.server_address[1], public_base_url() or "not set")
    return server
