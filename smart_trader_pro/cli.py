"""Ligne de commande : scan, watch, wide, backtest, compare, web, test-telegram.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List

from . import (__version__, alpha, backtest, breakout, chain, compare, levels, onchain, pressure, quality, strategy, surge,
               tracker,
               wide)
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
    "pressure": True,                 # pre-explosion : prevenir avant la cassure
    "pressure_strategy": {},
    "pressure_max_alerts": 3,         # par passage et par source
    "pressure_telegram": False,       # au test, ces alertes ont perdu : page seule par defaut
    "alpha": True,                    # jetons de la rubrique Binance Alpha
    "alpha_count": 120,
    "alpha_min_volume": 200_000,      # USD sur 24 h
    "alpha_min_liquidity": 50_000,
    "alpha_file": "alpha.json",
    "wide": True,
    "wide_telegram": True,            # False : les reveils restent sur la page, sans message
    **wide.DEFAULTS,
    "exclude": ["MSTRBUSDT", "CRCLBUSDT", "NVDABUSDT"],   # actions tokenisees
    "levels": True,
    "news": True,
    "exchanges": True,
    "status_file": "status.json",
    "signals_file": "signals.json",
    "tracking": True,                 # suit le resultat reel de chaque alerte
    "tracking_file": "tracking.json",
    # Application web : comptes, abonnements payes en crypto, lien vers le DEX.
    "app_db": "app.db",
    "paywall": False,                 # True : niveaux et details reserves aux abonnes
    "plans": {
        "week": {"label": "Hebdomadaire", "days": 7, "price": 9},
        "month": {"label": "Mensuel", "days": 30, "price": 29},
        "year": {"label": "Annuel", "days": 365, "price": 249},
    },
    "pay_address": "",                # adresse BNB Chain qui recoit les abonnements
    "pay_symbol": "USDT",
    "pay_token": chain.USDT_BSC,
    "pay_decimals": 18,
    "pay_confirmations": 5,
    "pay_rpc": [],                    # vide : noeuds publics de BNB Chain
    # Trading en un clic sur Coinbase (cle API du titulaire, sans permission de retrait).
    # Flux on-chain des jetons Alpha (acheteurs / vendeurs sur les DEX), avec la cle CoinGecko payante.
    # Alerte rapide : verifie tout le marche chaque minute, sans attendre la fin de la bougie.
    "surge": True,
    "surge_seconds": 60,              # frequence de verification
    "surge_window_min": 3,            # le mouvement est mesure sur ces dernieres minutes
    "surge_min_move": 2.0,            # hausse minimale sur la fenetre, en %
    "surge_vol_mult": 8,              # volume de la fenetre / rythme moyen des 24 h
    "surge_min_volume": 500000,       # volume minimal sur 24 h, en USDT
    "surge_cooldown_hours": 6,        # une seule alerte rapide par crypto sur cette duree
    "surge_max_alerts": 2,            # alertes rapides au plus par verification
    "min_quality": "C",               # "B" : seules les alertes notees A ou B partent sur Telegram
    "onchain": True,
    "onchain_per_pass": 8,            # jetons interroges par passage : 1 credit CoinGecko chacun
    "trading": True,
    "trading_clients": False,         # True : ouvert aussi aux abonnes ; False : administrateur seul
    "trade_min": 5,                   # montant minimal d'un ordre
    "trade_max_order": 100,           # montant maximal d'un ordre
    "trade_max_day": 300,             # montant maximal engage par 24 h et par compte
    "trade_max_drift_pct": 3,         # ecart maximal entre le prix du signal et le prix Coinbase
    "dex_url": "",                    # ex. https://dex.exemple.com/swap?outputCurrency={address}
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


SIGNALS_KEEP = 600        # alertes conservees pour le bilan
FILE_LOCK = threading.Lock()   # le fichier des alertes est ecrit par deux fils : scans et voie rapide


def add_signals(cfg: dict, new_signals: list) -> list:
    """Ajoute des alertes en tete du fichier, sans ecraser celles ecrites entre-temps."""
    with FILE_LOCK:
        history = (new_signals + load_list(cfg["signals_file"]))[:SIGNALS_KEEP]
        write_json(cfg["signals_file"], history)
    return history


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
        "trigger": getattr(ev, "trigger", None),
    }


# Reglages du signal Binance en bougies de 15 minutes : 96 bougies = 24 heures.
# market_bars 672 = 7 jours : pas d'alerte d'explosion quand le Bitcoin baisse.
RADAR_DEFAULTS = {"breakout_lookback": 96, "rs_bars": 96, "late_bars": 96, "market_bars": 672}
MARKET_DOWN = "Bitcoin en tendance baissiere"
RADAR_TITLE = "\U0001F6A8 EXPLOSION BINANCE"
PRESSURE_TITLE = "\u23F3 PRE-EXPLOSION"
ALPHA_TITLE = "\U0001F6A8 EXPLOSION BINANCE ALPHA"
ALPHA_WARNING = ("Jeton Binance Alpha : tres volatil, liquidite faible, "
                 "il peut perdre l'essentiel de sa valeur en quelques heures.")


class SpotSource:
    """Paires USDT de Binance Spot."""
    title, prefix, kind, label = RADAR_TITLE, "radar:", "radar", "Signal Binance"
    use_exchanges = True

    def __init__(self, cfg: dict, md: MarketData):
        self.cfg, self.md = cfg, md

    def ids(self) -> List[str]:
        return radar_symbols(self.cfg, self.md, self.cfg["radar_count"])

    def klines(self, sid: str, interval: str, need: int):
        return self.md.klines(sid, interval, need)

    def book(self, sid: str):
        return self.md.book_imbalance(sid)

    def live(self, sid: str):
        return self.md.last_price(sid)

    def info(self, sid: str) -> List[str]:
        return []

    def token(self, sid: str):
        return None

    def flow(self, sid: str):
        return None

    def change_24h(self, sid: str):
        return None

    def warning(self) -> str:
        return ""


class AlphaSource:
    """Jetons de la rubrique Binance Alpha."""
    title, prefix, kind, label = ALPHA_TITLE, "alpha:", "alpha", "Binance Alpha"
    use_exchanges = False

    def __init__(self, cfg: dict, data: "alpha.AlphaData", chain_data: "onchain.OnChain" = None):
        self.cfg, self.data, self.chain_data = cfg, data, chain_data
        if chain_data is not None:
            chain_data.start_pass()

    def flow(self, sid: str):
        """Flux on-chain du jeton sur les DEX (None si indisponible)."""
        t = self.data.tokens_by_id.get(sid) or {}
        if self.chain_data is None or not self.cfg["onchain"] or not chain.is_address(t.get("contract")):
            return None
        return self.chain_data.flow(t.get("chain_id"), t["contract"])

    def ids(self) -> List[str]:
        return [t["id"] for t in self.data.tokens(self.cfg["alpha_min_volume"],
                                                  self.cfg["alpha_min_liquidity"],
                                                  self.cfg["alpha_count"])]

    def klines(self, sid: str, interval: str, need: int):
        return self.data.klines(sid, interval, need)

    def book(self, sid: str):
        return None

    def live(self, sid: str):
        return None

    def info(self, sid: str) -> List[str]:
        t = self.data.tokens_by_id.get(sid)
        return alpha.info_lines(t) if t else []

    def token(self, sid: str):
        """Contrat du jeton, pour l'ouvrir sur un DEX (None si l'adresse est inutilisable)."""
        t = self.data.tokens_by_id.get(sid) or {}
        if not chain.is_address(t.get("contract")):
            return None
        return {"contract": t["contract"], "chain_id": t.get("chain_id", ""), "chain": t.get("chain", "")}

    def change_24h(self, sid: str):
        t = self.data.tokens_by_id.get(sid)
        return t["change_24h"] if t else None

    def warning(self) -> str:
        return ALPHA_WARNING


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
            "kind": "repli", "interval": interval,
            "symbol": sym, "ts": ev.ts, "price": ev.price, "score": ev.score,
            "stop": ev.stop, "tp1": ev.tp1, "tp2": ev.tp2,
        })
    save_state(cfg["state_file"], state)
    if new_signals:
        history = add_signals(cfg, new_signals)
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
               exchanges: "Exchanges" = None, source=None):
    """Cherche les explosions sur une source (Binance Spot par defaut, ou Binance Alpha).

    Renvoie (lignes des explosions, nombre de paires analysees, alertes envoyees,
    lignes des cryptos sous pression, etat du marche "up" / "down" / None).
    """
    source = source or SpotSource(cfg, md)
    rp = radar_params(cfg)
    interval = cfg["radar_interval"]
    day = max(1, 86_400_000 // INTERVAL_MS[interval])     # bougies par 24 heures
    need = max(breakout.warmup(rp) + 30, day + 2)
    cooldown_ms = int(cfg["radar_cooldown_hours"] * 3_600_000)
    try:
        ids = source.ids()
        btc = md.klines("BTCUSDT", interval, max(need, breakout.market_need(rp)))
    except Exception as exc:
        log(f"{source.label} indisponible : {exc}")
        return [], 0, 0, [], None
    market = breakout.market_now(btc, rp)
    blocked = MARKET_DOWN if market == "down" else ""
    held = below = 0
    found, sent, confirmed, series = [], 0, {}, {}
    runs: dict = {}

    def grade_of(sid, ev, c, i, fl) -> dict:
        """Note de qualite A / B / C de la cassure : ce qui l'entoure, pas le score."""
        of = levels.order_flow(c, i)
        return quality.grade(ev.risk_pct, runs.get(sid), ctx.market_change_24h, confirmed.get(sid),
                             of["cvd"] if of else None, fl)

    pp = pressure.Params.from_dict(cfg["pressure_strategy"])
    pressured: list = []
    for sid in ids:
        try:
            c = source.klines(sid, interval, need)
        except Exception:
            continue
        if len(c) < breakout.warmup(rp) + 1:
            continue
        if len(btc) and c.ts[-1] < btc.ts[-1] - 2 * INTERVAL_MS[interval]:
            continue                                  # paire suspendue : bougies perimees
        i = len(c) - 1
        ev = breakout.evaluate(breakout.prepare(c, rp, btc=btc), i, rp)
        if ev is None:
            continue
        sym = ev.symbol
        change = source.change_24h(sid)
        if change is None:
            change = (c.close[i] / c.close[i - day] - 1) * 100 if i >= day else None
        found.append((ev, change, sid))
        series[sid] = c
        runs[sid] = (c.close[i] / c.close[i - rp.late_bars] - 1) * 100 if i >= rp.late_bars else None
        if cfg["pressure"] and len(c) >= pressure.warmup(pp) + 1:
            pev = pressure.evaluate(pressure.prepare(c, pp, btc=btc), i, pp)
            if pev is not None:
                pressured.append((pev, change, sid))
        key = source.prefix + sid
        if ev.is_signal and exchanges is not None and source.use_exchanges:
            confirmed[sid] = exchanges.confirmations(sym)
        if not ev.is_signal or ev.ts - state.get(key, 0) < cooldown_ms:
            continue
        if blocked:                                   # cassure vue, alerte retenue
            hk = "held:" + key
            if ev.ts - state.get(hk, 0) >= cooldown_ms:
                state[hk] = ev.ts
                held += 1
                new_signals.append({                  # suivie quand meme, pour juger le filtre
                    "kind": "held-" + source.kind, "interval": interval, "sid": sid,
                    "symbol": sym, "ts": ev.ts, "price": ev.price, "score": ev.score,
                    "stop": ev.stop, "tp1": ev.tp1, "tp2": ev.tp2,
                    "quality": grade_of(sid, ev, c, i, None)["grade"],
                })
            continue
        fl = source.flow(sid)
        q = grade_of(sid, ev, c, i, fl)
        state[key] = ev.ts
        entry = {
            "kind": source.kind, "interval": interval, "sid": sid,
            "symbol": sym, "ts": ev.ts, "price": ev.price, "score": ev.score,
            "stop": ev.stop, "tp1": ev.tp1, "tp2": ev.tp2,
            "onchain": fl["verdict"] if fl else None, "quality": q["grade"],
        }
        if not quality.allowed(q["grade"], cfg["min_quality"]):
            below += 1                                # suivie dans le bilan, sans message
            new_signals.append(dict(entry, sent=False))
            continue
        text = format_signal(ev, interval, cfg["capital"], cfg["risk_pct"], title=source.title)
        head = text.split("\n")
        live = source.live(sid)                       # ou en est le prix au moment d'envoyer
        now_line = []
        if live:
            drift = (live / ev.price - 1) * 100
            if live >= ev.tp1:
                now_line = [f"TROP TARD : le prix actuel ({fmt_price(live)}, {drift:+.1f} %) a deja depasse "
                            "l'objectif 1. Ne cours pas apres."]
            elif live <= ev.stop:
                now_line = [f"ANNULE : le prix actuel ({fmt_price(live)}, {drift:+.1f} %) est deja sous le stop."]
            else:
                now_line = [f"Prix actuel : {fmt_price(live)} ({drift:+.1f} % depuis la cloture de la bougie)"]
        text = "\n".join(head[:2] + quality.lines(q) + now_line + head[2:])
        info = source.info(sid)
        if info:
            text += "\n\nLe jeton :\n" + "\n".join(f"  {l}" for l in info)
        if cfg["levels"]:
            lv = levels.analyze(c, i, source.book(sid))
            text += "\n\nNiveaux et flux d'ordres :\n" + "\n".join(f"  {l}" for l in lv["lines"])
        if fl:
            text += (f"\n\nFlux on-chain (DEX) : {fl['label']}\n"
                     + "\n".join(f"  {l}" for l in fl["lines"]))
        if sid in confirmed:
            text += "\n\n" + "\n".join(summary_lines(confirmed[sid]))
        extra = ctx.lines(sym)
        if extra:
            text += "\n\nContexte :\n" + "\n".join(f"  {l}" for l in extra)
        nl = news_lines(news_items, sym)
        if nl:
            text += "\n\n" + "\n".join(nl)
        if source.warning():
            text += "\n\n" + source.warning()
        print("\n" + text + "\n", flush=True)
        if tg.enabled and not tg.send(text):
            log(f"{sym} : echec de l'envoi Telegram")
        sent += 1
        new_signals.append(entry)
    found.sort(key=lambda t: (t[0].is_signal, t[0].score), reverse=True)
    rows = []
    for rank, (ev, change, sid) in enumerate(found[:20]):
        row = row_dict(ev, blocked)
        row["change_24h"] = change
        row["info"] = source.info(sid)
        row["sid"], row["token"] = sid, source.token(sid)
        row["onchain"] = source.flow(sid)             # plafonne par passage, les mieux notes d'abord
        row["quality"] = grade_of(sid, ev, series[sid], len(series[sid]) - 1, row["onchain"])
        if cfg["levels"]:
            cs = series[sid]
            book = source.book(sid) if (rank < 5 or ev.is_signal) else None
            row["levels"] = levels.analyze(cs, len(cs) - 1, book)
        row["exchanges"] = confirmed.get(sid)
        row["news"] = [it.as_dict() for it in for_coin(list(news_items), ev.symbol)]
        rows.append(row)
    # Pre-explosion : les cryptos sous pression, pas encore parties.
    pressured.sort(key=lambda t: (t[0].is_signal, t[0].score), reverse=True)
    pre_sent = 0
    for pev, change, sid in pressured:
        key = "pre:" + source.prefix + sid
        if (not pev.is_signal or pre_sent >= cfg["pressure_max_alerts"]
                or pev.ts - state.get(key, 0) < cooldown_ms):
            continue
        c = series[sid]
        text = format_signal(pev, interval, cfg["capital"], cfg["risk_pct"],
                             title=f"{PRESSURE_TITLE} {source.label.upper()}")
        text += ("\n\nPas encore partie. Niveau a casser : " + fmt_price(pev.trigger)
                 + f" ({(pev.trigger / pev.price - 1) * 100:+.1f} %)."
                 "\n  Prudent : attendre une cloture au-dessus de ce niveau avec du volume."
                 "\n  En avance : entrer maintenant avec le stop indique (plus risque).")
        info = source.info(sid)
        if info:
            text += "\n\nLe jeton :\n" + "\n".join(f"  {l}" for l in info)
        if cfg["levels"]:
            lv = levels.analyze(c, len(c) - 1, source.book(sid))
            text += "\n\nNiveaux et flux d'ordres :\n" + "\n".join(f"  {l}" for l in lv["lines"])
        nl = news_lines(news_items, pev.symbol)
        if nl:
            text += "\n\n" + "\n".join(nl)
        text += ("\n\nUne zone calme peut ne jamais casser, ou casser vers le bas. "
                 "Ce n'est pas une prediction.")
        if source.warning():
            text += "\n" + source.warning()
        print("\n" + text + "\n", flush=True)
        if cfg["pressure_telegram"] and tg.enabled and not tg.send(text):
            log(f"{pev.symbol} : echec de l'envoi Telegram")
        state[key] = pev.ts
        pre_sent += 1
        new_signals.append({
            "kind": "pre-" + source.kind, "interval": interval, "sid": sid,
            "symbol": pev.symbol, "ts": pev.ts, "price": pev.price, "score": pev.score,
            "stop": pev.stop, "tp1": pev.tp1, "tp2": pev.tp2,
        })
    pre_rows = []
    for pev, change, sid in pressured[:12]:
        row = row_dict(pev, "")
        row["change_24h"] = change
        row["info"] = source.info(sid)
        row["sid"], row["token"] = sid, source.token(sid)
        row["onchain"] = source.flow(sid)
        pre_rows.append(row)
    log(f"{source.label} ({interval}) : {len(found)} paires analysees, {sent} explosion(s), "
        f"{pre_sent} pre-explosion(s).")
    if below:
        log(f"{source.label} : {below} alerte(s) sous la qualite minimale {cfg['min_quality']}, "
            "suivie(s) dans le bilan sans message.")
    if blocked:
        log(f"{source.label} : alertes d'explosion suspendues ({MARKET_DOWN}), "
            f"{held} cassure(s) retenue(s).")
    return rows, len(found), sent + pre_sent, pre_rows, market


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
    rows, scanned, sent, pre_rows, market = scan_radar(
        cfg, md, tg, ctx, state, new_signals, news_items,
        exchanges if cfg["exchanges"] else None)
    save_state(cfg["state_file"], state)
    if new_signals:
        add_signals(cfg, new_signals)
    write_json(cfg["radar_file"], {
        "enabled": True,
        "updated": int(time.time() * 1000),
        "interval": cfg["radar_interval"],
        "scanned": scanned,
        "market": market,
        "min_score": radar_params(cfg).min_score,
        "rows": rows,
        "pressure": {"min_score": pressure.Params.from_dict(cfg["pressure_strategy"]).min_score,
                     "rows": pre_rows},
    })
    return sent


