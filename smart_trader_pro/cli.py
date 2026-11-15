"""Ligne de commande : scan, watch, backtest, test-telegram.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List

from . import __version__, backtest
from .context import ContextProvider, MarketContext
from .data import INTERVAL_MS, MarketData
from .notify import Telegram, fmt_price, format_signal
from .strategy import Params, evaluate, prepare, warmup

DEFAULTS = {
    "symbols": "auto",
    "auto_count": 20,
    "min_quote_volume": 20_000_000,
    "interval": "1h",
    "capital": 1000,
    "risk_pct": 1,
    "btc_filter": True,
    "market_drop_block_pct": 5,
    "cooldown_hours": 12,
    "state_file": "state.json",
    "strategy": {},
}


def load_config(path: str) -> dict:
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    elif path and path != "config.json":
        sys.exit(f"Fichier de configuration introuvable : {path}")
    return cfg


def load_state(path: str) -> Dict[str, int]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: Dict[str, int]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def resolve_symbols(cfg: dict, md: MarketData) -> List[str]:
    syms = cfg["symbols"]
    if syms == "auto":
        return md.top_symbols(count=cfg["auto_count"],
                              min_quote_volume=cfg["min_quote_volume"])
    return [s.upper() for s in syms]


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp} UTC] {msg}", flush=True)


def scan_once(cfg: dict, md: MarketData, tg: Telegram, ctx_provider: ContextProvider) -> int:
    p = Params.from_dict(cfg["strategy"])
    interval = cfg["interval"]
    need = warmup(p) + 60
    state = load_state(cfg["state_file"])
    cooldown_ms = int(cfg["cooldown_hours"] * 3_600_000)
    ctx: MarketContext = ctx_provider.get()
    for err in ctx.errors:
        log(f"Contexte indisponible - {err}")
    for line in ctx.lines():
        log(line)

    blocked = ""
    if ctx.risk_off(cfg["market_drop_block_pct"]):
        blocked = "marche global en forte baisse"
    elif cfg["btc_filter"]:
        btc = md.klines("BTCUSDT", interval, need)
        fb = prepare(btc, p)
        if len(btc) and fb.trend(len(btc) - 1, p.htf_slope_bars) == -1:
            blocked = "tendance de fond du BTC baissiere"
    if blocked:
        log(f"Signaux suspendus : {blocked}.")

    rows, sent = [], 0
    for sym in resolve_symbols(cfg, md):
        try:
            c = md.klines(sym, interval, need)
        except RuntimeError as exc:
            log(f"{sym} : {exc}")
            continue
        if len(c) < warmup(p) + 1:
            continue
        ev = evaluate(prepare(c, p), len(c) - 1, p)
        if ev is None:
            continue
        rows.append(ev)
        if not ev.is_signal or blocked:
            continue
        if ev.ts - state.get(sym, 0) < cooldown_ms:
            continue
        text = format_signal(ev, interval, cfg["capital"], cfg["risk_pct"])
        extra = ctx.lines(sym)
        if extra:
            text += "\n\nContexte :\n" + "\n".join(f"  {l}" for l in extra)
        print("\n" + text + "\n", flush=True)
        if tg.enabled and not tg.send(text):
            log(f"{sym} : echec de l'envoi Telegram")
        state[sym] = ev.ts
        sent += 1
    save_state(cfg["state_file"], state)

    rows.sort(key=lambda e: e.score, reverse=True)
    print(f"{'Paire':<12}{'Prix':>14}{'Score':>7}  {'Fond':<8}Etat")
    for ev in rows:
        fond = {1: "hausse", -1: "baisse", 0: "neutre"}[ev.trend]
        etat = "SIGNAL" if ev.is_signal and not blocked else ("suspendu" if ev.is_signal else "-")
        print(f"{ev.symbol:<12}{fmt_price(ev.price):>14}{ev.score:>7}  {fond:<8}{etat}")
    log(f"{len(rows)} paires analysees, {sent} nouveau(x) signal(aux).")
    return sent


def cmd_scan(args) -> None:
    cfg = load_config(args.config)
    scan_once(cfg, MarketData(), Telegram(), ContextProvider())


def cmd_watch(args) -> None:
    cfg = load_config(args.config)
    md, tg, ctxp = MarketData(), Telegram(), ContextProvider()
    step = INTERVAL_MS[cfg["interval"]] / 1000
    log(f"Smart Trader Pro {__version__} - surveillance {cfg['interval']}, "
        f"Telegram {'actif' if tg.enabled else 'inactif'}.")
    while True:
        try:
            scan_once(cfg, md, tg, ctxp)
        except Exception as exc:                      # le service ne doit pas tomber
            log(f"Erreur pendant le scan : {exc}")
        now = time.time()
        wait = step - (now % step) + 30               # 30 s apres la cloture
        log(f"Prochain scan dans {int(wait // 60)} min.")
        time.sleep(wait)


def cmd_backtest(args) -> None:
    cfg = load_config(args.config)
    md = MarketData()
    p = Params.from_dict(cfg["strategy"])
    interval = cfg["interval"]
    symbols = [s.upper() for s in args.symbols] if args.symbols else resolve_symbols(cfg, md)
    market = None
    if cfg["btc_filter"]:
        btc = md.klines("BTCUSDT", interval, args.candles)
        fb = prepare(btc, p)
        market = {btc.ts[i]: fb.trend(i, p.htf_slope_bars) for i in range(len(btc))}
    reports = []
    print(f"Backtest {interval} sur {args.candles} bougies - objectif {args.tp_r}R, "
          f"frais {args.fee} % par ordre\n")
    head = f"{'Paire':<12}{'Trades':>7}{'Gagnants':>10}{'R moyen':>9}{'PF':>7}{'Total R':>9}{'DD max':>8}{'Garder':>9}"
    print(head)

    def show(r) -> None:
        pf = "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
        print(f"{r.symbol:<12}{r.count:>7}{r.win_rate:>9.1f}%{r.expectancy:>9.2f}{pf:>7}"
              f"{r.total_r:>9.1f}{r.max_drawdown_r:>8.1f}{r.buy_hold_pct:>8.1f}%")

    for sym in symbols:
        try:
            c = md.klines(sym, interval, args.candles)
        except RuntimeError as exc:
            print(f"{sym:<12}{exc}")
            continue
        r = backtest.run(c, p, tp_r=args.tp_r, max_bars=args.max_bars,
                         fee_pct=args.fee, market_trend=market)
        reports.append(r)
        show(r)
    total = backtest.merge(reports)
    print("-" * len(head))
    show(total)
    print(f"\nEn risquant {cfg['risk_pct']} % par trade : "
          f"{total.return_pct(cfg['risk_pct']):+.1f} % sur la periode "
          f"(garder sans rien faire, moyenne : {total.buy_hold_pct:+.1f} %).")
    print("R = multiple du risque pris. PF = gains / pertes. DD max = pire serie, en R.")
    print("Le contexte CoinGecko/CoinMarketCap n'est pas rejoue dans le backtest.")
    print("Resultats passes : aucune garantie pour la suite.")


def cmd_test_telegram(args) -> None:
    tg = Telegram()
    if not tg.enabled:
        sys.exit("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID ne sont pas definis.")
    ok = tg.send("Smart Trader Pro : la liaison Telegram fonctionne.")
    print("Message envoye." if ok else "Echec : verifie le jeton et l'identifiant du chat.")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="smart-trader-pro",
                                 description="Scanner de points d'entree crypto (analyse seule).")
    ap.add_argument("--config", default="config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="une passe d'analyse").set_defaults(fn=cmd_scan)
    sub.add_parser("watch", help="surveillance continue").set_defaults(fn=cmd_watch)
    bt = sub.add_parser("backtest", help="tester la strategie sur l'historique")
    bt.add_argument("--symbols", nargs="*")
    bt.add_argument("--candles", type=int, default=3000)
    bt.add_argument("--tp-r", type=float, default=2.0)
    bt.add_argument("--max-bars", type=int, default=72)
    bt.add_argument("--fee", type=float, default=0.1)
    bt.set_defaults(fn=cmd_backtest)
    sub.add_parser("test-telegram", help="verifier Telegram").set_defaults(fn=cmd_test_telegram)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
