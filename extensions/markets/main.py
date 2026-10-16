#!/usr/bin/env python3
"""Markets for Pilot: a stock, an ETF or a coin and its day's change in the menu bar; its popover and page list every
symbol with its price, its change and a spark line of the day (Surfaces v2, Pilot story 165), and Hyper+Space's Markets
lists the same rows. Each row opens the symbol's page in the browser.

Data, free and keyless: stocks and ETFs listed in the US from Nasdaq's quote API (api.nasdaq.com, the one Nasdaq.com's
own pages read; it answers only a browser's User-Agent), one call a symbol giving the last price, the day's change and
the day's minutes; coins from CoinGecko's public API, one call for all of them, its change over 24 h since a coin never
closes; euros and other currencies from the ECB's daily rates through Frankfurter, so a stock's converted price is near
(≈), its change in percent the exchange's own.

Every network call runs in a detached child (`main.py --refresh`, one at a time under refresh.lock) through curl, the
symbols due asked side by side, so a slow network never holds a view past Pilot's limit (Weather's way). A coin is due
after FRESH s; a stock after FRESH s while New York's session runs (09:30 to 16:00, closing prints SETTLE s later), and
otherwise only once after each close, so a closed market costs nothing; a holiday shows itself when an answer 10
minutes into the session holds no minute of that day, and is asked no more till the next close. A
failed symbol keeps its last answer, marked old, and is not asked again for RETRY s.
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
CURL_TIMEOUT = 8
BUDGET = {"bar": 1.5, "popover": 3.5, "page": 3.5, "list": 1.5}  # seconds a view waits for a fetch; Pilot's limits 2, 5, 5, 2
REFRESH_BUDGET = 25  # Refresh is a no-view run, stopped at 30 s
REFRESH_SOON = 3  # a view whose fetch still runs asks again this many seconds later
MOST = 12  # symbols asked
SPARK = 16  # a spark line's bars
BARS = "▁▂▃▄▅▆▇█"
QUOTES = "quotes.json"
NAMES = "names.json"
FX = "fx.json"
LOCK = "refresh.lock"
AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
NASDAQ = "https://api.nasdaq.com/api/quote/%s/chart?assetclass=%s"
COINS_API = "https://api.coingecko.com/api/v3/coins/markets"
SEARCH_API = "https://api.coingecko.com/api/v3/search"
FX_API = "https://api.frankfurter.dev/v1/latest"
NEW_YORK = ZoneInfo("America/New_York")
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,11}\Z")
# The coins a bare symbol means, by CoinGecko id; any other is a stock unless written coin:XYZ (stock:XYZ forces a
# stock: ETH is also Ethan Allen's ticker).
COINS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple", "BNB": "binancecoin", "DOGE": "dogecoin",
         "ADA": "cardano", "TRX": "tron", "AVAX": "avalanche-2", "DOT": "polkadot", "LINK": "chainlink", "LTC": "litecoin",
         "BCH": "bitcoin-cash", "XLM": "stellar", "TON": "the-open-network", "SHIB": "shiba-inu", "SUI": "sui",
         "HBAR": "hedera-hashgraph", "UNI": "uniswap", "ATOM": "cosmos", "XMR": "monero", "NEAR": "near", "APT": "aptos",
         "PEPE": "pepe", "HYPE": "hyperliquid", "USDT": "tether", "USDC": "usd-coin"}
MONEY = {"eur": "€", "usd": "$", "gbp": "£", "chf": "CHF "}
KINDS = {"stocks": "🏢", "etf": "🧺", "coin": "🪙"}
# How a stock's and a coin's move read: a stock's over its session, a coin's over 24 h (it never closes).
WORDS = {"stock": {"badge": "", "window": "Today", "range": "Day range"},
         "coin": {"badge": " 24h", "window": "24h", "range": "24h range"}}
CREDIT = ("Stocks and ETFs from [Nasdaq](https://www.nasdaq.com/), may be delayed; coins from "
          "[CoinGecko](https://www.coingecko.com/); currencies from the ECB through [Frankfurter](https://frankfurter.dev/).")


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

def symbols(text):
    """[{"key", "kind", "symbol"}] in the order typed, at most MOST: a known coin's symbol (or coin:XYZ) a coin, any other
    (or stock:XYZ) a stock or an ETF; a symbol that cannot be one is skipped."""
    found, seen = [], set()
    for word in re.split(r"[\s,;]+", str(text or "").upper()):
        kind, _, name = word.rpartition(":")
        kind = {"COIN": "coin", "STOCK": "stock", "": "coin" if name in COINS else "stock"}.get(kind)
        key = "%s:%s" % (kind, name)
        if not kind or not SYMBOL.match(name) or key in seen:
            continue
        seen.add(key)
        found.append({"key": key, "kind": kind, "symbol": name})
    return found[:MOST]


def settings(run):
    """{"symbols", "currency", "pinned", "price"}: what to ask, in which money, the bar's symbol (the first when the one
    named is not listed) and whether the bar shows its price."""
    prefs = run.get("preferences") or {}
    listed = symbols(prefs.get("symbols"))
    currency = prefs.get("currency") if prefs.get("currency") in MONEY else "eur"
    named = str(prefs.get("pinned") or "").strip().upper()
    pinned = next((s["key"] for s in listed if named in (s["symbol"], s["key"].upper())), listed[0]["key"] if listed else None)
    return {"symbols": listed, "currency": currency, "pinned": pinned, "price": prefs.get("bar") == "price"}


def record_key(symbol, currency):
    """Where a symbol's answer is kept: a stock's in dollars, whatever the currency; a coin's in the currency asked."""
    return symbol["key"] if symbol["kind"] == "stock" else "%s:%s" % (symbol["key"], currency)


