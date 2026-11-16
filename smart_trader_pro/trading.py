"""Trading en un clic depuis l'application, avec garde-fous.

Un ordre n'est jamais passe automatiquement : le client prepare l'ordre,
lit le recapitulatif, puis confirme. Chaque achat est borne (montant par
ordre, montant par jour, ecart maximal entre le prix du signal et le
prix reel) et suivi d'une vente a deux branches : objectif ou stop.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import re
import threading
import time
from decimal import Decimal
from typing import Dict, Optional

from .exchange import QUOTES, Coinbase, ExchangeError, Vault, check_key, dec, floor_to, fmt

BASE_RE = re.compile(r"^[A-Z0-9]{2,12}$")
OPEN = ("ouvert", "sans_protection")


class TradeError(Exception):
    """Refus affichable au client."""


class Trader:
    def __init__(self, cfg: dict, store, vault: Vault, client_factory=Coinbase):
        self.cfg, self.store, self.vault, self.client_factory = cfg, store, vault, client_factory
        self.locks: Dict[int, threading.Lock] = {}
        self.guard = threading.Lock()

    # -- droits ----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("trading")) and self.vault.ready

    def allowed(self, user: Optional[dict]) -> bool:
        if not self.enabled or not user:
            return False
        if user["is_admin"]:
            return True
        return bool(self.cfg.get("trading_clients")) and user["sub_until"] > time.time()

    def _lock(self, user_id: int) -> threading.Lock:
        with self.guard:
            return self.locks.setdefault(user_id, threading.Lock())

    def _require(self, user: Optional[dict]) -> None:
        if not self.allowed(user):
            raise TradeError("Le trading depuis l'application n'est pas ouvert pour ce compte.")

    def _client(self, user: dict):
        row = self.store.exchange_key(user["id"], "coinbase")
        if not row:
            raise TradeError("Connecte d'abord ton compte Coinbase dans « Compte ».")
        try:
            return self.client_factory(row["key_name"], self.vault.open(row["secret"]))
        except ExchangeError as exc:
            raise TradeError(str(exc)) from None

    # -- compte Coinbase -------------------------------------------------
    def connect(self, user: dict, key_name, private_key) -> dict:
        self._require(user)
        if not isinstance(key_name, str) or not isinstance(private_key, str) or len(private_key) > 4000:
            raise TradeError("Cle invalide.")
        try:
            client = self.client_factory(key_name, private_key)
            check_key(client)
        except ExchangeError as exc:
            raise TradeError(str(exc)) from None
        self.store.save_exchange_key(user["id"], "coinbase", client.key_name, self.vault.seal(private_key))
        return self.status(user)

    def disconnect(self, user: dict) -> dict:
        self.store.delete_exchange_key(user["id"], "coinbase")
        return self.status(user)

    def limits(self) -> dict:
        return {"min": float(self.cfg.get("trade_min", 5)), "max_order": float(self.cfg.get("trade_max_order", 100)),
                "max_day": float(self.cfg.get("trade_max_day", 300)),
                "max_drift_pct": float(self.cfg.get("trade_max_drift_pct", 3))}

    def status(self, user: Optional[dict]) -> dict:
        out = {"enabled": self.enabled, "allowed": self.allowed(user), "connected": False,
               "limits": self.limits(), "balances": {}, "trades": [], "key": ""}
        if not out["allowed"]:
            return out
        row = self.store.exchange_key(user["id"], "coinbase")
        if not row:
            return out
        out["connected"] = True
        out["key"] = "…" + row["key_name"][-6:]
        try:
            client = self._client(user)
            bal = client.balances()
            out["balances"] = {q: float(bal[q]) for q in QUOTES if q in bal}
            for t in self.store.trades(user["id"], 5, OPEN):          # ordres de sortie executes entre-temps
                self._sync(client, t)
        except (TradeError, ExchangeError) as exc:
            out["error"] = str(exc)
        out["trades"] = [self._view(t) for t in self.store.trades(user["id"], 20)]
        out["spent_today"] = float(self.store.spent_since(user["id"], int(time.time()) - 86400))
        return out

    def _sync(self, client, t: dict) -> None:
        if not t["exit_id"]:
            return
        o = client.order(t["exit_id"])
        if o.get("status") == "FILLED":
            avg = dec(o.get("average_filled_price"))
            how = "objectif atteint" if avg >= dec(t["entry"]) else "stop touche"
            self.store.update_trade(t["id"], status="cloture", closed=int(time.time()),
                                    exit_price=fmt(avg), note=how)

    @staticmethod
    def _view(t: dict) -> dict:
        keep = ("id", "symbol", "product", "quote_size", "entry", "base_size", "stop", "target",
                "status", "note", "exit_price")
        v = {k: t[k] for k in keep}
        v["created"], v["closed"] = t["created"] * 1000, (t["closed"] or 0) * 1000
        return v

    # -- ordre -----------------------------------------------------------
    def plan(self, user: dict, body: dict, client=None) -> dict:
        self._require(user)
        lim = self.limits()
        base = re.sub(r"USDT$", "", str(body.get("symbol") or "").upper())
        if not BASE_RE.match(base):
            raise TradeError("Crypto inconnue.")
        amount, ref = dec(body.get("amount")), dec(body.get("ref"))
        stop, target = dec(body.get("stop")), dec(body.get("target"))
        if not (0 < stop < ref < target):
            raise TradeError("Il faut un stop sous le prix et un objectif au-dessus.")
        if amount < dec(lim["min"]) or amount > dec(lim["max_order"]):
            raise TradeError(f"Montant entre {lim['min']:g} et {lim['max_order']:g} par ordre.")
        spent = self.store.spent_since(user["id"], int(time.time()) - 86400)
        if spent + amount > dec(lim["max_day"]):
            raise TradeError(f"Limite de {lim['max_day']:g} par 24 h atteinte ({fmt(spent)} deja engages).")
        client = client or self._client(user)
        try:
            balances = client.balances()
            product = None
            listed = False
            for q in QUOTES:
                try:
                    p = client.product(f"{base}-{q}")
                except ExchangeError:
                    continue
                if not p.get("product_id") or p.get("trading_disabled") or p.get("is_disabled"):
                    continue
                listed = True
                if balances.get(q, Decimal(0)) >= amount:
                    product, quote = p, q
                    break
        except ExchangeError as exc:
            raise TradeError(str(exc)) from None
        if product is None:
            raise TradeError(f"Solde insuffisant en {' / '.join(QUOTES)} sur Coinbase pour cet ordre." if listed
                             else f"{base} n'est pas negociable sur Coinbase.")
        live = dec(product.get("price"))
        if live <= 0:
            raise TradeError("Prix Coinbase indisponible pour cette crypto.")
        if amount < dec(product.get("quote_min_size")):
            raise TradeError(f"Montant minimal sur Coinbase : {product.get('quote_min_size')} {quote}.")
        drift = float((live / ref - 1) * 100)
        if abs(drift) > lim["max_drift_pct"]:
            raise TradeError(f"Le prix sur Coinbase ({fmt(live)}) s'ecarte de {drift:+.1f} % du prix du signal : "
                             "ordre refuse, le plan n'est plus valable.")
        step = dec(product.get("price_increment") or product.get("quote_increment"), "0.00000001")
        new_stop, new_target = floor_to(live * stop / ref, step), floor_to(live * target / ref, step)
        qty = amount / live
        return {
            "product": product["product_id"], "quote": quote, "base": base, "amount": fmt(amount),
            "price": fmt(live), "stop": fmt(new_stop), "target": fmt(new_target), "drift_pct": drift,
            "quantity": fmt(floor_to(qty, dec(product.get("base_increment"), "0.00000001"))),
            "max_loss": float(qty * (live - new_stop)), "gain": float(qty * (new_target - live)),
            "base_increment": str(product.get("base_increment") or "0.00000001"),
            "price_increment": fmt(step),
        }

    def buy(self, user: dict, body: dict) -> dict:
        self._require(user)
        if body.get("confirm") is not True:
            raise TradeError("Confirmation manquante.")
        lock = self._lock(user["id"])
        if not lock.acquire(blocking=False):
            raise TradeError("Un ordre est deja en cours pour ce compte.")
        try:
            client = self._client(user)
            plan = self.plan(user, body, client)
            amount = dec(plan["amount"])
            trade_id = self.store.add_trade(user["id"], "coinbase", plan["base"], plan["product"], fmt(amount),
                                            plan["stop"], plan["target"])
            try:
                buy_id = client.market_buy(plan["product"], amount)
                o = client.wait_filled(buy_id)
            except ExchangeError as exc:
                self.store.update_trade(trade_id, status="echec", note=str(exc)[:200])
                raise TradeError(str(exc)) from None
            filled, avg = dec(o.get("filled_size")), dec(o.get("average_filled_price"))
            if o.get("status") != "FILLED" or filled <= 0 or avg <= 0:
                self.store.update_trade(trade_id, status="a_verifier", buy_id=buy_id,
                                        note="Achat envoye, execution non confirmee : verifie sur Coinbase.")
                raise TradeError("Achat envoye mais execution non confirmee. Verifie tes ordres sur Coinbase "
                                 "avant de recommencer.")
            base_size = floor_to(filled, dec(plan["base_increment"]))
            step = dec(plan["price_increment"])
            ratio = avg / dec(plan["price"])
            stop, target = floor_to(dec(plan["stop"]) * ratio, step), floor_to(dec(plan["target"]) * ratio, step)
            self.store.update_trade(trade_id, buy_id=buy_id, entry=fmt(avg), base_size=fmt(base_size),
                                    stop=fmt(stop), target=fmt(target), status="sans_protection")
            try:
                exit_id = client.bracket_sell(plan["product"], base_size, target, stop)
                self.store.update_trade(trade_id, exit_id=exit_id, status="ouvert", note="")
            except ExchangeError as exc:
                self.store.update_trade(trade_id, note=str(exc)[:200])
                return {"ok": True, "warning": "Achat execute, mais le stop et l'objectif n'ont PAS pu etre places : "
                        f"{exc} Place-les toi-meme sur Coinbase ou vends depuis l'application.",
                        "trade": self._view(self.store.trade(trade_id))}
            return {"ok": True, "trade": self._view(self.store.trade(trade_id))}
        finally:
            lock.release()

    def close(self, user: dict, trade_id) -> dict:
        self._require(user)
        t = self.store.trade(trade_id) if isinstance(trade_id, int) else None
        if not t or t["user_id"] != user["id"] or t["status"] not in OPEN:
            raise TradeError("Position introuvable ou deja fermee.")
        lock = self._lock(user["id"])
        if not lock.acquire(blocking=False):
            raise TradeError("Un ordre est deja en cours pour ce compte.")
        try:
            client = self._client(user)
            try:
                if t["exit_id"]:
                    self._sync(client, t)
                    if self.store.trade(t["id"])["status"] == "cloture":
                        return {"ok": True, "trade": self._view(self.store.trade(t["id"]))}
                    client.cancel(t["exit_id"])
                sell_id = client.market_sell(t["product"], dec(t["base_size"]))
                o = client.wait_filled(sell_id)
            except ExchangeError as exc:
                raise TradeError(f"{exc} Verifie la position sur Coinbase.") from None
            self.store.update_trade(t["id"], status="cloture", closed=int(time.time()),
                                    exit_price=fmt(dec(o.get("average_filled_price"))), note="vendu a la main")
            return {"ok": True, "trade": self._view(self.store.trade(t["id"]))}
        finally:
            lock.release()
