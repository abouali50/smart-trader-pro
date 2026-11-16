import base64
import json
import time
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from smart_trader_pro import exchange as ex
from smart_trader_pro.store import Store
from smart_trader_pro.trading import TradeError, Trader


def pem_key():
    k = ec.generate_private_key(ec.SECP256R1())
    return k, k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                              serialization.NoEncryption()).decode()


def unpad(x):
    return base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))


def test_jwt_is_signed_and_bound_to_the_request():
    k, pem = pem_key()
    token = ex.build_jwt("organizations/o/apiKeys/k", ex.load_private_key(pem), "post", "/api/v3/brokerage/orders", now=1000)
    h, p, s = token.split(".")
    head, claims, sig = json.loads(unpad(h)), json.loads(unpad(p)), unpad(s)
    assert head["alg"] == "ES256" and head["kid"] == "organizations/o/apiKeys/k" and len(head["nonce"]) == 32
    assert claims == {"sub": "organizations/o/apiKeys/k", "iss": "cdp", "nbf": 1000, "exp": 1120,
                      "uri": "POST api.coinbase.com/api/v3/brokerage/orders"}
    k.public_key().verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
                          (h + "." + p).encode(), ec.ECDSA(hashes.SHA256()))
    assert ex.load_private_key(pem.replace("\n", "\\n")) is not None          # cle collee sur une ligne
    ed = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    for bad in ("pas une cle", ed):
        with pytest.raises(ex.ExchangeError):
            ex.load_private_key(bad)


def test_vault_and_rounding():
    v = ex.Vault(ex.new_master_key())
    blob = v.seal("-----BEGIN EC PRIVATE KEY-----secret")
    assert b"secret" not in blob and v.open(blob).endswith("secret")
    with pytest.raises(ex.ExchangeError):
        ex.Vault(ex.new_master_key()).open(blob)                               # autre cle maitresse
    assert not ex.Vault(None).ready and not ex.Vault("trop-court").ready
    assert ex.fmt(ex.floor_to(Decimal("0.123456789"), Decimal("0.0001"))) == "0.1234"
    assert ex.fmt(ex.floor_to(Decimal("512.409"), Decimal("0.01"))) == "512.4" and ex.dec("x") == 0


class FakeResponse:
    def __init__(self, code, body):
        self.status_code, self._body = code, body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self):
        self.calls, self.replies = [], []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, json, headers))
        return self.replies.pop(0)


def test_coinbase_client_builds_orders_and_reads_errors():
    _, pem = pem_key()
    s = FakeSession()
    c = ex.Coinbase("organizations/o/apiKeys/k", pem, session=s)
    s.replies = [FakeResponse(200, {"success": True, "success_response": {"order_id": "B1"}}),
                 FakeResponse(200, {"success": True, "success_response": {"order_id": "S1"}}),
                 FakeResponse(200, {"success": False, "error_response": {"message": "Insufficient balance"}}),
                 FakeResponse(401, {}),
                 FakeResponse(200, {"accounts": [{"currency": "USDC", "available_balance": {"value": "50.5"}},
                                                 {"currency": "BTC", "available_balance": {"value": "0"}}]})]
    assert c.market_buy("ZEC-USDC", Decimal("25")) == "B1"
    method, url, body, headers = s.calls[0]
    assert (method, url) == ("POST", "https://api.coinbase.com/api/v3/brokerage/orders")
    assert body["side"] == "BUY" and body["order_configuration"] == {"market_market_ioc": {"quote_size": "25"}}
    assert headers["Authorization"].startswith("Bearer ") and len(body["client_order_id"]) == 36
    assert c.bracket_sell("ZEC-USDC", Decimal("0.05"), Decimal("580"), Decimal("489.5")) == "S1"
    assert s.calls[1][2]["order_configuration"] == {"trigger_bracket_gtc": {
        "base_size": "0.05", "limit_price": "580", "stop_trigger_price": "489.5"}} and s.calls[1][2]["side"] == "SELL"
    with pytest.raises(ex.ExchangeError, match="Insufficient balance"):
        c.market_buy("ZEC-USDC", Decimal("25"))
    with pytest.raises(ex.ExchangeError, match="401"):
        c.permissions()
    assert c.balances() == {"USDC": Decimal("50.5")}


