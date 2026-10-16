#!/usr/bin/env python3
"""Markets for Pilot: stocks, ETFs and coins and their day's change in the menu bar; its popover and page list every
symbol with its price, its change and a spark line of the day (Surfaces v2, Pilot story 165), and Hyper+Space's Markets
lists the same rows. Each row opens the symbol's page on the source the prices come from.

Sources, free and keyless, picked in the settings:
- TradingView (the default): its screener API (scanner.tradingview.com, the one tradingview.com's screener reads), one
  call for every stock and ETF on any exchange and one for every coin pair in the currency asked. A bare ticker is the
  listing traded most (AAPL is NASDAQ:AAPL), XETR:VWCE names one. It keeps no history, so a row's line is drawn from the
  prices this extension read through the day, and grows as it reads them. A coin's change is since 00:00 UTC.
- Nasdaq and CoinGecko: stocks and ETFs listed in the US from Nasdaq's quote API (it answers only a browser's
  User-Agent), one call a symbol with the day's minutes; coins from CoinGecko, one call for all, their change over 24 h.
Other currencies come from the ECB's daily rates through Frankfurter, so a converted price is near (≈) and its change in
percent the exchange's own. Yahoo Finance is left out: it refuses this machine (429) and has broken often for others.

Every network call runs in a detached child (`main.py --refresh`, one at a time under refresh.lock) through curl, so a
slow network never holds a view past Pilot's limit (Weather's way). A coin is due after FRESH s; a stock after FRESH s
while its session may run (Nasdaq: New York's 09:30 to 16:00; TradingView, whose listings span Europe and the US: 03:00
to 16:00 New York), closing prints SETTLE s later, and otherwise once after each close, so a closed market costs
nothing. On Nasdaq a holiday shows itself when an answer 10 minutes into the session holds no minute of that day. A
failed symbol keeps its last answer, marked old, and is not asked again for RETRY s.

The menu bar shows one or several symbols (side by side, or one a minute in turn) as text, as text with the day's line,
or as mini bars: one of Pilot's gauges a symbol, its meter where the price stands in the day's range, in its own tone.
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

FRESH = 5 * 60
RETRY = 60
FX_FRESH = 12 * 3600  # the ECB sets its rates once a working day
SETTLE = 20 * 60
DAY = 24 * 3600
CURL_TIMEOUT = 8
BUDGET = {"bar": 1.5, "popover": 3.5, "page": 3.5, "list": 1.5}  # seconds a view waits for a fetch; Pilot's limits 2, 5, 5, 2
REFRESH_BUDGET = 25  # Refresh is a no-view run, stopped at 30 s
REFRESH_SOON = 3  # a view whose fetch still runs asks again this many seconds later
MOST = 12  # symbols asked
SPARK = 16  # a row's line, in bars
BAR_SPARK = 6  # the menu bar's line
LONGEST = 24  # Pilot's bar text
GAUGES = 3  # Pilot's gauges a bar item
GAUGE_TEXT = 6
GAUGE_LABEL = 5  # Pilot's gauge label since 2026-10-16; an older Pilot cuts it to two
BARS = "▁▂▃▄▅▆▇█"
QUOTES = "quotes.json"
NAMES = "names.json"
FX = "fx.json"
TURN = "turn.json"
LOCK = "refresh.lock"
AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
NASDAQ = "https://api.nasdaq.com/api/quote/%s/chart?assetclass=%s"
COINS_API = "https://api.coingecko.com/api/v3/coins/markets"
SEARCH_API = "https://api.coingecko.com/api/v3/search"
SCANNER = "https://scanner.tradingview.com/%s/scan"
FX_API = "https://api.frankfurter.dev/v1/latest"
NEW_YORK = ZoneInfo("America/New_York")
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,11}\Z")
EXCHANGE = re.compile(r"[A-Z][A-Z0-9_]{0,11}\Z")
# The coins a bare symbol means, by CoinGecko id; any other is a stock unless written coin:XYZ (stock:XYZ forces a
# stock: ETH is also Ethan Allen's ticker).
COINS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple", "BNB": "binancecoin", "DOGE": "dogecoin",
         "ADA": "cardano", "TRX": "tron", "AVAX": "avalanche-2", "DOT": "polkadot", "LINK": "chainlink", "LTC": "litecoin",
         "BCH": "bitcoin-cash", "XLM": "stellar", "TON": "the-open-network", "SHIB": "shiba-inu", "SUI": "sui",
         "HBAR": "hedera-hashgraph", "UNI": "uniswap", "ATOM": "cosmos", "XMR": "monero", "NEAR": "near", "APT": "aptos",
         "PEPE": "pepe", "HYPE": "hyperliquid", "USDT": "tether", "USDC": "usd-coin"}
# Where a coin pair is read on TradingView, the first that lists it in the currency asked.
COIN_EXCHANGES = ["COINBASE", "KRAKEN", "BITSTAMP", "BINANCE", "BITVAVO", "GEMINI", "BITFINEX", "OKX", "BYBIT"]
TV_COLUMNS = ["name", "close", "change", "change_abs", "high", "low", "description", "currency", "type", "exchange"]
MONEY = {"eur": "€", "usd": "$", "gbp": "£", "chf": "CHF "}
KINDS = {"stocks": "🏢", "etf": "🧺", "coin": "🪙"}
# Each source: its short name in a kept record's key, when its stocks' session may open (New York time), whether it
# knows a holiday, and per kind the site a row opens and what a move is measured over.
STOCK_WORDS = {"badge": "", "window": "Today", "range": "Day range"}
SOURCES = {
    "tradingview": {"tag": "tv", "name": "TradingView", "session": (3, 0), "holidays": False,
                    "words": {"stock": dict(STOCK_WORDS, site="TradingView"),
                              "coin": {"badge": "", "window": "Since 00:00 UTC", "range": "Day range", "site": "TradingView"}},
                    "credit": "Prices from [TradingView](https://www.tradingview.com/), some exchanges delayed"},
    "nasdaq": {"tag": "nq", "name": "Nasdaq · CoinGecko", "session": (9, 30), "holidays": True,
               "words": {"stock": dict(STOCK_WORDS, site="Nasdaq"),
                         "coin": {"badge": " 24h", "window": "24h", "range": "24h range", "site": "CoinGecko"}},
               "credit": "Stocks and ETFs from [Nasdaq](https://www.nasdaq.com/), may be delayed; coins from "
                         "[CoinGecko](https://www.coingecko.com/)"},
}
FX_CREDIT = "; currencies from the ECB through [Frankfurter](https://frankfurter.dev/)."


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")


def load(folder, name):
    try:
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save(folder, name, value):
    path = os.path.join(folder, name)
    temp = "%s.%d.tmp" % (path, os.getpid())
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
    os.replace(temp, path)


# ---- what is asked ----

def words(text):
    return [w for w in re.split(r"[\s,;]+", str(text or "").upper()) if w]


def symbols(text):
    """[{"key", "kind", "symbol", "listing"}] in the order typed, at most MOST: a known coin's symbol (or coin:XYZ) a coin,
    any other (or stock:XYZ) a stock or an ETF, EXCHANGE:XYZ that listing on TradingView; one that cannot be is skipped."""
    found, seen = [], set()
    for word in words(text):
        prefix, _, name = word.rpartition(":")
        listing = None
        if prefix in ("COIN", "STOCK"):
            kind = prefix.lower()
        elif prefix:
            kind, listing = ("stock", word) if EXCHANGE.match(prefix) else (None, None)
        else:
            kind = "coin" if name in COINS else "stock"
        key = "%s:%s" % (kind, listing or name)
        if not kind or not SYMBOL.match(name) or key in seen:
            continue
        seen.add(key)
        found.append({"key": key, "kind": kind, "symbol": name, "listing": listing})
    return found[:MOST]


def settings(run):
    """What to ask, from where, in which money, and how the bar shows it: its symbols (the first when none named is
    listed), side by side or in turn, its style and its parts."""
    prefs = run.get("preferences") or {}
    listed = symbols(prefs.get("symbols"))
    named = words(prefs.get("pinned"))
    pinned = []
    for word in named:
        pinned += [s["key"] for s in listed if word in (s["symbol"], s["key"].upper(), s["listing"]) and s["key"] not in pinned]
    pinned = pinned or [s["key"] for s in listed[:1]]
    show = {part: prefs.get(name, default) is not False for part, name, default in
            (("symbol", "showSymbol", True), ("price", "showPrice", False), ("percent", "showPercent", True),
             ("change", "showChange", False))}
    choice = lambda name, allowed, default: prefs.get(name) if prefs.get(name) in allowed else default
    return {"symbols": listed, "source": choice("source", SOURCES, "tradingview"), "currency": choice("currency", MONEY, "eur"),
            "pinned": pinned, "style": choice("style", ("text", "line", "bars"), "text"), "rotate": prefs.get("several") == "rotate",
            "show": show}


def record_key(symbol, want):
    """Where a symbol's answer is kept, per source: a stock's in its listing's money, a coin's in the currency asked."""
    key = "%s:%s" % (SOURCES[want["source"]]["tag"], symbol["key"])
    return key if symbol["kind"] == "stock" else "%s:%s" % (key, want["currency"])


# ---- sessions ----

def session(day, opens=(9, 30)):
    """(opens, closes at 16:00) in New York on `day`, a date, or None on a weekend (holidays not known)."""
    if day.weekday() >= 5:
        return None
    at = lambda h, m: datetime(day.year, day.month, day.day, h, m, tzinfo=NEW_YORK).timestamp()
    return at(*opens), at(16, 0)


def ny_day(now):
    return datetime.fromtimestamp(now, NEW_YORK).date()


def in_session(now, settle=SETTLE, opens=(9, 30)):
    """Whether a session opening at `opens` New York time runs at `now` (seconds since 1970), `settle` s after its close
    included."""
    hours = session(ny_day(now), opens)
    return bool(hours) and hours[0] <= now < hours[1] + settle


def last_settled(now):
    """When the last close's prints settled, at or before `now`: a session's close plus SETTLE."""
    for back in range(8):
        hours = session(ny_day(now) - timedelta(days=back))
        if hours and hours[1] + SETTLE <= now:
            return hours[1] + SETTLE
    return 0


def market_line(now):
    """The US market open until its close, or closed until its next open, in the Mac's own time."""
    day = ny_day(now)
    hours = session(day)
    if in_session(now, settle=0):
        return "US market open until %s" % clock(hours[1])
    while not hours or hours[0] <= now:
        day += timedelta(days=1)
        hours = session(day)
    return "US market closed · opens %s" % clock(hours[0])


