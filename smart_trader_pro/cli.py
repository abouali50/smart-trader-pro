"""Ligne de commande : scan, watch, wide, backtest, compare, web, test-telegram.

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

from . import __version__, backtest, breakout, compare, levels, strategy, wide
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
    "radar_interval": "15m",          # bougies du signal Binance
    "radar_count": 1000,              # plafond : toutes les paires USDT de Binance
    "radar_min_volume": 0,            # 0 = aucune paire ecartee pour son volume
    "radar_cooldown_hours": 24,
    "radar_strategy": {},
    "radar_file": "radar.json",
    "wide": True,
    "wide_telegram": True,            # False : les reveils restent sur la page, sans message
    **wide.DEFAULTS,
    "exclude": ["MSTRBUSDT", "CRCLBUSDT", "NVDABUSDT"],   # actions tokenisees
    "levels": True,
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


# Reglages du signal Binance en bougies de 15 minutes : 96 bougies = 24 heures.
RADAR_DEFAULTS = {"breakout_lookback": 96, "rs_bars": 96, "late_bars": 96}
RADAR_TITLE = "\U0001F6A8 EXPLOSION BINANCE"


def radar_params(cfg: dict) -> "breakout.Params":
    return breakout.Params.from_dict({**RADAR_DEFAULTS, **(cfg.get("radar_strategy") or {})})


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
    level_info: Dict[str, dict] = {}
    symbols = resolve_symbols(cfg, md)
    news_items: list = []
    if news is not None and cfg["news"]:
        news_items = news.latest(list(symbols))
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
        if cfg["levels"]:
            book = md.book_imbalance(sym) if (ev.is_signal and not blocked) else None
            level_info[sym] = levels.analyze(c, len(c) - 1, book)
        if not ev.is_signal or blocked:
            continue
        if ev.ts - state.get(sym, 0) < cooldown_ms:
            continue
        text = format_signal(ev, interval, cfg["capital"], cfg["risk_pct"])
        if sym in level_info:
            text += "\n\nNiveaux et flux d'ordres :\n" + "\n".join(
                f"  {l}" for l in level_info[sym]["lines"])
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
        "rows": [dict(row_dict(ev, blocked), levels=level_info.get(ev.symbol)) for ev in
                 sorted(rows, key=lambda e: e.score, reverse=True)],
        "news": [it.as_dict() for it in news_items[:14]],
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
    rp = radar_params(cfg)
    interval = cfg["radar_interval"]
    day = max(1, 86_400_000 // INTERVAL_MS[interval])     # bougies par 24 heures
    need = max(breakout.warmup(rp) + 30, day + 2)
    cooldown_ms = int(cfg["radar_cooldown_hours"] * 3_600_000)
    try:
        symbols = radar_symbols(cfg, md, cfg["radar_count"])
        btc = md.klines("BTCUSDT", interval, need)
    except RuntimeError as exc:
        log(f"Radar indisponible : {exc}")
        return [], 0, 0
    found, sent, confirmed, series = [], 0, {}, {}
    for sym in symbols:
        try:
            c = md.klines(sym, interval, need)
        except RuntimeError:
            continue
        if len(c) < breakout.warmup(rp) + 1:
            continue
        if len(btc) and c.ts[-1] < btc.ts[-1] - 2 * INTERVAL_MS[interval]:
            continue                                  # paire suspendue : bougies perimees
        i = len(c) - 1
        ev = breakout.evaluate(breakout.prepare(c, rp, btc=btc), i, rp)
        if ev is None:
            continue
        change = (c.close[i] / c.close[i - day] - 1) * 100 if i >= day else None
        found.append((ev, change))
        series[sym] = c
        key = "radar:" + sym
        if ev.is_signal and exchanges is not None:
            confirmed[sym] = exchanges.confirmations(sym)
        if not ev.is_signal or ev.ts - state.get(key, 0) < cooldown_ms:
            continue
        text = format_signal(ev, interval, cfg["capital"], cfg["risk_pct"],
                             title=RADAR_TITLE)
        if cfg["levels"]:
            info = levels.analyze(c, i, md.book_imbalance(sym))
            text += "\n\nNiveaux et flux d'ordres :\n" + "\n".join(f"  {l}" for l in info["lines"])
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
    for rank, (ev, change) in enumerate(found[:20]):
        row = row_dict(ev, "")
        row["change_24h"] = change
        if cfg["levels"]:
            cs = series[ev.symbol]
            book = md.book_imbalance(ev.symbol) if (rank < 5 or ev.is_signal) else None
            row["levels"] = levels.analyze(cs, len(cs) - 1, book)
        row["exchanges"] = confirmed.get(ev.symbol)
        row["news"] = [it.as_dict() for it in for_coin(list(news_items), ev.symbol)]
        rows.append(row)
    log(f"Signal Binance ({interval}) : {len(found)} paires analysees, {sent} explosion(s) detectee(s).")
    return rows, len(found), sent


def radar_once(cfg: dict, md: MarketData, tg: Telegram, ctx_provider: ContextProvider,
               news: "NewsProvider" = None, exchanges: "Exchanges" = None) -> int:
    """Un passage du signal Binance. Ecrit radar_file et renvoie le nombre d'alertes."""
    state = load_state(cfg["state_file"])
    ctx: MarketContext = ctx_provider.get()
    news_items: list = []
    if news is not None and cfg["news"]:
        try:
            news_items = news.latest(radar_symbols(cfg, md, cfg["radar_count"]))
        except RuntimeError:
            news_items = []
    new_signals: list = []
    rows, scanned, sent = scan_radar(cfg, md, tg, ctx, state, new_signals, news_items,
                                     exchanges if cfg["exchanges"] else None)
    save_state(cfg["state_file"], state)
    if new_signals:
        write_json(cfg["signals_file"], (new_signals + load_list(cfg["signals_file"]))[:100])
    write_json(cfg["radar_file"], {
        "enabled": True,
        "updated": int(time.time() * 1000),
        "interval": cfg["radar_interval"],
        "scanned": scanned,
        "min_score": radar_params(cfg).min_score,
        "rows": rows,
    })
    return sent