class FakeCoinbase:
    """Plateforme simulee, partagee entre les instances."""
    perms = {"can_view": True, "can_trade": True, "can_transfer": False}
    bal = {"USDC": Decimal("200")}
    products = {"ZEC-USDC": {"product_id": "ZEC-USDC", "price": "500", "base_increment": "0.001",
                             "price_increment": "0.01", "quote_min_size": "1"}}
    orders, log = {}, []
    fail_bracket = False
    fill = True

    def __init__(self, key_name, private_pem):
        if "BEGIN" not in private_pem:
            raise ex.ExchangeError("Cle privee illisible.")
        self.key_name = key_name

    def permissions(self):
        return dict(self.perms)

    def balances(self):
        return dict(self.bal)

    def product(self, pid):
        if pid not in self.products:
            raise ex.ExchangeError("inconnu")
        return dict(self.products[pid])

    def market_buy(self, pid, quote):
        FakeCoinbase.log.append(("buy", pid, quote))
        oid = f"B{len(self.orders)}"
        price = Decimal("505")
        self.orders[oid] = ({"status": "FILLED", "filled_size": str(quote / price), "average_filled_price": str(price)}
                            if self.fill else {"status": "OPEN", "filled_size": "0", "average_filled_price": "0"})
        return oid

    def bracket_sell(self, pid, base, target, stop):
        if self.fail_bracket:
            raise ex.ExchangeError("Coinbase a refuse l'ordre : taille trop petite")
        FakeCoinbase.log.append(("bracket", pid, base, target, stop))
        oid = f"X{len(self.orders)}"
        self.orders[oid] = {"status": "OPEN"}
        return oid

    def market_sell(self, pid, base):
        FakeCoinbase.log.append(("sell", pid, base))
        oid = f"S{len(self.orders)}"
        self.orders[oid] = {"status": "FILLED", "average_filled_price": "510"}
        return oid

    def cancel(self, oid):
        FakeCoinbase.log.append(("cancel", oid))

    def order(self, oid):
        return dict(self.orders[oid])

    def wait_filled(self, oid, tries=8, pause=0.5):
        return self.order(oid)


@pytest.fixture
def desk():
    FakeCoinbase.perms = {"can_view": True, "can_trade": True, "can_transfer": False}
    FakeCoinbase.bal, FakeCoinbase.orders, FakeCoinbase.log = {"USDC": Decimal("200")}, {}, []
    FakeCoinbase.fail_bracket, FakeCoinbase.fill = False, True
    cfg = {"trading": True, "trading_clients": False, "trade_min": 5, "trade_max_order": 100,
           "trade_max_day": 150, "trade_max_drift_pct": 3}
    store = Store(":memory:")
    admin = store.create_user("chef@exemple.com", "motdepasse1")
    store.set_admin("chef@exemple.com")
    client = store.create_user("client@exemple.com", "motdepasse1")
    store.grant(client["id"], 30)
    t = Trader(cfg, store, ex.Vault(ex.new_master_key()), FakeCoinbase)
    return t, store, store.user_by_id(admin["id"]), store.user_by_id(client["id"])


ORDER = {"symbol": "ZECUSDT", "amount": 50, "ref": 498, "stop": 478.08, "target": 557.76}


def test_who_may_trade(desk):
    t, store, admin, client = desk
    assert t.allowed(admin) and not t.allowed(client) and not t.allowed(None)
    with pytest.raises(TradeError):
        t.plan(client, ORDER)
    t.cfg["trading_clients"] = True
    assert t.allowed(client)                                                   # abonne actif
    store.db.execute("UPDATE users SET sub_until = 0 WHERE id = ?", (client["id"],))
    assert not t.allowed(store.user_by_id(client["id"]))                       # abonnement expire
    assert not Trader(t.cfg, store, ex.Vault(None), FakeCoinbase).allowed(admin)   # pas de cle maitresse
    t.cfg["trading"] = False
    assert not t.allowed(admin) and t.status(admin)["enabled"] is False


