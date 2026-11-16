"""Application web : tableau de bord, comptes, abonnements, ecran de trading.

Le serveur lit les fichiers ecrits par le scanner et ne passe aucun
ordre. Sans objet `app`, il sert seulement la page et l'etat (lecture
seule). Avec un objet `App`, il ajoute les comptes, les abonnements
payes en crypto, les bougies pour le graphique et l'espace admin.

Aucune cle d'exchange ni cle privee n'est stockee : pour trader, le
client est envoye vers le DEX, ou il signe avec son propre portefeuille.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qs, urlsplit

from . import chain as chainmod
from .data import INTERVAL_MS

HERE = Path(__file__).parent
PAGE = HERE / "dashboard.html"          # ancienne page, gardee sous /classique
APP_PAGE = HERE / "app.html"
SIGNALS_SHOWN = 40       # la page n'affiche que les dernieres alertes
MAX_BODY = 8192
COOKIE = "stp_session"
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")
ALPHA_ID_RE = re.compile(r"^ALPHA_\d{1,8}$")
CHART_INTERVALS = ("15m", "1h", "4h", "1d")
# Champs reserves aux abonnes quand le paywall est actif.
LOCKED_KEYS = ("stop", "tp1", "tp2", "trigger", "levels", "reasons", "missing", "families",
               "info", "news", "exchanges", "where_to_trade", "token")
FREE_ROWS = 5
ICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#ff8a3d"/><stop offset="1" stop-color="#35d08a"/>'
        '</linearGradient></defs><rect width="512" height="512" rx="112" fill="#0b1017"/>'
        '<path d="M96 344l96-104 72 64 152-168" fill="none" stroke="url(#g)" '
        'stroke-width="44" stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="416" cy="136" r="34" fill="#35d08a"/></svg>')
MANIFEST = {
    "name": "Smart Trader Pro", "short_name": "Trader Pro", "start_url": "/", "display": "standalone",
    "background_color": "#0b1017", "theme_color": "#0b1017", "lang": "fr",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}],
}


def _read_json(path: Optional[str], default):
    if not path:
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


class ApiError(Exception):
    def __init__(self, code: int, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


class App:
    """Comptes, abonnements et graphique. `cfg` est la configuration du scanner."""

    def __init__(self, cfg: dict, store, chain=None, md=None, alpha_data=None):
        self.cfg, self.store, self.chain, self.md, self.alpha = cfg, store, chain, md, alpha_data
        self.hits: Dict[str, deque] = {}
        self.cache: Dict[tuple, tuple] = {}

    # -- reglages --------------------------------------------------------
    @property
    def pay_ready(self) -> bool:
        return chainmod.is_address(self.cfg.get("pay_address")) and self.chain is not None

    def plans(self) -> list:
        out = []
        for key, p in (self.cfg.get("plans") or {}).items():
            try:
                out.append({"id": str(key), "label": str(p["label"]), "days": int(p["days"]),
                            "price": float(p["price"])})
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(out, key=lambda p: p["days"])

    def dex_url(self) -> str:
        url = str(self.cfg.get("dex_url") or "")
        return url if url.startswith("https://") and "{address}" in url else ""

    # -- limites ---------------------------------------------------------
    def limit(self, ip: str, bucket: str, count: int, seconds: int) -> None:
        now = time.time()
        q = self.hits.setdefault(f"{bucket}:{ip}", deque())
        while q and q[0] < now - seconds:
            q.popleft()
        if len(q) >= count:
            raise ApiError(429, "Trop de tentatives. Reessayez dans quelques minutes.")
        q.append(now)
        if len(self.hits) > 5000:                    # memoire bornee
            self.hits = {k: v for k, v in self.hits.items() if v and v[-1] > now - 3600}

    # -- vues ------------------------------------------------------------
    def access(self, user: Optional[dict]) -> bool:
        """Vrai si le visiteur voit tout le contenu."""
        if not self.cfg.get("paywall"):
            return True
        return bool(user and (user["is_admin"] or user["sub_until"] > time.time()))

    def me(self, user: Optional[dict]) -> dict:
        decimals = int(self.cfg.get("pay_decimals", 18))
        out = {
            "user": None, "paywall": bool(self.cfg.get("paywall")), "access": self.access(user),
            "plans": self.plans(), "dex_url": self.dex_url(),
            "pay": {"ready": self.pay_ready, "symbol": self.cfg.get("pay_symbol", "USDT"),
                    "network": "BNB Chain (BEP-20)", "chain_id": chainmod.BSC_CHAIN_ID,
                    "token": self.cfg.get("pay_token"), "decimals": decimals,
                    "address": self.cfg.get("pay_address") if self.pay_ready else ""},
            "capital": self.cfg.get("capital"), "risk_pct": self.cfg.get("risk_pct"),
        }
        if user:
            out["user"] = {"email": user["email"], "admin": bool(user["is_admin"]),
                           "sub_until": user["sub_until"] * 1000,
                           "active": user["sub_until"] > time.time()}
            order = self.store.pending_order(user["id"])
            out["order"] = self.order_view(order) if order else None
        return out

    def order_view(self, o: dict) -> dict:
        decimals = int(self.cfg.get("pay_decimals", 18))
        from .store import ORDER_MINUTES
        return {"id": o["id"], "plan": o["plan"], "days": o["days"], "units": o["units"],
                "amount": chainmod.format_units(o["units"], decimals),
                "status": o["status"], "created": o["created"] * 1000,
                "expires": (o["created"] + ORDER_MINUTES * 60) * 1000}

    def redact(self, data: dict) -> dict:
        """Version gratuite : les lignes restent visibles, les niveaux sont masques."""
        def strip(row):
            if not isinstance(row, dict):
                return row
            out = {k: v for k, v in row.items() if k not in LOCKED_KEYS}
            out["locked"] = True
            return out

        def rows(block, key="rows"):
            if isinstance(block, dict) and isinstance(block.get(key), list):
                block[key] = [strip(r) for r in block[key][:FREE_ROWS]]

        for name in ("status", "radar", "alpha"):
            rows(data.get(name))
            if isinstance(data.get(name), dict):
                rows(data[name].get("pressure"))
        if isinstance(data.get("status"), dict):
            data["status"].pop("news", None)
        rows(data.get("wide"))
        rows(data.get("wide"), "alerts")
        data["signals"] = [strip(s) for s in (data.get("signals") or [])[:FREE_ROWS]]
        data["locked"] = True
        return data

    # -- actions ---------------------------------------------------------
    def register(self, body: dict, ip: str) -> dict:
        self.limit(ip, "auth", 10, 600)
        try:
            return self.store.create_user(body.get("email"), body.get("password"))
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None

    def login(self, body: dict, ip: str) -> dict:
        self.limit(ip, "auth", 10, 600)
        user = self.store.authenticate(body.get("email"), body.get("password"))
        if not user:
            raise ApiError(401, "Adresse e-mail ou mot de passe incorrect.")
        return user

    def new_order(self, user: dict, body: dict, ip: str) -> dict:
        self.limit(ip, "order", 20, 600)
        if not self.pay_ready:
            raise ApiError(409, "Les abonnements ne sont pas encore ouverts.")
        plan = next((p for p in self.plans() if p["id"] == body.get("plan")), None)
        if plan is None or plan["price"] <= 0:
            raise ApiError(400, "Formule inconnue.")
        decimals = int(self.cfg.get("pay_decimals", 18))
        order = self.store.create_order(user["id"], plan["id"], plan["days"],
                                        chainmod.to_units(plan["price"], decimals),
                                        10 ** (decimals - 4))
        return self.order_view(order)

    def confirm(self, user: dict, body: dict, ip: str) -> dict:
        self.limit(ip, "confirm", 60, 600)
        if not self.pay_ready:
            raise ApiError(409, "Les abonnements ne sont pas encore ouverts.")
        order = self.store.pending_order(user["id"])
        if order is None or order["id"] != body.get("id"):
            raise ApiError(404, "Aucune commande en attente. Creez-en une nouvelle.")
        tx = str(body.get("tx") or "").strip()
        if chainmod.TX_RE.match(tx) and self.store.tx_used(tx):
            raise ApiError(409, "Cette transaction a deja servi a payer une commande.")
        try:
            self.chain.verify_transfer(tx, self.cfg["pay_token"], self.cfg["pay_address"],
                                       int(order["units"]), order["created"] - 120,
                                       int(self.cfg.get("pay_confirmations", 5)))
        except chainmod.Pending as exc:
            raise ApiError(202, str(exc), pending=True) from None
        except chainmod.Rejected as exc:
            raise ApiError(400, str(exc)) from None
        try:
            until = self.store.mark_paid(order["id"], tx)
        except ValueError as exc:
            raise ApiError(409, str(exc)) from None
        return {"ok": True, "sub_until": until * 1000}

    def klines(self, query: dict, ip: str) -> dict:
        self.limit(ip, "klines", 90, 60)
        src = (query.get("src") or ["spot"])[0]
        interval = (query.get("interval") or ["15m"])[0]
        symbol = (query.get("symbol") or [""])[0].upper()
        if interval not in CHART_INTERVALS or interval not in INTERVAL_MS:
            raise ApiError(400, "Intervalle non gere.")
        key = (src, symbol, interval)
        hit = self.cache.get(key)
        if hit and hit[0] > time.time() - 45:
            return hit[1]
        try:
            if src == "alpha":
                if self.alpha is None or not ALPHA_ID_RE.match(symbol):
                    raise ApiError(400, "Jeton Alpha inconnu.")
                c = self.alpha.klines(symbol, interval, 200)
            else:
                if self.md is None or not SYMBOL_RE.match(symbol):
                    raise ApiError(400, "Paire inconnue.")
                c = self.md.klines(symbol, interval, 200)
        except ApiError:
            raise
        except Exception:
            raise ApiError(502, "Bougies indisponibles pour cette crypto.") from None
        out = {"symbol": c.symbol, "interval": interval, "t": c.ts, "o": c.open, "h": c.high,
               "l": c.low, "c": c.close, "v": c.volume}
        if len(self.cache) > 400:
            self.cache.clear()
        self.cache[key] = (time.time(), out)
        return out

    def admin(self, user: dict) -> dict:
        decimals = int(self.cfg.get("pay_decimals", 18))
        ov = self.store.overview()
        ov["revenue"] = chainmod.format_units(ov.pop("revenue_units"), decimals, 2)
        for o in ov["orders"]:
            o["amount"] = chainmod.format_units(o.pop("units"), decimals)
            o["paid_at"] = (o["paid_at"] or 0) * 1000
        for u in ov["recent_users"]:
            u["created"] *= 1000
            u["sub_until"] *= 1000
        ov["pay_ready"], ov["paywall"], ov["dex_url"] = self.pay_ready, bool(self.cfg.get("paywall")), self.dex_url()
        return ov

    def admin_grant(self, body: dict) -> dict:
        target = self.store.user_by_email(body.get("email"))
        try:
            days = int(body.get("days"))
        except (TypeError, ValueError):
            raise ApiError(400, "Nombre de jours invalide.") from None
        if target is None or not 1 <= days <= 3660:
            raise ApiError(400, "Compte introuvable ou duree invalide.")
        return {"ok": True, "sub_until": self.store.grant(target["id"], days) * 1000}


def make_handler(status_file: str, signals_file: str, wide_file: str = None,
                 radar_file: str = None, alpha_file: str = None, tracking_file: str = None,
                 app: Optional[App] = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SmartTraderPro"
        sys_version = ""

        # -- reponses ----------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str, cookie: str = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; connect-src 'self'; "
                             "img-src 'self' data:; manifest-src 'self'; "
                             "base-uri 'none'; form-action 'self'")
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, data, cookie: str = None) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", cookie)

        # -- contexte de la requete --------------------------------------
        def _ip(self) -> str:
            peer = self.client_address[0]
            fwd = self.headers.get("X-Forwarded-For")
            if fwd and peer in ("127.0.0.1", "::1"):     # derriere le proxy local
                return fwd.split(",")[0].strip()[:64]
            return peer

        def _token(self) -> str:
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE:
                    return value
            return ""

        def _user(self) -> Optional[dict]:
            return app.store.user_by_session(self._token()) if app else None

        def _cookie(self, token: str, max_age: int) -> str:
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            return f"{COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"

        def _body(self) -> dict:
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != (self.headers.get("Host") or ""):
                raise ApiError(403, "Origine refusee.")
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                raise ApiError(415, "Format attendu : JSON.")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                raise ApiError(413, "Requete trop longue.")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise ApiError(400, "JSON invalide.") from None
            if not isinstance(body, dict):
                raise ApiError(400, "JSON invalide.")
            return body

        # -- routes ------------------------------------------------------
        def _status(self) -> dict:
            signals = _read_json(signals_file, [])
            data = {"status": _read_json(status_file, None),
                    "signals": signals[:SIGNALS_SHOWN] if isinstance(signals, list) else []}
            if tracking_file:
                data["tracking"] = _read_json(tracking_file, None)
            if wide_file:
                data["wide"] = _read_json(wide_file, None)
            if radar_file:
                data["radar"] = _read_json(radar_file, None)
            if alpha_file:
                data["alpha"] = _read_json(alpha_file, None)
            if app and not app.access(self._user()):
                data = app.redact(data)
            return data

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            path = url.path
            try:
                if path == "/":
                    page = APP_PAGE if (app and APP_PAGE.exists()) else PAGE
                    self._send(200, page.read_bytes(), "text/html; charset=utf-8")
                elif path == "/classique":
                    self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                elif path == "/icon.svg":
                    self._send(200, ICON.encode("utf-8"), "image/svg+xml")
                elif path == "/manifest.webmanifest":
                    self._send(200, json.dumps(MANIFEST).encode("utf-8"),
                               "application/manifest+json; charset=utf-8")
                elif path == "/api/status":
                    self._json(200, self._status())
                elif app and path == "/api/me":
                    self._json(200, app.me(self._user()))
                elif app and path == "/api/klines":
                    if not app.access(self._user()):
                        raise ApiError(402, "Reserve aux abonnes.")
                    self._json(200, app.klines(parse_qs(url.query), self._ip()))
                elif app and path == "/api/admin":
                    user = self._user()
                    if not user or not user["is_admin"]:
                        raise ApiError(403, "Reserve a l'administrateur.")
                    self._json(200, app.admin(user))
                else:
                    self._send(404, b"Introuvable", "text/plain; charset=utf-8")
            except ApiError as exc:
                self._json(exc.code, {"error": exc.message, **exc.extra})

        do_HEAD = do_GET

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if not app:
                self._send(501, b"Lecture seule", "text/plain; charset=utf-8")
                return
            if not path.startswith("/api/"):
                self._send(404, b"Introuvable", "text/plain; charset=utf-8")
                return
            try:
                body, ip = self._body(), self._ip()
                if path in ("/api/register", "/api/login"):
                    user = app.register(body, ip) if path == "/api/register" else app.login(body, ip)
                    token = app.store.new_session(user["id"])
                    self._json(200, app.me(user), self._cookie(token, 30 * 86400))
                    return
                if path == "/api/logout":
                    app.store.drop_session(self._token())
                    self._json(200, {"ok": True}, self._cookie("", 0))
                    return
                user = self._user()
                if not user:
                    raise ApiError(401, "Connectez-vous d'abord.")
                if path == "/api/orders":
                    self._json(200, app.new_order(user, body, ip))
                elif path == "/api/orders/confirm":
                    self._json(200, app.confirm(user, body, ip))
                elif path == "/api/admin/grant":
                    if not user["is_admin"]:
                        raise ApiError(403, "Reserve a l'administrateur.")
                    self._json(200, app.admin_grant(body))
                else:
                    self._send(404, b"Introuvable", "text/plain; charset=utf-8")
            except ApiError as exc:
                self._json(exc.code, {"error": exc.message, **exc.extra})

        def log_message(self, fmt, *args) -> None:   # journal silencieux
            pass

    return Handler


def make_server(status_file: str, signals_file: str, host: str = "0.0.0.0",
                port: int = 8765, wide_file: str = None,
                radar_file: str = None, alpha_file: str = None,
                tracking_file: str = None, app: Optional[App] = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(status_file, signals_file, wide_file,
                                                          radar_file, alpha_file, tracking_file, app))


def serve(status_file: str, signals_file: str, host: str = "0.0.0.0",
          port: int = 8765, wide_file: str = None, radar_file: str = None,
          alpha_file: str = None, tracking_file: str = None, app: Optional[App] = None) -> None:
    make_server(status_file, signals_file, host, port, wide_file, radar_file,
                alpha_file, tracking_file, app).serve_forever()
