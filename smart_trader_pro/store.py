"""Comptes, sessions et abonnements de l'application web.

Tout tient dans un fichier SQLite. Les mots de passe sont haches avec
scrypt et un sel par compte ; les jetons de session ne sont stockes que
sous forme d'empreinte. Aucune cle privee, aucun moyen de paiement n'est
conserve : un abonnement est paye par un transfert en crypto que le
client signe lui-meme depuis son portefeuille.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from typing import Dict, List, Optional

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,180}\.[^@\s]{2,24}$")
SESSION_DAYS = 30
ORDER_MINUTES = 60          # delai annonce au client pour payer
ORDER_GRACE_HOURS = 24      # une commande reste confirmable 24 h
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    salt BLOB NOT NULL,
    pw_hash BLOB NOT NULL,
    created INTEGER NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    sub_until INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    plan TEXT NOT NULL,
    days INTEGER NOT NULL,
    units TEXT NOT NULL,
    created INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    tx TEXT UNIQUE,
    paid_at INTEGER
);
CREATE TABLE IF NOT EXISTS exchange_keys (
    user_id INTEGER NOT NULL,
    exchange TEXT NOT NULL,
    key_name TEXT NOT NULL,
    secret BLOB NOT NULL,
    created INTEGER NOT NULL,
    PRIMARY KEY (user_id, exchange)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    product TEXT NOT NULL,
    quote_size TEXT NOT NULL,
    entry TEXT NOT NULL DEFAULT '',
    base_size TEXT NOT NULL DEFAULT '',
    stop TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'en_cours',
    buy_id TEXT NOT NULL DEFAULT '',
    exit_id TEXT NOT NULL DEFAULT '',
    exit_price TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created INTEGER NOT NULL,
    closed INTEGER
);
"""
TRADE_FIELDS = ("entry", "base_size", "stop", "target", "status", "buy_id", "exit_id", "exit_price",
                "note", "closed")


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: str):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript(SCHEMA)
        if path != ":memory:":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    # -- comptes ---------------------------------------------------------
    def create_user(self, email: str, password: str, now: Optional[int] = None) -> dict:
        email = (email or "").strip().lower()
        if not EMAIL_RE.match(email):
            raise ValueError("Adresse e-mail invalide.")
        if not isinstance(password, str) or len(password) < 8 or len(password) > 200:
            raise ValueError("Le mot de passe doit contenir au moins 8 caracteres.")
        salt = secrets.token_bytes(16)
        try:
            with self.lock:
                self.db.execute(
                    "INSERT INTO users (email, salt, pw_hash, created) VALUES (?, ?, ?, ?)",
                    (email, salt, _hash_password(password, salt), now or int(time.time())))
        except sqlite3.IntegrityError:
            raise ValueError("Un compte existe deja avec cette adresse.") from None
        return self.user_by_email(email)

    def set_password(self, email: str, password: str) -> None:
        if not isinstance(password, str) or len(password) < 8 or len(password) > 200:
            raise ValueError("Le mot de passe doit contenir au moins 8 caracteres.")
        salt = secrets.token_bytes(16)
        with self.lock:
            cur = self.db.execute("UPDATE users SET salt = ?, pw_hash = ? WHERE email = ?",
                                  (salt, _hash_password(password, salt), email.strip().lower()))
            if cur.rowcount:
                self.db.execute("DELETE FROM sessions WHERE user_id = "
                                "(SELECT id FROM users WHERE email = ?)", (email.strip().lower(),))
        if not cur.rowcount:
            raise ValueError("Compte introuvable.")

    def _row(self, sql: str, args: tuple) -> Optional[dict]:
        with self.lock:
            r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None

    def user_by_email(self, email: str) -> Optional[dict]:
        return self._row("SELECT * FROM users WHERE email = ?", ((email or "").strip().lower(),))

    def user_by_id(self, user_id: int) -> Optional[dict]:
        return self._row("SELECT * FROM users WHERE id = ?", (user_id,))

    def authenticate(self, email: str, password: str) -> Optional[dict]:
        user = self.user_by_email(email)
        if not isinstance(password, str) or len(password) > 200:
            return None
        # Meme cout que le compte existe ou non, pour ne pas reveler les adresses inscrites.
        salt = user["salt"] if user else b"\0" * 16
        digest = _hash_password(password, salt)
        if user and hmac.compare_digest(digest, user["pw_hash"]):
            return user
        return None

    def set_admin(self, email: str, admin: bool = True) -> None:
        with self.lock:
            cur = self.db.execute("UPDATE users SET is_admin = ? WHERE email = ?",
                                  (1 if admin else 0, (email or "").strip().lower()))
        if not cur.rowcount:
            raise ValueError("Compte introuvable.")

    # -- sessions --------------------------------------------------------
    def new_session(self, user_id: int, now: Optional[int] = None) -> str:
        token = secrets.token_urlsafe(32)
        now = now or int(time.time())
        with self.lock:
            self.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            self.db.execute("INSERT INTO sessions (token_hash, user_id, expires) VALUES (?, ?, ?)",
                            (_token_hash(token), user_id, now + SESSION_DAYS * 86400))
        return token

    def user_by_session(self, token: str, now: Optional[int] = None) -> Optional[dict]:
        if not token or len(token) > 200:
            return None
        return self._row(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires > ?",
            (_token_hash(token), now or int(time.time())))

    def drop_session(self, token: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token or ""),))

    # -- abonnements -----------------------------------------------------
    def grant(self, user_id: int, days: int, now: Optional[int] = None) -> int:
        """Prolonge l'abonnement de `days` jours ; renvoie la nouvelle echeance."""
        now = now or int(time.time())
        with self.lock:
            self.db.execute(
                "UPDATE users SET sub_until = MAX(sub_until, ?) + ? WHERE id = ?",
                (now, int(days) * 86400, user_id))
            r = self.db.execute("SELECT sub_until FROM users WHERE id = ?", (user_id,)).fetchone()
        if r is None:
            raise ValueError("Compte introuvable.")
        return r["sub_until"]

    def create_order(self, user_id: int, plan: str, days: int, price_units: int,
                     step_units: int, now: Optional[int] = None) -> dict:
        """Commande au montant unique : prix + quelques dix-milliemes tires au hasard.

        Ce montant exact sert a relier le paiement a la commande : deux commandes
        en attente n'ont jamais le meme.
        """
        now = now or int(time.time())
        since = now - ORDER_GRACE_HOURS * 3600
        with self.lock:
            taken = {r["units"] for r in self.db.execute(
                "SELECT units FROM orders WHERE status = 'pending' AND created > ?", (since,))}
            self.db.execute("UPDATE orders SET status = 'expired' "
                            "WHERE status = 'pending' AND (created <= ? OR user_id = ?)",
                            (since, user_id))
            for _ in range(200):
                units = str(price_units + secrets.randbelow(9999) * step_units + step_units)
                if units not in taken:
                    break
            else:
                raise RuntimeError("Trop de commandes en attente, reessayez dans un instant.")
            cur = self.db.execute(
                "INSERT INTO orders (user_id, plan, days, units, created) VALUES (?, ?, ?, ?, ?)",
                (user_id, plan, int(days), units, now))
            order_id = cur.lastrowid
        return self.order(order_id)

    def order(self, order_id: int) -> Optional[dict]:
        return self._row("SELECT * FROM orders WHERE id = ?", (order_id,))

    def pending_order(self, user_id: int, now: Optional[int] = None) -> Optional[dict]:
        now = now or int(time.time())
        return self._row(
            "SELECT * FROM orders WHERE user_id = ? AND status = 'pending' AND created > ? "
            "ORDER BY id DESC LIMIT 1", (user_id, now - ORDER_GRACE_HOURS * 3600))

    def tx_used(self, tx: str) -> bool:
        return self._row("SELECT id FROM orders WHERE tx = ?", (tx.lower(),)) is not None

    def mark_paid(self, order_id: int, tx: str, now: Optional[int] = None) -> int:
        """Valide la commande et prolonge l'abonnement, une seule fois par transaction."""
        now = now or int(time.time())
        with self.lock:
            o = self.db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if o is None or o["status"] != "pending":
                raise ValueError("Cette commande n'est plus en attente.")
            self.db.execute("BEGIN IMMEDIATE")       # commande et abonnement : tout ou rien
            try:
                self.db.execute("UPDATE orders SET status = 'paid', tx = ?, paid_at = ? WHERE id = ?",
                                (tx.lower(), now, order_id))
                self.db.execute("UPDATE users SET sub_until = MAX(sub_until, ?) + ? WHERE id = ?",
                                (now, o["days"] * 86400, o["user_id"]))
                self.db.execute("COMMIT")
            except sqlite3.IntegrityError:
                self.db.execute("ROLLBACK")
                raise ValueError("Cette transaction a deja servi a payer une commande.") from None
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            r = self.db.execute("SELECT sub_until FROM users WHERE id = ?", (o["user_id"],)).fetchone()
        return r["sub_until"]

    # -- cles d'exchange (chiffrees par l'appelant) et positions -----------
    def save_exchange_key(self, user_id: int, exchange: str, key_name: str, secret: bytes) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO exchange_keys (user_id, exchange, key_name, secret, created) "
                            "VALUES (?, ?, ?, ?, ?)", (user_id, exchange, key_name, secret, int(time.time())))

    def exchange_key(self, user_id: int, exchange: str) -> Optional[dict]:
        return self._row("SELECT * FROM exchange_keys WHERE user_id = ? AND exchange = ?", (user_id, exchange))

    def delete_exchange_key(self, user_id: int, exchange: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM exchange_keys WHERE user_id = ? AND exchange = ?", (user_id, exchange))

    def add_trade(self, user_id: int, exchange: str, symbol: str, product: str, quote_size: str,
                  stop: str, target: str) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO trades (user_id, exchange, symbol, product, quote_size, stop, target, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, exchange, symbol, product, quote_size, stop, target, int(time.time())))
            return cur.lastrowid

    def update_trade(self, trade_id: int, **fields) -> None:
        keys = [k for k in fields if k in TRADE_FIELDS]
        if not keys:
            return
        with self.lock:
            self.db.execute(f"UPDATE trades SET {', '.join(k + ' = ?' for k in keys)} WHERE id = ?",
                            [fields[k] for k in keys] + [trade_id])

    def trade(self, trade_id: int) -> Optional[dict]:
        return self._row("SELECT * FROM trades WHERE id = ?", (trade_id,))

    def trades(self, user_id: int, limit: int = 20, statuses: tuple = ()) -> List[dict]:
        sql, args = "SELECT * FROM trades WHERE user_id = ?", [user_id]
        if statuses:
            sql += f" AND status IN ({', '.join('?' * len(statuses))})"
            args += list(statuses)
        with self.lock:
            return [dict(r) for r in self.db.execute(sql + " ORDER BY id DESC LIMIT ?", args + [int(limit)])]

    def spent_since(self, user_id: int, since: int):
        """Total engage en achats depuis `since` (les ordres en echec ne comptent pas)."""
        from decimal import Decimal
        with self.lock:
            rows = self.db.execute("SELECT quote_size FROM trades WHERE user_id = ? AND created > ? "
                                   "AND status != 'echec'", (user_id, since)).fetchall()
        return sum((Decimal(r["quote_size"]) for r in rows), Decimal(0))

    # -- administration --------------------------------------------------
    def overview(self, now: Optional[int] = None) -> Dict[str, object]:
        now = now or int(time.time())
        with self.lock:
            users = self.db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            active = self.db.execute("SELECT COUNT(*) c FROM users WHERE sub_until > ?",
                                     (now,)).fetchone()["c"]
            paid = [dict(r) for r in self.db.execute(
                "SELECT o.id, o.plan, o.units, o.paid_at, o.tx, u.email FROM orders o "
                "JOIN users u ON u.id = o.user_id WHERE o.status = 'paid' "
                "ORDER BY o.paid_at DESC")]
            recent = [dict(r) for r in self.db.execute(
                "SELECT email, created, sub_until, is_admin FROM users ORDER BY id DESC LIMIT 50")]
        return {"users": users, "active": active, "paid_orders": len(paid),
                "revenue_units": str(sum(int(p["units"]) for p in paid)),
                "orders": paid[:50], "recent_users": recent}

    def users(self) -> List[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM users ORDER BY id")]