def test_key_with_withdrawal_permission_is_refused(desk):
    t, store, admin, _ = desk
    with pytest.raises(TradeError, match="Connecte"):
        t.plan(admin, ORDER)
    FakeCoinbase.perms = {"can_view": True, "can_trade": True, "can_transfer": True}
    with pytest.raises(TradeError, match="Transfer"):
        t.connect(admin, "organizations/o/apiKeys/k", "-----BEGIN EC PRIVATE KEY-----x")
    FakeCoinbase.perms = {"can_view": True, "can_trade": False, "can_transfer": False}
    with pytest.raises(TradeError, match="Trade"):
        t.connect(admin, "organizations/o/apiKeys/k", "-----BEGIN EC PRIVATE KEY-----x")
    assert store.exchange_key(admin["id"], "coinbase") is None
    FakeCoinbase.perms = {"can_view": True, "can_trade": True, "can_transfer": False}
    st = t.connect(admin, "organizations/o/apiKeys/abcdef123456", "-----BEGIN EC PRIVATE KEY-----secret")
    assert st["connected"] and st["key"] == "…123456" and st["balances"] == {"USDC": 200.0}
    row = store.exchange_key(admin["id"], "coinbase")
    assert b"secret" not in bytes(row["secret"]) and b"BEGIN" not in bytes(row["secret"])   # chiffree en base
    assert t.disconnect(admin)["connected"] is False


def test_plan_rescales_levels_and_enforces_limits(desk):
    t, store, admin, _ = desk
    t.connect(admin, "organizations/o/apiKeys/k", "-----BEGIN EC PRIVATE KEY-----x")
    p = t.plan(admin, ORDER)
    assert p["product"] == "ZEC-USDC" and p["price"] == "500" and p["quantity"] == "0.1"
    assert p["stop"] == "480" and p["target"] == "560"                         # memes ecarts en % que le signal
    assert abs(p["max_loss"] - 2.0) < 1e-9 and abs(p["gain"] - 6.0) < 1e-9 and 0.4 < p["drift_pct"] < 0.41
    for change, why in (({"amount": 2}, "Montant"), ({"amount": 500}, "Montant"), ({"stop": 600}, "stop"),
                        ({"ref": 400, "stop": 380, "target": 450}, "ecarte"), ({"symbol": "NOPEUSDT"}, "negociable"),
                        ({"symbol": "../x"}, "inconnue"), ({"amount": "abc"}, "Montant")):
        with pytest.raises(TradeError, match=why):
            t.plan(admin, dict(ORDER, **change))
    FakeCoinbase.bal = {"USDC": Decimal("10")}
    with pytest.raises(TradeError, match="Solde insuffisant"):
        t.plan(admin, ORDER)
    assert FakeCoinbase.log == []                                              # un plan ne passe aucun ordre


def test_buy_places_protection_and_respects_daily_limit(desk):
    t, store, admin, _ = desk
    t.connect(admin, "organizations/o/apiKeys/k", "-----BEGIN EC PRIVATE KEY-----x")
    with pytest.raises(TradeError, match="Confirmation"):
        t.buy(admin, ORDER)
    with pytest.raises(TradeError, match="Confirmation"):
        t.buy(admin, dict(ORDER, confirm="true"))
    assert FakeCoinbase.log == []
    res = t.buy(admin, dict(ORDER, confirm=True))
    tr = res["trade"]
    assert res["ok"] and "warning" not in res and tr["status"] == "ouvert" and tr["entry"] == "505"
    assert tr["base_size"] == "0.099" and tr["stop"] == "484.8" and tr["target"] == "565.6"   # recalcules sur le prix d'achat
    assert [x[0] for x in FakeCoinbase.log] == ["buy", "bracket"]
    assert FakeCoinbase.log[1] == ("bracket", "ZEC-USDC", Decimal("0.099"), Decimal("565.60"), Decimal("484.80"))
    t.buy(admin, dict(ORDER, confirm=True))
    with pytest.raises(TradeError, match="Limite"):
        t.buy(admin, dict(ORDER, amount=60, confirm=True))                     # 100 deja engages sur 150
    st = t.status(admin)
    assert len(st["trades"]) == 2 and st["spent_today"] == 100.0
    # L'ordre de sortie s'execute sur Coinbase : la position passe en cloturee au prochain affichage.
    FakeCoinbase.orders[store.trade(tr["id"])["exit_id"]] = {"status": "FILLED", "average_filled_price": "565.6"}
    done = next(x for x in t.status(admin)["trades"] if x["id"] == tr["id"])
    assert done["status"] == "cloture" and done["note"] == "objectif atteint"


