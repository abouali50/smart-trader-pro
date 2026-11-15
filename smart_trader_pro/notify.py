"""Mise en forme des signaux et envoi Telegram (facultatif).

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import requests

from .strategy import Evaluation, position_size


def fmt_price(x: float) -> str:
    if x >= 1000:
        return f"{x:,.2f}".replace(",", " ")
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.8f}".rstrip("0")


def format_signal(ev: Evaluation, interval: str, capital: float, risk_pct: float) -> str:
    when = datetime.fromtimestamp(ev.ts / 1000, tz=timezone.utc).strftime("%d/%m %H:%M UTC")
    qty, notional = position_size(capital, risk_pct, ev.price, ev.stop)
    lines = [
        f"SIGNAL D'ENTREE - {ev.symbol} ({interval})",
        f"Score : {ev.score}/100 - bougie du {when}",
        "",
        f"Entree : {fmt_price(ev.price)}",
        f"Stop : {fmt_price(ev.stop)} (-{ev.risk_pct:.2f} %)",
        f"Objectif 1 : {fmt_price(ev.tp1)}",
        f"Objectif 2 : {fmt_price(ev.tp2)}",
        "",
        f"Taille pour risquer {risk_pct:g} % de {capital:g} USDT : "
        f"{notional:.2f} USDT ({qty:.6g} unites)",
    ]
    if ev.fib:
        lines += [
            f"Fibonacci : impulsion {fmt_price(ev.fib['low'])} -> {fmt_price(ev.fib['high'])}, "
            f"zone d'achat {fmt_price(ev.fib['0.618'])} - {fmt_price(ev.fib['0.382'])}, "
            f"extension 1.618 a {fmt_price(ev.fib['1.618'])}",
        ]
    lines += ["", "Notes par famille : " + ", ".join(
        f"{k} {g}/{t}" for k, (g, t) in ev.families.items()), "", "Conditions reunies :"]
    lines += [f"  + {r}" for r in ev.reasons]
    if ev.missing:
        lines.append("Conditions absentes :")
        lines += [f"  - {m}" for m in ev.missing]
    lines += ["", "Analyse automatique, pas un conseil. Aucun ordre n'est passe."]
    return "\n".join(lines)


class Telegram:
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text,
                      "disable_web_page_preview": True},
                timeout=15,
            )
            return r.status_code == 200
        except requests.RequestException:
            return False
