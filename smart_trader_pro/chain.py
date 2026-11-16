"""Verification d'un paiement en jeton BEP-20 sur BNB Chain.

Le client envoie lui-meme les jetons depuis son portefeuille vers
l'adresse du vendeur. Ce module ne fait que LIRE la blockchain, par un
noeud public (JSON-RPC) : il ne detient aucune cle et ne peut rien
depenser.

Un paiement est accepte si la transaction a reussi, qu'elle contient un
transfert du bon jeton vers la bonne adresse pour le montant exact de la
commande, qu'elle a ete minee apres la creation de la commande, et
qu'elle a recu assez de confirmations.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import re
from typing import List, Optional

import requests

USDT_BSC = "0x55d398326f99059fF775485246999027B3197955"      # Tether sur BNB Chain, 18 decimales
BSC_CHAIN_ID = 56
DEFAULT_RPC = ["https://bsc-dataseed.binance.org", "https://bsc-dataseed1.defibit.io",
               "https://bsc-rpc.publicnode.com"]
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class Pending(Exception):
    """Le paiement n'est pas encore verifiable : reessayer plus tard."""


class Rejected(Exception):
    """Le paiement ne correspond pas a la commande."""


def is_address(value) -> bool:
    return isinstance(value, str) and bool(ADDRESS_RE.match(value))


def to_units(amount: float, decimals: int) -> int:
    """Montant decimal -> unites entieres du jeton, sans erreur d'arrondi flottant."""
    whole, _, frac = f"{amount:.8f}".partition(".")
    return int(whole) * 10 ** decimals + int(frac) * 10 ** (decimals - 8)


def format_units(units, decimals: int, places: int = 4) -> str:
    units = int(units)
    scaled = units * 10 ** places // 10 ** decimals
    return f"{scaled // 10 ** places}.{scaled % 10 ** places:0{places}d}"


class Chain:
    def __init__(self, rpc_urls: Optional[List[str]] = None, timeout: int = 12):
        self.rpc_urls = list(rpc_urls or DEFAULT_RPC)
        self.timeout = timeout
        self.session = requests.Session()

    def call(self, method: str, params: list):
        last = "aucun noeud configure"
        for url in self.rpc_urls:
            try:
                r = self.session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                 "params": params}, timeout=self.timeout)
                body = r.json()
                if r.status_code == 200 and isinstance(body, dict) and "error" not in body:
                    return body.get("result")
                last = f"{url} -> {str(body)[:80]}"
            except (requests.RequestException, ValueError) as exc:
                last = f"{url} -> {exc}"
        raise Pending(f"La blockchain ne repond pas pour l'instant ({last[:120]}).")

    def verify_transfer(self, tx_hash: str, token: str, to: str, units: int,
                        not_before: int, confirmations: int = 5) -> dict:
        """Renvoie {"block", "confirmations", "from"} ou leve Pending / Rejected."""
        if not isinstance(tx_hash, str) or not TX_RE.match(tx_hash):
            raise Rejected("Identifiant de transaction invalide (0x suivi de 64 caracteres).")
        receipt = self.call("eth_getTransactionReceipt", [tx_hash])
        if not receipt:
            raise Pending("Transaction introuvable pour l'instant : elle n'est peut-etre pas encore minee.")
        if receipt.get("status") != "0x1":
            raise Rejected("Cette transaction a echoue sur la blockchain.")
        token, to = token.lower(), to.lower()
        received, sender = 0, ""
        for lg in receipt.get("logs") or []:
            topics = lg.get("topics") or []
            if (str(lg.get("address", "")).lower() != token or len(topics) < 3
                    or topics[0].lower() != TRANSFER_TOPIC):
                continue
            if "0x" + topics[2][-40:].lower() != to:
                continue
            received += int(lg.get("data") or "0x0", 16)
            sender = "0x" + topics[1][-40:].lower()
        if received == 0:
            raise Rejected("Cette transaction n'envoie pas le bon jeton a l'adresse de paiement.")
        if received != int(units):
            raise Rejected("Le montant envoye ne correspond pas au montant exact de la commande.")
        block = int(receipt["blockNumber"], 16)
        head = self.call("eth_getBlockByNumber", [hex(block), False])
        if not head:
            raise Pending("Bloc introuvable pour l'instant.")
        if int(head["timestamp"], 16) < not_before:
            raise Rejected("Cette transaction est anterieure a la commande.")
        confirmed = int(self.call("eth_blockNumber", []), 16) - block + 1
        if confirmed < confirmations:
            raise Pending(f"Paiement recu, en attente de confirmations ({max(confirmed, 0)}/{confirmations}).")
        return {"block": block, "confirmations": confirmed, "from": sender}
