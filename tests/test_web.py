import json
import threading
import urllib.error
import urllib.request

import pytest

import smart_trader_pro.context as context
from smart_trader_pro import cli, web
from smart_trader_pro.data import MarketData
from smart_trader_pro.strategy import Params, evaluate, prepare, warmup
from conftest import HOUR, make_candles
from test_io import FakeResponse, kline_rows

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture
def server(tmp_path):
    status, signals = tmp_path / "status.json", tmp_path / "signals.json"
    srv = web.make_server(str(status), str(signals), host="127.0.0.1", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", status, signals
    srv.shutdown()
    srv.server_close()


def test_page_and_api(server):
    url, status, signals = server
    r = OPENER.open(url + "/")
    html = r.read().decode()
    assert r.status == 200 and "Smart Trader" in html and "Aziz Mernissi" in html
    assert "default-src 'none'" in r.headers["Content-Security-Policy"]
    assert json.loads(OPENER.open(url + "/api/status").read()) == {"status": None, "signals": []}
    status.write_text(json.dumps({"rows": [{"symbol": "BTCUSDT"}]}))
    signals.write_text("pas du json")                       # fichier abime : tolere
    data = json.loads(OPENER.open(url + "/api/status?x=1").read())
    assert data["status"]["rows"][0]["symbol"] == "BTCUSDT" and data["signals"] == []


@pytest.mark.parametrize("path", ["/.env", "/config.json", "/../.env", "/api", "/status.json"])
def test_nothing_else_is_served(server, path):
    with pytest.raises(urllib.error.HTTPError) as err:
        OPENER.open(server[0] + path)
    assert err.value.code == 404


def test_post_is_refused(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        OPENER.open(urllib.request.Request(server[0] + "/", data=b"x", method="POST"))
    assert err.value.code == 501


def test_scan_writes_status_and_signal_history(monkeypatch, tmp_path):
    c = make_candles()
    p = Params()
    f = prepare(c, p)
    idx = next(i for i in range(warmup(p), len(c)) if evaluate(f, i, p).is_signal)
    md = MarketData(base_urls=["https://x"])
    monkeypatch.setattr(md.session, "get",
                        lambda url, params=None, timeout=None:
                        FakeResponse(kline_rows(c, idx + 1)[-params["limit"]:]))
    monkeypatch.setattr("smart_trader_pro.data.time.time", lambda: (c.ts[idx] + HOUR + 5) / 1000)

    class Tg:
        enabled = False

    class Ctx:
        def get(self):
            return context.MarketContext(market_change_24h=1.5, btc_dominance=59.4,
                                         sources=["CoinGecko"])

    cfg = dict(cli.DEFAULTS, symbols=["TESTUSDT"], btc_filter=False, radar=False,
               state_file=str(tmp_path / "state.json"),
               status_file=str(tmp_path / "status.json"),
               signals_file=str(tmp_path / "signals.json"))
    assert cli.scan_once(cfg, md, Tg(), Ctx()) == 1
    status = json.loads((tmp_path / "status.json").read_text())
    row = status["rows"][0]
    assert status["blocked"] == "" and status["min_score"] == 70 and status["interval"] == "1h"
    assert status["context"]["btc_dominance"] == 59.4
    assert row["symbol"] == "TESTUSDT" and row["signal"] is True and row["stop"] < row["price"]
    assert sum(t for _, t in row["families"].values()) == 100
    history = json.loads((tmp_path / "signals.json").read_text())
    assert len(history) == 1 and history[0]["ts"] == c.ts[idx]
    assert cli.scan_once(cfg, md, Tg(), Ctx()) == 0          # pas de doublon dans l'historique
    assert len(json.loads((tmp_path / "signals.json").read_text())) == 1
