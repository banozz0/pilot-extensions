import fcntl
import importlib.util
import json
import os
import time
import unittest
from unittest import mock
from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from harness import EXTENSIONS, ExtensionTestCase

_spec = importlib.util.spec_from_file_location("markets", EXTENSIONS / "markets" / "main.py")
markets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(markets)

NY = ZoneInfo("America/New_York")


def ny(y, mo, d, h, mi=0):
    """Seconds since 1970 at a New York wall-clock time."""
    return datetime(y, mo, d, h, mi, tzinfo=NY).timestamp()


def chart(symbol, company, last, change, percent, previous, line):
    """Nasdaq's chart answer as its API gives it (trimmed): Thursday 15 October 2026, a pre-market minute, the session
    every 30 minutes from 09:30 to 16:00, an after-hours minute."""
    stamps = [ny(2026, 10, 15, 4)] + [ny(2026, 10, 15, 9, 30) + i * 1800 for i in range(14)] + [ny(2026, 10, 15, 20)]
    values = [line[0] - 5] + line + [last]
    return {"data": {"symbol": symbol, "company": company, "timeAsOf": "Oct 15, 2026", "lastSalePrice": "$%s" % last,
                     "netChange": change, "percentageChange": percent, "deltaIndicator": "up", "previousClose": "$%s" % previous,
                     "volume": "39,767,602", "exchange": "NASDAQ-GS",
                     "chart": [{"z": {"dateTime": "", "value": str(v)}, "x": int(t * 1000), "y": v} for t, v in zip(stamps, values)]},
            "message": None, "status": {"rCode": 200, "bCodeMessage": None, "developerMessage": None}}


NOT_FOUND = {"data": None, "message": None,
             "status": {"rCode": 400, "bCodeMessage": [{"code": 1001, "errorMessage": "Symbol not exists."}]}}
SESSION = [340 + (i % 5) for i in range(14)]
FIXTURES = {
    "nasdaq-AAPL-stocks": chart("AAPL", "Apple Inc. Common Stock", "342.00", "-0.96", "-0.28%", "342.96", SESSION),
    "nasdaq-SPY-stocks": NOT_FOUND,
    "nasdaq-SPY-etf": chart("SPY", "State Street SPDR S&P 500 ETF Trust", "771.79", "+2.19", "+0.28%", "769.60",
                            [765 + i for i in range(14)]),
    "nasdaq-ZZZZQ-stocks": NOT_FOUND, "nasdaq-ZZZZQ-etf": NOT_FOUND,
    "markets": [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "current_price": 73093, "high_24h": 73420,
                 "low_24h": 71016, "price_change_24h": 1196.72, "price_change_percentage_24h": 1.43819,
                 "last_updated": "2026-10-16T07:05:56.000Z", "sparkline_in_7d": {"price": [80000 + i * 10 for i in range(168)]}},
                {"id": "ethereum", "symbol": "eth", "name": "Ethereum", "current_price": 2650.5, "high_24h": 2700,
                 "low_24h": 2600, "price_change_24h": -15.2, "price_change_percentage_24h": -0.57,
                 "last_updated": "2026-10-16T07:05:56.000Z", "sparkline_in_7d": {"price": [3000 - i for i in range(168)]}},
                {"id": "kaspa", "symbol": "kas", "name": "Kaspa", "current_price": 0.0612345, "high_24h": 0.063,
                 "low_24h": 0.06, "price_change_24h": 0, "price_change_percentage_24h": 0,
                 "last_updated": "2026-10-16T07:05:56.000Z", "sparkline_in_7d": {"price": [0.06] * 168}}],
    "search": {"coins": [{"id": "kaspa-fake", "symbol": "KAS", "market_cap_rank": 900},
                         {"id": "kaspa", "symbol": "KAS", "market_cap_rank": 40},
                         {"id": "kasta", "symbol": "KASTA", "market_cap_rank": 10}]},
    "fx": {"amount": 1.0, "base": "USD", "date": "2026-10-15", "rates": {"EUR": 0.89222}},
}