def make_onchain(cfg: dict):
    """Lecteur du flux on-chain, ou None s'il est coupe ou sans cle CoinGecko payante."""
    if not cfg["onchain"]:
        return None
    reader = onchain.OnChain(per_pass=int(cfg["onchain_per_pass"]))
    return reader if reader.enabled else None


def alpha_once(cfg: dict, md: MarketData, data: "alpha.AlphaData", tg: Telegram,
               ctx_provider: ContextProvider, chain_data: "onchain.OnChain" = None) -> int:
    """Un passage sur les jetons Binance Alpha. Ecrit alpha_file ; renvoie le nombre d'alertes."""
    state = load_state(cfg["state_file"])
    new_signals: list = []
    source = AlphaSource(cfg, data, chain_data)
    rows, scanned, sent, pre_rows, market = scan_radar(cfg, md, tg, ctx_provider.get(), state,
                                                       new_signals, source=source)
    save_state(cfg["state_file"], state)
    if chain_data is not None and (chain_data.used or chain_data.failed):
        log(f"Flux on-chain : {chain_data.used} jeton(s) interroge(s), {chain_data.failed} echec(s) au total.")
    if new_signals:
        add_signals(cfg, new_signals)
    write_json(cfg["alpha_file"], {
        "onchain": chain_data is not None,
        "enabled": True,
        "available": scanned > 0 or bool(data.tokens_by_id),
        "updated": int(time.time() * 1000),
        "interval": cfg["radar_interval"],
        "scanned": scanned,
        "market": market,
        "min_score": radar_params(cfg).min_score,
        "rows": rows,
        "pressure": {"min_score": pressure.Params.from_dict(cfg["pressure_strategy"]).min_score,
                     "rows": pre_rows},
    })
    return sent