def resting(record, now):
    """A failed try is not repeated for RETRY s."""
    return bool(record.get("error")) and now - (record.get("triedAt") or 0) < RETRY


def due(kind, record, now, source="nasdaq"):
    """Whether a kept answer is asked again: none kept, a coin's after FRESH s, a stock's after FRESH s while its session
    may run or once after each close; never while resting."""
    if resting(record, now):
        return False
    if "quote" not in record:
        return True
    fetched = record.get("fetchedAt") or 0
    if kind == "coin":
        return now - fetched >= FRESH
    running = in_session(now, opens=SOURCES[source]["session"]) and now - fetched >= FRESH
    return (running and not (SOURCES[source]["holidays"] and holiday(record, now))) or fetched < last_settled(now)


def holiday(record, now):
    """A session day the market keeps shut: asked 10 minutes into it, Nasdaq still had no minute from that day."""
    opens = session(ny_day(now))[0]
    as_of = (record.get("quote") or {}).get("asOf")
    return bool(as_of) and (record.get("fetchedAt") or 0) >= opens + 600 and ny_day(as_of) != ny_day(now)


def fx_pairs(want, quotes):
    """The rates the kept quotes need: each money a quote is in that is not the currency asked, as "USD:EUR"."""
    asked = want["currency"].upper()
    found = {(quotes.get(record_key(s, want)) or {}).get("quote", {}).get("currency") for s in want["symbols"]}
    return sorted("%s:%s" % (c, asked) for c in found if isinstance(c, str) and c and c != asked)