def engine_for(args, cfg: dict):
    """Moteur et parametres selon --mode."""
    if getattr(args, "mode", "repli") == "radar":
        return breakout, radar_params(cfg)
    return strategy, Params.from_dict(cfg["strategy"])


def cmd_scan(args) -> None:
    cfg = load_config(args.config)
    md, tg, ctxp, newsp, exch = MarketData(), Telegram(), ContextProvider(), NewsProvider(), Exchanges()
    scan_once(cfg, md, tg, ctxp, newsp, exch)
    if cfg["radar"]:
        radar_once(cfg, md, tg, ctxp, newsp, exch)


def wide_once(scanner: "wide.WideScanner", tg: Telegram, notify: bool = True) -> int:
    """Un balayage du marche large ; envoie les nouveaux reveils si `notify`. Renvoie leur nombre."""
    res = scanner.sweep()
    for err in res["errors"]:
        log(f"Marche large - {err}")
    for a in res["new"]:
        text = wide.format_alert(a)
        print("\n" + text + "\n", flush=True)
        if notify and tg.enabled and not tg.send(text):
            log(f"{a['symbol']} : echec de l'envoi Telegram")
    log(f"Marche large : {res['scanned']} cryptos balayees, {res['eligible']} assez liquides, "
        f"{len(res['new'])} reveil(s)"
        + (f", {res['learning']} encore en apprentissage." if res["learning"] else "."))
    return len(res["new"])


def cmd_wide(args) -> None:
    cfg = load_config(args.config)
    wide_once(wide.WideScanner(cfg), Telegram(), cfg["wide_telegram"])