# ---- New York's session ----

def session(day):
    """(opens, closes) of New York's regular session on `day`, a date, or None on a weekend (holidays not known)."""
    if day.weekday() >= 5:
        return None
    at = lambda h, m: datetime(day.year, day.month, day.day, h, m, tzinfo=NEW_YORK).timestamp()
    return at(9, 30), at(16, 0)


def ny_day(now):
    return datetime.fromtimestamp(now, NEW_YORK).date()


def in_session(now, settle=SETTLE):
    """Whether New York's regular session runs at `now` (seconds since 1970), `settle` s after its close included."""
    hours = session(ny_day(now))
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


def due(kind, record, now):
    """Whether a kept answer is asked again: none kept, a coin's after FRESH s, a stock's after FRESH s in session or
    once after each close; never while resting."""
    if resting(record, now):
        return False
    if "quote" not in record:
        return True
    fetched = record.get("fetchedAt") or 0
    if kind == "coin":
        return now - fetched >= FRESH
    return (in_session(now) and now - fetched >= FRESH and not holiday(record, now)) or fetched < last_settled(now)


def holiday(record, now):
    """A session day the market keeps shut: asked 10 minutes into it, Nasdaq still had no minute from that day."""
    opens = session(ny_day(now))[0]
    as_of = (record.get("quote") or {}).get("asOf")
    return bool(as_of) and (record.get("fetchedAt") or 0) >= opens + 600 and ny_day(as_of) != ny_day(now)


def fx_due(want, fx, now, force=False):
    """Whether the dollar's rate in the currency asked is asked again: only when a stock shows in another money."""
    if want["currency"] == "usd" or not any(s["kind"] == "stock" for s in want["symbols"]):
        return False
    record = fx.get(want["currency"]) or {}
    return force or (not resting(record, now) and now - (record.get("fetchedAt") or 0) >= FX_FRESH)


# ---- asking ----

