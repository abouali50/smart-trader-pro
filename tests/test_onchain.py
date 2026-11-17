import json

from smart_trader_pro import onchain


def pool(reserve, buyers, sellers, v15, v1h, v24, buys=None, sells=None):
    return {"attributes": {"reserve_in_usd": str(reserve),
                           "volume_usd": {"m15": str(v15), "h1": str(v1h), "h24": str(v24)},
                           "transactions": {"h1": {"buyers": buyers, "sellers": sellers,
                                                   "buys": buys or buyers, "sells": sells or sellers}}}}


def test_summarize_reads_buyers_volume_and_liquidity():
    s = onchain.summarize([pool(1_500_000, 80, 40, 60_000, 120_000, 900_000),
                           pool(600_000, 10, 10, 0, 20_000, 100_000),
                           pool(0, 999, 0, 1, 1, 1)])                          # pool vide : ignore
    assert s["verdict"] == "confirme" and s["pools"] == 2 and s["buyers"] == 90 and s["sellers"] == 50
    assert abs(s["buyer_share"] - 90 / 140) < 1e-9 and abs(s["acceleration"] - 60_000 * 4 / 140_000) < 1e-9
    assert s["liquidity"] == 2_100_000 and "64 %" in s["lines"][0] and "2.1 M $" in s["lines"][-1]
    assert onchain.summarize([pool(500_000, 30, 70, 5_000, 40_000, 300_000)])["verdict"] == "contredit"
    assert onchain.summarize([pool(500_000, 60, 40, 2_000, 40_000, 300_000)])["verdict"] == "neutre"   # volume qui ralentit
    quiet = onchain.summarize([pool(40_000, 3, 2, 0, 500, 9_000)])
    assert quiet["verdict"] == "neutre" and quiet["buyer_share"] is None and "faible" in quiet["lines"][-1]
    orders = onchain.summarize([pool(500_000, None, None, 30_000, 60_000, 300_000, buys=70, sells=30)])
    assert orders["verdict"] == "confirme" and "ordres" in orders["lines"][0]   # pas de portefeuilles : on compte les ordres
    assert onchain.summarize([]) is None and onchain.summarize(None) is None and onchain.summarize([{}]) is None


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body

    def json(self):
        return self._body


class Session:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        return self.replies.pop(0)


def test_flow_is_capped_cached_and_survives_errors():
    good = Resp(200, {"data": [pool(500_000, 80, 20, 30_000, 60_000, 300_000)]})
    s = Session([good, Resp(429, {}), good])
    oc = onchain.OnChain(api_key="cle", per_pass=2, session=s)
    a, b = "0x" + "aa" * 20, "0x" + "bb" * 20
    assert oc.flow("56", a)["verdict"] == "confirme"
    url, headers = s.calls[0]
    assert url.endswith(f"/onchain/networks/bsc/tokens/{a}/pools") and headers == {"x-cg-pro-api-key": "cle"}
    assert oc.flow("56", a.upper().replace("0X", "0x"))["verdict"] == "confirme" and len(s.calls) == 1   # en memoire
    assert oc.flow("56", b) is None and oc.failed == 1                         # panne : pas d'exception
    assert oc.flow("56", "0x" + "cc" * 20) is None and len(s.calls) == 2       # plafond du passage atteint
    oc.start_pass()
    assert oc.flow("56", b)["verdict"] == "confirme" and len(s.calls) == 3
    assert oc.flow("999", a) is None and oc.flow("56", "") is None             # reseau non gere
    off = onchain.OnChain(api_key="", per_pass=8)
    assert not off.enabled and off.flow("56", a) is None
    assert not onchain.OnChain(api_key="cle", per_pass=0).enabled


def test_alpha_alert_carries_the_onchain_verdict(tmp_path, monkeypatch):
    from smart_trader_pro import cli, context
    from smart_trader_pro.data import MarketData
    from test_alpha import FakeAlpha, flat_btc, quiet_then_breakout

    class WithContract(FakeAlpha):
        def tokens(self, *args):
            out = super().tokens(*args)
            self.tokens_by_id["ALPHA_7"].update(contract="0x" + "5a" * 20, chain_id="56")
            return out

    pump = quiet_then_breakout()
    monkeypatch.setattr(MarketData, "klines",
                        lambda self, sym, interval, total, now_ms=None: flat_btc(len(pump)))
    sent = []
    tg = type("Tg", (), {"enabled": True, "send": lambda self, text: sent.append(text) or True})()
    ctx = type("Ctx", (), {"get": lambda self: context.MarketContext()})()
    cfg = dict(cli.DEFAULTS, radar_interval="1h", state_file=str(tmp_path / "state.json"),
               signals_file=str(tmp_path / "signals.json"), alpha_file=str(tmp_path / "alpha.json"))
    s = Session([Resp(200, {"data": [pool(500_000, 20, 80, 30_000, 60_000, 300_000)]})])
    oc = onchain.OnChain(api_key="cle", per_pass=8, session=s)
    assert cli.alpha_once(cfg, MarketData(), WithContract({"ALPHA_7": pump}), tg, ctx, oc) == 1
    assert "Flux on-chain (DEX) : Les vendeurs dominent on-chain" in sent[0] and "20 acheteurs / 80 vendeurs" in sent[0]
    out = json.loads((tmp_path / "alpha.json").read_text())
    assert out["onchain"] is True and out["rows"][0]["onchain"]["verdict"] == "contredit"
    assert out["rows"][0]["token"]["contract"] == "0x" + "5a" * 20
    assert json.loads((tmp_path / "signals.json").read_text())[0]["onchain"] == "contredit"
    assert len(s.calls) == 1                                                   # alerte et ligne : un seul appel
    # Sans lecteur on-chain, ou coupe dans les reglages, l'alerte part quand meme.
    cfg2 = dict(cfg, onchain=False, state_file=str(tmp_path / "s2.json"))
    assert cli.alpha_once(cfg2, MarketData(), WithContract({"ALPHA_7": pump}), tg, ctx, oc) == 1
    assert "Flux on-chain" not in sent[1] and cli.make_onchain(cfg2) is None