def cmd_watch(args) -> None:
    cfg = load_config(args.config)
    md, tg, ctxp = MarketData(), Telegram(), ContextProvider()
    newsp, exch = NewsProvider(), Exchanges()
    scanner = wide.WideScanner(cfg) if cfg["wide"] else None
    step = INTERVAL_MS[cfg["interval"]] / 1000
    radar_step = INTERVAL_MS[cfg["radar_interval"]] / 1000 if cfg["radar"] else step
    tick = min([step, radar_step] + ([cfg["wide_interval_min"] * 60] if scanner else []))
    log(f"Smart Trader Pro {__version__} - replis {cfg['interval']}, "
        f"signal Binance {cfg['radar_interval'] if cfg['radar'] else 'inactif'}, "
        f"marche large {'toutes les ' + str(cfg['wide_interval_min']) + ' min' if scanner else 'inactif'}, "
        f"Telegram {'actif' if tg.enabled else 'inactif'}.")
    last_candle = last_radar = None
    while True:
        moment = time.time() - 30                     # une bougie est lue 30 s apres sa cloture
        if cfg["radar"] and int(moment // radar_step) != last_radar:
            last_radar = int(moment // radar_step)
            try:
                radar_once(cfg, md, tg, ctxp, newsp, exch)
            except Exception as exc:                  # le service ne doit pas tomber
                log(f"Erreur pendant le signal Binance : {exc}")
        if int(moment // step) != last_candle:
            last_candle = int(moment // step)
            try:
                scan_once(cfg, md, tg, ctxp, newsp, exch)
            except Exception as exc:
                log(f"Erreur pendant le scan : {exc}")
        if scanner:
            try:
                wide_once(scanner, tg, cfg["wide_telegram"])
            except Exception as exc:
                log(f"Erreur pendant le balayage large : {exc}")
        now = time.time()
        wait = tick - (now % tick) + 30
        log(f"Prochain passage dans {int(wait // 60)} min.")
        time.sleep(wait)


def cmd_backtest(args) -> None:
    cfg = load_config(args.config)
    md = MarketData()
    engine, p = engine_for(args, cfg)
    radar = engine is breakout
    tp_r = args.tp_r if args.tp_r else engine.DEFAULT_TP
    interval = args.interval or (cfg["radar_interval"] if radar else cfg["interval"])
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
    interval = args.interval or (cfg["radar_interval"] if radar else cfg["interval"])
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
    web.serve(cfg["status_file"], cfg["signals_file"], host=host, port=port,
              wide_file=cfg["wide_file"], radar_file=cfg["radar_file"])


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
    bt.add_argument("--interval", choices=sorted(INTERVAL_MS), help="bougies a utiliser")
    bt.add_argument("--tp-r", type=float, default=0.0)
    bt.add_argument("--max-bars", type=int, default=72)
    bt.add_argument("--fee", type=float, default=0.1)
    bt.set_defaults(fn=cmd_backtest)
    cp = sub.add_parser("compare", help="comparer des variantes, avec validation sur donnees jamais vues")
    cp.add_argument("--mode", choices=("repli", "radar"), default="repli")
    cp.add_argument("--count", type=int, default=0, help="nombre de paires (mode radar)")
    cp.add_argument("--interval", choices=sorted(INTERVAL_MS), help="bougies a utiliser")
    cp.add_argument("--symbols", nargs="*")
    cp.add_argument("--candles", type=int, default=8000)
    cp.add_argument("--train-ratio", type=float, default=0.6)
    cp.add_argument("--fee", type=float, default=0.1)
    cp.add_argument("--top", type=int, default=8)
    cp.set_defaults(fn=cmd_compare)
    sub.add_parser("wide", help="un balayage du marche large").set_defaults(fn=cmd_wide)
    wb = sub.add_parser("web", help="tableau de bord web")
    wb.add_argument("--port", type=int, default=0)
    wb.set_defaults(fn=cmd_web)
    sub.add_parser("test-telegram", help="verifier Telegram").set_defaults(fn=cmd_test_telegram)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