def fx_due(pair, fx, now, force=False):
    record = fx.get(pair) or {}
    return force or (not resting(record, now) and now - (record.get("fetchedAt") or 0) >= FX_FRESH)


# ---- asking ----

def curl(url, browser=False, body=None):
    """(parsed JSON, None) or (None, why); `browser` sends a browser's User-Agent, which Nasdaq answers alone; `body`
    is POSTed as JSON. The URL is curl's last argument."""
    binary = shutil.which("curl") or "/usr/bin/curl"
    extra = ["-A", AGENT, "-H", "Accept: application/json"] if browser else []
    if body is not None:
        extra += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(body, separators=(",", ":"))]
    try:
        done = subprocess.run([binary, "-sS", "--fail", "--compressed", "--max-time", str(CURL_TIMEOUT)] + extra + [url],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=CURL_TIMEOUT + 2)
    except (OSError, subprocess.TimeoutExpired):
        return None, "could not reach it"
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip().splitlines()
        return None, (said[-1] if said else "no answer").replace("curl: ", "")[:160]
    try:
        return json.loads(done.stdout.decode("utf-8")), None
    except ValueError:
        return None, "its answer did not read"


def number(value):
    """A figure as Nasdaq writes it ("$1,342.00", "+2.19", "-0.28%", "UNCH"), or as a number; None when there is none."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").replace("$", "").replace(",", "").replace("%", "").strip()
    if text.upper() == "UNCH":
        return 0.0
    try:
        return float(text)
    except ValueError:
        return None


def thin(values, count=SPARK):
    """`values` cut to `count`, each the last of its stretch, so the line ends on the latest."""
    if len(values) <= count:
        return list(values)
    return [values[int((i + 1) * len(values) / count) - 1] for i in range(count)]


# Nasdaq and CoinGecko

def stock_quote(data, asset):
    """Nasdaq's chart answer as kept: name, price, change, percent, previous close, the session's low, high and line, and
    when its last minute was."""
    price = number(data.get("lastSalePrice"))
    if price is None:
        return None
    points = [(p["x"] / 1000.0, number(p.get("y"))) for p in data.get("chart") or []
              if isinstance(p, dict) and isinstance(p.get("x"), (int, float))]
    points = [(t, y) for t, y in points if y is not None]
    hours = session(ny_day(points[-1][0])) if points else None
    regular = [y for t, y in points if hours and hours[0] <= t <= hours[1]]  # 09:30 to the 16:00 minute
    line = regular or [y for _, y in points]
    previous = number(data.get("previousClose"))
    change = number(data.get("netChange"))
    percent = number(data.get("percentageChange"))
    if percent is None and change is not None and previous:
        percent = change / previous * 100
    name = str(data.get("company") or "")
    for tail in (" Common Stock", " Ordinary Shares", " American Depositary Shares", " Class A", " Class B"):
        name = name.split(tail)[0]
    return {"name": name.strip(), "price": price, "change": change, "percent": percent, "previous": previous,
            "low": min(line) if line else None, "high": max(line) if line else None, "line": thin(line),
            "asOf": points[-1][0] if points else None, "asset": asset, "currency": "USD"}


def fetch_stock(symbol, asset):
    """(quote, None) or (None, why): the asset class Nasdaq files it under when known, else stocks and then ETFs."""
    for each in [asset] if asset else ["stocks", "etf"]:
        answer, error = curl(NASDAQ % (quote(symbol, safe=""), each), browser=True)
        if error:
            return None, "Nasdaq: %s" % error
        data = answer.get("data") if isinstance(answer, dict) else None
        if isinstance(data, dict):
            found = stock_quote(data, each)
            return (found, None) if found else (None, "Nasdaq sent no price for %s" % symbol)
    return None, "Nasdaq knows no %s" % symbol


def coin_id(symbol, names):
    """(CoinGecko's id for a coin's symbol, None) or (None, why): a known coin's, one found before, else the coin with
    that symbol CoinGecko ranks highest."""
    if symbol in COINS:
        return COINS[symbol], None
    if "cg:" + symbol in names:
        return names["cg:" + symbol], None
    answer, error = curl(SEARCH_API + "?" + urlencode({"query": symbol}))
    if error:
        return None, "CoinGecko: %s" % error
    found = [c for c in (answer or {}).get("coins") or [] if isinstance(c, dict) and str(c.get("symbol", "")).upper() == symbol
             and isinstance(c.get("id"), str)]
    if not found:
        return None, "CoinGecko knows no %s" % symbol
    best = min(found, key=lambda c: c.get("market_cap_rank") if isinstance(c.get("market_cap_rank"), int) else 1 << 30)
    names["cg:" + symbol] = best["id"]
    return best["id"], None


def coin_quote(row, currency):
    """A CoinGecko markets row as kept; its line is the last 24 of the week's hours (CoinGecko draws it in dollars:
    the line's shape, never its figures)."""
    price = number(row.get("current_price"))
    if price is None:
        return None
    week = [number(v) for v in ((row.get("sparkline_in_7d") or {}).get("price") or [])]
    try:
        stamp = datetime.strptime(str(row.get("last_updated"))[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        stamp = None
    return {"name": str(row.get("name") or ""), "price": price, "change": number(row.get("price_change_24h")),
            "percent": number(row.get("price_change_percentage_24h")), "low": number(row.get("low_24h")),
            "high": number(row.get("high_24h")), "line": thin([v for v in week if v is not None][-24:]), "asOf": stamp,
            "id": row.get("id"), "asset": "coin", "currency": currency.upper()}


def fetch_coins(coins, currency, names):
    """{symbol: (quote, None) or (None, why)} for `coins`, one markets call for all."""
    found, ids = {}, {}
    for symbol in coins:
        cid, error = coin_id(symbol, names)
        if error:
            found[symbol] = (None, error)
        else:
            ids[cid] = symbol
    if not ids:
        return found
    answer, error = curl(COINS_API + "?" + urlencode({"vs_currency": currency, "ids": ",".join(sorted(ids)), "sparkline": "true"}))
    rows = {r.get("id"): r for r in answer if isinstance(r, dict)} if isinstance(answer, list) else {}
    if not error and not isinstance(answer, list):
        error = "its answer did not read"
    for cid, symbol in ids.items():
        made = coin_quote(rows[cid], currency) if cid in rows else None
        found[symbol] = (made, None) if made else (None, "CoinGecko: %s" % error if error else "CoinGecko sent no %s" % symbol)
    return found


def nasdaq_fetch(asked, want, names):
    """{record key: (quote, None) or (None, why)}: each stock its own call, side by side, the coins one call."""
    from concurrent.futures import ThreadPoolExecutor  # the child's alone: a view never pays for it

    stocks = [s for s in asked if s["kind"] == "stock"]
    coins = [s for s in asked if s["kind"] == "coin"]
    with ThreadPoolExecutor(max_workers=len(stocks) + 1) as pool:
        coin_job = pool.submit(fetch_coins, [s["symbol"] for s in coins], want["currency"], names) if coins else None
        jobs = {record_key(s, want): pool.submit(fetch_stock, s["symbol"], names.get(record_key(s, want))) for s in stocks}
        answers = {key: job.result() for key, job in jobs.items()}
        if coin_job:
            found = coin_job.result()
            answers.update((record_key(s, want), found[s["symbol"]]) for s in coins)
    for s in stocks:
        made = answers[record_key(s, want)][0]
        if made:
            names[record_key(s, want)] = made["asset"]
    return answers


# TradingView

def tv_quote(ticker, row, coin):
    """A screener row as kept; its previous close is its price less the day's change."""
    d = dict(zip(TV_COLUMNS, row))
    price = number(d.get("close"))
    if price is None:
        return None
    change = number(d.get("change_abs"))
    asset = "coin" if coin else "etf" if d.get("type") == "fund" else "stocks"
    return {"name": str(d.get("description") or d.get("name") or ""), "price": price, "change": change,
            "percent": number(d.get("change")), "previous": None if coin or change is None else price - change,
            "low": number(d.get("low")), "high": number(d.get("high")), "ticker": ticker, "asset": asset,
            "currency": str(d.get("currency") or "").upper() or None}


def scan(market, body):
    """The screener's rows for `body` on `market` (global, crypto) as [(ticker, row)], in its order; or (None, why)."""
    answer, error = curl(SCANNER % market, body=dict(body, columns=TV_COLUMNS))
    if error:
        return None, "TradingView: %s" % error
    rows = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(rows, list):
        return None, "TradingView: its answer did not read"
    return [(r["s"], r["d"]) for r in rows if isinstance(r, dict) and isinstance(r.get("s"), str)
            and isinstance(r.get("d"), list) and len(r["d"]) == len(TV_COLUMNS)], None


def tv_market(market, asks, pick, coin):
    """{key: (quote, None) or (None, why)} for `asks` ({key: (name, ticker or None)}) on one market: the tickers known
    in one call, the names not yet placed in another, each placed on the listing `pick` chooses from the screener's
    rows for its name (the most traded first)."""
    found = {}
    known = {k: t for k, (_, t) in asks.items() if t}
    if known:
        rows, error = scan(market, {"symbols": {"tickers": sorted(set(known.values()))}})
        by_ticker = dict(rows or [])
        for key, ticker in known.items():
            made = tv_quote(ticker, by_ticker[ticker], coin) if ticker in by_ticker else None
            found[key] = (made, None) if made else (None, error or "TradingView knows no %s" % ticker)
    unknown = {k: n for k, (n, t) in asks.items() if not t}
    if unknown:
        rows, error = scan(market, {"filter": [{"left": "name", "operation": "in_range", "right": sorted(set(unknown.values()))}],
                                    "sort": {"sortBy": "Value.Traded", "sortOrder": "desc"}, "range": [0, 200]})
        for key, name in unknown.items():
            chosen = pick([(t, d) for t, d in rows or [] if d[0] == name])
            made = tv_quote(chosen[0], chosen[1], coin) if chosen else None
            found[key] = (made, None) if made else (None, error or "TradingView knows no %s" % name)
    return found


def tv_fetch(asked, want, names):
    """{record key: (quote, None) or (None, why)}: every stock in one call on the global market, every coin as its pair
    in the currency asked in one on the crypto market; each listing found is kept for the next time."""
    from concurrent.futures import ThreadPoolExecutor  # the child's alone: a view never pays for it

    currency = want["currency"].upper()
    rank = {e: i for i, e in enumerate(COIN_EXCHANGES)}

    def coin_pick(rows):
        pairs = [r for r in rows if str(dict(zip(TV_COLUMNS, r[1])).get("currency")).upper() == currency]
        return min(pairs, key=lambda r: rank.get(r[0].split(":")[0], len(rank)), default=None)

    stocks = {record_key(s, want): (s["symbol"], s["listing"] or names.get(record_key(s, want))) for s in asked if s["kind"] == "stock"}
    coins = {record_key(s, want): (s["symbol"] + currency, names.get(record_key(s, want))) for s in asked if s["kind"] == "coin"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(tv_market, "global", stocks, lambda rows: rows[0] if rows else None, False) if stocks else None,
                pool.submit(tv_market, "crypto", coins, coin_pick, True) if coins else None]
        answers = {}
        for job in jobs:
            answers.update(job.result() if job else {})
    for key, (made, _) in answers.items():
        if made:
            names[key] = made["ticker"]
    return answers


def fetch_fx(pair):
    """(the rate `pair` names, "USD:EUR", None) or (None, why)."""
    base, to = pair.split(":")
    answer, error = curl(FX_API + "?" + urlencode({"base": base, "symbols": to}))
    if error:
        return None, "Frankfurter: %s" % error
    rate = number((answer.get("rates") or {}).get(to)) if isinstance(answer, dict) else None
    return (rate, None) if rate else (None, "Frankfurter sent no %s rate" % pair.replace(":", " to "))


def sampled(record, made, now):
    """The prices read before, as [[seconds, price]], with this one: a coin's of the last 24 h, a stock's of the day
    (New York's) it was last read on, so a source with no history still draws the day's line."""
    samples = [s for s in record.get("samples") or [] if isinstance(s, list) and len(s) == 2] + [[now, made["price"]]]
    if made["asset"] == "coin":
        return [s for s in samples if now - s[0] < DAY]
    today = ny_day(now)
    return [s for s in samples if ny_day(s[0]) == today]


def fetch(want, folder, force):
    """Asks the source for every symbol due (all when `force`), then each rate a price needs, and keeps each answer with
    when it was tried; a failure keeps the last answer and says why."""
    now = time.time()
    quotes, names, fx = load(folder, QUOTES), load(folder, NAMES), load(folder, FX)
    known = dict(names)
    asked = [s for s in want["symbols"] if force or due(s["kind"], quotes.get(record_key(s, want)) or {}, now, want["source"])]
    answers = FETCH[want["source"]](asked, want, names) if asked else {}
    for key, (made, error) in answers.items():
        kept = quotes.get(key) or {}
        if not made:
            quotes[key] = dict(kept, triedAt=now, error=error)
            continue
        quotes[key] = {"triedAt": now, "error": None, "fetchedAt": now, "quote": made}
        if not made.get("line"):  # a source with no history: the line from the prices read
            quotes[key]["samples"] = sampled(kept, made, now)
            made["line"] = thin([p for _, p in quotes[key]["samples"]])
    asked_now = {record_key(s, want) for s in want["symbols"]}
    quotes = {k: v for k, v in quotes.items() if k in asked_now}  # a symbol, source or currency no longer asked
    rates = [p for p in fx_pairs(want, quotes) if fx_due(p, fx, now, force)]
    for pair in rates:
        rate, error = fetch_fx(pair)
        fx[pair] = dict(fx.get(pair) or {}, triedAt=now, error=error, **({"rate": rate, "fetchedAt": now} if rate else {}))
    if rates:
        save(folder, FX, fx)
    if names != known:
        save(folder, NAMES, names)
    save(folder, QUOTES, quotes)


# ---- the refresh child ----

FETCH = {"tradingview": tv_fetch, "nasdaq": nasdaq_fetch}


def locked(f):
    """Takes the refresh lock on the open file `f`; False when a refresh holds it."""
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def refreshing(folder):
    """True while a refresh holds the lock."""
    try:
        with open(os.path.join(folder, LOCK), "a") as f:
            return not locked(f)
    except OSError:
        return False


def refresh_main():
    """`main.py --refresh`: one fetch for the symbols on stdin, the refresh lock held through the file it was handed."""
    job = json.loads(sys.stdin.read())
    fetch(job["want"], job["cache"], job.get("force", False))


def start_refresh(folder, want, force):
    """Takes the refresh lock and hands it to a detached child that fetches, so the lock is held from before it starts
    until it ends; None when a refresh runs already or the child could not start."""
    try:
        lock = open(os.path.join(folder, LOCK), "a")
    except OSError:
        return None
    with lock:
        if not locked(lock):
            return None
        try:
            child = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--refresh"], stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(lock.fileno(),),
                                     start_new_session=True, cwd=os.path.dirname(os.path.abspath(__file__)))
            child.stdin.write(json.dumps({"cache": folder, "want": want, "force": force}).encode())
            child.stdin.close()
        except OSError:
            return None
    return child


def cache_dir(run):
    """Pilot's cache folder for this extension, which it makes on the first run."""
    folder = (run.get("paths") or {}).get("cache")
    if not isinstance(folder, str) or not folder:
        send({"type": "error", "text": "Pilot sent no cache folder"})
        sys.exit(0)
    os.makedirs(folder, exist_ok=True)
    return folder


def markets(run, budget, force=False):
    """(settings, Board of the kept answers, True while a fetch still runs). Any symbol or rate due, or `force`, starts
    a fetch; a view with every symbol's answer shows them at once, one missing an answer (or `force`) waits `budget` s
    at most."""
    started = time.monotonic()
    folder = cache_dir(run)
    want = settings(run)
    now = time.time()
    quotes, fx = load(folder, QUOTES), load(folder, FX)
    records = [(s["kind"], quotes.get(record_key(s, want)) or {}) for s in want["symbols"]]
    late = [record for kind, record in records if due(kind, record, now, want["source"])]
    waits = force or any("quote" not in record for record in late)
    rates = any(fx_due(p, fx, now) for p in fx_pairs(want, quotes))
    child = start_refresh(folder, want, force) if force or late or rates else None
    if child and waits:
        try:
            child.wait(max(0.1, budget - (time.monotonic() - started)))
        except subprocess.TimeoutExpired:
            pass
    running = refreshing(folder)  # this child, or another view's
    if waits:
        while running and time.monotonic() - started < budget:
            time.sleep(0.1)
            running = refreshing(folder)
        quotes, fx = load(folder, QUOTES), load(folder, FX)
    return want, Board(want, quotes, fx), running


# ---- words ----

def clock(stamp):
    """14:05 when today, else Mon 14:05, in the Mac's time."""
    moment = time.localtime(stamp)
    if time.strftime("%Y-%m-%d", moment) == time.strftime("%Y-%m-%d"):
        return time.strftime("%H:%M", moment)
    return time.strftime("%a %H:%M", moment)


def figure(value):
    """A price as people read one: whole from 10,000, cents from 1, four figures below."""
    if value is None:
        return "–"
    size = abs(value)
    if size >= 10000:
        return "{:,.0f}".format(value)
    if size >= 1:
        return "{:,.2f}".format(value)
    return "{:.4g}".format(value)


def amount(value):
    """A change in money: in cents from one cent, as a price below it (a small coin's)."""
    return "{:,.2f}".format(value) if 0.01 <= abs(value) < 1 else figure(value)


def short(value):
    """A price in at most six characters for the bar: 73.1k, 342, 4.21."""
    size = abs(value)
    if size >= 10000:
        return "%.1fk" % (value / 1000) if size < 1e6 else "%.1fM" % (value / 1e6)
    return "%.0f" % value if size >= 100 else figure(value)


def tone(percent):
    """Up fine, down alert, flat plain: always with its arrow, never the colour alone."""
    if percent is None or round(percent, 2) == 0:
        return None
    return "fine" if percent > 0 else "alert"


def change(percent, tight=False):
    """▲ 1.29%, ▼ 0.28%, or 0.00% when flat; `tight` with no space, one decimal from 10%."""
    if percent is None:
        return "–"
    arrow = {"fine": "▲", "alert": "▼"}.get(tone(percent), "")
    figures = "%.1f" % abs(percent) if tight and abs(percent) >= 10 else "%.2f" % abs(percent)
    return "%s%s%s%%" % (arrow, "" if tight or not arrow else " ", figures)


def sign(unit):
    """A money's sign: €, $, £, CHF, else its code."""
    return MONEY.get(unit.lower(), unit + " ")


def signed(value):
    return "+" if value > 0 else "−" if value < 0 else ""


def spark(values):
    """The day as a line of bars, lowest to highest; flat as a level line."""
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    if high - low <= abs(high) * 1e-6:
        return BARS[3] * len(values)
    return "".join(BARS[int(round((v - low) / (high - low) * (len(BARS) - 1)))] for v in values)


def age(seconds):
    minutes = int(seconds // 60)
    return "%dm" % minutes if minutes < 60 else "%dh" % (minutes // 60) if minutes < 2880 else "%dd" % (minutes // 1440)


class Row:
    """One symbol's kept answer, read for a view, its money settled once: in the currency asked when the price is, or
    converted at the ECB's rate (≈), or in its own money while no rate is known."""

    def __init__(self, symbol, record, board):
        self.symbol, self.kind, self.key, self.listing = symbol["symbol"], symbol["kind"], symbol["key"], symbol["listing"]
        self.quote, self.error, self.fetched = record.get("quote") or {}, record.get("error"), record.get("fetchedAt")
        self.coin, self.source = self.kind == "coin", board.source
        self.words = SOURCES[board.source]["words"][self.kind]
        own = self.quote.get("currency") or (board.currency.upper() if self.coin else "USD")
        rate = 1 if own == board.currency.upper() else board.rates.get("%s:%s" % (own, board.currency.upper()))
        self.approx = bool(rate) and rate != 1
        self.rate = rate or 1
        self.unit = board.currency.upper() if rate else own
        self.sign = sign(self.unit)

    @property
    def stale(self):
        """Its answer is old: the last try failed."""
        return bool(self.quote and self.error)

    @property
    def tone(self):
        """Warn while its answer is old or missing for a failure, else its move's tone."""
        if self.error:
            return "warn"
        return tone(self.quote.get("percent")) if self.quote else None

    @property
    def line(self):
        """The day's prices: the source's own minutes, or those this extension read."""
        return self.quote.get("line") or []

    def scaled(self, value):
        return None if value is None else value * self.rate

    def money(self, value, near=True, shown=figure):
        """A figure with its money sign, ≈ before a converted one."""
        if value is None:
            return "–"
        return ("≈ " if near and self.approx else "") + self.sign + shown(self.scaled(value))

    def url(self):
        """Its page on the source the prices come from."""
        if self.source == "tradingview":
            ticker = self.quote.get("ticker") or self.listing
            return ("https://www.tradingview.com/symbols/%s/" % ticker.replace(":", "-") if ticker else
                    "https://www.tradingview.com/chart/?symbol=%s" % (self.symbol + (self.unit if self.coin else "")))
        if self.coin:
            return "https://www.coingecko.com/en/coins/%s" % (self.quote.get("id") or COINS.get(self.symbol, self.symbol.lower()))
        section = "etfs" if self.quote.get("asset") == "etf" else "stocks"
        return "https://www.nasdaq.com/market-activity/%s/%s" % (section, self.symbol.lower())

    def badges(self):
        if not self.quote:
            return [{"text": "no price", "tone": "warn"}] if self.error else []
        moved = self.quote.get("percent")
        found = [{"text": change(moved) + self.words["badge"], "tone": tone(moved)}]
        if self.stale:
            found.append({"text": "⚠ %s old" % age(time.time() - (self.fetched or 0)), "tone": "warn"})
        return [{k: v for k, v in b.items() if v is not None} for b in found]

    def item(self):
        where = self.words["site"]
        item = {"id": self.key, "title": self.symbol, "icon": KINDS[self.quote.get("asset") or ("coin" if self.coin else "stocks")],
                "keywords": " ".join(filter(None, [self.symbol, self.quote.get("name"), self.kind])), "badges": self.badges(),
                "actions": [{"type": "open", "url": self.url(), "title": "Open %s on %s" % (self.symbol, where)}],
                "inline": [{"type": "open", "url": self.url(), "title": "Open on %s" % where, "icon": "🌐"}]}
        if not self.quote:
            item["subtitle"] = self.error or "Asking…"
            return item
        price = self.money(self.quote["price"])
        item["title"] = "%s · %s" % (self.symbol, price)
        item["subtitle"] = "  ".join(w for w in [spark(self.line), self.quote.get("name"), self.error] if w)
        item["actions"].append({"type": "copy", "text": "%s %s" % (self.symbol, price), "title": "Copy the price"})
        return item

    def parts(self, show, style):
        """The bar's words for it, each (part, text) in order: symbol, line, price, change in percent, change in money."""
        q = self.quote
        plain = [("symbol", self.symbol), ("percent", change(q.get("percent"), tight=True) if q else "–")]
        if not q:
            return plain
        found = [("symbol", self.symbol)] if show["symbol"] else []
        if style == "line" and len(self.line) > 1:
            found.append(("line", spark(thin(self.line, BAR_SPARK))))
        if show["price"]:
            found.append(("price", self.sign.strip() + short(self.scaled(q["price"]))))
        if show["percent"]:
            found.append(("percent", change(q.get("percent"), tight=True)))
        if show["change"] and q.get("change") is not None:
            moved = abs(self.scaled(q["change"]))
            found.append(("change", signed(q["change"]) + self.sign.strip() + (amount(moved) if moved < 100 else short(moved))))
        return found or plain

    def gauge(self, show):
        """A mini bar: its symbol (five characters at most), a meter where the price stands between the day's low and high, and the
        first of its change, its change in money or its price that fits six characters, in its own tone."""
        q = self.quote
        gauge = {"label": self.symbol[:GAUGE_LABEL] if show["symbol"] else ""}
        low, high, price = q.get("low"), q.get("high"), q.get("price")
        if None not in (low, high, price):
            gauge["meters"] = [50 if high <= low else min(max((price - low) / (high - low) * 100, 0), 100)]
        texts = [p for _, p in self.parts(dict(show, symbol=False), "text")]
        gauge["text"] = next((t for t in texts if len(t) <= GAUGE_TEXT), "")
        if self.tone:
            gauge["tone"] = self.tone
        return {k: v for k, v in gauge.items() if v != ""}

    def hero(self):
        """The bar's symbol big: its price and unit, its name, then its move, its range and its last close or time."""
        q = self.quote
        moved, moved_by = q.get("percent"), q.get("change")
        where = [q.get("name") or self.symbol, self.symbol]
        if self.approx:
            where.append("≈ from %s%s" % (sign(q.get("currency") or ""), figure(q["price"])))
        delta = change(moved)
        if moved_by is not None:
            delta += " · %s%s" % (signed(moved_by), self.money(abs(moved_by), near=False, shown=amount))
        items = [{"label": self.words["window"], "value": delta, "tone": tone(moved)},
                 {"label": self.words["range"], "value": "%s – %s" % (figure(self.scaled(q.get("low"))), figure(self.scaled(q.get("high"))))}]
        if q.get("previous") is None:
            items.append({"label": "As of", "value": clock(q.get("asOf") or self.fetched or time.time())})
        else:
            items.append({"label": "Prev close", "value": figure(self.scaled(q["previous"]))})
        hero = {"type": "hero", "glyph": {"fine": "📈", "alert": "📉"}.get(tone(moved), "➖"), "value": figure(self.scaled(q["price"])),
                "unit": self.unit, "place": " · ".join(where), "tone": self.tone,
                "items": [{k: v for k, v in stat.items() if v is not None} for stat in items]}
        return {k: v for k, v in hero.items() if v is not None}


class Board:
    """Every symbol asked, with its kept answer, and the rates its prices are shown at."""

    def __init__(self, want, quotes, fx):
        self.currency, self.source = want["currency"], want["source"]
        self.rates = {pair: r["rate"] for pair, r in fx.items() if isinstance(r, dict) and r.get("rate")}
        self.rows = [Row(s, quotes.get(record_key(s, want)) or {}, self) for s in want["symbols"]]
        self.pinned = [r for key in want["pinned"] for r in self.rows if r.key == key]
        self.us = any(not r.coin and r.quote.get("currency", "USD") == "USD" for r in self.rows)

    def status(self):
        """Updated 14:05 · US market closed · opens Mon 15:30."""
        times = [r.fetched for r in self.rows if r.fetched]
        found = ["Updated %s" % clock(max(times))] if times else ["Not asked yet"]
        if self.us:
            found.append(market_line(time.time()))
        return " · ".join(found)

    def credit(self):
        return SOURCES[self.source]["credit"] + FX_CREDIT

    def summary(self):
        return ", ".join("%s %s" % (r.symbol, change(r.quote.get("percent"))) for r in self.rows if r.quote) or "no prices yet"


# ---- the menu bar ----

def fitted(rows, show, style):
    """The bar's text for `rows` side by side, within Pilot's 24 characters: each row's parts, the lines dropped first,
    then the prices, then the rows that still do not fit (the first always kept, cut by Pilot)."""
    parts = [r.parts(show, style) for r in rows]
    for dropped in ((), ("line",), ("line", "price")):
        segments = [" ".join(t for p, t in each if p not in dropped) for each in parts]
        text = " · ".join(segments)
        if len(text) <= LONGEST:
            return text
    kept = segments[:1]
    for segment in segments[1:]:
        if len(" · ".join(kept + [segment])) > LONGEST:
            break
        kept.append(segment)
    return " · ".join(kept)


def shared_tone(rows):
    """One tone for text showing `rows`: warn when one is old or failed, else theirs when they agree, else none (the
    arrows say it)."""
    tones = {r.tone for r in rows}
    if "warn" in tones:
        return "warn"
    return tones.pop() if len(tones) == 1 else None


def turn(folder, count, advance):
    """Which of `count` bar symbols shows now when they take turns; a bar run (not a popover) moves to the next."""
    kept = load(folder, TURN).get("turn")
    now = kept if isinstance(kept, int) else -1
    if advance:
        now += 1
        save(folder, TURN, {"turn": now % count})
    return max(now, 0) % count


def bar_face(want, board, folder):
    """The bar line: its text, or gauges with no text, and its tone."""
    rows = board.pinned
    if not rows:
        return {"text": "📈 –", "tone": "warn"}
    if want["rotate"] and len(rows) > 1:
        rows = [rows[turn(folder, len(rows), advance=True)]]
    shown = [r for r in rows if r.quote]
    if want["style"] == "bars" and shown:
        return {"text": "", "gauges": [r.gauge(want["show"]) for r in shown[:GAUGES]]}
    face = {"text": fitted(rows, want["show"], want["style"])}
    found = shared_tone(rows)
    if found:
        face["tone"] = found
    return face


# ---- views ----

def drawn(run):
    """The block kinds this Pilot draws (its run message's `pilot.blocks`); an older Pilot names none."""
    pilot = run.get("pilot")
    return {k.strip() for k in str(pilot.get("blocks") or "").split(",")} if isinstance(pilot, dict) else set()


def refresh_button():
    return {"type": "run", "command": "refresh", "title": "Refresh", "icon": "refresh"}


def view_blocks(run, board, view):
    """The bar's first symbol big (where Pilot draws a hero), every symbol a row, then when it was read and the market's
    hours, and Refresh; a page adds its sources."""
    kinds = drawn(run)
    if not board.rows:
        return [{"type": "markdown", "text": "No symbols: add some in Markets' settings, e.g. SPY, AAPL, BTC, ETH."}]
    found = []
    first = board.pinned[0] if board.pinned else None
    if "hero" in kinds and first and first.quote:
        found.append(first.hero())
    found.append({"type": "list", "items": [r.item() for r in board.rows]})
    name = SOURCES[board.source]["name"]
    if "note" in kinds:
        found.append({"type": "note", "text": board.status(), "aside": name, "icon": "chart"})
    else:
        found.append({"type": "markdown", "text": "%s · %s" % (board.status(), name)})
    if view == "page":
        found += [{"type": "buttons", "actions": [refresh_button()]}, {"type": "markdown", "text": board.credit()}]
    elif "links" in kinds:
        found.append({"type": "links", "items": [{"icon": "refresh", "help": "Ask for the prices now",
                                                  "actions": [refresh_button()]}]})
    return found


def answer_blocks(run, board, running, view):
    message = {"type": "blocks", "blocks": view_blocks(run, board, view)}
    if running:
        message["refresh"] = REFRESH_SOON
    send(message)


def bar(run):
    popover = run.get("surface") == "popover"
    want, board, running = markets(run, BUDGET["popover" if popover else "bar"])
    if popover:
        answer_blocks(run, board, running, "popover")
        return
    send(dict(bar_face(want, board, run["paths"]["cache"]), type="bar", blocks=view_blocks(run, board, "popover")))


def page(run):
    _, board, running = markets(run, BUDGET["page"])
    answer_blocks(run, board, running, "page")


def listing(run):
    """Hyper+Space's Markets: every symbol a row, Refresh after them."""
    _, board, running = markets(run, BUDGET["list"])
    items = [r.item() for r in board.rows]
    items.append({"id": "refresh", "title": "Refresh the prices", "subtitle": board.status(), "icon": "🔄",
                  "actions": [dict(refresh_button(), title="Refresh the prices")]})
    send({"type": "list", "items": items})


def refresh(run):
    _, board, running = markets(run, REFRESH_BUDGET, force=True)
    failed = [r for r in board.rows if r.error]
    if running or (failed and len(failed) == len(board.rows)):
        send({"type": "error", "text": "could not refresh: %s" % (failed[0].error if failed else "no answer in time")})
        return
    send({"type": "hud", "text": "Markets: %s" % board.summary()})


def main():
    if sys.argv[1:] == ["--refresh"]:
        refresh_main()
        return
    try:
        run = json.loads(sys.stdin.readline())
    except ValueError:
        send({"type": "error", "text": "got a run message it could not read"})
        return
    command = run.get("command")
    view = {"markets": listing, "bar": bar, "page": page, "refresh": refresh}.get(command)
    if view:
        view(run)
    else:
        send({"type": "error", "text": "has no command %s" % command})


if __name__ == "__main__":
    main()