class MarketsTests(ExtensionTestCase):
    EXTENSION = "markets"
    PILOT = {"repos": "/tmp/code", "terminal": "com.mitchellh.ghostty",
             "blocks": "stats,meter,list,markdown,buttons,section,chat,input,hero,strip,note,links"}
    OLDER = {"repos": "/tmp/code", "terminal": "com.mitchellh.ghostty"}

    def setUp(self):
        super().setUp()
        self.addCleanup(self.settle)
        self.fixtures = self.box.root / "fixtures"
        self.fixtures.mkdir()
        for name, body in FIXTURES.items():
            (self.fixtures / (name + ".json")).write_text(json.dumps(body))
        self.fake_curl()

    def fake_curl(self, fail=None, delay=0):
        """A curl that logs its arguments and answers from the fixtures by host and path, `delay` s late, or fails as
        curl does; a URL with no fixture is a 404."""
        body = 'for url; do :; done\nprintf "%%s\\n" "$*" >> "%s"\n' % self.box.log
        if delay:
            body += "sleep %s\n" % delay
        if fail:
            body += 'echo "curl: %s" >&2; exit 6' % fail
        else:
            body += ('case "$url" in\n'
                     '  *api.nasdaq.com/api/quote/*) s=${url#*/api/quote/}; s=${s%%%%/*}; f="nasdaq-$s-${url##*assetclass=}";;\n'
                     '  *coins/markets*) f=markets;; *api/v3/search*) f=search;; *frankfurter*) f=fx;; *) f=none;;\n'
                     'esac\n'
                     'if [ -f "%s/$f.json" ]; then cat "%s/$f.json"; else echo "curl: (22) The requested URL returned error: 404" >&2; exit 22; fi'
                     % (self.fixtures, self.fixtures))
        self.box.shim("curl", body)

    def calls(self):
        """(host, path, query, the arguments before the URL) per curl call."""
        found = []
        for line in self.box.shim_calls():
            args, _, url = line.rpartition(" ")
            parts = urlsplit(url)
            found.append((parts.netloc, parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}, args))
        return found

    def asked(self, since=0):
        """What was asked from the `since`th call on, in short and sorted: AAPL/stocks, markets, search, fx."""
        short = []
        for host, path, query, _ in self.calls()[since:]:
            if host == "api.nasdaq.com":
                short.append("%s/%s" % (path.split("/")[3], query["assetclass"]))
            else:
                short.append({"api.frankfurter.dev": "fx"}.get(host, path.rsplit("/", 1)[-1]))
        return sorted(short)

    def settle(self):
        lock = self.box.cache / "refresh.lock"
        if lock.exists():
            with open(str(lock), "a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)

    def kept(self):
        return json.loads((self.box.cache / "quotes.json").read_text())

    def age_all(self, seconds):
        kept = self.kept()
        for record in kept.values():
            record["fetchedAt"] -= seconds
            record["triedAt"] -= seconds
        (self.box.cache / "quotes.json").write_text(json.dumps(kept))

    def coins_query(self):
        """The query of the CoinGecko markets call."""
        return next(q for _, p, q, _ in self.calls() if p.endswith("/markets"))

    def rows(self, blocks):
        return {i["id"]: i for b in blocks if b["type"] == "list" for i in b["items"]}

    # ---- the bar and the cache ----

    def test_bar_shows_the_first_symbol_and_its_change_and_asks_each_source_once(self):
        line = self.bar()
        self.assertEqual((line["text"], line["tone"]), ("SPY ▲0.28%", "fine"))
        self.assertEqual(self.asked(), ["AAPL/stocks", "SPY/etf", "SPY/stocks", "fx", "markets"])
        coins = self.coins_query()
        self.assertEqual((coins["vs_currency"], coins["ids"], coins["sparkline"]), ("eur", "bitcoin,ethereum", "true"))
        for host, _, _, args in self.calls():
            self.assertIn(host, {"api.nasdaq.com", "api.coingecko.com", "api.frankfurter.dev"})
            self.assertEqual("Mozilla/" in args, host == "api.nasdaq.com", "Nasdaq answers a browser's User-Agent alone")
        self.assertEqual(self.bar()["text"], "SPY ▲0.28%")
        self.assertEqual(len(self.calls()), 5, "fresh answers are not asked again")

    def test_a_stock_found_as_an_etf_is_asked_as_one_from_then_on(self):
        self.bar()
        self.age_all(8 * 86400)
        self.blocks_message("bar", "popover")
        self.settle()
        self.assertEqual(self.asked(since=5), ["AAPL/stocks", "SPY/etf", "markets"], "neither the class nor the rate asked again")

    def test_a_falling_pin_is_alert_with_its_arrow_and_the_price_shows_when_asked(self):
        line = self.bar(preferences={"pinned": "aapl"})
        self.assertEqual((line["text"], line["tone"]), ("AAPL ▼0.28%", "alert"))
        self.assertEqual(self.bar(preferences={"pinned": "BTC", "bar": "price"})["text"], "BTC €73.1k ▲1.44%")
        self.assertEqual(self.bar(preferences={"pinned": "SPY", "bar": "price"})["text"], "SPY €689 ▲0.28%")
        self.assertEqual(self.bar(preferences={"pinned": "NOPE"})["text"], "SPY ▲0.28%", "an unlisted pin is the first")

    def test_offline_keeps_the_last_prices_marked_old_and_waits_a_minute_to_retry(self):
        self.bar()
        self.age_all(8 * 86400)
        self.fake_curl(fail="(6) Could not resolve host: api.nasdaq.com")
        self.bar()
        self.settle()
        line = self.bar()
        self.assertEqual((line["text"], line["tone"]), ("SPY ▲0.28%", "warn"))
        rows = self.rows(self.popover(pilot=self.PILOT))
        self.assertIn({"text": "⚠ 8d old", "tone": "warn"}, rows["stock:SPY"]["badges"])
        self.assertIn("Nasdaq: (6) Could not resolve host", rows["stock:SPY"]["subtitle"])
        self.assertEqual(len(self.calls()), 5 + 3, "one failed try each (SPY, AAPL, coins), then none for a minute")

    def test_offline_with_nothing_kept_says_why(self):
        self.fake_curl(fail="(6) Could not resolve host: api.nasdaq.com")
        line = self.bar()
        self.assertEqual((line["text"], line["tone"]), ("SPY –", "warn"))
        spy = self.rows(line["blocks"])["stock:SPY"]
        self.assertEqual((spy["title"], spy["subtitle"]), ("SPY", "Nasdaq: (6) Could not resolve host: api.nasdaq.com"))
        self.assertEqual(spy["badges"], [{"text": "no price", "tone": "warn"}])

    def test_a_slow_network_shows_the_rows_asking_then_the_prices(self):
        self.fake_curl(delay=1.8)
        line = self.bar()
        self.assertEqual(line["text"], "SPY –")
        self.assertNotIn("tone", line, "asking is no failure")
        spy = self.rows(line["blocks"])["stock:SPY"]
        self.assertEqual((spy["subtitle"], spy["badges"]), ("Asking…", []))
        self.settle()
        self.assertEqual(self.bar()["text"], "SPY ▲0.28%")

    def test_an_unknown_symbol_says_so_and_the_rest_still_show(self):
        rows = self.rows(self.popover(preferences={"symbols": "ZZZZQ, BTC"}))
        self.assertEqual(rows["stock:ZZZZQ"]["subtitle"], "Nasdaq knows no ZZZZQ")
        self.assertEqual(rows["coin:BTC"]["title"], "BTC · €73,093")
        self.popover(preferences={"symbols": "ZZZZQ, BTC"})
        self.assertEqual(self.asked().count("ZZZZQ/stocks"), 1, "not asked again within a minute")

    def test_a_coin_not_known_is_found_once_by_its_symbol(self):
        rows = self.rows(self.popover(preferences={"symbols": "coin:KAS"}))
        self.assertEqual((rows["coin:KAS"]["title"], rows["coin:KAS"]["badges"]), ("KAS · €0.06123", [{"text": "0.00% 24h"}]))
        self.assertEqual(self.coins_query()["ids"], "kaspa", "the highest-ranked KAS")
        self.age_all(3600)
        self.popover(preferences={"symbols": "coin:KAS"})
        self.settle()
        self.assertEqual(self.asked(), ["markets", "markets", "search"], "searched once, kept")

    def test_dollars_need_no_rate(self):
        rows = self.rows(self.popover(preferences={"currency": "usd"}))
        self.assertEqual(rows["stock:SPY"]["title"], "SPY · $771.79")
        self.assertNotIn("fx", self.asked())
        self.assertEqual(self.coins_query()["vs_currency"], "usd")

    # ---- views ----

    maxDiff = None

    def test_popover_leads_with_the_pin_big_then_a_row_a_symbol_with_its_line_and_the_market_hours(self):
        hero, listed, note, links = self.popover(pilot=self.PILOT)
        self.assertEqual(hero, {"type": "hero", "glyph": "📈", "value": "688.61", "unit": "EUR", "tone": "fine",
                                "place": "State Street SPDR S&P 500 ETF Trust · SPY · ≈ from $771.79",
                                "items": [{"label": "Today", "value": "▲ 0.28% · +€1.95", "tone": "fine"},
                                          {"label": "Day range", "value": "682.55 – 694.15"},
                                          {"label": "Prev close", "value": "686.65"}]})
        rows = listed["items"]
        self.assertEqual([r["title"] for r in rows], ["SPY · ≈ €688.61", "AAPL · ≈ €305.14", "BTC · €73,093", "ETH · €2,650.50"])
        self.assertEqual([r["badges"] for r in rows],
                         [[{"text": "▲ 0.28%", "tone": "fine"}], [{"text": "▼ 0.28%", "tone": "alert"}],
                          [{"text": "▲ 1.44% 24h", "tone": "fine"}], [{"text": "▼ 0.57% 24h", "tone": "alert"}]])
        self.assertEqual([r["icon"] for r in rows], ["🧺", "🏢", "🪙", "🪙"])
        self.assertEqual(rows[0]["subtitle"], "▁▂▂▃▃▄▄▅▅▆▆▇▇█  State Street SPDR S&P 500 ETF Trust",
                         "the session alone, 09:30 to 16:00")
        self.assertEqual(rows[1]["subtitle"].split("  ")[1], "Apple Inc.")
        self.assertEqual(len(rows[2]["subtitle"].split("  ")[0]), 16, "a coin's last 24 hours in 16 bars")
        self.assertEqual(rows[0]["actions"][0], {"type": "open", "url": "https://www.nasdaq.com/market-activity/etfs/spy",
                                                 "title": "Open SPY in the browser"})
        self.assertEqual(rows[1]["inline"][0]["url"], "https://www.nasdaq.com/market-activity/stocks/aapl")
        self.assertEqual(rows[2]["inline"][0], {"type": "open", "url": "https://www.coingecko.com/en/coins/bitcoin",
                                                "title": "Open in the browser", "icon": "🌐"})
        self.assertEqual(rows[1]["actions"][1], {"type": "copy", "text": "AAPL ≈ €305.14", "title": "Copy the price"})
        self.assertRegex(note["text"], r"^Updated \d\d:\d\d · US market (open until|closed · opens) ")
        self.assertEqual((note["aside"], links["items"][0]["actions"][0]["command"]), ("Nasdaq · CoinGecko", "refresh"))

    def test_an_older_pilot_gets_the_rows_and_the_hours_in_words(self):
        for pilot in (None, self.OLDER):
            with self.subTest(pilot=pilot):
                blocks = self.popover(pilot=pilot)
                self.assertEqual([b["type"] for b in blocks], ["list", "markdown"])
                self.assertIn("· Nasdaq · CoinGecko", blocks[1]["text"])

    def test_page_adds_refresh_and_the_credit(self):
        blocks = self.page(pilot=self.PILOT)
        self.assertEqual([b["type"] for b in blocks], ["hero", "list", "note", "buttons", "markdown"])
        self.assertEqual(blocks[3]["actions"][0]["command"], "refresh")
        self.assertIn("ECB", blocks[4]["text"])

    def test_no_symbols_says_where_to_add_them(self):
        line = self.bar(preferences={"symbols": " , ; "})
        self.assertEqual((line["text"], line["tone"]), ("📈 –", "warn"))
        self.assertIn("settings", line["blocks"][0]["text"])
        self.assertEqual(self.calls(), [])

    def test_hyper_space_lists_the_rows_then_refresh(self):
        items = self.run_ext("markets").items
        self.assertItemShape(items)
        self.assertEqual([i["id"] for i in items], ["stock:SPY", "stock:AAPL", "coin:BTC", "coin:ETH", "refresh"])
        self.assertEqual(items[-1]["actions"][0]["command"], "refresh")

    def test_refresh_asks_everything_again_and_says_the_moves(self):
        self.bar()
        result = self.run_ext("refresh")
        self.assertEqual(result.of_type("hud")[0]["text"],
                         "Markets: SPY ▲ 0.28%, AAPL ▼ 0.28%, BTC ▲ 1.44%, ETH ▼ 0.57%")
        self.assertEqual(len(self.calls()), 5 + 4, "SPY as an ETF, AAPL, the coins and the rate again")


class MarketWords(unittest.TestCase):
    """The parts that need no network or clock: symbols, New York's session, the figures and the line."""

    def test_symbols(self):
        found = markets.symbols("spy, AAPL btc;ETH, coin:kas stock:ETH, BTC, bad$sym, ")
        self.assertEqual([s["key"] for s in found], ["stock:SPY", "stock:AAPL", "coin:BTC", "coin:ETH", "coin:KAS", "stock:ETH"])
        self.assertEqual(len(markets.symbols(" ".join("S%d" % i for i in range(20)))), 12)

    def test_a_stock_is_asked_every_five_minutes_in_session_and_once_after_each_close(self):
        def due(now, fetched, key="stock:SPY", **extra):
            return markets.due(key.split(":")[0], dict({"quote": {}, "fetchedAt": fetched, "triedAt": fetched}, **extra), now)
        thursday_noon = ny(2026, 10, 15, 12)
        self.assertFalse(due(thursday_noon, thursday_noon - 200))
        self.assertTrue(due(thursday_noon, thursday_noon - 400))
        self.assertTrue(due(ny(2026, 10, 15, 16, 10), ny(2026, 10, 15, 16, 1)), "the closing prints still settle")
        self.assertTrue(due(ny(2026, 10, 15, 18), ny(2026, 10, 15, 16, 10)), "asked once after they settled")
        self.assertFalse(due(ny(2026, 10, 15, 23), ny(2026, 10, 15, 18)))
        self.assertFalse(due(ny(2026, 10, 18, 12), ny(2026, 10, 16, 17)), "a Sunday asks nothing after Friday's close")
        self.assertTrue(due(ny(2026, 10, 19, 9, 40), ny(2026, 10, 16, 17)), "Monday's session")
        self.assertTrue(due(ny(2026, 10, 18, 12), ny(2026, 10, 18, 11), key="coin:BTC:eur"), "a coin never closes")
        self.assertFalse(due(thursday_noon, 0, error="x", triedAt=thursday_noon - 30), "a minute after a failure")
        self.assertTrue(markets.due("stock", {}, thursday_noon))

    def test_a_holiday_is_asked_once_into_the_session_then_not_till_the_close(self):
        thanksgiving_noon = ny(2026, 11, 26, 12)
        record = {"quote": {"asOf": ny(2026, 11, 25, 20)}, "fetchedAt": ny(2026, 11, 26, 9, 35)}
        self.assertTrue(markets.due("stock", record, thanksgiving_noon), "five minutes in: not yet known")
        record["fetchedAt"] = ny(2026, 11, 26, 9, 45)
        self.assertFalse(markets.due("stock", record, thanksgiving_noon), "no minute today at 09:45: shut")
        record["quote"]["asOf"] = ny(2026, 11, 26, 9, 44)
        self.assertTrue(markets.due("stock", record, thanksgiving_noon), "a trading day")

    def test_market_line_in_the_macs_time(self):
        with mock.patch.dict(os.environ, {"TZ": "Europe/Malta"}):
            time.tzset()
            self.addCleanup(time.tzset)
            self.assertEqual(markets.market_line(ny(2026, 10, 15, 12))[:21], "US market open until ")
            self.assertRegex(markets.market_line(ny(2026, 10, 17, 12)), r"^US market closed · opens (Mon )?15:30$")
            self.assertRegex(markets.market_line(ny(2026, 10, 15, 8)), r"^US market closed · opens (Thu )?15:30$")
            self.assertRegex(markets.market_line(ny(2026, 10, 16, 17)), r"^US market closed · opens (Mon )?15:30$")

    def test_figures(self):
        self.assertEqual([markets.figure(v) for v in (73093, 342.0, 0.0612345)], ["73,093", "342.00", "0.06123"])
        self.assertEqual([markets.short(v) for v in (73093, 688.6, 4.2)], ["73.1k", "689", "4.20"])
        self.assertEqual([markets.change(v) for v in (1.4381, -0.28, 0.001, None)], ["▲ 1.44%", "▼ 0.28%", "0.00%", "–"])
        self.assertEqual([markets.tone(v) for v in (1, -1, 0.004)], ["fine", "alert", None])
        self.assertEqual([markets.number(v) for v in ("$1,342.00", "+2.19", "-0.28%", "UNCH", "N/A")], [1342, 2.19, -0.28, 0, None])

    def test_spark_and_thin(self):
        self.assertEqual(markets.spark([1, 2, 3, 4, 5, 6, 7, 8]), "▁▂▃▄▅▆▇█")
        self.assertEqual(markets.spark([5, 5, 5]), "▄▄▄")
        self.assertEqual(markets.spark([5]), "")
        self.assertEqual(markets.thin(list(range(100)), 4), [24, 49, 74, 99])


if __name__ == "__main__":
    unittest.main()
