from smart_trader_pro import quality, tracker


def conf(*ratios):
    return [{"name": f"P{i}", "ratio": r, "confirmed": r is not None and r >= 2} for i, r in enumerate(ratios)]


def test_grade_reads_what_surrounds_the_breakout():
    ace = quality.grade(1.72, 3.5, -2.75, conf(0.9, 0.2), 0.02)        # l'alerte ACE du 17/11
    assert ace["grade"] == "C" and ace["fav"] == 1 and ace["con"] == 3 and ace["known"] == 5
    assert any("Binance seulement" in c for c in ace["cons"]) and any("serre" in c for c in ace["cons"])
    best = quality.grade(4.0, 2.0, 1.2, conf(3.1, 0.5), 0.18)
    assert best["grade"] == "A" and best["fav"] == 5 and best["con"] == 0
    assert quality.grade(4.0, 2.0, 1.2, conf(3.1, 0.5), -0.2)["grade"] == "B"     # 4 pour mais 1 contre
    assert quality.grade(4.0, 7.0, -1.0, conf(3.1), 0.05)["grade"] == "B"         # 2 pour, 0 contre
    assert quality.grade(9.5, 14.0, -3.0, conf(0.4), 0.3)["grade"] == "C"         # 3 contre
    # Jeton Alpha : le flux on-chain remplace les autres plateformes.
    on = quality.grade(4.0, 2.0, 1.0, None, 0.2, {"verdict": "confirme", "buyer_share": 0.64})
    assert on["grade"] == "A" and any("on-chain (64 %" in p for p in on["pros"])
    off = quality.grade(4.0, 2.0, 1.0, None, 0.2, {"verdict": "contredit", "buyer_share": 0.3})
    assert off["grade"] == "B" and off["con"] == 1
    # Donnees manquantes : non comptees, et jamais de A sans assez d'elements.
    thin = quality.grade(4.0, None, None, conf(None, None), None)
    assert thin["known"] == 1 and thin["grade"] == "C"
    text = quality.lines(ace)
    assert text[0] == "Qualite : C - 1 point(s) pour, 3 contre, sur 5" and text[1].startswith("  Pour : ")
    assert quality.allowed("A", "B") and quality.allowed("B", "b") and not quality.allowed("C", "B")
    assert quality.allowed("C", "C") and not quality.allowed("B", "A") and quality.allowed("C", "inconnu")


def test_tally_is_split_by_quality():
    def s(q, status, r):
        return {"kind": "radar", "quality": q, "outcome": {"status": status, "r": r, "pct": r}}
    g = tracker.summary([s("A", "tp2", 2.9), s("A", "stop", -1.1), s("C", "stop", -1.0), s("C", "open", 0.2),
                         {"kind": "radar", "outcome": {"status": "stop", "r": -1.0, "pct": -1}}])["radar"]
    assert g["closed"] == 4 and g["by_quality"]["A"]["closed"] == 2 and g["by_quality"]["A"]["win_rate"] == 50
    assert abs(g["by_quality"]["A"]["avg_r"] - 0.9) < 1e-9 and g["by_quality"]["C"] == {
        "closed": 1, "wins": 0, "total_r": -1.0, "win_rate": 0.0, "avg_r": -1.0}


def test_alert_shows_the_grade_and_min_quality_holds_weak_ones(monkeypatch, tmp_path):
    import json
    from smart_trader_pro import cli, context
    from smart_trader_pro.data import MarketData
    from test_breakout import flat_btc, quiet_then_breakout

    pump = quiet_then_breakout()
    series = {"PUMPUSDT": pump, "BTCUSDT": flat_btc(len(pump))}
    monkeypatch.setattr(MarketData, "klines", lambda self, sym, interval, total, now_ms=None: series[sym])
    monkeypatch.setattr(MarketData, "top_symbols",
                        lambda self, quote="USDT", count=20, min_quote_volume=0: ["PUMPUSDT"])
    sent = []
    tg = type("Tg", (), {"enabled": True, "send": lambda self, text: sent.append(text) or True})()
    ctx = type("Ctx", (), {"get": lambda self: context.MarketContext(market_change_24h=-3.0)})()
    cfg = dict(cli.DEFAULTS, symbols=[], radar_interval="1h", pressure=False, exchanges=False,
               state_file=str(tmp_path / "state.json"), signals_file=str(tmp_path / "signals.json"),
               radar_file=str(tmp_path / "radar.json"))
    assert cli.radar_once(cfg, MarketData(), tg, ctx) == 1
    head = sent[0].splitlines()
    assert head[0].startswith("🚨 EXPLOSION BINANCE - PUMPUSDT") and head[1].startswith("Score : ")
    assert head[2].startswith("Qualite : ") and "marche en baisse sur 24 h (-3.0 %)" in sent[0]
    row = json.loads((tmp_path / "radar.json").read_text())["rows"][0]
    saved = json.loads((tmp_path / "signals.json").read_text())[0]
    assert row["quality"]["grade"] == saved["quality"] and "sent" not in saved
    grade = saved["quality"]
    # Meme cassure avec un minimum plus exigeant que sa note : suivie, mais pas envoyee.
    stricter = {"C": "B", "B": "A"}.get(grade)
    if stricter:
        cfg2 = dict(cfg, min_quality=stricter, state_file=str(tmp_path / "s2.json"),
                    signals_file=str(tmp_path / "g2.json"))
        assert cli.radar_once(cfg2, MarketData(), tg, ctx) == 0 and len(sent) == 1
        kept = json.loads((tmp_path / "g2.json").read_text())
        assert kept[0]["sent"] is False and kept[0]["quality"] == grade
        assert cli.radar_once(cfg2, MarketData(), tg, ctx) == 0 and len(json.loads((tmp_path / "g2.json").read_text())) == 1