def track_once(cfg: dict, md: MarketData, alpha_data: "alpha.AlphaData" = None,
               now_ms: int = None) -> int:
    """Releve ce que le prix a fait apres chaque alerte. Ecrit tracking_file et renvoie
    le nombre d'alertes cloturees pendant ce passage."""
    signals = load_list(cfg["signals_file"])
    now_ms = now_ms or int(time.time() * 1000)

    def interval_of(sig: dict) -> str:
        if sig.get("interval") in INTERVAL_MS:
            return sig["interval"]
        return cfg["interval"] if (sig.get("kind") or "repli") == "repli" else cfg["radar_interval"]

    def fetch(sig: dict, interval: str, total: int):
        if (sig.get("kind") or "").endswith("alpha"):
            if alpha_data is None:
                raise RuntimeError("Binance Alpha inactif")
            sid = sig.get("sid") or next(
                (i for i, t in alpha_data.tokens_by_id.items()
                 if f"{t.get('symbol')}USDT" == sig["symbol"]), None)
            if sid is None:
                raise RuntimeError("jeton Alpha introuvable")
            return alpha_data.klines(sid, interval, total)
        return md.klines(sig["symbol"], interval, total)

    checked, closed = tracker.update(signals, fetch, now_ms, interval_of)
    if checked:
        # Des alertes ont pu etre ajoutees pendant la lecture des bougies : on relit le fichier
        # et on y reporte seulement les resultats, au lieu de l'ecraser.
        done = {(s.get("kind"), s.get("symbol"), s.get("ts")): s for s in signals}
        with FILE_LOCK:
            fresh = load_list(cfg["signals_file"])
            for s in fresh:
                src = done.get((s.get("kind"), s.get("symbol"), s.get("ts")))
                if src is None:
                    continue
                for k in ("outcome", "next_check", "failures"):
                    if k in src:
                        s[k] = src[k]
                    else:
                        s.pop(k, None)
            write_json(cfg["signals_file"], fresh)
        signals = fresh
    write_json(cfg["tracking_file"], {
        "updated": now_ms, "max_bars": tracker.MAX_BARS, "fee_pct": tracker.FEE_PCT,
        "groups": tracker.summary(signals),
    })
    if checked:
        log(f"Suivi des alertes : {checked} relue(s), {closed} cloturee(s).")
    return closed


