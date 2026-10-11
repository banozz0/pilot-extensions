import fcntl
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import time
import unittest

from harness import EXTENSIONS, ExtensionTestCase

_spec = importlib.util.spec_from_file_location("ai_usage_main", EXTENSIONS / "ai-usage" / "main.py")
ai_usage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ai_usage)

SECRET_SAMPLE = "sk-THIS-MUST-NEVER-BE-PRINTED"
MIN, HOUR, DAY = 60, 3600, 86400
EMAIL, OTHER = "sven@example.com", "work@example.com"

# A stand-in for `codex`, each call logged with its CODEX_HOME. `login` prints a sign-in link, waits for the file
# `release-login` in CODEX_HOME (at most 20 s), then saves a stand-in login there; `logout` removes it. `app-server`: JSON-RPC
# lines in, its answer from $CODEX_HOME/fake-codex.json (test-only); no such file is a codex that fails at once.
FAKE_CODEX = r'''
import json, os, sys, time
home = os.environ.get("CODEX_HOME", "")
with open(sys.argv[1], "a") as log:
    log.write("codex %s CODEX_HOME=%s\n" % (" ".join(sys.argv[2:]), home))
if sys.argv[2:3] == ["login"]:
    print("If your browser did not open, navigate to this URL to authenticate:\n\nhttps://auth.example.test/sign-in?x=1", flush=True)
    for _ in range(200):
        if os.path.exists(os.path.join(home, "release-login")):
            break
        time.sleep(0.1)
    with open(os.path.join(home, "auth.json"), "w") as f:
        f.write('{"stand_in": "signed in"}')
    sys.exit(0)
if sys.argv[2:3] == ["logout"]:
    os.unlink(os.path.join(home, "auth.json"))
    sys.exit(0)
try:
    with open(os.path.join(home, "fake-codex.json")) as f:
        fake = json.load(f)
except (OSError, ValueError):
    sys.exit(1)
time.sleep(fake.get("sleep", 0))
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    result = {1: {"userAgent": "fake"}, 2: {"account": fake["account"], "requiresOpenaiAuth": True},
              3: fake.get("limits")}[message["id"]]
    if result is None:
        print(json.dumps({"id": message["id"], "error": {"code": -32600, "message": "not signed in"}}), flush=True)
    else:
        print(json.dumps({"id": message["id"], "result": result}), flush=True)
    if message["id"] == 3:
        break
'''


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


def iso_offset(t):
    """An ISO time as Claude Code caches it: microseconds and an offset."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000000+00:00"


def window(pct, minutes, resets_at=None, resets_in=None):
    w = {"used_percent": pct, "window_minutes": minutes}
    if resets_at is not None:
        w["resets_at"] = int(resets_at)
    if resets_in is not None:
        w["resets_in_seconds"] = int(resets_in)
    return w


def codex_event(t, primary, secondary, limit_id="codex", credits=None):
    """A token_count line as Codex writes it (shape read from a real 2026-10 log)."""
    usage = {"input_tokens": 1000, "cached_input_tokens": 900, "cache_write_input_tokens": 0, "output_tokens": 10,
             "reasoning_output_tokens": 0, "total_tokens": 1010}
    return {"timestamp": iso(t), "ordinal": 1, "type": "event_msg",
            "payload": {"type": "token_count",
                        "info": {"total_token_usage": usage, "last_token_usage": usage, "model_context_window": 258400},
                        "rate_limits": {"limit_id": limit_id, "limit_name": None, "primary": primary,
                                        "secondary": secondary,
                                        "credits": credits or {"has_credits": False, "unlimited": False, "balance": "0"},
                                        "individual_limit": None, "spend_control_reached": None, "plan_type": None,
                                        "rate_limit_reached_type": None}}}


def live_window(pct, minutes, resets_at):
    return {"usedPercent": pct, "windowDurationMins": minutes, "resetsAt": int(resets_at)}


def codex_answer(email, plan, primary, secondary, credits=(), balance="0"):
    """`account/read` and `account/rateLimits/read` as codex-cli 0.162.1 answered them live on 2026-10-10."""
    snapshot = {"limitId": "codex", "limitName": None, "normalModelSlug": None, "primary": primary,
                "secondary": secondary, "credits": {"hasCredits": balance != "0", "unlimited": False, "balance": balance},
                "individualLimit": None, "spendControlReached": False, "planType": plan, "rateLimitReachedType": None}
    resets = [{"id": "RateLimitResetCredit_%d" % i, "resetType": "codexRateLimits", "status": status,
               "grantedAt": int(expires - 30 * DAY), "expiresAt": int(expires), "title": "Full reset (Weekly + 5 hr)",
               "description": "Thanks for using Codex!"} for i, (expires, status) in enumerate(credits)]
    return {"account": {"type": "chatgpt", "email": email, "planType": plan},
            "limits": {"ordinaryUsageAllowed": True, "rateLimits": snapshot, "rateLimitsByLimitId": {"codex": snapshot},
                       "rateLimitResetCredits": {"availableCount": sum(s == "available" for _, s in credits),
                                                 "credits": resets},
                       "accountId": "acc", "rateLimitUpsell": None}}


def statusline(updated, five=None, week=None):
    """A Ghostex status-line file as it stores Claude Code's status-line input (shape read live 2026-10-10)."""
    payload = {"session_id": "s", "transcript_path": "/x.jsonl", "cwd": "/x", "model": {"id": "claude-opus-5-5"},
               "context_window": {"total_input_tokens": 5, "used_percentage": 12}, "fast_mode": False}
    if five or week:
        payload["rate_limits"] = {k: {"used_percentage": v[0], "resets_at": int(v[1])}
                                  for k, v in (("five_hour", five), ("seven_day", week)) if v}
    return {"version": 1, "updatedAt": iso(updated), "payload": payload}


