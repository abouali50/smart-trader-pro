"""Ordres sur Coinbase (API Advanced Trade) au nom d'un compte.

Le serveur n'a jamais acces a l'argent : il envoie des ordres d'achat et
de vente avec une cle API que le titulaire du compte a creee lui-meme.
Regles de securite appliquees ici :

  - une cle qui a la permission de transfert (depot / retrait) est
    refusee : meme volee, une cle acceptee ne peut pas sortir d'argent ;
  - la cle privee est chiffree avant d'etre ecrite sur le disque, avec
    une cle maitresse qui reste dans l'environnement du serveur ;
  - chaque achat est suivi d'un ordre de vente a deux branches
    (objectif et stop) pour la quantite achetee.

Authentification : un jeton JWT signe en ES256 par requete, valable deux
minutes et lie a la methode et au chemin appeles.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import base64
import json
import secrets
import time
import uuid
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Dict, Optional

import requests

HOST = "api.coinbase.com"
API = "/api/v3/brokerage"
QUOTES = ("USDC", "USD", "USDT")       # monnaies de cotation essayees, dans cet ordre


class ExchangeError(Exception):
    """Refus ou panne cote plateforme ; le message est affichable au client."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def load_private_key(pem: str):
    """Charge une cle privee ECDSA P-256 au format PEM (celle des cles API Coinbase)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        key = serialization.load_pem_private_key(pem.replace("\\n", "\n").strip().encode(), password=None)
    except Exception:
        raise ExchangeError("Cle privee illisible. Copie-la en entier, de -----BEGIN a -----END.") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise ExchangeError("Type de cle non accepte : cree une cle ECDSA (pas Ed25519).")
    return key


def build_jwt(key_name: str, key, method: str, path: str, now: Optional[int] = None) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    now = now or int(time.time())
    header = {"alg": "ES256", "typ": "JWT", "kid": key_name, "nonce": secrets.token_hex(16)}
    claims = {"sub": key_name, "iss": "cdp", "nbf": now, "exp": now + 120,
              "uri": f"{method.upper()} {HOST}{path}"}
    signing = (_b64(json.dumps(header, separators=(",", ":")).encode()) + "."
               + _b64(json.dumps(claims, separators=(",", ":")).encode()))
    r, s = decode_dss_signature(key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
    return signing + "." + _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def dec(value, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def floor_to(value: Decimal, increment: Decimal) -> Decimal:
    """Arrondit vers le bas au pas autorise par la plateforme."""
    if increment <= 0:
        return value
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


class Vault:
    """Chiffrement des cles API avant ecriture en base (Fernet : AES + HMAC)."""

    def __init__(self, master_key: Optional[str]):
        self.fernet = None
        if master_key:
            from cryptography.fernet import Fernet
            try:
                self.fernet = Fernet(master_key.encode())
            except Exception:
                self.fernet = None

    @property
    def ready(self) -> bool:
        return self.fernet is not None

    def seal(self, text: str) -> bytes:
        return self.fernet.encrypt(text.encode())

    def open(self, blob: bytes) -> str:
        from cryptography.fernet import InvalidToken
        try:
            return self.fernet.decrypt(bytes(blob)).decode()
        except InvalidToken:
            raise ExchangeError("Cle enregistree illisible : reconnecte ton compte Coinbase.") from None


def new_master_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


class Coinbase:
    name = "coinbase"

    def __init__(self, key_name: str, private_pem: str, session=None, timeout: int = 15):
        self.key_name = (key_name or "").strip()
        if not self.key_name or len(self.key_name) > 200:
            raise ExchangeError("Nom de cle invalide.")
        self.key = load_private_key(private_pem)
        self.session = session or requests.Session()
        self.timeout = timeout

    def _request(self, method: str, path: str, params: dict = None, body: dict = None) -> dict:
        full = API + path
        headers = {"Authorization": "Bearer " + build_jwt(self.key_name, self.key, method, full),
                   "Content-Type": "application/json"}
        try:
            r = self.session.request(method, f"https://{HOST}{full}", params=params, json=body,
                                     headers=headers, timeout=self.timeout)
        except requests.RequestException:
            raise ExchangeError("Coinbase ne repond pas. Reessaie dans un instant.") from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 401:
            raise ExchangeError("Coinbase refuse la cle (401) : cle supprimee, mal copiee, ou adresse IP non autorisee.")
        if r.status_code != 200:
            msg = data.get("message") or data.get("error") or f"erreur {r.status_code}"
            raise ExchangeError(f"Coinbase a refuse la demande : {str(msg)[:160]}")
        return data if isinstance(data, dict) else {}

    # -- lecture ---------------------------------------------------------
    def permissions(self) -> dict:
        return self._request("GET", "/key_permissions")

    def balances(self) -> Dict[str, Decimal]:
        out: Dict[str, Decimal] = {}
        for a in self._request("GET", "/accounts", params={"limit": 250}).get("accounts") or []:
            value = dec((a.get("available_balance") or {}).get("value"))
            if value > 0:
                out[str(a.get("currency"))] = out.get(str(a.get("currency")), Decimal(0)) + value
        return out

    def product(self, product_id: str) -> dict:
        return self._request("GET", f"/products/{product_id}")

    def order(self, order_id: str) -> dict:
        return self._request("GET", f"/orders/historical/{order_id}").get("order") or {}

    # -- ordres ----------------------------------------------------------
    def _create(self, body: dict) -> str:
        res = self._request("POST", "/orders", body=body)
        if not res.get("success"):
            err = res.get("error_response") or {}
            why = (err.get("message") or err.get("error_details") or err.get("preview_failure_reason")
                   or err.get("new_order_failure_reason") or err.get("error") or "ordre refuse")
            raise ExchangeError(f"Coinbase a refuse l'ordre : {str(why)[:160]}")
        return str((res.get("success_response") or {}).get("order_id") or res.get("order_id") or "")

    def market_buy(self, product_id: str, quote_size: Decimal) -> str:
        return self._create({"client_order_id": str(uuid.uuid4()), "product_id": product_id, "side": "BUY",
                             "order_configuration": {"market_market_ioc": {"quote_size": fmt(quote_size)}}})

    def market_sell(self, product_id: str, base_size: Decimal) -> str:
        return self._create({"client_order_id": str(uuid.uuid4()), "product_id": product_id, "side": "SELL",
                             "order_configuration": {"market_market_ioc": {"base_size": fmt(base_size)}}})

    def bracket_sell(self, product_id: str, base_size: Decimal, target: Decimal, stop: Decimal) -> str:
        """Vente a deux branches : au prix objectif, ou au marche si le stop est touche."""
        return self._create({"client_order_id": str(uuid.uuid4()), "product_id": product_id, "side": "SELL",
                             "order_configuration": {"trigger_bracket_gtc": {
                                 "base_size": fmt(base_size), "limit_price": fmt(target),
                                 "stop_trigger_price": fmt(stop)}}})

    def cancel(self, order_id: str) -> None:
        self._request("POST", "/orders/batch_cancel", body={"order_ids": [order_id]})

    def wait_filled(self, order_id: str, tries: int = 8, pause: float = 0.5) -> dict:
        """Attend l'execution d'un ordre au marche ; renvoie le dernier etat connu."""
        last: dict = {}
        for _ in range(tries):
            last = self.order(order_id)
            if last.get("status") in ("FILLED", "CANCELLED", "EXPIRED", "FAILED"):
                break
            time.sleep(pause)
        return last


def check_key(client: Coinbase) -> dict:
    """Refuse une cle qui ne peut pas trader, ou qui peut transferer des fonds."""
    perms = client.permissions()
    if not perms.get("can_trade"):
        raise ExchangeError("Cette cle n'a pas la permission Trade. Recree-la en cochant View et Trade.")
    if perms.get("can_transfer"):
        raise ExchangeError("Cette cle a la permission Transfer (retrait). Pour ta securite elle est refusee : "
                            "recree une cle avec View et Trade seulement.")
    return perms