def engine_for(args, cfg: dict):
    """Moteur et parametres selon --mode."""
    mode = getattr(args, "mode", "repli")
    if mode == "radar":
        return breakout, radar_params(cfg)
    if mode == "pression":
        return pressure, pressure.Params.from_dict(cfg["pressure_strategy"])
    return strategy, Params.from_dict(cfg["strategy"])


def cmd_scan(args) -> None:
    cfg = load_config(args.config)
    md, tg, ctxp, newsp, exch = MarketData(), Telegram(), ContextProvider(), NewsProvider(), Exchanges()
    scan_once(cfg, md, tg, ctxp, newsp, exch)
    if cfg["radar"]:
        radar_once(cfg, md, tg, ctxp, newsp, exch)
    if cfg["alpha"]:
        alpha_once(cfg, md, alpha.AlphaData(), tg, ctxp, make_onchain(cfg))


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


def surge_once(cfg: dict, watcher: "surge.SurgeWatcher", tg: Telegram, now: float = None) -> int:
    """Une verification de la voie rapide ; renvoie le nombre d'alertes envoyees."""
    plans = watcher.tick(now)
    for p in plans:
        text = surge.format_alert(p, fmt_price)
        print("\n" + text + "\n", flush=True)
        if tg.enabled and not tg.send(text):
            log(f"{p['symbol']} : echec de l'envoi Telegram")
    if plans:
        add_signals(cfg, [{
            "kind": "surge", "interval": cfg["radar_interval"], "sid": p["symbol"], "symbol": p["symbol"],
            "ts": p["ts"], "price": p["price"], "score": min(100, int(p["ratio"] * 5)),
            "stop": p["stop"], "tp1": p["tp1"], "tp2": p["tp2"],
        } for p in plans])
        log(f"Voie rapide : {len(plans)} depart(s) en cours signale(s).")
    return len(plans)