def curl(url, browser=False):
    """(parsed JSON, None) or (None, why); `browser` sends a browser's User-Agent, which Nasdaq answers alone."""
    binary = shutil.which("curl") or "/usr/bin/curl"
    agent = ["-A", AGENT, "-H", "Accept: application/json"] if browser else []
    try:
        done = subprocess.run([binary, "-sS", "--fail", "--compressed", "--max-time", str(CURL_TIMEOUT)] + agent + [url],
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
            "asOf": points[-1][0] if points else None, "asset": asset}


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
    if "coin:" + symbol in names:
        return names["coin:" + symbol], None
    answer, error = curl(SEARCH_API + "?" + urlencode({"query": symbol}))
    if error:
        return None, "CoinGecko: %s" % error
    found = [c for c in (answer or {}).get("coins") or [] if isinstance(c, dict) and str(c.get("symbol", "")).upper() == symbol
             and isinstance(c.get("id"), str)]
    if not found:
        return None, "CoinGecko knows no %s" % symbol
    best = min(found, key=lambda c: c.get("market_cap_rank") if isinstance(c.get("market_cap_rank"), int) else 1 << 30)
    names["coin:" + symbol] = best["id"]
    return best["id"], None


def coin_quote(row):
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
            "id": row.get("id"), "asset": "coin"}


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
    answer, error = curl(COINS_API + "?" + urlencode({"vs_currency": currency, "ids": ",".join(sorted(ids)),
                                                      "sparkline": "true"}))
    rows = {r.get("id"): r for r in answer if isinstance(r, dict)} if isinstance(answer, list) else {}
    if not error and not isinstance(answer, list):
        error = "its answer did not read"
    for cid, symbol in ids.items():
        made = coin_quote(rows[cid]) if cid in rows else None
        found[symbol] = (made, None) if made else (None, "CoinGecko: %s" % error if error else "CoinGecko sent no %s" % symbol)
    return found


def fetch_fx(currency):
    """(the dollar's rate in `currency`, None) or (None, why)."""
    answer, error = curl(FX_API + "?" + urlencode({"base": "USD", "symbols": currency.upper()}))
    if error:
        return None, "Frankfurter: %s" % error
    rate = number((answer.get("rates") or {}).get(currency.upper())) if isinstance(answer, dict) else None
    return (rate, None) if rate else (None, "Frankfurter sent no %s rate" % currency.upper())


def fetch(want, folder, force):
    """Asks for every symbol due (all when `force`), the stocks side by side with the coins' one call and the rate, and
    keeps each answer with when it was tried; a failure keeps the last answer and says why."""
    now = time.time()
    from concurrent.futures import ThreadPoolExecutor  # the child's alone: a view never pays for it

    quotes, names, fx = load(folder, QUOTES), load(folder, NAMES), load(folder, FX)
    known = dict(names)
    asked = [s for s in want["symbols"] if force or due(s["kind"], quotes.get(record_key(s, want["currency"])) or {}, now)]
    stocks = [s for s in asked if s["kind"] == "stock"]
    coins = [s for s in asked if s["kind"] == "coin"]
    with ThreadPoolExecutor(max_workers=len(stocks) + 2) as pool:
        coin_job = pool.submit(fetch_coins, [s["symbol"] for s in coins], want["currency"], names) if coins else None
        fx_job = pool.submit(fetch_fx, want["currency"]) if fx_due(want, fx, now, force) else None
        jobs = {record_key(s, want["currency"]): pool.submit(fetch_stock, s["symbol"], names.get(s["key"])) for s in stocks}
        answers = {key: job.result() for key, job in jobs.items()}
        if coin_job:
            found = coin_job.result()
            answers.update((record_key(s, want["currency"]), found[s["symbol"]]) for s in coins)
        if fx_job:
            rate, error = fx_job.result()
            kept = fx.get(want["currency"]) or {}
            fx[want["currency"]] = dict(kept, triedAt=now, error=error, **({"rate": rate, "fetchedAt": now} if rate else {}))
            save(folder, FX, fx)
    for key, (made, error) in answers.items():
        if made:
            quotes[key] = {"triedAt": now, "error": None, "fetchedAt": now, "quote": made}
            if made["asset"] in ("stocks", "etf"):
                names[key] = made["asset"]
        else:
            quotes[key] = dict(quotes.get(key) or {}, triedAt=now, error=error)
    if names != known:
        save(folder, NAMES, names)
    save(folder, QUOTES, quotes)


