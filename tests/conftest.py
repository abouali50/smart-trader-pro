import math
import random

import pytest

from smart_trader_pro.data import Candles

HOUR = 3_600_000


def make_candles(n=1500, seed=7, drift=0.0004, symbol="TESTUSDT"):
    """Marche synthetique : tendance, cycles et bruit, sur des bougies 1h."""
    rng = random.Random(seed)
    c = Candles(symbol, "1h")
    price = 100.0
    t0 = 1_700_000_000_000 - (1_700_000_000_000 % (4 * HOUR))
    for i in range(n):
        o = price
        move = drift + 0.004 * math.sin(i / 23) + rng.gauss(0, 0.006)
        cl = o * (1 + move)
        h = max(o, cl) * (1 + abs(rng.gauss(0, 0.002)))
        l = min(o, cl) * (1 - abs(rng.gauss(0, 0.002)))
        v = 1000 * (1 + abs(rng.gauss(0, 0.5))) * (1.6 if cl > o else 1.0)
        c.append(t0 + i * HOUR, o, h, l, cl, v)
        price = cl
    return c


@pytest.fixture
def candles():
    return make_candles()


REAL_BOOK_IMBALANCE = None


@pytest.fixture(autouse=True)
def no_live_order_book(monkeypatch):
    """Les tests ne touchent jamais le reseau : le carnet d'ordres est neutralise par defaut."""
    from smart_trader_pro.data import MarketData
    global REAL_BOOK_IMBALANCE
    if REAL_BOOK_IMBALANCE is None:
        REAL_BOOK_IMBALANCE = MarketData.book_imbalance
    monkeypatch.setattr(MarketData, "book_imbalance", lambda self, sym, pct=1.0: None)
    monkeypatch.setattr(MarketData, "last_price", lambda self, sym: None)