def claude_json(fetched, session=None, week=None, fable=None, account="acc-1", cached_for="acc-1"):
    """~/.claude.json's account and Claude Code's cached usage read (shape read live 2026-10-10), with an MCP server's
    secret beside them that must never show."""
    limits = []
    for kind, value, model in (("session", session, None), ("weekly_all", week, None), ("weekly_scoped", fable, "Fable")):
        if value:
            limits.append({"kind": kind, "group": "weekly", "percent": value[0], "severity": "normal",
                           "resets_at": iso_offset(value[1]), "is_active": False,
                           "scope": {"model": {"id": None, "display_name": model}, "surface": None} if model else None})
    return {"mcpServers": {"x": {"env": {"TOKEN_SAMPLE": SECRET_SAMPLE}}},
            "oauthAccount": {"accountUuid": account, "emailAddress": EMAIL, "organizationType": "claude_max",
                             "organizationRateLimitTier": "default_claude_max_20x", "billingType": "stripe_subscription"},
            "cachedUsageUtilization": {"fetchedAtMs": int(fetched * 1000), "accountUuid": cached_for,
                                       "utilization": {"five_hour": None, "limits": limits}}}


def claude_line(t, msg_id, inp=0, out=0, write=0, read=0):
    """An assistant line as Claude Code writes it (shape read from a real 2026-10 log)."""
    return {"parentUuid": "p", "isSidechain": False,
            "message": {"model": "claude-opus-5-5", "id": msg_id, "type": "message", "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}], "stop_reason": None,
                        "usage": {"input_tokens": inp, "cache_creation_input_tokens": write,
                                  "cache_read_input_tokens": read, "output_tokens": out,
                                  "service_tier": "standard"}},
            "requestId": "req_" + msg_id, "type": "assistant", "uuid": "u-" + msg_id, "timestamp": iso(t),
            "sessionId": "s", "version": "2.1.0"}


def cards(blocks, title):
    return [b for b in blocks if b["type"] == "section" and b["title"] == title]


def card(blocks, title):
    return cards(blocks, title)[0]


def hhmm(t):
    return time.strftime("%H:%M", time.localtime(t))