# ---- the refresh child ----

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
    """(settings, Board of the kept answers, True while a fetch still runs). Any symbol due, or `force`, starts a fetch;
    a view with every symbol's answer shows them at once, one missing an answer (or `force`) waits `budget` s at most."""
    started = time.monotonic()
    folder = cache_dir(run)
    want = settings(run)
    now = time.time()
    quotes, fx = load(folder, QUOTES), load(folder, FX)
    kept = [(s["kind"], quotes.get(record_key(s, want["currency"])) or {}) for s in want["symbols"]]
    late = [record for kind, record in kept if due(kind, record, now)]
    waits = force or any("quote" not in record for record in late)
    child = start_refresh(folder, want, force) if force or late or fx_due(want, fx, now) else None
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


def short(value):
    """A price in at most six characters for the bar: 73.1k, 342, 4.21."""
    if value >= 10000:
        return "%.1fk" % (value / 1000) if value < 1e6 else "%.1fM" % (value / 1e6)
    return "%.0f" % value if value >= 100 else figure(value)


def tone(percent):
    """Up fine, down alert, flat plain: always with its arrow, never the colour alone."""
    if percent is None or round(percent, 2) == 0:
        return None
    return "fine" if percent > 0 else "alert"


def change(percent, tight=False):
    """▲ 1.29%, ▼ 0.28%, or 0.00% when flat."""
    if percent is None:
        return "–"
    arrow = {"fine": "▲", "alert": "▼"}.get(tone(percent), "")
    return "%s%s%.2f%%" % (arrow, "" if tight or not arrow else " ", abs(percent))


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
    """One symbol's kept answer, read for a view, its money settled once: a coin's in the currency CoinGecko priced it
    in, a stock's dollars at the ECB's rate (≈), or in dollars while no rate is known."""

    def __init__(self, symbol, record, board):
        self.symbol, self.kind, self.key = symbol["symbol"], symbol["kind"], symbol["key"]
        self.quote, self.error, self.fetched = record.get("quote") or {}, record.get("error"), record.get("fetchedAt")
        self.coin = self.kind == "coin"
        self.words = WORDS[self.kind]
        self.approx = not self.coin and board.currency != "usd" and bool(board.rate)
        self.rate = board.rate if self.approx else 1
        self.unit = "USD" if not self.coin and not self.approx else board.currency.upper()
        self.sign = MONEY[self.unit.lower()]

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

    def scaled(self, value):
        return None if value is None else value * self.rate

    def money(self, value, near=True):
        """A figure with its money sign, ≈ before a converted one."""
        if value is None:
            return "–"
        return ("≈ " if near and self.approx else "") + self.sign + figure(self.scaled(value))

    def url(self):
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
        item = {"id": self.key, "title": self.symbol, "icon": KINDS[self.quote.get("asset") or ("coin" if self.coin else "stocks")],
                "keywords": " ".join(filter(None, [self.symbol, self.quote.get("name"), self.kind])), "badges": self.badges(),
                "actions": [{"type": "open", "url": self.url(), "title": "Open %s in the browser" % self.symbol}],
                "inline": [{"type": "open", "url": self.url(), "title": "Open in the browser", "icon": "🌐"}]}
        if not self.quote:
            item["subtitle"] = self.error or "Asking…"
            return item
        price = self.money(self.quote["price"])
        item["title"] = "%s · %s" % (self.symbol, price)
        words = [spark(self.quote.get("line") or []), self.quote.get("name"), self.error]
        item["subtitle"] = "  ".join(w for w in words if w)
        item["actions"].append({"type": "copy", "text": "%s %s" % (self.symbol, price), "title": "Copy the price"})
        return item

    def bar_text(self, price):
        """SPY ▲0.28%, or with `price` SPY €689 ▲0.28% when it fits Pilot's 24 characters."""
        if not self.quote:
            return "%s –" % self.symbol
        moved = change(self.quote.get("percent"), tight=True)
        text = "%s %s%s %s" % (self.symbol, self.sign.strip(), short(self.scaled(self.quote["price"])), moved)
        return text if price and len(text) <= 24 else ("%s %s" % (self.symbol, moved))[:24]

    def hero(self):
        """The bar's symbol big: its price and unit, its name, then its move, its range and its last close or time."""
        q = self.quote
        moved, moved_by = q.get("percent"), q.get("change")
        where = [q.get("name") or self.symbol, self.symbol] + (["≈ from $%s" % figure(q["price"])] if self.approx else [])
        delta = change(moved)
        if moved_by is not None:
            delta += " · %s%s" % ("+" if moved_by > 0 else "−" if moved_by < 0 else "", self.money(abs(moved_by), near=False))
        items = [{"label": self.words["window"], "value": delta, "tone": tone(moved)},
                 {"label": self.words["range"], "value": "%s – %s" % (figure(self.scaled(q.get("low"))), figure(self.scaled(q.get("high"))))}]
        if q.get("previous") is None:
            items.append({"label": "As of", "value": clock(q.get("asOf") or self.fetched or time.time())})
        else:
            items.append({"label": "Prev close", "value": figure(self.scaled(q["previous"]))})
        hero = {"type": "hero", "glyph": {"fine": "📈", "alert": "📉"}.get(tone(moved), "➖"), "value": figure(self.scaled(q["price"])),
                "unit": self.unit, "place": " · ".join(where), "items": items, "tone": self.tone}
        hero["items"] = [{k: v for k, v in stat.items() if v is not None} for stat in items]
        return {k: v for k, v in hero.items() if v is not None}


