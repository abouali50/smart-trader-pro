"""Tableau de bord web en lecture seule.

Sert une page et deux fichiers JSON produits par le scanner. Aucune
saisie n'est acceptee, aucune action n'est possible depuis la page.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = Path(__file__).with_name("dashboard.html")


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def make_handler(status_file: str, signals_file: str, wide_file: str = None,
                 radar_file: str = None, alpha_file: str = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SmartTraderPro"
        sys_version = ""

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; connect-src 'self'; img-src data:")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/status":
                data = {"status": _read_json(status_file, None),
                        "signals": _read_json(signals_file, [])}
                if wide_file:
                    data["wide"] = _read_json(wide_file, None)
                if radar_file:
                    data["radar"] = _read_json(radar_file, None)
                if alpha_file:
                    data["alpha"] = _read_json(alpha_file, None)
                self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            else:
                self._send(404, b"Introuvable", "text/plain; charset=utf-8")

        do_HEAD = do_GET

        def log_message(self, fmt, *args) -> None:   # journal silencieux
            pass

    return Handler


def make_server(status_file: str, signals_file: str, host: str = "0.0.0.0",
                port: int = 8765, wide_file: str = None,
                radar_file: str = None, alpha_file: str = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(status_file, signals_file, wide_file,
                                                          radar_file, alpha_file))


def serve(status_file: str, signals_file: str, host: str = "0.0.0.0",
          port: int = 8765, wide_file: str = None, radar_file: str = None,
          alpha_file: str = None) -> None:
    make_server(status_file, signals_file, host, port, wide_file, radar_file,
                alpha_file).serve_forever()
