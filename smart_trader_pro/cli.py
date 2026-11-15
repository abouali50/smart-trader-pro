"""Ligne de commande : scan, watch, backtest, compare, web, test-telegram.

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

from . import __version__, backtest, breakout, compare, strategy
from .context import ContextProvider, MarketContext
from .data import INTERVAL_MS, MarketData
from .exchanges import Exchanges, summary_lines
from .news import NewsProvider, for_coin
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
    "radar": True,
    "radar_count": 150,
    "radar_min_volume": 3_000_000,
    "radar_cooldown_hours": 24,
    "radar_strategy": {},
    "exclude": ["MSTRBUSDT", "CRCLBUSDT", "NVDABUSDT"],   # actions tokenisees
    "news": True,
    "exchanges": True,
    "status_file": "status.json",
    "signals_file": "signals.json",
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


def write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def load_list(path: str) -> list:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def row_dict(ev, blocked: str) -> dict:
    """Une ligne du tableau de bord."""
    return {
        "symbol": ev.symbol, "price": ev.price, "score": ev.score, "trend": ev.trend,
        "signal": bool(ev.is_signal and not blocked),
        "suspended": bool(ev.is_signal and blocked),
        "families": {k: list(v) for k, v in ev.families.items()},
        "reasons": ev.reasons, "missing": ev.missing,
        "stop": ev.stop, "tp1": ev.tp1, "tp2": ev.tp2, "ts": ev.ts,
    }


def radar_symbols(cfg: dict, md: MarketData, count: int) -> List[str]:
    """Paires balayees par le radar, sans celles de la liste `exclude`."""
    excluded = {s.upper() for s in cfg.get("exclude", [])}
    found = md.top_symbols(count=count + len(excluded),
                           min_quote_volume=cfg["radar_min_volume"])
    return [s for s in found if s not in excluded][:count]


def resolve_symbols(cfg: dict, md: MarketData) -> List[str]:
    syms = cfg["symbols"]
    if syms == "auto":
        return md.top_symbols(count=cfg["auto_count"],
                              min_quote_volume=cfg["min_quote_volume"])
    return [s.upper() for s in syms]


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp} UTC] {msg}", flush=True)


def news_lines(news_items: list, sym: str) -> List[str]:
    found = for_coin(news_items, sym)
    if not found:
        return []
    return ["Actualites recentes :"] + [f"  {it.source} : {it.title}" for it in found]


def scan_once(cfg: dict, md: MarketData, tg: Telegram, ctx_provider: ContextProvider,
              news: "NewsProvider" = None, exchanges: "Exchanges" = None) -> int:
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

    rows, sent, new_signals = [], 0, []
    symbols = resolve_symbols(cfg, md)
    news_items: list = []
    if news is not None and cfg["news"]:
        watched = list(symbols)
        if cfg["radar"]:
            try:
                watched += radar_symbols(cfg, md, cfg["radar_count"])
            except RuntimeError:
                pass
        news_items = news.latest(watched)
        for err in news.errors:
            log(f"Actualites indisponibles - {err}")
    for sym in symbols:
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
        nl = news_lines(news_items, sym)
        if nl:
            text += "\n\n" + "\n".join(nl)
        print("\n" + text + "\n", flush=True)
        if tg.enabled and not tg.send(text):
            log(f"{sym} : echec de l'envoi Telegram")
        state[sym] = ev.ts
        sent += 1
        new_signals.append({
            "kind": "repli",
            "symbol": sym, "ts": ev.ts, "price": ev.price, "score": ev.score,
            "stop": ev.stop, "tp1": ev.tp1, "tp2": ev.tp2,
        })
    radar_rows, radar_scanned = [], 0
    if cfg["radar"]:
        radar_rows, radar_scanned, radar_sent = scan_radar(
            cfg, md, tg, ctx, state, new_signals, news_items,
            exchanges if cfg["exchanges"] else None)
        sent += radar_sent
    save_state(cfg["state_file"], state)
    if new_signals:
        history = (new_signals + load_list(cfg["signals_file"]))[:100]
        write_json(cfg["signals_file"], history)
    write_json(cfg["status_file"], {
        "version": __version__,
        "updated": int(time.time() * 1000),
        "interval": interval,
        "min_score": p.min_score,
        "trend_unit": "jour" if p.htf_factor * INTERVAL_MS[interval] == 86_400_000
                      else f"x{p.htf_factor}",
        "blocked": blocked,
        "context": {
            "market_change_24h": ctx.market_change_24h,
            "btc_dominance": ctx.btc_dominance,
            "fear_greed": ctx.fear_greed,
            "fear_greed_label": ctx.fear_greed_label,
            "sources": ctx.sources,
        },
        "rows": [row_dict(ev, blocked) for ev in
                 sorted(rows, key=lambda e: e.score, reverse=True)],
        "news": [it.as_dict() for it in news_items[:14]],
        "radar": {
            "enabled": bool(cfg["radar"]),
            "scanned": radar_scanned,
            "min_score": breakout.Params.from_dict(cfg["radar_strategy"]).min_score,
            "rows": radar_rows,
        },
    })

    rows.sort(key=lambda e: e.score, reverse=True)
    print(f"{'Paire':<12}{'Prix':>14}{'Score':>7}  {'Fond':<8}Etat")
    for ev in rows:
        fond = {1: "hausse", -1: "baisse", 0: "neutre"}[ev.trend]
        etat = "SIGNAL" if ev.is_signal and not blocked else ("suspendu" if ev.is_signal else "-")
        print(f"{ev.symbol:<12}{fmt_price(ev.price):>14}{ev.score:>7}  {fond:<8}{etat}")
    log(f"{len(rows)} paires analysees, {sent} nouveau(x) signal(aux).")
    return sent


def scan_radar(cfg: dict, md: MarketData, tg: Telegram, ctx: MarketContext,
               state: Dict[str, int], new_signals: list, news_items: list = (),
               exchanges: "Exchanges" = None):
    """Radar d'explosion : balaie un grand nombre de paires a la recherche d'un depart.

    Renvoie (lignes pour le tableau de bord, nombre de paires analysees, alertes envoyees).
    """
    rp = breakout.Params.from_dict(cfg["radar_strategy"])
    interval = cfg["interval"]
    need = breakout.warmup(rp) + 30
    cooldown_ms = int(cfg["radar_cooldown_hours"] * 3_600_000)
    try:
        symbols = radar_symbols(cfg, md, cfg["radar_count"])
        btc = md.klines("BTCUSDT", interval, need)
    except RuntimeError as exc:
        log(f"Radar indisponible : {exc}")
        return [], 0, 0
    found, sent, confirmed = [], 0, {}
    for sym in symbols:
        try:
            c = md.klines(sym, interval, need)
        except RuntimeError:
            continue
        if len(c) < breakout.warmup(rp) + 1:
            continue
        i = len(c) - 1
        ev = breakout.evaluate(breakout.prepare(c, rp, btc=btc), i, rp)
        if ev is None:
            continue
        change = (c.close[i] / c.close[i - 24] - 1) * 100 if i >= 24 else None
        found.append((ev, change))
        key = "radar:" + sym
        if ev.is_signal and exchanges is not None:
            confirmed[sym] = exchanges.confirmations(sym)
        if not ev.is_signal or ev.ts - state.get(key, 0) < cooldown_ms:
            continue
        text = format_signal(ev, interval, cfg["capital"], cfg["risk_pct"],
                             title="RADAR D'EXPLOSION")
        if sym in confirmed:
            text += "\n\n" + "\n".join(summary_lines(confirmed[sym]))
        extra = ctx.lines(sym)
        if extra:
            text += "\n\nContexte :\n" + "\n".join(f"  {l}" for l in extra)
        nl = news_lines(news_items, sym)
        if nl:
            text += "\n\n" + "\n".join(nl)
        print("\n" + text + "\n", flush=True)
        if tg.enabled and not tg.send(text):
            log(f"{sym} : echec de l'envoi Telegram")
        state[key] = ev.ts
        sent += 1
        new_signals.append({
            "kind": "radar",
            "symbol": sym, "ts": ev.ts, "price": ev.price, "score": ev.score,
            "stop": ev.stop, "tp1": ev.tp1, "tp2": ev.tp2,
        })
    found.sort(key=lambda t: (t[0].is_signal, t[0].score), reverse=True)
    rows = []
    for ev, change in found[:15]:
        row = row_dict(ev, "")
        row["change_24h"] = change
        row["exchanges"] = confirmed.get(ev.symbol)
        row["news"] = [it.as_dict() for it in for_coin(list(news_items), ev.symbol)]
        rows.append(row)
    log(f"Radar : {len(found)} paires analysees, {sent} depart(s) detecte(s).")
    return rows, len(found), sent


def engine_for(args, cfg: dict):
    """Moteur et parametres selon --mode."""
    if getattr(args, "mode", "repli") == "radar":
        return breakout, breakout.Params.from_dict(cfg["radar_strategy"])
    return strategy, Params.from_dict(cfg["strategy"])


def cmd_scan(args) -> None:
    cfg = load_config(args.config)
    scan_once(cfg, MarketData(), Telegram(), ContextProvider(), NewsProvider(), Exchanges())


def cmd_watch(args) -> None:
    cfg = load_config(args.config)
    md, tg, ctxp = MarketData(), Telegram(), ContextProvider()
    newsp, exch = NewsProvider(), Exchanges()
    step = INTERVAL_MS[cfg["interval"]] / 1000
    log(f"Smart Trader Pro {__version__} - surveillance {cfg['interval']}, "
        f"Telegram {'actif' if tg.enabled else 'inactif'}.")
    while True:
        try:
            scan_once(cfg, md, tg, ctxp, newsp, exch)
        except Exception as exc:                      # le service ne doit pas tomber
            log(f"Erreur pendant le scan : {exc}")
        now = time.time()
        wait = step - (now % step) + 30               # 30 s apres la cloture
        log(f"Prochain scan dans {int(wait // 60)} min.")
        time.sleep(wait)


def cmd_backtest(args) -> None:
    cfg = load_config(args.config)
    md = MarketData()
    engine, p = engine_for(args, cfg)
    radar = engine is breakout
    tp_r = args.tp_r if args.tp_r else engine.DEFAULT_TP
    interval = cfg["interval"]
    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    elif radar:
        symbols = radar_symbols(cfg, md, args.count or 30)
    else:
        symbols = resolve_symbols(cfg, md)
    btc = md.klines("BTCUSDT", interval, args.candles) if (radar or cfg["btc_filter"]) else None
    market = engine.market_filter(btc, p)
    reports = []
    print(f"Backtest {'radar' if radar else 'repli'} {interval} sur {args.candles} bougies - objectif {tp_r:g}R, "
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
        r = backtest.run(c, p, tp_r=tp_r, max_bars=args.max_bars, fee_pct=args.fee,
                         market_trend=market, engine=engine,
                         features=engine.prepare(c, p, btc=btc))
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


def cmd_compare(args) -> None:
    cfg = load_config(args.config)
    md = MarketData()
    engine, base = engine_for(args, cfg)
    radar = engine is breakout
    interval = cfg["interval"]
    if args.candles < compare.needed_candles(base, engine):
        sys.exit(f"Il faut au moins {compare.needed_candles(base, engine)} bougies (--candles).")
    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    elif radar:
        symbols = radar_symbols(cfg, md, args.count or 25)
    else:
        symbols = resolve_symbols(cfg, md)[:8]
    print(f"Telechargement de {args.candles} bougies {interval} pour {len(symbols)} paires...")
    candles = []
    for sym in symbols:
        try:
            c = md.klines(sym, interval, args.candles)
        except RuntimeError as exc:
            print(f"  {sym} ignore : {exc}")
            continue
        if len(c) >= compare.needed_candles(base, engine):
            candles.append(c)
        else:
            print(f"  {sym} ignore : historique trop court ({len(c)} bougies)")
    if not candles:
        sys.exit("Aucune paire exploitable.")
    btc = md.klines("BTCUSDT", interval, args.candles) if (radar or cfg["btc_filter"]) else None
    total = len(compare.variants(base, engine))
    done = [0]

    def progress(label: str) -> None:
        done[0] += 1
        print(f"  variante {done[0]}/{total}", end="\r", flush=True)

    results = compare.run(candles, btc, base, train_ratio=args.train_ratio,
                          fee_pct=args.fee, progress=progress, engine=engine)
    c0 = candles[0]
    cut = compare.split_index(c0, args.train_ratio)

    def day(ts: int) -> str:
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%d/%m/%Y")

    print(f"\nApprentissage : {day(c0.ts[0])} -> {day(c0.ts[cut])}   "
          f"Validation : {day(c0.ts[cut])} -> {day(c0.ts[-1])}")
    print(f"Paires : {', '.join(c.symbol for c in candles)}\n")
    head = (f"{'Variante':<40}| {'APPRENTISSAGE':^24}| {'VALIDATION (jamais vue)':^24}")
    sub = f"{'':<40}| {'Trades':>6}{'R moy':>8}{'PF':>7}   | {'Trades':>6}{'R moy':>8}{'PF':>7}"
    print(head)
    print(sub)
    print("-" * len(sub))

    def pf(r) -> str:
        return "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"

    def row(v, mark="") -> None:
        print(f"{(mark + v.label):<40}| {v.train.count:>6}{v.train.expectancy:>8.2f}{pf(v.train):>7}   "
              f"| {v.test.count:>6}{v.test.expectancy:>8.2f}{pf(v.test):>7}")

    for v in results[:args.top]:
        row(v, "* " if v.is_default(base) else "  ")
    default = next((v for v in results if v.is_default(base)), None)
    if default is not None and default not in results[:args.top]:
        print("...")
        row(default, "* ")
    print("\n* = reglage actuel. Classement selon l'apprentissage uniquement.")

    positives = sum(v.test.expectancy > 0 and v.test.count >= compare.MIN_TEST_TRADES
                    for v in results)
    print(f"Variantes gagnantes sur la validation : {positives} sur {len(results)}.")
    champion = compare.best(results)
    print("\nVERDICT")
    if champion is None:
        print("Pas assez de trades sur l'apprentissage pour departager les variantes. "
              "Relance avec plus de paires ou de bougies.")
        return
    t = champion.test
    print(f"Meilleure variante sur l'apprentissage : {champion.label.strip()}")
    print(f"Sur la validation : {t.count} trades, {t.win_rate:.1f} % gagnants, "
          f"R moyen {t.expectancy:+.2f}, PF {pf(t)}, "
          f"{t.return_pct(cfg['risk_pct']):+.1f} % en risquant {cfg['risk_pct']} % par trade "
          f"(garder : {t.buy_hold_pct:+.1f} %).")
    if compare.holds(champion):
        print("Elle TIENT sur des donnees jamais vues. C'est encourageant, pas une preuve : "
              "surveille-la en reel sans argent avant de t'y fier.")
        print("Pour l'adopter, mets ceci dans config.json puis redemarre le service :")
        print("  " + engine.config_snippet(champion.params))
        print(f"  (objectif a viser : {champion.tp_r:g} fois le risque)")
    else:
        print("Elle NE TIENT PAS sur des donnees jamais vues "
              f"(il faudrait au moins {compare.MIN_TEST_TRADES} trades, "
              f"R moyen >= {compare.MIN_TEST_EXPECTANCY:+.2f} et PF >= {compare.MIN_TEST_PF}).")
        print("Conclusion : cette famille de strategie n'a pas d'avantage demontre. "
              "Ne suis pas ses signaux avec de l'argent reel.")


def cmd_web(args) -> None:
    from . import web
    cfg = load_config(args.config)
    port = args.port or int(os.environ.get("DASHBOARD_PORT", "8765"))
    host = os.environ.get("DASHBOARD_HOST") or "0.0.0.0"
    log(f"Tableau de bord sur {host}:{port}.")
    web.serve(cfg["status_file"], cfg["signals_file"], host=host, port=port)


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
    bt.add_argument("--mode", choices=("repli", "radar"), default="repli")
    bt.add_argument("--count", type=int, default=0, help="nombre de paires (mode radar)")
    bt.add_argument("--tp-r", type=float, default=0.0)
    bt.add_argument("--max-bars", type=int, default=72)
    bt.add_argument("--fee", type=float, default=0.1)
    bt.set_defaults(fn=cmd_backtest)
    cp = sub.add_parser("compare", help="comparer des variantes, avec validation sur donnees jamais vues")
    cp.add_argument("--mode", choices=("repli", "radar"), default="repli")
    cp.add_argument("--count", type=int, default=0, help="nombre de paires (mode radar)")
    cp.add_argument("--symbols", nargs="*")
    cp.add_argument("--candles", type=int, default=8000)
    cp.add_argument("--train-ratio", type=float, default=0.6)
    cp.add_argument("--fee", type=float, default=0.1)
    cp.add_argument("--top", type=int, default=8)
    cp.set_defaults(fn=cmd_compare)
    wb = sub.add_parser("web", help="tableau de bord web")
    wb.add_argument("--port", type=int, default=0)
    wb.set_defaults(fn=cmd_web)
    sub.add_parser("test-telegram", help="verifier Telegram").set_defaults(fn=cmd_test_telegram)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
