#!/usr/bin/env python3
"""
arbitrage_scanner.py — Mide oportunidades de arbitraje REALES sin operar (solo APIs públicas, sin claves).

No envía órdenes ni necesita cuentas. Solo lee precios y graba lo que ve para decidir con datos
si el arbitraje deja dinero DESPUÉS de comisiones. Tres mediciones en paralelo:

  1. Espacial    Mismo par (BTCUSDT...) en varios exchanges: comprar al ask de A y vender al bid de B
                 a la vez, con saldo ya repartido en ambos. neto = bid_B / ask_A - 1 - taker_A - taker_B.
  2. Triangular  Dentro de Binance: USDT -> X -> Y -> USDT con 3 órdenes taker (3 comisiones).
  3. Funding     Tasa de financiación de los perpetuos en cada exchange, anualizada. Base del
                 cash-and-carry (spot comprado + perpetuo en corto) y del funding entre exchanges.

Cada exchange que no responda desde el país del VPS (HTTP 451/403, timeout...) se anota y se ignora.

Uso:
  python arbitrage_scanner.py --hours 6                       # graba en data/arbitrage/ y al final hace el informe
  python arbitrage_scanner.py --once                          # una sola ronda, para ver qué exchanges responden
  python arbitrage_scanner.py --report --out-md data/arbitrage/informe.md     # informe con lo ya grabado
  python arbitrage_scanner.py --symbols BTCUSDT,ETHUSDT --fees '{"kraken": 0.0026}'
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

USER_AGENT = "Ingresosbot-arbitrage-scanner/1.0"
DEFAULT_DIR = Path("data/arbitrage")
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")
TRIANGLE_COINS = ("BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "LTC", "LINK", "TRX", "AVAX", "DOT")
DEFAULT_BALANCE = 500.0

# Comisión taker spot del nivel más bajo (cuenta nueva, sin descuentos). Verifícalas en tu cuenta y
# cámbialas con --fees si pagas otra cosa (p. ej. Binance con BNB = 0.00075).
SPOT_TAKER = {
    "binance": 0.0010, "bybit": 0.0010, "okx": 0.0010, "kucoin": 0.0010, "gate": 0.0010,
    "bitget": 0.0010, "mexc": 0.0005, "kraken": 0.0040, "coinbase": 0.0060,
}
PERP_TAKER = {
    "binance": 0.0005, "bybit": 0.00055, "okx": 0.0005, "bitget": 0.0006, "gate": 0.0005,
    "kucoin": 0.0006, "mexc": 0.0002, "hyperliquid": 0.00045, "kraken": 0.0005,
}

Getter = Callable[..., object]


def _http_json(url: str, data: Optional[dict] = None, timeout: float = 8.0) -> object:
    body = json.dumps(data).encode() if data is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


@dataclass
class Quote:
    bid: float
    ask: float
    bid_qty: Optional[float] = None
    ask_qty: Optional[float] = None


def _split(symbol: str) -> tuple[str, str]:
    if not symbol.endswith("USDT") or len(symbol) <= 4:
        raise ValueError(f"Solo pares contra USDT, p. ej. BTCUSDT (recibido {symbol})")
    return symbol[:-4], "USDT"


def _f(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _quote(bid, ask, bid_qty=None, ask_qty=None) -> Quote:
    b, a = _f(bid), _f(ask)
    if b is None or a is None or b > a * 1.05:
        raise ValueError(f"bid/ask inválidos: {bid} / {ask}")
    return Quote(b, a, _f(bid_qty), _f(ask_qty))


# ---------------------------------------------------------------- spot: mejor bid/ask por exchange

def q_binance(sym: str, get: Getter) -> Quote:
    d = get(f"https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol={sym}")
    return _quote(d["bidPrice"], d["askPrice"], d["bidQty"], d["askQty"])


def q_bybit(sym: str, get: Getter) -> Quote:
    d = get(f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={sym}")
    r = d["result"]["list"][0]
    return _quote(r["bid1Price"], r["ask1Price"], r.get("bid1Size"), r.get("ask1Size"))


def q_okx(sym: str, get: Getter) -> Quote:
    base, quote = _split(sym)
    r = get(f"https://www.okx.com/api/v5/market/ticker?instId={base}-{quote}")["data"][0]
    return _quote(r["bidPx"], r["askPx"], r.get("bidSz"), r.get("askSz"))


def q_kucoin(sym: str, get: Getter) -> Quote:
    base, quote = _split(sym)
    r = get(f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={base}-{quote}")["data"]
    return _quote(r["bestBid"], r["bestAsk"], r.get("bestBidSize"), r.get("bestAskSize"))


def q_gate(sym: str, get: Getter) -> Quote:
    base, quote = _split(sym)
    r = get(f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={base}_{quote}")[0]
    return _quote(r["highest_bid"], r["lowest_ask"], r.get("highest_size"), r.get("lowest_size"))


def q_bitget(sym: str, get: Getter) -> Quote:
    r = get(f"https://api.bitget.com/api/v2/spot/market/tickers?symbol={sym}")["data"][0]
    return _quote(r["bidPr"], r["askPr"], r.get("bidSz"), r.get("askSz"))


def q_mexc(sym: str, get: Getter) -> Quote:
    d = get(f"https://api.mexc.com/api/v3/ticker/bookTicker?symbol={sym}")
    return _quote(d["bidPrice"], d["askPrice"], d.get("bidQty"), d.get("askQty"))


def q_kraken(sym: str, get: Getter) -> Quote:
    base, quote = _split(sym)
    pair = ("XBT" if base == "BTC" else "XDG" if base == "DOGE" else base) + quote
    d = get(f"https://api.kraken.com/0/public/Ticker?pair={pair}")
    if d.get("error"):
        raise ValueError(f"Kraken: {d['error']}")
    r = next(iter(d["result"].values()))
    return _quote(r["b"][0], r["a"][0], r["b"][2], r["a"][2])


def q_coinbase(sym: str, get: Getter) -> Quote:
    base, quote = _split(sym)
    d = get(f"https://api.exchange.coinbase.com/products/{base}-{quote}/book?level=1")
    return _quote(d["bids"][0][0], d["asks"][0][0], d["bids"][0][1], d["asks"][0][1])


SPOT_SOURCES: dict[str, Callable[[str, Getter], Quote]] = {
    "binance": q_binance, "bybit": q_bybit, "okx": q_okx, "kucoin": q_kucoin, "gate": q_gate,
    "bitget": q_bitget, "mexc": q_mexc, "kraken": q_kraken, "coinbase": q_coinbase,
}


# ---------------------------------------------------------------- perpetuos: funding por periodo y horas del periodo

def fr_binance(sym: str, get: Getter) -> tuple[float, float]:
    d = get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}")
    return float(d["lastFundingRate"]), 8.0


def fr_bybit(sym: str, get: Getter) -> tuple[float, float]:
    r = get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}")["result"]["list"][0]
    return float(r["fundingRate"]), float(r.get("fundingIntervalHour") or 8)


def fr_okx(sym: str, get: Getter) -> tuple[float, float]:
    base, quote = _split(sym)
    r = get(f"https://www.okx.com/api/v5/public/funding-rate?instId={base}-{quote}-SWAP")["data"][0]
    hours = 8.0
    if r.get("fundingTime") and r.get("nextFundingTime"):
        hours = (int(r["nextFundingTime"]) - int(r["fundingTime"])) / 3_600_000 or 8.0
    return float(r["fundingRate"]), hours


def fr_bitget(sym: str, get: Getter) -> tuple[float, float]:
    r = get(f"https://api.bitget.com/api/v2/mix/market/current-fund-rate?symbol={sym}"
            "&productType=usdt-futures")["data"][0]
    return float(r["fundingRate"]), float(r.get("fundingRateInterval") or 8)


def fr_gate(sym: str, get: Getter) -> tuple[float, float]:
    base, quote = _split(sym)
    r = get(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{base}_{quote}")
    return float(r["funding_rate"]), float(r.get("funding_interval") or 28_800) / 3600


def fr_kucoin(sym: str, get: Getter) -> tuple[float, float]:
    base, quote = _split(sym)
    r = get(f"https://api-futures.kucoin.com/api/v1/funding-rate/{'XBT' if base == 'BTC' else base}{quote}M/current")
    r = r["data"]
    return float(r["value"]), float(r.get("granularity") or 28_800_000) / 3_600_000


def fr_mexc(sym: str, get: Getter) -> tuple[float, float]:
    base, quote = _split(sym)
    r = get(f"https://contract.mexc.com/api/v1/contract/funding_rate/{base}_{quote}")["data"]
    return float(r["fundingRate"]), float(r.get("collectCycle") or 8)


_HL_CACHE: dict[str, object] = {}


def fr_hyperliquid(sym: str, get: Getter) -> tuple[float, float]:
    base, _ = _split(sym)
    now = time.time()
    if now - float(_HL_CACHE.get("t", 0)) > 30:          # una sola llamada sirve para todas las monedas
        _HL_CACHE.update(t=now, d=get("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"}))
    meta, ctxs = _HL_CACHE["d"]
    for asset, ctx in zip(meta["universe"], ctxs):
        if asset["name"] == base:
            return float(ctx["funding"]), 1.0
    raise KeyError(f"Hyperliquid no lista {base}")


FUNDING_SOURCES: dict[str, Callable[[str, Getter], tuple[float, float]]] = {
    "binance": fr_binance, "bybit": fr_bybit, "okx": fr_okx, "bitget": fr_bitget, "gate": fr_gate,
    "kucoin": fr_kucoin, "mexc": fr_mexc, "hyperliquid": fr_hyperliquid,
}


def annualize(rate: float, hours: float) -> float:
    """Tasa por periodo -> % anual (sin interés compuesto)."""
    return rate * (24.0 / hours) * 365 * 100


# ---------------------------------------------------------------- cálculos

def spatial_opportunities(quotes: dict[str, Quote], fees: dict[str, float]) -> list[dict]:
    """Todas las combinaciones comprar-en-A / vender-en-B para un mismo par."""
    out = []
    for buy_ex, qa in quotes.items():
        for sell_ex, qb in quotes.items():
            if buy_ex == sell_ex:
                continue
            gross = (qb.bid / qa.ask - 1) * 100
            net = gross - (fees[buy_ex] + fees[sell_ex]) * 100
            depth = None
            if qa.ask_qty and qb.bid_qty:
                depth = min(qa.ask_qty * qa.ask, qb.bid_qty * qb.bid)
            out.append({"buy": buy_ex, "sell": sell_ex, "gross_pct": gross, "net_pct": net, "depth_usd": depth})
    return out


def triangles(available: set[str], coins=TRIANGLE_COINS) -> list[tuple[str, str, str]]:
    """Ciclos USDT -> X -> Y -> USDT donde existe el par cruzado XY (o YX) en el exchange."""
    out = []
    for x in coins:
        for y in coins:
            if x != y and f"{x}USDT" in available and f"{y}USDT" in available \
                    and (f"{y}{x}" in available or f"{x}{y}" in available):
                out.append((x, y, f"{y}{x}" if f"{y}{x}" in available else f"{x}{y}"))
    return out


def triangle_return(x: str, y: str, cross: str, book: dict[str, Quote], fee: float) -> float:
    """% neto de dar la vuelta USDT -> X -> Y -> USDT tomando siempre el lado caro (taker)."""
    amt = 1.0 / book[f"{x}USDT"].ask                      # compra X con USDT
    q = book[cross]
    amt = amt / q.ask if cross == f"{y}{x}" else amt * q.bid   # X -> Y
    amt = amt * book[f"{y}USDT"].bid                       # vende Y por USDT
    return (amt * (1 - fee) ** 3 - 1) * 100


def carry_estimate(apr_pct: float, balance: float = DEFAULT_BALANCE, leverage: float = 2.0,
                   spot_fee: float = 0.001, perp_fee: float = 0.0005, days: float = 30) -> dict:
    """Cash-and-carry: compras N en spot y abres N en corto en el perpetuo con margen N/leverage."""
    notional = balance * leverage / (leverage + 1)
    income = notional * apr_pct / 100 * days / 365
    costs = notional * 2 * (spot_fee + perp_fee)          # abrir y cerrar las dos patas
    return {"notional_usd": round(notional, 2), "income_usd": round(income, 2),
            "costs_usd": round(costs, 2), "net_usd": round(income - costs, 2), "days": days}


# ---------------------------------------------------------------- grabación

SPOT_COLS = ["ts", "exchange", "symbol", "bid", "ask", "bid_qty", "ask_qty", "latency_ms"]
TRI_COLS = ["ts", "path", "net_pct"]
FUND_COLS = ["ts", "exchange", "symbol", "rate", "interval_h", "apr_pct"]
ERR_COLS = ["ts", "kind", "exchange", "error"]


class Recorder:
    def __init__(self, out_dir: Path):
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, cols: list[str], rows: list[dict]) -> None:
        if not rows:
            return
        path = self.dir / f"{name}.csv"
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerows(rows)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _err(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    return f"{type(exc).__name__}: {exc}"[:160]


class Scanner:
    def __init__(self, symbols, exchanges=None, perp_exchanges=None, get: Optional[Getter] = None,
                 recorder: Optional[Recorder] = None, workers: int = 16):
        self.symbols = [s.upper() for s in symbols]
        self.spot = {k: SPOT_SOURCES[k] for k in (exchanges or SPOT_SOURCES)}
        self.perp = {k: FUNDING_SOURCES[k] for k in (perp_exchanges or FUNDING_SOURCES)}
        self.get = get or _http_json
        self.rec = recorder
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.tri_symbols: Optional[list[str]] = None
        self.tri_paths: list[tuple[str, str, str]] = []
        self.down: dict[str, int] = defaultdict(int)       # exchanges que fallan seguido se consultan menos

    def _timed(self, fn, sym):
        t0 = time.perf_counter()
        res = fn(sym, self.get)
        return res, (time.perf_counter() - t0) * 1000

    def _skip(self, ex: str, rnd: int) -> bool:
        return self.down[ex] >= 3 and rnd % 20 != 0         # tras 3 fallos seguidos, reintenta cada 20 rondas

    def spot_round(self, rnd: int = 0) -> tuple[list[dict], list[dict]]:
        ts = _now()
        jobs = {(ex, s): self.pool.submit(self._timed, fn, s)
                for ex, fn in self.spot.items() if not self._skip(ex, rnd) for s in self.symbols}
        rows, errs, ok = [], [], defaultdict(bool)
        for (ex, s), fut in jobs.items():
            try:
                q, ms = fut.result()
                ok[ex] = True
                rows.append({"ts": ts, "exchange": ex, "symbol": s, "bid": q.bid, "ask": q.ask,
                             "bid_qty": q.bid_qty, "ask_qty": q.ask_qty, "latency_ms": round(ms)})
            except Exception as exc:  # noqa: BLE001 — un exchange caído no debe parar la medición
                errs.append({"ts": ts, "kind": "spot", "exchange": ex, "error": f"{s}: {_err(exc)}"})
        for ex in {e for e, _ in jobs}:
            self.down[ex] = 0 if ok[ex] else self.down[ex] + 1
        if self.rec:
            self.rec.write("spot_quotes", SPOT_COLS, rows)
            self.rec.write("errors", ERR_COLS, errs)
        return rows, errs

    def triangle_round(self, fee: float) -> list[dict]:
        ts = _now()
        try:
            if self.tri_symbols is None:                     # descubre una vez qué pares cruzados existen
                every = self.get("https://data-api.binance.vision/api/v3/ticker/bookTicker")
                available = {d["symbol"] for d in every}
                self.tri_paths = triangles(available)
                self.tri_symbols = sorted({s for x, y, c in self.tri_paths for s in (f"{x}USDT", f"{y}USDT", c)})
            if not self.tri_paths:
                return []
            qs = urllib.parse.urlencode({"symbols": json.dumps(self.tri_symbols, separators=(",", ":"))})
            data = self.get(f"https://data-api.binance.vision/api/v3/ticker/bookTicker?{qs}")
            book = {d["symbol"]: _quote(d["bidPrice"], d["askPrice"]) for d in data if _f(d["bidPrice"])}
        except Exception as exc:  # noqa: BLE001
            if self.rec:
                self.rec.write("errors", ERR_COLS, [{"ts": ts, "kind": "triangular", "exchange": "binance",
                                                    "error": _err(exc)}])
            return []
        rows = []
        for x, y, cross in self.tri_paths:
            if {f"{x}USDT", f"{y}USDT", cross} <= book.keys():
                rows.append({"ts": ts, "path": f"USDT>{x}>{y}>USDT",
                             "net_pct": round(triangle_return(x, y, cross, book, fee), 5)})
        if self.rec:
            self.rec.write("triangular", TRI_COLS, rows)
        return rows

    def funding_round(self) -> tuple[list[dict], list[dict]]:
        ts = _now()
        jobs = {(ex, s): self.pool.submit(fn, s, self.get) for ex, fn in self.perp.items() for s in self.symbols}
        rows, errs = [], []
        for (ex, s), fut in jobs.items():
            try:
                rate, hours = fut.result()
                rows.append({"ts": ts, "exchange": ex, "symbol": s, "rate": rate, "interval_h": hours,
                             "apr_pct": round(annualize(rate, hours), 3)})
            except Exception as exc:  # noqa: BLE001
                errs.append({"ts": ts, "kind": "funding", "exchange": ex, "error": f"{s}: {_err(exc)}"})
        if self.rec:
            self.rec.write("funding", FUND_COLS, rows)
            self.rec.write("errors", ERR_COLS, errs)
        return rows, errs


# ---------------------------------------------------------------- informe

def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build_report(out_dir: Path, fees: dict[str, float], perp_fees: dict[str, float],
                 balance: float = DEFAULT_BALANCE, min_depth: float = 100.0, max_latency_ms: float = 1500) -> dict:
    out_dir = Path(out_dir)
    spot = _read(out_dir / "spot_quotes.csv")
    tri = _read(out_dir / "triangular.csv")
    fund = _read(out_dir / "funding.csv")
    errs = _read(out_dir / "errors.csv")

    # ---- espacial: reconstruye cada ronda y evalúa todas las parejas de exchanges
    rounds: dict[tuple[str, str], dict[str, Quote]] = defaultdict(dict)
    for r in spot:
        if float(r["latency_ms"] or 0) > max_latency_ms:      # precio viejo = oportunidad falsa
            continue
        rounds[(r["ts"], r["symbol"])][r["exchange"]] = Quote(float(r["bid"]), float(r["ask"]),
                                                              _f(r["bid_qty"]), _f(r["ask_qty"]))
    pairs: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for (ts, sym), quotes in rounds.items():
        for o in spatial_opportunities(quotes, fees):
            pairs[(sym, o["buy"], o["sell"])].append(o)
    spatial_rows = []
    for (sym, buy, sell), obs in pairs.items():
        nets = [o["net_pct"] for o in obs]
        usable = [o for o in obs if o["net_pct"] > 0 and (o["depth_usd"] or 0) >= min_depth]
        spatial_rows.append({
            "symbol": sym, "buy": buy, "sell": sell, "samples": len(obs),
            "avg_gross_pct": round(statistics.fmean(o["gross_pct"] for o in obs), 4),
            "median_net_pct": round(statistics.median(nets), 4), "max_net_pct": round(max(nets), 4),
            "pct_time_positive": round(100 * len(usable) / len(obs), 2),
            "usd_per_trade_250": round(250 * statistics.fmean(o["net_pct"] for o in usable) / 100, 3) if usable else 0.0,
        })
    spatial_rows.sort(key=lambda r: (r["pct_time_positive"], r["max_net_pct"]), reverse=True)

    # ---- triangular
    by_path: dict[str, list[float]] = defaultdict(list)
    for r in tri:
        by_path[r["path"]].append(float(r["net_pct"]))
    tri_rows = sorted(({"path": p, "samples": len(v), "median_net_pct": round(statistics.median(v), 4),
                        "max_net_pct": round(max(v), 4), "pct_time_positive": round(100 * sum(x > 0 for x in v) / len(v), 2)}
                       for p, v in by_path.items()), key=lambda r: r["max_net_pct"], reverse=True)

    # ---- funding
    by_ex: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in fund:
        by_ex[(r["symbol"], r["exchange"])].append(float(r["apr_pct"]))
    fund_rows = []
    for (sym, ex), v in sorted(by_ex.items()):
        apr = statistics.fmean(v)
        est = carry_estimate(apr, balance, 2.0, fees.get(ex, 0.001), perp_fees.get(ex, 0.0006))
        fund_rows.append({"symbol": sym, "exchange": ex, "samples": len(v), "avg_apr_pct": round(apr, 2),
                          "min_apr_pct": round(min(v), 2), "max_apr_pct": round(max(v), 2),
                          "carry_30d_net_usd": est["net_usd"]})
    fund_rows.sort(key=lambda r: r["avg_apr_pct"], reverse=True)

    # funding entre exchanges: largo en el que menos cobra, corto en el que más (dos perpetuos, sin spot)
    xfund = []
    syms = {s for s, _ in by_ex}
    for sym in syms:
        avg = {ex: statistics.fmean(v) for (s, ex), v in by_ex.items() if s == sym}
        if len(avg) >= 2:
            hi, lo = max(avg, key=avg.get), min(avg, key=avg.get)
            notional = balance * 2.0 / 2               # mitad del capital como margen en cada exchange, x2
            income = notional * (avg[hi] - avg[lo]) / 100 * 30 / 365
            costs = notional * 2 * (perp_fees.get(hi, 0.0006) + perp_fees.get(lo, 0.0006))
            xfund.append({"symbol": sym, "short_on": hi, "long_on": lo, "spread_apr_pct": round(avg[hi] - avg[lo], 2),
                          "net_30d_usd": round(income - costs, 2)})
    xfund.sort(key=lambda r: r["spread_apr_pct"], reverse=True)

    # ---- alcance desde este servidor
    seen = {r["exchange"] for r in spot} | {f"{r['exchange']} (perp)" for r in fund}
    failing: dict[str, str] = {}
    for e in errs:
        key = e["exchange"] if e["kind"] == "spot" else f"{e['exchange']} (perp)" if e["kind"] == "funding" else "binance (triangular)"
        if key not in seen:
            failing[key] = e["error"]

    span = sorted({r["ts"] for r in spot} | {r["ts"] for r in fund})
    return {"from": span[0] if span else None, "to": span[-1] if span else None, "balance_usd": balance,
            "spot_rounds": len({r["ts"] for r in spot}), "spatial": spatial_rows, "triangular": tri_rows,
            "funding": fund_rows, "funding_cross": xfund, "unreachable": failing, "fees_spot": fees,
            "fees_perp": perp_fees, "min_depth_usd": min_depth}


def _table(rows: list[dict], cols: list[str], limit: int = 15) -> str:
    if not rows:
        return "_Sin datos._\n"
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows[:limit]:
        out.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    return "\n".join(out) + "\n"


def report_markdown(rep: dict) -> str:
    best_sp = rep["spatial"][0] if rep["spatial"] else None
    best_tri = rep["triangular"][0] if rep["triangular"] else None
    best_fund = rep["funding"][0] if rep["funding"] else None
    lines = [
        "# Informe de arbitraje (medición real, sin operar)",
        "",
        f"Periodo: {rep['from']} → {rep['to']} · rondas de precios: {rep['spot_rounds']} · capital supuesto: {rep['balance_usd']} USD",
        "",
        "## Resumen",
        "",
        "| Variante | Mejor caso medido | ¿Deja dinero tras comisiones? |",
        "|---|---|---|",
        f"| Espacial (entre exchanges) | {best_sp['symbol'] + ' ' + best_sp['buy'] + '→' + best_sp['sell'] + ': ' + str(best_sp['pct_time_positive']) + '% del tiempo positivo, máx ' + str(best_sp['max_net_pct']) + '%' if best_sp else 'sin datos'} | "
        f"{'Sí, a veces' if best_sp and best_sp['pct_time_positive'] > 1 else 'No de forma utilizable'} |",
        f"| Triangular (Binance) | {best_tri['path'] + ': máx ' + str(best_tri['max_net_pct']) + '%' if best_tri else 'sin datos'} | "
        f"{'Sí, a veces' if best_tri and best_tri['pct_time_positive'] > 1 else 'No'} |",
        f"| Funding / cash-and-carry | {best_fund['symbol'] + ' en ' + best_fund['exchange'] + ': ' + str(best_fund['avg_apr_pct']) + '% anual' if best_fund else 'sin datos'} | "
        f"{('Sí: ~' + str(best_fund['carry_30d_net_usd']) + ' USD en 30 días') if best_fund and best_fund['carry_30d_net_usd'] > 0 else 'No'} |",
        "",
        f"## 1. Espacial: comprar en A y vender en B a la vez (profundidad mínima {rep['min_depth_usd']} USD)",
        "",
        "`% tiempo positivo` = rondas con ganancia neta > 0 y libro suficiente. `USD/op 250` = ganancia media por operación de 250 USD cuando fue positiva.",
        "",
        _table(rep["spatial"], ["symbol", "buy", "sell", "samples", "avg_gross_pct", "median_net_pct", "max_net_pct",
                                "pct_time_positive", "usd_per_trade_250"]),
        "## 2. Triangular en Binance (3 comisiones taker)",
        "",
        _table(rep["triangular"], ["path", "samples", "median_net_pct", "max_net_pct", "pct_time_positive"], 10),
        "## 3. Funding de perpetuos (positivo = los largos pagan a los cortos)",
        "",
        f"`carry 30d` = ganancia estimada de un cash-and-carry de {rep['balance_usd']} USD (spot + corto x2) durante 30 días, ya restando abrir y cerrar.",
        "",
        _table(rep["funding"], ["symbol", "exchange", "samples", "avg_apr_pct", "min_apr_pct", "max_apr_pct",
                                "carry_30d_net_usd"], 30),
        "### Funding entre exchanges (dos perpetuos, sin spot)",
        "",
        _table(rep["funding_cross"], ["symbol", "short_on", "long_on", "spread_apr_pct", "net_30d_usd"]),
        "## Exchanges que no respondieron desde este servidor",
        "",
        _table([{"exchange": k, "error": v} for k, v in sorted(rep["unreachable"].items())], ["exchange", "error"], 30),
        "Comisiones usadas (taker, nivel básico): " + json.dumps(rep["fees_spot"]) + "",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mide arbitraje espacial, triangular y funding sin operar.")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--exchanges", default=",".join(SPOT_SOURCES), help="exchanges spot a comparar")
    ap.add_argument("--perp-exchanges", default=",".join(FUNDING_SOURCES))
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--every", type=float, default=15.0, help="segundos entre rondas de precios")
    ap.add_argument("--funding-every", type=float, default=300.0, help="segundos entre lecturas de funding")
    ap.add_argument("--out-dir", default=str(DEFAULT_DIR))
    ap.add_argument("--fees", default="{}", help='JSON con comisiones spot taker, p. ej. {"binance": 0.00075}')
    ap.add_argument("--perp-fees", default="{}")
    ap.add_argument("--balance", type=float, default=DEFAULT_BALANCE)
    ap.add_argument("--min-depth", type=float, default=100.0, help="USD mínimos en el mejor nivel del libro")
    ap.add_argument("--once", action="store_true", help="una ronda y muestra qué exchanges responden")
    ap.add_argument("--report", action="store_true", help="solo genera el informe con lo ya grabado")
    ap.add_argument("--out-md", help="guarda el informe en Markdown (por defecto OUT_DIR/informe.md)")
    args = ap.parse_args(argv)

    fees = {**SPOT_TAKER, **json.loads(args.fees)}
    perp_fees = {**PERP_TAKER, **json.loads(args.perp_fees)}
    out_dir = Path(args.out_dir)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    for s in symbols:
        _split(s)
    exchanges = [e for e in args.exchanges.split(",") if e in SPOT_SOURCES]
    perps = [e for e in args.perp_exchanges.split(",") if e in FUNDING_SOURCES]

    def finish() -> int:
        rep = build_report(out_dir, fees, perp_fees, args.balance, args.min_depth)
        md = report_markdown(rep)
        path = Path(args.out_md) if args.out_md else out_dir / "informe.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(md, encoding="utf-8")
        (out_dir / "informe.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        print(md)
        print(f"\nInforme guardado en {path}")
        return 0

    if args.report:
        return finish()

    scanner = Scanner(symbols, exchanges, perps, recorder=Recorder(out_dir))
    if args.once:
        rows, errs = scanner.spot_round()
        frows, ferrs = scanner.funding_round()
        tri = scanner.triangle_round(fees["binance"])
        ok = sorted({r["exchange"] for r in rows})
        print(json.dumps({"spot_ok": ok, "spot_fail": sorted({e["exchange"] for e in errs} - set(ok)),
                          "perp_ok": sorted({r["exchange"] for r in frows}),
                          "perp_fail": sorted({e["exchange"] for e in ferrs} - {r["exchange"] for r in frows}),
                          "triangles": len(tri), "errors": [e["exchange"] + " " + e["error"] for e in errs + ferrs][:20]},
                         ensure_ascii=False, indent=2))
        return 0

    end = time.time() + args.hours * 3600
    next_funding, rnd = 0.0, 0
    print(f"Midiendo {', '.join(symbols)} en {len(exchanges)} exchanges durante {args.hours} h "
          f"(Ctrl+C para parar antes). Datos en {out_dir}/", flush=True)
    try:
        while time.time() < end:
            t0 = time.time()
            rows, _ = scanner.spot_round(rnd)
            scanner.triangle_round(fees["binance"])
            if t0 >= next_funding:
                scanner.funding_round()
                next_funding = t0 + args.funding_every
            rnd += 1
            if rnd % 40 == 1:
                print(f"{_now()} ronda {rnd}: {len(rows)} precios de {len({r['exchange'] for r in rows})} exchanges",
                      flush=True)
            time.sleep(max(0.0, args.every - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\nParado por el usuario; generando informe...")
    return finish()


if __name__ == "__main__":
    sys.exit(main())
