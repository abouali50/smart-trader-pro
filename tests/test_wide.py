import json
import threading
import urllib.request

from smart_trader_pro import cli, web, wide
from test_io import FakeResponse

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
T = 1_800_000_000


def coin(cid, price=0.5, mcap=20e6, vol=2e6, h1=0.2, d1=1.0, d7=2.0, sym=None, name=None, rank=900):
    return {"id": cid, "symbol": sym or cid[:4], "name": name or cid.title(), "current_price": price,
            "market_cap": mcap, "market_cap_rank": rank, "total_volume": vol,
            "price_change_percentage_1h_in_currency": h1,
            "price_change_percentage_24h_in_currency": d1,
            "price_change_percentage_7d_in_currency": d7}


class Market:
    """Faux CoinGecko : une page de cryptos, modifiable entre deux balayages."""

    def __init__(self, coins):
        self.coins = coins
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        if url.endswith("/coins/markets"):
            return FakeResponse(self.coins if params["page"] == 1 else [])
        if "/tickers" in url:
            return FakeResponse({"tickers": [{"market": {"name": "SmallDex"}},
                                             {"market": {"name": "Gate"}},
                                             {"market": {"name": "Gate"}},
                                             {"market": {"name": "Binance"}}]})
        return FakeResponse({}, status=404)


def scanner(tmp_path, market, **over):
    cfg = dict(wide_pages=2, wide_state_file=str(tmp_path / "ws.json"),
               wide_file=str(tmp_path / "w.json"), **over)
    sc = wide.WideScanner(cfg, sleep=lambda s: None)
    sc.session.get = market.get
    return sc


def test_noise_filter():
    assert wide.is_noise(coin("tether", price=1.0, d7=0.1, sym="usdt"))
    assert wide.is_noise(coin("x", name="Wrapped Bitcoin"))
    assert wide.is_noise(coin("pegged", price=1.001, d7=0.3))
    assert not wide.is_noise(coin("sol", price=71.0))
    assert not wide.is_noise(coin("dollarish", price=1.0, d7=25.0))


def test_score_scale():
    pts, why = wide.score(coin("a", h1=12, d1=14, d7=20, mcap=10e6, vol=12e6), vol_x=3.5)
    assert pts == 100 and len(why) == 5
    assert wide.score(coin("a", h1=0.1), vol_x=None)[0] == 20          # seulement "pas trop tard"
    assert wide.score(coin("a", h1=12, d1=45, d7=120, vol=12e6, mcap=10e6), vol_x=3.5)[0] == 80


def test_learning_then_alert_then_24h_result(tmp_path):
    quiet = [coin("gem"), coin("calm"), coin("tether", price=1.0, d7=0.1, sym="usdt"),
             coin("dust", vol=1000)]
    m = Market(quiet)
    sc = scanner(tmp_path, m)
    for k in range(4):                                  # 4 balayages calmes : apprentissage
        res = sc.sweep(now=T + k * 900)
        assert res["new"] == [] and res["scanned"] == 4 and res["eligible"] == 2
    assert res["learning"] == 2

    m.coins = [coin("gem", price=0.6, vol=9e6, h1=11, d1=18, d7=22), coin("calm"),
               coin("tether", price=1.0, d7=0.1, sym="usdt")]
    res = sc.sweep(now=T + 4 * 900)
    assert [a["id"] for a in res["new"]] == ["gem"]
    a = res["new"][0]
    assert a["vol_x"] == 4.5 and a["exchanges"] == ["Binance", "Gate", "SmallDex"] and a["score"] >= 90
    assert res["rows"][0]["id"] == "gem" and res["rows"][0]["alert"]
    text = wide.format_alert(a)
    assert text.startswith("REVEIL - Gem (GEM)") and "x4.5" in text and "Binance, Gate" in text
    assert "coingecko.com/fr/pieces/gem" in text

    assert sc.sweep(now=T + 5 * 900)["new"] == []        # delai entre deux alertes

    m.coins = [coin("gem", price=0.75), coin("calm")]
    res = sc.sweep(now=T + 4 * 900 + 86_400 + 60)        # 24 h plus tard
    tracked = res["alerts"][0]
    assert round(tracked["result_24h"], 1) == 25.0
    assert res["summary"] == {"count": 1, "average": tracked["result_24h"], "positive_pct": 100.0}
    m.coins = [coin("gem", price=0.30), coin("calm")]
    res = sc.sweep(now=T + 4 * 900 + 2 * 86_400)
    assert round(res["alerts"][0]["result_24h"], 1) == 25.0   # le resultat a 24 h est fige
    assert json.loads((tmp_path / "w.json").read_text())["scanned"] == 2


def test_outage_keeps_history_and_reports_error(tmp_path):
    m = Market([coin("gem")])
    sc = scanner(tmp_path, m)
    sc.sweep(now=T)

    def down(url, params=None, timeout=None):
        return FakeResponse({}, status=500)

    sc.session.get = down
    res = sc.sweep(now=T + 900)
    assert res["scanned"] == 0 and len(res["errors"]) == 1
    assert json.loads((tmp_path / "ws.json").read_text())["hist"]["gem"]


def test_quota_is_retried(tmp_path):
    m = Market([coin("gem")])
    sc = scanner(tmp_path, m)
    waits, answers = [], [FakeResponse({}, status=429)]

    def flaky(url, params=None, timeout=None):
        return answers.pop() if answers else m.get(url, params=params, timeout=timeout)

    sc.session.get = flaky
    sc.sleep = waits.append
    assert sc.sweep(now=T)["scanned"] == 1 and 60 in waits


def test_wide_once_sends_alerts_and_api_serves_wide(tmp_path):
    class Scanner:
        def sweep(self):
            return {"errors": [], "scanned": 4000, "eligible": 900, "learning": 0,
                    "new": [{"id": "gem", "symbol": "GEM", "name": "Gem", "price": 0.6, "score": 95,
                             "chg_1h": 11.0, "chg_24h": 18.0, "vol_x": 4.5, "mcap": 2e7, "rank": 900,
                             "exchanges": ["Gate"]}]}

    sent = []

    class Tg:
        enabled = True

        def send(self, text):
            sent.append(text)
            return True

    assert cli.wide_once(Scanner(), Tg()) == 1 and sent[0].startswith("REVEIL - Gem")

    wf = tmp_path / "wide.json"
    wf.write_text(json.dumps({"scanned": 4000, "rows": []}))
    srv = web.make_server(str(tmp_path / "s.json"), str(tmp_path / "g.json"), host="127.0.0.1",
                          port=0, wide_file=str(wf))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        data = json.loads(OPENER.open(f"http://127.0.0.1:{srv.server_address[1]}/api/status").read())
    finally:
        srv.shutdown()
        srv.server_close()
    assert data["wide"]["scanned"] == 4000 and data["status"] is None


def test_paid_key_switches_endpoint_and_demo_key_is_not_used(tmp_path, monkeypatch):
    monkeypatch.setenv("COINGECKO_API_KEY", "demo")
    free = wide.WideScanner({}, sleep=lambda s: None)
    assert free.api == wide.API and "x-cg-demo-api-key" not in free.session.headers and free.pause == 7.0
    monkeypatch.setenv("COINGECKO_PRO_API_KEY", "paid")
    paid = wide.WideScanner({}, sleep=lambda s: None)
    assert paid.api == wide.PRO_API and paid.session.headers["x-cg-pro-api-key"] == "paid"
    assert paid.pause < 1