class Board:
    """Every symbol asked, with its kept answer, and the rate a stock's dollars are shown at."""

    def __init__(self, want, quotes, fx):
        self.currency = want["currency"]
        self.rate = (fx.get(self.currency) or {}).get("rate") if self.currency != "usd" else None
        self.rows = [Row(s, quotes.get(record_key(s, self.currency)) or {}, self) for s in want["symbols"]]
        self.pinned = next((r for r in self.rows if r.key == want["pinned"]), None)
        self.stocks = any(not r.coin for r in self.rows)

    def status(self):
        """Updated 14:05 · US market closed · opens Mon 15:30."""
        times = [r.fetched for r in self.rows if r.fetched]
        words = ["Updated %s" % clock(max(times))] if times else ["Not asked yet"]
        if self.stocks:
            words.append(market_line(time.time()))
        return " · ".join(words)

    def sources(self):
        kinds = {r.coin for r in self.rows}
        return " · ".join(n for n, k in (("Nasdaq", False), ("CoinGecko", True)) if k in kinds)

    def summary(self):
        return ", ".join("%s %s" % (r.symbol, change(r.quote.get("percent"))) for r in self.rows if r.quote) or "no prices yet"


# ---- views ----

def drawn(run):
    """The block kinds this Pilot draws (its run message's `pilot.blocks`); an older Pilot names none."""
    pilot = run.get("pilot")
    return {k.strip() for k in str(pilot.get("blocks") or "").split(",")} if isinstance(pilot, dict) else set()


def refresh_button():
    return {"type": "run", "command": "refresh", "title": "Refresh", "icon": "refresh"}


def view_blocks(run, board, view):
    """The bar's symbol big (where Pilot draws a hero), every symbol a row, then when it was read and the market's hours,
    and Refresh; a page adds its sources."""
    kinds = drawn(run)
    if not board.rows:
        return [{"type": "markdown", "text": "No symbols: add some in Markets' settings, e.g. SPY, AAPL, BTC, ETH."}]
    found = []
    if "hero" in kinds and board.pinned and board.pinned.quote:
        found.append(board.pinned.hero())
    found.append({"type": "list", "items": [r.item() for r in board.rows]})
    if "note" in kinds:
        found.append({"type": "note", "text": board.status(), "aside": board.sources(), "icon": "chart"})
    else:
        found.append({"type": "markdown", "text": "%s · %s" % (board.status(), board.sources())})
    if view == "page":
        found += [{"type": "buttons", "actions": [refresh_button()]}, {"type": "markdown", "text": CREDIT}]
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
    row = board.pinned
    message = {"type": "bar", "text": row.bar_text(want["price"]) if row else "📈 –",
               "blocks": view_blocks(run, board, "popover")}
    found = row.tone if row else "warn"
    if found:
        message["tone"] = found
    send(message)


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