def test_buy_failures_are_reported_honestly(desk):
    t, store, admin, _ = desk
    t.connect(admin, "organizations/o/apiKeys/k", "-----BEGIN EC PRIVATE KEY-----x")
    FakeCoinbase.fail_bracket = True
    res = t.buy(admin, dict(ORDER, confirm=True))
    assert "PAS pu" in res["warning"] and res["trade"]["status"] == "sans_protection"
    closed = t.close(admin, res["trade"]["id"])                                # vente a la main
    assert closed["trade"]["status"] == "cloture" and FakeCoinbase.log[-1][0] == "sell"
    with pytest.raises(TradeError):
        t.close(admin, res["trade"]["id"])                                     # deja fermee
    FakeCoinbase.fail_bracket, FakeCoinbase.fill = False, False
    with pytest.raises(TradeError, match="non confirmee"):
        t.buy(admin, dict(ORDER, confirm=True))
    assert store.trades(admin["id"], 1)[0]["status"] == "a_verifier"


def test_close_cancels_protection_and_users_are_isolated(desk):
    t, store, admin, client = desk
    t.cfg["trading_clients"] = True
    t.connect(admin, "organizations/o/apiKeys/k", "-----BEGIN EC PRIVATE KEY-----x")
    t.connect(client, "organizations/o/apiKeys/c", "-----BEGIN EC PRIVATE KEY-----y")
    tr = t.buy(admin, dict(ORDER, confirm=True))["trade"]
    with pytest.raises(TradeError):
        t.close(client, tr["id"])                                              # la position d'un autre compte
    assert t.status(client)["trades"] == []
    FakeCoinbase.log.clear()
    out = t.close(admin, tr["id"])
    assert [x[0] for x in FakeCoinbase.log] == ["cancel", "sell"] and out["trade"]["exit_price"] == "510"
    with pytest.raises(TradeError):
        t.close(admin, "1")


def test_trade_api_requires_login_and_reports_errors(tmp_path):
    import threading
    from smart_trader_pro import cli, web
    from test_app import Client
    FakeCoinbase.perms = {"can_view": True, "can_trade": True, "can_transfer": False}
    FakeCoinbase.bal, FakeCoinbase.orders, FakeCoinbase.log = {"USDC": Decimal("200")}, {}, []
    FakeCoinbase.fail_bracket, FakeCoinbase.fill = False, True
    cfg = dict(cli.DEFAULTS)
    store = Store(str(tmp_path / "app.db"))
    app = web.App(cfg, store, None, None, None, Trader(cfg, store, ex.Vault(ex.new_master_key()), FakeCoinbase))
    srv = web.make_server(str(tmp_path / "s.json"), str(tmp_path / "g.json"), host="127.0.0.1", port=0, app=app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = Client(f"http://127.0.0.1:{srv.server_address[1]}")
        assert c.call("/api/trade")[0] == 401 and c.call("/api/trade/buy", dict(ORDER, confirm=True))[0] == 401
        c.call("/api/register", {"email": "client@exemple.com", "password": "motdepasse1"})
        assert c.call("/api/me")[1]["trading"] is False                      # clients : ferme par defaut
        assert c.call("/api/trade")[1]["allowed"] is False and c.call("/api/trade/plan", ORDER)[0] == 400
        store.set_admin("client@exemple.com")
        assert c.call("/api/me")[1]["trading"] is True
        code, st = c.call("/api/trade/key", {"key_name": "organizations/o/apiKeys/k",
                                             "private_key": "-----BEGIN EC PRIVATE KEY-----x"})
        assert code == 200 and st["connected"] is True and "private" not in json.dumps(st)
        assert c.call("/api/trade/plan", ORDER)[1]["stop"] == "480"
        assert c.call("/api/trade/buy", ORDER)[0] == 400                      # sans confirmation
        code, res = c.call("/api/trade/buy", dict(ORDER, confirm=True))
        assert code == 200 and res["trade"]["status"] == "ouvert"
        assert c.call("/api/trade/close", {"id": res["trade"]["id"]})[1]["trade"]["status"] == "cloture"
        assert c.call("/api/trade/inconnu", {})[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()