def surge_loop(cfg: dict) -> None:
    """Fil separe : la voie rapide ne doit pas attendre la fin d'un passage complet."""
    watcher, tg = surge.SurgeWatcher(cfg, MarketData()), Telegram()
    while True:
        started = time.time()
        try:
            surge_once(cfg, watcher, tg)
        except Exception as exc:                      # le fil ne doit pas mourir
            log(f"Erreur pendant la voie rapide : {exc}")
        time.sleep(max(5.0, float(cfg["surge_seconds"]) - (time.time() - started)))


def cmd_watch(args) -> None:
    cfg = load_config(args.config)
    md, tg, ctxp = MarketData(), Telegram(), ContextProvider()
    newsp, exch = NewsProvider(), Exchanges()
    scanner = wide.WideScanner(cfg) if cfg["wide"] else None
    alpha_data = alpha.AlphaData() if cfg["alpha"] else None
    chain_data = make_onchain(cfg) if alpha_data else None
    step = INTERVAL_MS[cfg["interval"]] / 1000
    radar_step = (INTERVAL_MS[cfg["radar_interval"]] / 1000
                  if (cfg["radar"] or cfg["alpha"]) else step)
    tick = min([step, radar_step] + ([cfg["wide_interval_min"] * 60] if scanner else []))
    log(f"Smart Trader Pro {__version__} - replis {cfg['interval']}, "
        f"signal Binance {cfg['radar_interval'] if cfg['radar'] else 'inactif'}, "
        f"Binance Alpha {'actif' if alpha_data else 'inactif'}, "
        f"marche large {'toutes les ' + str(cfg['wide_interval_min']) + ' min' if scanner else 'inactif'}, "
        f"Telegram {'actif' if tg.enabled else 'inactif'}.")
    if cfg["surge"]:
        threading.Thread(target=surge_loop, args=(cfg,), daemon=True, name="voie-rapide").start()
        log(f"Voie rapide active : verification toutes les {cfg['surge_seconds']} s.")
    last_candle = last_radar = None
    while True:
        moment = time.time() - 30                     # une bougie est lue 30 s apres sa cloture
        if (cfg["radar"] or alpha_data) and int(moment // radar_step) != last_radar:
            last_radar = int(moment // radar_step)
            if cfg["radar"]:
                try:
                    radar_once(cfg, md, tg, ctxp, newsp, exch)
                except Exception as exc:              # le service ne doit pas tomber
                    log(f"Erreur pendant le signal Binance : {exc}")
            if alpha_data:
                try:
                    alpha_once(cfg, md, alpha_data, tg, ctxp, chain_data)
                except Exception as exc:
                    log(f"Erreur pendant Binance Alpha : {exc}")
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
        if cfg["tracking"]:
            try:
                track_once(cfg, md, alpha_data)
            except Exception as exc:
                log(f"Erreur pendant le suivi des alertes : {exc}")
        now = time.time()
        wait = tick - (now % tick) + 30
        log(f"Prochain passage dans {int(wait // 60)} min.")
        time.sleep(wait)


def cmd_backtest(args) -> None:
    cfg = load_config(args.config)
    md = MarketData()
    engine, p = engine_for(args, cfg)
    radar = engine is not strategy                # moteurs de Binance en 15 minutes
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
    print(f"Backtest {getattr(args, 'mode', 'repli')} {interval} sur {args.candles} bougies - objectif {tp_r:g}R, "
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
    radar = engine is not strategy                # moteurs de Binance en 15 minutes
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
    from .store import Store
    from .exchange import Vault
    from .trading import Trader
    store = Store(cfg["app_db"])
    trader = Trader(cfg, store, Vault(os.environ.get("APP_SECRET_KEY")))
    app = web.App(cfg, store, chain.Chain(cfg["pay_rpc"] or None),
                  MarketData(), alpha.AlphaData(pause=0) if cfg["alpha"] else None, trader)
    log(f"Application web sur {host}:{port} - abonnements "
        f"{'ouverts' if app.pay_ready else 'fermes (pay_address non renseignee)'}, "
        f"paywall {'actif' if cfg['paywall'] else 'inactif'}, trading "
        f"{'actif' if trader.enabled else 'inactif'}"
        f"{' (administrateur seul)' if trader.enabled and not cfg['trading_clients'] else ''}.")
    web.serve(cfg["status_file"], cfg["signals_file"], host=host, port=port,
              wide_file=cfg["wide_file"], radar_file=cfg["radar_file"],
              alpha_file=cfg["alpha_file"], tracking_file=cfg["tracking_file"], app=app)


def cmd_admin(args) -> None:
    """Cree le compte administrateur, ou change son mot de passe. Le mot de passe est
    demande au clavier sans etre affiche ni enregistre dans l'historique du terminal."""
    import getpass
    from .store import Store
    cfg = load_config(args.config)
    store = Store(cfg["app_db"])
    email = (args.email or input("Adresse e-mail de l'administrateur : ")).strip().lower()
    first = getpass.getpass("Mot de passe (8 caracteres au moins, rien ne s'affiche) : ")
    if first != getpass.getpass("Encore une fois : "):
        sys.exit("Les deux saisies sont differentes. Rien n'a ete change.")
    try:
        if store.user_by_email(email):
            store.set_password(email, first)
        else:
            store.create_user(email, first)
        store.set_admin(email)
    except ValueError as exc:
        sys.exit(str(exc))
    print("Compte administrateur pret. Connecte-toi sur l'application avec cette adresse.")


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
    bt.add_argument("--mode", choices=("repli", "radar", "pression"), default="repli")
    bt.add_argument("--count", type=int, default=0, help="nombre de paires (mode radar)")
    bt.add_argument("--interval", choices=sorted(INTERVAL_MS), help="bougies a utiliser")
    bt.add_argument("--tp-r", type=float, default=0.0)
    bt.add_argument("--max-bars", type=int, default=72)
    bt.add_argument("--fee", type=float, default=0.1)
    bt.set_defaults(fn=cmd_backtest)
    cp = sub.add_parser("compare", help="comparer des variantes, avec validation sur donnees jamais vues")
    cp.add_argument("--mode", choices=("repli", "radar", "pression"), default="repli")
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
    ad = sub.add_parser("admin", help="creer le compte administrateur de l'application web")
    ad.add_argument("--email")
    ad.set_defaults(fn=cmd_admin)
    sub.add_parser("test-telegram", help="verifier Telegram").set_defaults(fn=cmd_test_telegram)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