class AIUsageTests(ExtensionTestCase):
    EXTENSION = "ai-usage"

    def setUp(self):
        super().setUp()
        self.now = int(time.time())
        self.codex = self.box.home / ".codex" / "sessions" / "2026" / "10" / "10"
        self.projects = self.box.home / ".claude" / "projects"
        self.store = self.box.home / ".local" / "state" / "ghostex" / "agent-hooks" / "claude-statusline"
        for secret in (self.box.home / ".codex" / "auth.json", self.box.home / ".claude" / ".credentials.json",
                       self.box.home / ".gemini" / "oauth_creds.json"):
            secret.parent.mkdir(parents=True, exist_ok=True)
            secret.write_text(json.dumps({"bot": SECRET_SAMPLE}))
        fake = self.box.root / "fake_codex.py"
        fake.write_text(FAKE_CODEX)
        # Always shimmed: the real codex on PATH must never be asked from a test.
        self.box.shim("codex", 'exec python3 "%s" "%s" "$@"' % (fake, self.box.log))
        self.status_pages({"claude": {"indicator": "none", "description": "All Systems Operational", "updated": None},
                           "codex": {"indicator": "none", "description": "All Systems Operational", "updated": None}})

    def status_pages(self, found, age=0):
        """The status pages' last read, fresh, so no test reaches the network."""
        self.box.cache.mkdir(parents=True, exist_ok=True)
        (self.box.cache / "status.json").write_text(json.dumps(dict(found, at=self.now - age)))

    def write(self, path, lines, age=0, append=False):
        """JSON lines, or a str written as it is."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a" if append else "w") as f:
            f.write(lines if isinstance(lines, str) else
                    "".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines))
        os.utime(path, (self.now - age, self.now - age))
        return path

    def status(self, name, data, age=0):
        self.write(self.store / ("%s.json" % name), data if isinstance(data, str) else json.dumps(data), age)

    def claude_cache(self, data):
        self.write(self.box.home / ".claude.json", json.dumps(data))

    def codex_log(self, name, events, age=0, home=".codex"):
        meta = {"timestamp": iso(self.now - HOUR), "type": "session_meta", "payload": {"id": name, "cwd": "/x"}}
        folder = self.box.home / home / "sessions" / "2026" / "10" / "10"
        return self.write(folder / ("rollout-%s.jsonl" % name), [meta] + events, age)

    def codex_says(self, answer, home=".codex"):
        self.write(self.box.home / home / "fake-codex.json", json.dumps(answer))

    def codex_calls(self, verb="app-server"):
        return [line.split("CODEX_HOME=")[1] for line in self.box.shim_calls() if line.startswith("codex " + verb)]

    def face(self, bar):
        """The bar item's gauges as words: claude 90% openai 89%."""
        return " ".join("%s %s" % (g["icon"], g["text"]) for g in bar["gauges"]) or bar["text"]

    def live_cache(self, home=".codex"):
        name = "codex-%s.json" % hashlib.sha1(str(self.box.home / home).encode()).hexdigest()[:12]
        return self.box.cache / name

    def claude_fixture(self):
        """A block of exactly 1,200,000 tokens (three messages, one in a subagent's log, one written as three growing
        lines), an older block of 5,000 the week counts too, a message 7.5 days old the week leaves out, and a log
        last changed 9 days ago that is never read."""
        n = self.now
        main = self.projects / "-Users-x-app" / "s1.jsonl"
        tool_noise = {"type": "user", "timestamp": iso(n - 80 * MIN), "message": {"role": "user", "content": [
            {"type": "tool_result", "content": json.dumps(claude_line(n, "msg_fake", read=9999999))}]}}
        self.write(main, [claude_line(n - 7.5 * DAY, "msg_old", read=1000000),
                          claude_line(n - 8 * HOUR, "msg_prev", inp=5, out=95, write=900, read=4000),
                          claude_line(n - 90 * MIN, "msg_a", inp=100, out=10, write=1000, read=200000),
                          tool_noise,
                          claude_line(n - 90 * MIN, "msg_a", inp=100, out=50, write=1000, read=200000),
                          claude_line(n - 90 * MIN, "msg_a", inp=100, out=50, write=1000, read=200000),
                          claude_line(n - 30 * MIN, "msg_b", read=698900)])
        self.write(self.projects / "-Users-x-app" / "s1" / "subagents" / "agent-1.jsonl",
                   [claude_line(n - 60 * MIN, "msg_sub", out=299950)])
        self.write(self.projects / "-Users-x-old" / "s0.jsonl", [claude_line(n - HOUR, "msg_stale", read=7777777)],
                   age=9 * DAY)
        return main


    def sven_like(self):
        """Claude Max 20x: session 10% used a third of the way in, week 90% used five days in (a status line 3 min old),
        Fable's week unused (Claude Code's cache, 20 min old, so the status line wins). Codex Plus, asked live: session 11% half way, week 13%
        three days in, two reset credits left and one spent."""
        n = self.now
        self.status("new", statusline(n - 3 * MIN, (10, n + 200 * MIN), (90, n + 2 * DAY)), age=3 * MIN)
        self.claude_cache(claude_json(n - 20 * MIN, (50, n + 200 * MIN), (50, n + 2 * DAY), (0, n + 2 * DAY)))
        self.codex_says(codex_answer(EMAIL, "plus", live_window(11, 300, n + 150 * MIN), live_window(13, 10080, n + 4 * DAY),
                                     credits=((n + 19 * DAY, "available"), (n + 27 * DAY, "available"),
                                              (n + 9 * DAY, "redeemed"))))

    def check_quiet(self, result):
        self.assertNotIn(SECRET_SAMPLE, result)
        return result

    def detail(self):
        r = self.run_ext("usage", home=self.box.home)
        self.check_quiet(r.stdout)
        d = r.detail
        self.assertIsInstance(d["markdown"], str)
        for row in d["metadata"]:
            self.assertIsInstance(row["label"], str)
            self.assertIsInstance(row["text"], str)
        for action in d["actions"]:
            self.assertActionShape(action)
        return d, {row["label"]: row["text"] for row in d["metadata"]}

    def hud(self):
        r = self.run_ext("summary", home=self.box.home)
        self.check_quiet(r.stdout)
        huds = r.of_type("hud")
        self.assertEqual(len(huds), 1, r.stdout)
        return huds[0]["text"]

    def test_popover_like_codexbar_then_the_bar_from_codex_itself(self):
        self.sven_like()
        n = self.now
        blocks = self.popover(home=self.box.home)
        self.check_quiet(json.dumps(blocks))
        claude = card(blocks, "Claude")
        self.assertEqual((claude["subtitle"], claude["aside"], claude["asideNote"]),
                         ("Updated %s" % hhmm(n - 3 * MIN), EMAIL, "Max 20x"))
        self.assertEqual(claude["blocks"], [
            {"type": "meter", "label": "Session", "percent": 90, "value": "90% left", "tone": "fine", "mark": 66.7,
             "markTone": "fine", "resetsAt": n + 200 * MIN, "note": "23% in reserve · Lasts until reset"},
            {"type": "meter", "label": "Weekly", "percent": 10, "value": "10% left", "tone": "alert", "mark": 28.6,
             "markTone": "alert", "resetsAt": n + 2 * DAY, "note": "19% in deficit · Runs out in 13h 21m"},
            {"type": "meter", "label": "Fable weekly", "percent": 100, "value": "100% left", "tone": "fine", "mark": 28.6,
             "markTone": "fine", "resetsAt": n + 2 * DAY, "note": "71% in reserve · Lasts until reset"}],
            "Claude never shows CodexBar's headroom hint")
        codex = card(blocks, "Codex")
        self.assertTrue(codex["subtitle"].startswith("Updated "), codex["subtitle"])
        self.assertEqual((codex["aside"], codex["asideNote"]), (EMAIL, "Plus"))
        self.assertEqual(codex["blocks"][:2], [
            {"type": "meter", "label": "Session", "percent": 89, "value": "89% left", "tone": "fine", "mark": 50.0,
             "markTone": "fine", "resetsAt": n + 150 * MIN, "note": "39% in reserve · Lasts until reset · 1.5× headroom"},
            {"type": "meter", "label": "Weekly", "percent": 87, "value": "87% left", "tone": "fine", "mark": 57.1,
             "markTone": "fine", "resetsAt": n + 4 * DAY, "note": "30% in reserve · Lasts until reset · 1.5× headroom"}])
        day = lambda t: time.strftime("%-d %b %H:%M", time.localtime(t))
        self.assertEqual(codex["blocks"][2:], [{"type": "markdown", "text": "**Limit reset credits:** 2 available · expire "
                                                "%s, %s" % (day(n + 19 * DAY), day(n + 27 * DAY))}],
                         "a zero credit balance is left out, as CodexBar does")
        rows = blocks[-2]["items"]
        self.assertEqual([r["title"] for r in rows], ["Claude status", "Codex status", "Claude usage dashboard",
                                                      "Codex usage dashboard", "Add Codex account…"])
        self.assertEqual(rows[-1]["actions"], [{"type": "run", "command": "add-account", "title": "Add Codex account…"}])
        self.assertEqual(blocks[-1]["text"], "Codex's numbers come from Codex itself. Pilot reads no sign-in tokens.")
        self.assertEqual(self.codex_calls(), [str(self.box.home / ".codex")])
        bar = self.bar(home=self.box.home)
        self.assertEqual((bar["text"], bar["tone"], bar["gauges"]),
                         ("", "alert", [{"icon": "claude", "text": "90%", "tone": "alert"}, {"icon": "openai", "text": "89%"}]),
                         "CodexBar's menu bar: each logo and its session left, in the tone of its most used window")
        self.assertEqual(bar["blocks"], blocks[:-1], "the bar line carries the popover's cards until it answers")
        self.assertEqual(len(self.codex_calls()), 1, "a fresh answer is not asked again")
        self.assertEqual(self.hud(), "Claude: Session 90% left, Weekly 10% left, Fable weekly 100% left · "
                                     "Codex: Session 89% left, Weekly 87% left")

    def test_claude_takes_claude_codes_read_unless_the_status_line_is_10_min_newer(self):
        """CodexBar 2026-10-11 08:47: Claude Code's cached read said week 50% used (CodexBar: 50% left) while the status
        line said 49, and the session reset 09:49:59 UTC where the status line said 09:50."""
        n = self.now
        self.status("s", statusline(n - MIN, (34, n + HOUR), (49, n + DAY)), age=MIN)
        self.claude_cache(claude_json(n - 4 * MIN, (34, n + HOUR - 1), (50, n + DAY - 1), (0, n + DAY)))
        claude = card(self.popover(home=self.box.home), "Claude")
        self.assertEqual([(m["label"], m["value"], m["resetsAt"]) for m in claude["blocks"]],
                         [("Session", "66% left", n + HOUR - 1), ("Weekly", "50% left", n + DAY - 1),
                          ("Fable weekly", "100% left", n + DAY)])
        self.assertEqual(claude["subtitle"], "Updated " + hhmm(n - 4 * MIN))
        self.claude_cache(claude_json(n - 12 * MIN, (34, n + HOUR - 1), (50, n + DAY - 1), (0, n + DAY)))
        claude = card(self.popover(home=self.box.home), "Claude")
        self.assertEqual([(m["label"], m["value"]) for m in claude["blocks"]],
                         [("Session", "66% left"), ("Weekly", "51% left"), ("Fable weekly", "100% left")],
                         "a status line over 10 minutes newer wins the session and the week")
        self.assertEqual([m["resetsAt"] for m in claude["blocks"]], [n + HOUR - 1, n + DAY - 1, n + DAY],
                         "with the cached read's exact reset times, as CodexBar shows them")
        self.claude_cache(claude_json(n - 4 * MIN, (30, n + HOUR), (40, n + DAY), (5, n + DAY), cached_for="someone-else"))
        claude = card(self.popover(home=self.box.home), "Claude")
        self.assertEqual([(m["label"], m["value"]) for m in claude["blocks"]],
                         [("Session", "66% left"), ("Weekly", "51% left")], "another account's cached read is not used")
        self.assertEqual(claude["subtitle"], "Updated " + hhmm(n - MIN))

    def test_the_newest_status_line_with_limits_wins(self):
        n = self.now
        self.status("old", statusline(n - DAY, (90, n + HOUR), (90, n + DAY)), age=DAY)
        self.status("mid", statusline(n - 10 * MIN, (12, n + HOUR), (34, n + DAY)), age=10 * MIN)
        self.status("newer-no-limits", statusline(n - 5 * MIN), age=5 * MIN)
        self.status("newest-half-written", '{"version":1,"payl', age=0)
        self.assertEqual(self.face(self.bar(home=self.box.home)), "claude 88%")

    def test_claude_unknown_without_ghostex_or_a_reply(self):
        for setup in (lambda: None, lambda: self.status("bare", statusline(self.now))):
            setup()
            claude = card(self.popover(home=self.box.home), "Claude")
            self.assertEqual(claude["blocks"], [{"type": "markdown", "text": "Unknown until your next Claude Code reply."}])
            self.assertEqual(self.face(self.bar(home=self.box.home)), "AI")
        self.assertTrue(self.hud().startswith("Claude unknown"))

    def test_a_window_that_reset_since_shows_all_left_with_what_it_was(self):
        n = self.now
        self.status("s", statusline(n - 6 * HOUR, (88, n - HOUR), (61, n + DAY)), age=6 * HOUR)
        claude = card(self.popover(home=self.box.home), "Claude")
        self.assertEqual(claude["blocks"][0], {"type": "meter", "label": "Session", "percent": 100, "value": "100% left",
                                               "note": "reset since your last Claude Code reply (was 88% used)"})
        self.assertEqual(claude["subtitle"], "Updated %s" % hhmm(n - 6 * HOUR)
                         if time.localtime(n - 6 * HOUR)[:3] == time.localtime(n)[:3]
                         else "Updated yesterday %s" % hhmm(n - 6 * HOUR))
        bar = self.bar(home=self.box.home)
        self.assertEqual((self.face(bar), bar["tone"]), ("claude 100%", "warn"))

    def test_pace_used_up_and_unused(self):
        n = self.now
        self.status("s", statusline(n, (100, n + HOUR), (0, n + DAY)))
        session, week = card(self.popover(home=self.box.home), "Claude")["blocks"]
        self.assertEqual((session["value"], session["tone"], session["percent"]), ("0% left", "alert", 0))
        self.assertNotIn("note", session, "no pace once used up, as CodexBar")
        self.assertEqual((week["mark"], week["note"]), (round(100 - 600 / 7.0, 1), "86% in reserve · Lasts until reset"),
                         "nothing used: all of the expected use is in reserve, as CodexBar says")

    def accounts_root(self):
        return self.box.root / "data" / "codex-accounts"

    def run_account(self, command, home=None, **kw):
        context = {"home": str(home)} if home else None
        r = self.run_ext(command, home=self.box.home, context=context, **kw)
        self.check_quiet(r.stdout)
        return r

    def wait_for(self, check, seconds=10):
        deadline = time.monotonic() + seconds
        while not check() and time.monotonic() < deadline:
            time.sleep(0.1)
        return check()

    def test_add_codex_account_runs_codexs_own_sign_in_in_a_folder_of_pilots(self):
        n = self.now
        self.codex_says(codex_answer(EMAIL, "plus", live_window(20, 300, n + HOUR), live_window(30, 10080, n + DAY)))
        r = self.run_account("add-account")
        self.assertEqual(r.of_type("alert")[0]["text"], "Sign in to Codex in your browser")
        account = self.accounts_root() / "1"
        self.assertTrue(self.wait_for(lambda: self.codex_calls("login")), self.box.shim_calls())
        self.assertEqual(self.codex_calls("login"), [str(account)], "Codex's own login, in Pilot's folder")
        self.assertTrue(self.wait_for(lambda: "https://" in self.page_codex()[1]["blocks"][1]["actions"][0].get("url", "")))
        waiting = self.page_codex()[1]
        self.assertEqual((waiting["subtitle"], waiting["aside"]), ("Signing in", "Pilot account"))
        self.assertEqual([a["title"] for a in waiting["blocks"][1]["actions"]], ["Open sign-in page", "Remove"])
        self.assertEqual(waiting["blocks"][1]["actions"][0]["url"], "https://auth.example.test/sign-in?x=1")
        self.assertNotIn(str(account), self.codex_calls(), "nothing asks Codex in a folder while it signs in")
        self.assertIn("Already waiting", self.run_account("add-account", home=account).of_type("hud")[0]["text"])
        self.codex_says(codex_answer(OTHER, "pro", live_window(26, 300, n + HOUR), live_window(4, 10080, n + DAY)),
                        home=account)
        (account / "release-login").write_text("")
        self.assertTrue(self.wait_for(lambda: (account / "auth.json").exists() and not list(self.box.cache.glob("login-*"))))
        codex = self.page_codex()
        self.assertEqual([(c["aside"], c["asideNote"], c["blocks"][0]["value"]) for c in codex],
                         [(EMAIL, "Plus", "80% left"), (OTHER, "Pro", "74% left")])
        self.assertEqual([a["title"] for a in codex[1]["blocks"][-1]["actions"]], ["Remove"])
        self.assertEqual(sorted(self.codex_calls()), sorted([str(self.box.home / ".codex"), str(account)]))
        self.assertNotIn(SECRET_SAMPLE, (account / "auth.json").read_text())
        self.assertEqual(len(cards(self.popover(home=self.box.home), "Codex")), 2)
        self.assertEqual(self.run_account("add-account").of_type("alert")[0]["text"], "Sign in to Codex in your browser")
        self.assertTrue((self.accounts_root() / "2").is_dir(), "a second add makes a second folder")
        (self.accounts_root() / "2" / "release-login").write_text("")
        self.assertTrue(self.wait_for(lambda: not list(self.box.cache.glob("login-*"))))

    def page_codex(self):
        return cards(self.page(home=self.box.home), "Codex")

    def test_remove_signs_out_with_codex_then_deletes_the_folder(self):
        account = self.accounts_root() / "1"
        self.write(account / "auth.json", '{"stand_in": "signed in"}')
        self.codex_says({"account": {"type": "chatgpt", "email": OTHER, "planType": "plus"}, "limits": None},
                        home=account)
        signed = cards(self.popover(home=self.box.home), "Codex")[1]
        self.assertEqual(signed["aside"], OTHER)
        self.assertIn("remove-account", json.dumps(signed["blocks"]), "an account without numbers offers Remove")
        r = self.run_account("remove-account", home=account)
        self.assertEqual(r.of_type("hud")[0]["text"], "Removed the Codex account from Pilot")
        self.assertEqual(self.codex_calls("logout"), [str(account)])
        self.assertFalse(account.exists())
        self.assertEqual(len(cards(self.popover(home=self.box.home), "Codex")), 1)
        outside = self.box.home / ".codex"
        self.assertIn("already gone", self.run_account("remove-account", home=outside).of_type("hud")[0]["text"])
        self.assertTrue(outside.exists(), "only Pilot's own folders are ever removed")

    def test_an_account_signed_out_offers_sign_in_and_one_held_by_another_run_is_not_asked(self):
        account = self.accounts_root() / "1"
        self.codex_says(codex_answer(EMAIL, "plus", live_window(20, 300, self.now + HOUR), None))
        account.mkdir(parents=True)
        quiet = cards(self.popover(home=self.box.home), "Codex")[1]
        self.assertEqual((quiet["subtitle"], quiet["blocks"][0]["text"]),
                         ("Not signed in", "Not signed in yet: sign in with the account to add."))
        self.assertEqual([a["title"] for a in quiet["blocks"][1]["actions"]], ["Sign in…", "Remove"])
        self.assertEqual(quiet["blocks"][1]["actions"][0]["context"], {"home": str(account)})
        self.write(account / "auth.json", '{"stand_in": "signed in"}')
        self.codex_says(codex_answer(EMAIL, "plus", live_window(20, 300, self.now + HOUR), None), home=account)
        with open(self.box.cache / ai_usage.lock_name(str(account)), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.popover(home=self.box.home)
            self.assertEqual(self.codex_calls(), [str(self.box.home / ".codex")],
                             "another run holds that folder: it is not asked")
            self.assertFalse(self.live_cache(account).exists(), "and no failure is saved for it")
        same = cards(self.popover(home=self.box.home), "Codex")
        self.assertEqual(same[1]["aside"], EMAIL)
        self.assertIn(ai_usage.SAME, json.dumps(same[1]["blocks"]))

    def test_status_pages_and_dashboards_under_the_cards(self):
        self.status_pages({"claude": {"indicator": "minor", "description": "Partially Degraded Service",
                                      "updated": self.now - 23 * MIN}}, age=MIN)
        rows = self.popover(home=self.box.home)[-2]["items"]
        self.assertEqual(rows[0], {"title": "Claude status", "subtitle": "Partially Degraded Service · updated 23 min ago",
                                   "badges": [{"text": "minor", "tone": "warn"}],
                                   "actions": [{"type": "open", "url": "https://status.claude.com/", "title": "Open status page"}]})
        self.assertEqual(rows[1]["subtitle"], "unknown")
        self.assertEqual([r["actions"][0]["url"] for r in rows[2:4]],
                         ["https://claude.ai/settings/usage", "https://chatgpt.com/codex/cloud/settings/analytics#usage"])
        self.assertFalse(self.wait_for(lambda: "--status" in " ".join(self.box.shim_calls()), 0.3))

    def test_status_read_from_each_status_page(self):
        pages = self.box.root / "pages"
        for key, body in (("claude", {"page": {"updated_at": "2026-10-11T06:24:21.024Z"},
                                      "status": {"indicator": "minor", "description": "Partially Degraded Service"}}),
                          ("codex", "not json")):
            self.write(pages / key / "api" / "v2" / "status.json", body if isinstance(body, str) else json.dumps(body))
        saved = dict(ai_usage.STATUS_PAGES)
        ai_usage.STATUS_PAGES.update((k, (pages / k).as_uri()) for k in saved)
        try:
            ai_usage.background_status(str(self.box.cache))
        finally:
            ai_usage.STATUS_PAGES.update(saved)
            signal.alarm(0)
        found = json.loads((self.box.cache / "status.json").read_text())
        self.assertEqual(found["claude"], {"indicator": "minor", "description": "Partially Degraded Service",
                                           "updated": 1791699861})
        self.assertNotIn("codex", found, "a page that fails is left out")

    def test_tones_amber_from_60_red_from_85_used(self):
        n = self.now
        self.status("s", statusline(n, (59.5, n + HOUR), (12, n + DAY)))
        self.codex_says(codex_answer(EMAIL, "pro", live_window(60, 300, n + HOUR), live_window(12, 10080, n + DAY)))
        self.popover(home=self.box.home)
        bar = self.bar(home=self.box.home)
        self.assertEqual((self.face(bar), bar["tone"]), ("claude 40% openai 40%", "warn"))
        self.assertEqual([g.get("tone") for g in bar["gauges"]], [None, "warn"])
        tones = [m["tone"] for c in bar["blocks"][:2] for m in c["blocks"] if m["type"] == "meter"]
        self.assertEqual(tones, ["fine", "fine", "warn", "fine"])

    def test_codex_from_its_logs_while_it_does_not_answer(self):
        n = self.now
        self.codex_log("older", [codex_event(n - 2 * DAY, window(90.0, 300, n + HOUR), window(95.0, 10080, n + DAY))],
                       age=2 * DAY)
        self.codex_log("newest", [
            codex_event(n - 20 * MIN, window(40.0, 300, n + HOUR), window(10.0, 10080, n + DAY)),
            codex_event(n - 10 * MIN, window(52.0, 300, resets_in=2 * HOUR + 10 * MIN + 30), window(18.0, 10080, n + DAY),
                        credits={"has_credits": True, "unlimited": True, "balance": None}),
            codex_event(n - 5 * MIN, None, None, limit_id="premium")])
        codex = card(self.popover(home=self.box.home), "Codex")
        self.assertEqual([(m.get("label"), m.get("value")) for m in codex["blocks"]][:2],
                         [("Session", "48% left"), ("Weekly", "82% left")])
        self.assertEqual(codex["blocks"][0]["resetsAt"], int(n - 10 * MIN) + 2 * HOUR + 10 * MIN + 30)
        self.assertEqual(codex["blocks"][2]["items"], [{"label": "Credits", "value": "unlimited"}])
        self.assertEqual((codex["subtitle"], codex["aside"]), ("Last run %s" % hhmm(n - 10 * MIN), "~/.codex"))
        self.assertEqual(self.codex_calls(), [str(self.box.home / ".codex")], "asked, and it failed")

    def test_a_newer_log_beats_an_older_answer(self):
        n = self.now
        self.write(self.live_cache(), json.dumps({"at": n - HOUR, "account": {"email": EMAIL, "plan": "Plus"},
                                                  "limits": {"primary": live_window(5, 300, n + HOUR)}}))
        self.codex_log("now", [codex_event(n - MIN, window(70.0, 300, n + HOUR), window(10.0, 10080, n + DAY))])
        self.assertEqual(self.face(self.bar(home=self.box.home)), "openai 30%")

    def test_codex_signed_out_absent_or_quiet_for_8_days(self):
        self.codex_says({"account": None, "limits": None})
        codex = card(self.popover(home=self.box.home), "Codex")
        self.assertEqual((codex["subtitle"], codex["aside"], codex["blocks"][0]["text"]),
                         ("Signed out", "~/.codex", "Codex is signed out here. Run `codex login` in a terminal."))
        (self.box.home / ".codex" / "fake-codex.json").unlink()
        self.live_cache().unlink()
        codex = card(self.popover(home=self.box.home), "Codex")
        self.assertEqual(codex["blocks"][0]["text"], "Codex didn't answer and has no logs here. Run Codex once.")
        self.assertEqual(self.face(self.bar(home=self.box.home)), "AI")
        self.codex_log("ancient", [codex_event(self.now - 9 * DAY, window(80.0, 300, self.now), None)], age=9 * DAY)
        codex = card(self.popover(home=self.box.home), "Codex")
        self.assertEqual(codex["subtitle"], "No run in 8 days")

    def test_several_codex_accounts_only_the_listed_ones_asked(self):
        n = self.now
        self.codex_says(codex_answer(EMAIL, "plus", live_window(20, 300, n + HOUR), live_window(30, 10080, n + DAY)))
        self.codex_says(codex_answer(OTHER, "team", live_window(70, 300, n + HOUR), None), home=".codex-work")
        self.write(self.box.home / ".codex-pro" / "auth.json", json.dumps({"bot": SECRET_SAMPLE}))
        self.codex_log("pro", [codex_event(n - HOUR, window(15.0, 300, n + HOUR), None)], home=".codex-pro")
        self.write(self.box.home / ".codex-dead" / "auth.json", json.dumps({"bot": SECRET_SAMPLE}))
        (self.box.home / ".codexbar").mkdir()
        prefs = {"codexHomes": "~/.codex-work, ~/.codex"}
        blocks = self.popover(home=self.box.home, preferences=prefs)
        self.check_quiet(json.dumps(blocks))
        self.assertEqual([(c["aside"], c.get("asideNote") or c["subtitle"]) for c in cards(blocks, "Codex")],
                         [(OTHER, "Team"), (EMAIL, "Plus"), ("~/.codex-dead", "Unavailable"),
                          ("~/.codex-pro", "Last run %s" % hhmm(n - HOUR))])
        self.assertEqual(sorted(self.codex_calls()), [str(self.box.home / ".codex"), str(self.box.home / ".codex-work")],
                         "a folder not in the setting is never asked: asking makes Codex use its login")
        self.assertEqual(self.face(self.bar(home=self.box.home, preferences=prefs)), "openai 30%", "the first listed account")
        self.assertIn("Codex %s: Session 30%% left · Codex %s: Session 80%% left" % (OTHER, EMAIL),
                      self.run_ext("summary", home=self.box.home, preferences=prefs).of_type("hud")[0]["text"])

    def test_the_bar_never_waits_for_codex_and_refreshes_behind(self):
        n = self.now
        answer = codex_answer(EMAIL, "plus", live_window(20, 300, n + HOUR), live_window(30, 10080, n + DAY))
        answer["sleep"] = 1.0
        self.codex_says(answer)
        started = time.monotonic()
        bar = self.bar(home=self.box.home)
        self.assertLess(time.monotonic() - started, 1.0, "the bar answered without waiting")
        self.assertEqual(self.face(bar), "AI")
        self.wait_for(lambda: self.live_cache().exists())
        self.assertEqual(self.face(self.bar(home=self.box.home)), "openai 80%", "the refresh behind it saved Codex's answer")
        self.assertEqual(len(self.codex_calls()), 1)

    def test_a_failed_ask_waits_like_an_answer(self):
        self.popover(home=self.box.home)
        self.bar(home=self.box.home)
        self.popover(home=self.box.home)
        self.assertEqual(len(self.codex_calls()), 1, "the failure was saved, so neither asked again within 30 s")
        self.assertTrue(json.loads(self.live_cache().read_text())["failed"])

    def test_a_slow_codex_leaves_the_popover_on_time(self):
        answer = codex_answer(EMAIL, "plus", live_window(20, 300, self.now + HOUR), None)
        answer["sleep"] = 8
        self.codex_says(answer)
        self.codex_log("log", [codex_event(self.now - MIN, window(25.0, 300, self.now + HOUR), None)])
        codex = card(self.popover(home=self.box.home), "Codex")  # the harness holds it to 5 s
        self.assertEqual(codex["blocks"][0]["value"], "75% left")

    def test_always_left_and_what_the_bar_shows(self):
        self.sven_like()
        used = {"show": "used"}  # 3.1.0's setting, still saved on Sven's Mac: left now, always
        session = card(self.popover(home=self.box.home, preferences=used), "Claude")["blocks"][0]
        self.assertEqual((session["percent"], session["value"], session["mark"]), (90, "90% left", 66.7),
                         "the bar drains as it is used and the mark sits where the share left should be by now")
        self.assertEqual(self.face(self.bar(home=self.box.home, preferences=used)), "claude 90% openai 89%")
        self.assertEqual(self.face(self.bar(home=self.box.home, preferences={"barWindow": "tightest"})),
                         "claude 10% openai 87%")
        bar = self.bar(home=self.box.home, preferences={"barProviders": "codex"})
        self.assertEqual((self.face(bar), bar["tone"]), ("openai 89%", None), "the tone follows only the providers shown")
        self.assertEqual(self.face(self.bar(home=self.box.home, preferences={"barProviders": "claude"})), "claude 90%")

    def test_settings_hide_pace_other_codex_folders_and_gemini(self):
        self.sven_like()
        self.write(self.box.home / ".codex-pro" / "auth.json", json.dumps({"bot": SECRET_SAMPLE}))
        prefs = {"pace": False, "otherCodex": False, "gemini": False}
        blocks = self.popover(home=self.box.home, preferences=prefs)
        self.assertEqual([b.get("title") for b in blocks], ["Claude", "Codex", None, None])
        meters = [m for c in blocks[:2] for m in c["blocks"] if m["type"] == "meter"]
        self.assertFalse([m for m in meters if "mark" in m or "note" in m], meters)
        self.assertEqual(len(cards(self.popover(home=self.box.home), "Codex")), 2, "on by default")
        d = self.run_ext("usage", home=self.box.home, preferences=prefs).detail["markdown"]
        self.assertNotIn("## Gemini", d)
        self.assertNotIn("in reserve", d)

    def test_gemini_not_set_up_until_its_cli_is_on_path(self):
        gemini = card(self.popover(home=self.box.home), "Gemini")
        self.assertEqual(gemini["subtitle"], "Not set up")
        self.assertEqual(gemini["blocks"][1]["actions"],
                         [{"type": "open", "url": "pilot://open?page=ai", "title": "Set up in Pilot › AI…"}])
        self.box.shim("gemini")
        gemini = card(self.popover(home=self.box.home), "Gemini")
        self.assertEqual(gemini["subtitle"], "Limit unknown")
        self.assertNotIn("gemini", " ".join(self.box.shim_calls()), "the gemini CLI is looked for, never run")

    def test_nothing_known_still_shows_an_item_to_click(self):
        bar = self.bar(home=self.box.home)
        self.assertEqual((bar["text"], bar["gauges"], bar["tone"]), ("AI", [], None))
        self.assertEqual([c.get("title") for c in bar["blocks"]], ["Claude", "Codex", "Gemini", None])

    def test_page_cards_then_the_token_footnote(self):
        self.sven_like()
        self.claude_fixture()
        blocks = self.page(home=self.box.home)
        self.assertEqual([b.get("title") for b in blocks[:3]], ["Claude", "Codex", "Gemini"])
        self.assertEqual(blocks[3]["type"], "list")
        self.assertEqual(blocks[4]["text"], "Claude Code tokens: 1.2M in the last 7 days, 1.2M in this 5-hour block. "
                                            "They come from Claude Code's own logs and aren't your limit. Codex's numbers "
                                            "come from Codex itself. Pilot reads no sign-in tokens.")
        self.assertEqual(blocks[5]["actions"][0]["text"], "Claude: Session 90% left, Weekly 10% left, Fable weekly 100% "
                                                          "left · Codex: Session 89% left, Weekly 87% left")
        self.check_quiet(json.dumps(blocks))

    def test_detail_names_every_provider(self):
        self.sven_like()
        self.claude_fixture()
        self.popover(home=self.box.home)  # the detail reads Codex's saved answer
        d, facts = self.detail()
        self.assertTrue(facts["Claude session"].startswith("90% left · resets "), facts["Claude session"])
        self.assertIn("· 23% in reserve · Lasts until reset", facts["Claude session"])
        self.assertTrue(facts["Codex weekly"].startswith("87% left · resets "), facts["Codex weekly"])
        self.assertIn("▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░` 90% left", d["markdown"])
        self.assertIn("## Codex · %s" % EMAIL, d["markdown"])
        self.assertIn("- **Limit reset credits:** 2 available", d["markdown"])
        self.assertIn("## Gemini\n**Not set up.** The gemini CLI", d["markdown"])
        self.assertIn("Claude Code tokens: 1.2M in the last 7 days", d["markdown"])

    def test_no_block_running_after_a_quiet_5_hours(self):
        self.write(self.projects / "p" / "s.jsonl", [claude_line(self.now - 6 * HOUR, "msg_1", read=4000)])
        self.assertIn("4k in the last 7 days, none in this 5-hour block", self.page(home=self.box.home)[4]["text"])

    def test_no_claude_logs_no_token_counts(self):
        self.assertIn("No Claude Code logs on this Mac", self.page(home=self.box.home)[4]["text"])

    def test_a_rerun_reads_only_the_new_bytes(self):
        main = self.claude_fixture()
        self.assertIn("1.2M in this 5-hour block", self.page(home=self.box.home)[4]["text"])
        cache = json.loads((self.box.cache / "claude-usage.json").read_text())["files"]
        self.assertEqual(cache[str(main)]["offset"], main.stat().st_size)
        self.assertNotIn(str(self.projects / "-Users-x-old" / "s0.jsonl"), cache)
        # Same length, different numbers: a re-read from the start would change the total; reading on does not.
        text = main.read_text()
        main.write_text(text.replace('"cache_read_input_tokens":698900', '"cache_read_input_tokens":111111'))
        self.assertEqual(len(main.read_text()), len(text))
        self.write(main, [claude_line(self.now - 5 * MIN, "msg_c", out=300000)], append=True)
        self.assertIn("1.5M in this 5-hour block", self.page(home=self.box.home)[4]["text"])

    def test_while_another_refresh_runs_it_answers_from_the_cache_at_once(self):
        self.claude_fixture()
        self.page(home=self.box.home)
        with open(self.box.cache / "refresh.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            text = self.page(home=self.box.home)[4]["text"]
            self.assertTrue(text.startswith("Still counting"), text)
            self.assertIn("1.2M in this 5-hour block", text)

    def test_the_background_refresh_fills_the_cache(self):
        self.claude_fixture()
        program = EXTENSIONS / "ai-usage" / "main.py"
        python = os.environ.get("PILOT_EXT_PYTHON") or "python3"
        subprocess.run([python, str(program), "--refresh", str(self.box.cache)], timeout=30, check=True,
                       env={"HOME": str(self.box.home), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"})
        files = json.loads((self.box.cache / "claude-usage.json").read_text())["files"]
        self.assertEqual(len(files), 2)
        self.assertTrue(all(f["size"] is not None for f in files.values()))

    def test_an_unknown_command_is_an_error(self):
        program = EXTENSIONS / "ai-usage" / "main.py"
        out = subprocess.run([str(program)], input=b'{"type":"run","command":"nope"}\n', capture_output=True,
                             timeout=10, env={"HOME": str(self.box.home), "PATH": "/usr/bin:/bin"}).stdout
        self.assertEqual(json.loads(out), {"type": "error", "text": "has no command nope"})


class PaceTests(unittest.TestCase):
    """CodexBar's pace at fixed times (UsagePace.weekly and UsagePaceText in CodexBar 0.74): the mark sits on the
    draining bar where the share left should be by now, green in reserve and red in deficit, and only when the words
    say reserve or deficit."""
    NOW = 1800000000

    def pace(self, used, minutes, left_minutes, headroom=False):
        w = ai_usage.window("w", used, self.NOW + left_minutes * MIN, self.NOW, minutes)
        return ai_usage.pace(w, self.NOW, headroom)

    def test_codexbars_session_in_deficit(self):
        """CodexBar's 2026-10-11 menu: a session 46% used 50 min in (resets in 4 h 10 min) said "29% in deficit ·
        Projected empty in 1h" with its tick at 83% of a bar 54% full."""
        self.assertEqual(self.pace(46, 300, 250), (83.3, "alert", "29% in deficit · Projected empty in 59m"))

    def test_codexbars_week_of_2026_10_11(self):
        """CodexBar 08:47: Weekly 50% left, resets in 4 d 11 h 13 min: "14% in deficit · Runs out in 2d 12h"."""
        self.assertEqual(self.pace(50, 10080, 4 * 1440 + 11 * 60 + 13), (63.8, "alert", "14% in deficit · Runs out in 2d 12h"))
        self.assertEqual(self.pace(0, 10080, 4 * 1440 + 11 * 60 + 13), (63.8, "fine", "36% in reserve · Lasts until reset"),
                         "Fable weekly: 36% in reserve")

    def test_a_week_in_deficit_runs_out(self):
        self.assertEqual(self.pace(61, 10080, 3.5 * 1440), (50.0, "alert", "11% in deficit · Runs out in 2d 5h"))

    def test_headroom_only_for_codex_past_15_points(self):
        self.assertEqual(self.pace(10, 300, 200), (66.7, "fine", "23% in reserve · Lasts until reset"))
        self.assertEqual(self.pace(10, 300, 200, headroom=True),
                         (66.7, "fine", "23% in reserve · Lasts until reset · 1.5× headroom"))
        self.assertEqual(self.pace(37, 300, 150, headroom=True), (50.0, "fine", "13% in reserve · Lasts until reset"))

    def test_unused_is_all_reserve(self):
        self.assertEqual(self.pace(0, 10080, 3.5 * 1440, headroom=True), (50.0, "fine", "50% in reserve · Lasts until reset"))

    def test_on_pace_within_2_points_has_no_mark(self):
        self.assertEqual(self.pace(17, 300, 250), (None, None, "On pace · Projected empty in 4h 5m"))
        self.assertEqual(self.pace(48, 10080, 3.5 * 1440), (None, None, "On pace · Lasts until reset"))
        self.assertEqual(self.pace(47.9, 10080, 3.5 * 1440)[0], 50.0, "past 2 points the mark shows")

    def test_no_pace_under_3_percent_outside_the_window_or_used_up(self):
        for args in ((10, 300, 295), (10, 300, 400), (10, 300, 0), (100, 300, 150)):
            self.assertEqual(self.pace(*args), (None, None, None), args)

    def test_eta_and_share_words_as_codexbar_writes_them(self):
        self.assertEqual([ai_usage.eta(s) for s in (1, 59 * MIN + 1, 2 * HOUR, 2 * DAY + 5 * MIN, 3 * DAY)],
                         ["1m", "1h", "2h", "2d 5m", "3d"])
        self.assertEqual([ai_usage.percent_text(x) for x in (0, 0.4, 66.5, 67.5, 100)], ["0%", "<1%", "66%", "68%", "100%"])
