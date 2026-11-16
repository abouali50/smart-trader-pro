import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from smart_trader_pro import chain, cli, web
from smart_trader_pro.data import Candles
from smart_trader_pro.store import Store

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MERCHANT = "0x" + "ab" * 20
PAYER = "0x" + "cd" * 20
TX = "0x" + "11" * 32


def pad(addr):
    return "0x" + "0" * 24 + addr[2:]


class FakeChain(chain.Chain):
    """Noeud simule : une seule transaction connue."""

    def __init__(self):
        super().__init__(["https://rpc.invalid"])
        self.tx = {}
        self.head = 1000

    def add(self, tx, units, block=990, ts=None, token=chain.USDT_BSC, to=MERCHANT, status="0x1"):
        self.tx[tx.lower()] = {"units": units, "block": block, "ts": ts or int(time.time()),
                               "token": token, "to": to, "status": status}

    def call(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getTransactionReceipt":
            t = self.tx.get(params[0].lower())
            if not t:
                return None
            return {"status": t["status"], "blockNumber": hex(t["block"]), "logs": [
                {"address": "0x" + "99" * 20, "topics": [chain.TRANSFER_TOPIC, pad(PAYER), pad(MERCHANT)],
                 "data": hex(10 ** 30)},                                    # autre jeton : ignore
                {"address": t["token"], "topics": [chain.TRANSFER_TOPIC, pad(PAYER), pad(t["to"])],
                 "data": hex(t["units"])}]}
        if method == "eth_getBlockByNumber":
            t = next(v for v in self.tx.values() if hex(v["block"]) == params[0])
            return {"timestamp": hex(t["ts"])}
        raise AssertionError(method)


def test_units_are_exact():
    assert chain.to_units(29, 18) == 29 * 10 ** 18
    assert chain.to_units(0.1 + 0.2, 18) == 3 * 10 ** 17                  # pas d'erreur de flottant
    assert chain.format_units(29 * 10 ** 18 + 42 * 10 ** 14, 18) == "29.0042"
    assert chain.is_address(MERCHANT) and not chain.is_address("0x123") and not chain.is_address(None)


def test_verify_transfer_accepts_only_the_exact_payment():
    fc = FakeChain()
    units, now = 29 * 10 ** 18 + 42 * 10 ** 14, int(time.time())
    args = (chain.USDT_BSC, MERCHANT, units, now - 60)
    with pytest.raises(chain.Rejected):
        fc.verify_transfer("0x1234", *args)                               # identifiant mal forme
    with pytest.raises(chain.Pending):
        fc.verify_transfer(TX, *args)                                     # pas encore minee
    fc.add(TX, units)
    ok = fc.verify_transfer(TX, *args)
    assert ok["from"] == PAYER and ok["confirmations"] == 11
    fc.head = 992
    with pytest.raises(chain.Pending):
        fc.verify_transfer(TX, *args)                                     # 3 confirmations sur 5
    fc.head = 1000
    for change in ({"units": units - 1}, {"units": units + 10 ** 14}, {"to": PAYER},
                   {"token": "0x" + "77" * 20}, {"status": "0x0"}, {"ts": now - 3600}):
        fc.add(TX, **{"units": units, **change})
        with pytest.raises(chain.Rejected):
            fc.verify_transfer(TX, *args)


def test_store_accounts_sessions_and_orders():
    s = Store(":memory:")
    for bad in (("pas-un-mail", "motdepasse1"), ("a@b.co", "court")):
        with pytest.raises(ValueError):
            s.create_user(*bad)
    u = s.create_user("  Client@Exemple.com ", "motdepasse1")
    assert u["email"] == "client@exemple.com" and b"motdepasse1" not in u["pw_hash"]
    with pytest.raises(ValueError):
        s.create_user("client@exemple.com", "autremotdepasse")
    assert s.authenticate("client@exemple.com", "motdepasse1")["id"] == u["id"]
    assert s.authenticate("client@exemple.com", "mauvais-mot") is None
    assert s.authenticate("inconnu@exemple.com", "motdepasse1") is None
    token = s.new_session(u["id"])
    assert s.user_by_session(token)["id"] == u["id"] and s.user_by_session("faux") is None
    assert token not in json.dumps([dict(r) for r in s.db.execute("SELECT token_hash FROM sessions")])
    assert s.user_by_session(token, now=int(time.time()) + 31 * 86400) is None    # session expiree
    s.drop_session(token)
    assert s.user_by_session(token) is None

    step = 10 ** 14
    o1 = s.create_order(u["id"], "month", 30, 29 * 10 ** 18, step)
    assert 29 * 10 ** 18 < int(o1["units"]) < 30 * 10 ** 18 and int(o1["units"]) % step == 0
    o2 = s.create_order(u["id"], "year", 365, 249 * 10 ** 18, step)            # remplace la premiere
    assert s.order(o1["id"])["status"] == "expired" and s.pending_order(u["id"])["id"] == o2["id"]
    other = s.create_user("autre@exemple.com", "motdepasse2")
    seen = {s.create_order(other["id"], "month", 30, 29 * 10 ** 18, step)["units"] for _ in range(30)}
    assert o2["units"] not in seen
    until = s.mark_paid(o2["id"], TX.upper().replace("0X", "0x"))
    assert until > time.time() + 364 * 86400 and s.tx_used(TX)
    with pytest.raises(ValueError):
        s.mark_paid(o2["id"], "0x" + "22" * 32)                                 # deja payee
    o3 = s.pending_order(other["id"])
    with pytest.raises(ValueError):
        s.mark_paid(o3["id"], TX)                                               # transaction deja utilisee
    assert s.order(o3["id"])["status"] == "pending" and s.user_by_id(other["id"])["sub_until"] == 0
    assert s.grant(other["id"], 7) > time.time() + 6 * 86400
    ov = s.overview()
    assert ov["users"] == 2 and ov["active"] == 2 and ov["paid_orders"] == 1
    s.set_password("client@exemple.com", "nouveaumotdepasse")
    assert s.authenticate("client@exemple.com", "motdepasse1") is None


class Client:
    def __init__(self, base):
        self.base, self.cookie = base, ""

    def call(self, path, body=None, headers=None):
        h = {"Cookie": self.cookie} if self.cookie else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=h)
        try:
            r = OPENER.open(req)
            code, raw, hdrs = r.status, r.read(), r.headers
        except urllib.error.HTTPError as err:
            code, raw, hdrs = err.code, err.read(), err.headers
        sc = hdrs.get("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0]
            self.last_cookie = sc
        try:
            return code, json.loads(raw)
        except ValueError:
            return code, raw


class FakeMarket:
    def klines(self, symbol, interval, total, now_ms=None):
        if symbol == "NOPEUSDT":
            raise RuntimeError("inconnue")
        c = Candles(symbol, interval)
        for i in range(3):
            c.append(1_700_000_000_000 + i * 900_000, 1, 2, 0.5, 1.5, 100, 50)
        return c


@pytest.fixture
def site(tmp_path):
    files = {n: tmp_path / f"{n}.json" for n in ("status", "signals", "radar")}
    files["radar"].write_text(json.dumps({"rows": [
        {"symbol": f"C{i}USDT", "score": 80, "price": 1.0, "stop": 0.9, "tp1": 1.1, "tp2": 1.3,
         "reasons": ["cassure"], "signal": True} for i in range(8)],
        "pressure": {"rows": [{"symbol": "PUSDT", "stop": 1, "trigger": 2}]}}))
    files["signals"].write_text(json.dumps([{"symbol": "AUSDT", "price": 1, "stop": 0.9, "tp2": 2}]))
    cfg = dict(cli.DEFAULTS, pay_address=MERCHANT, dex_url="https://dex.exemple.com/swap?out={address}")
    fc = FakeChain()
    app = web.App(cfg, Store(str(tmp_path / "app.db")), fc, FakeMarket(), None)
    srv = web.make_server(str(files["status"]), str(files["signals"]), host="127.0.0.1", port=0,
                          radar_file=str(files["radar"]), app=app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Client(f"http://127.0.0.1:{srv.server_address[1]}"), app, fc
    srv.shutdown()
    srv.server_close()


def test_app_serves_page_icon_and_manifest(site):
    c, app, _ = site
    html = OPENER.open(c.base + "/").read().decode()
    assert "Smart Trader Pro" in html and "Aziz Mernissi" in html
    assert "Bilan des réveils" in OPENER.open(c.base + "/classique").read().decode()
    assert OPENER.open(c.base + "/icon.svg").read().startswith(b"<svg")
    assert json.loads(OPENER.open(c.base + "/manifest.webmanifest").read())["display"] == "standalone"


def test_register_login_and_session_cookie(site):
    c, app, _ = site
    code, me = c.call("/api/me")
    assert code == 200 and me["user"] is None and me["access"] is True and len(me["plans"]) == 3
    assert me["pay"]["address"] == MERCHANT and me["dex_url"].startswith("https://dex.exemple.com")
    assert c.call("/api/register", {"email": "x", "password": "motdepasse1"})[0] == 400
    code, me = c.call("/api/register", {"email": "client@exemple.com", "password": "motdepasse1"},
                      {"X-Forwarded-Proto": "https"})
    assert code == 200 and me["user"]["email"] == "client@exemple.com" and not me["user"]["admin"]
    assert "HttpOnly" in c.last_cookie and "SameSite=Lax" in c.last_cookie and "Secure" in c.last_cookie
    assert c.call("/api/me")[1]["user"]["email"] == "client@exemple.com"
    assert c.call("/api/admin")[0] == 403 and c.call("/api/admin/grant", {"email": "a@b.co", "days": 5})[0] == 403
    assert c.call("/api/logout", {})[0] == 200 and c.call("/api/me")[1]["user"] is None
    assert c.call("/api/login", {"email": "client@exemple.com", "password": "mauvais-mot"})[0] == 401
    assert c.call("/api/login", {"email": "client@exemple.com", "password": "motdepasse1"})[0] == 200
    # Requetes refusees : autre origine, mauvais format, corps trop long, non connecte.
    assert c.call("/api/logout", {}, {"Origin": "https://pirate.exemple"})[0] == 403
    assert c.call("/api/logout", {}, {"Content-Type": "text/plain"})[0] == 415
    assert c.call("/api/login", {"email": "a" * 9000, "password": "x"})[0] == 413
    assert Client(c.base).call("/api/orders", {"plan": "month"})[0] == 401


def test_login_attempts_are_limited(site):
    c, app, _ = site
    codes = [c.call("/api/login", {"email": "a@b.co", "password": "mauvais-mot"})[0] for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]


def test_subscription_is_activated_by_a_verified_payment(site):
    c, app, fc = site
    c.call("/api/register", {"email": "client@exemple.com", "password": "motdepasse1"})
    assert c.call("/api/orders", {"plan": "inconnu"})[0] == 400
    code, order = c.call("/api/orders", {"plan": "month"})
    assert code == 200 and order["amount"].startswith("29.") and order["days"] == 30
    assert c.call("/api/me")[1]["order"]["id"] == order["id"]
    code, res = c.call("/api/orders/confirm", {"id": order["id"], "tx": TX})
    assert code == 202 and res["pending"] is True                          # pas encore minee
    fc.add(TX, int(order["units"]) + 10 ** 14)
    assert c.call("/api/orders/confirm", {"id": order["id"], "tx": TX})[0] == 400   # mauvais montant
    fc.add(TX, int(order["units"]))
    code, res = c.call("/api/orders/confirm", {"id": order["id"], "tx": TX})
    assert code == 200 and res["sub_until"] > (time.time() + 29 * 86400) * 1000
    me = c.call("/api/me")[1]
    assert me["user"]["active"] is True and me["order"] is None
    # Un autre client ne peut pas reutiliser la meme transaction.
    thief = Client(c.base)
    thief.call("/api/register", {"email": "voleur@exemple.com", "password": "motdepasse1"})
    o2 = thief.call("/api/orders", {"plan": "month"})[1]
    assert thief.call("/api/orders/confirm", {"id": o2["id"], "tx": TX})[0] == 409
    assert thief.call("/api/me")[1]["user"]["active"] is False


def test_paywall_hides_levels_until_subscribed(site):
    c, app, _ = site
    app.cfg["paywall"] = True
    data = c.call("/api/status")[1]
    assert data["locked"] is True and len(data["radar"]["rows"]) == web.FREE_ROWS
    row = data["radar"]["rows"][0]
    assert row["symbol"] == "C0USDT" and row["score"] == 80 and row["locked"] is True
    assert not {"stop", "tp1", "tp2", "reasons"} & set(row)
    assert "trigger" not in data["radar"]["pressure"]["rows"][0] and "stop" not in data["signals"][0]
    assert c.call("/api/klines?symbol=BTCUSDT")[0] == 402
    c.call("/api/register", {"email": "client@exemple.com", "password": "motdepasse1"})
    assert c.call("/api/status")[1].get("locked") is True                  # inscrit mais pas abonne
    app.store.grant(app.store.user_by_email("client@exemple.com")["id"], 7)
    data = c.call("/api/status")[1]
    assert "locked" not in data and len(data["radar"]["rows"]) == 8 and data["radar"]["rows"][0]["stop"] == 0.9


def test_klines_admin_and_closed_subscriptions(site):
    c, app, _ = site
    code, k = c.call("/api/klines?symbol=btcusdt&interval=1h")
    assert code == 200 and k["symbol"] == "BTCUSDT" and len(k["c"]) == 3
    assert c.call("/api/klines?symbol=BTC/USDT")[0] == 400 and c.call("/api/klines?symbol=BTCUSDT&interval=1s")[0] == 400
    assert c.call("/api/klines?symbol=NOPEUSDT")[0] == 502 and c.call("/api/klines?src=alpha&symbol=ALPHA_1")[0] == 400
    c.call("/api/register", {"email": "chef@exemple.com", "password": "motdepasse1"})
    app.store.set_admin("chef@exemple.com")
    code, ov = c.call("/api/admin")
    assert code == 200 and ov["users"] == 1 and ov["revenue"] == "0.00" and ov["pay_ready"] is True
    assert c.call("/api/admin/grant", {"email": "chef@exemple.com", "days": 30})[0] == 200
    assert c.call("/api/admin/grant", {"email": "inconnu@exemple.com", "days": 30})[0] == 400
    app.cfg["pay_address"] = ""                                            # abonnements fermes
    assert c.call("/api/me")[1]["pay"] == dict(c.call("/api/me")[1]["pay"], ready=False, address="")
    assert c.call("/api/orders", {"plan": "month"})[0] == 409
    app.cfg["dex_url"] = "javascript:alert(1)//{address}"
    assert c.call("/api/me")[1]["dex_url"] == ""
