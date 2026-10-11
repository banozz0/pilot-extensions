import fcntl
import hashlib
import importlib.util
import json
import os
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

# A stand-in for `codex app-server`: JSON-RPC lines in, its answer from $CODEX_HOME/fake-codex.json (test-only), each
# call logged with its CODEX_HOME; no such file is a codex that fails at once.
FAKE_CODEX = r'''
import json, os, sys, time
home = os.environ.get("CODEX_HOME", "")
with open(sys.argv[1], "a") as log:
    log.write("codex %s CODEX_HOME=%s\n" % (" ".join(sys.argv[2:]), home))
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

    def codex_calls(self):
        return [line.split("CODEX_HOME=")[1] for line in self.box.shim_calls() if line.startswith("codex app-server")]

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
        Fable's week unused (Claude Code's cache, 10 min old). Codex Plus, asked live: session 11% half way, week 13%
        three days in, two reset credits left and one spent."""
        n = self.now
        self.status("new", statusline(n - 3 * MIN, (10, n + 200 * MIN), (90, n + 2 * DAY)), age=3 * MIN)
        self.claude_cache(claude_json(n - 10 * MIN, (50, n + 200 * MIN), (50, n + 2 * DAY), (0, n + 2 * DAY)))
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
        self.assertEqual(claude["subtitle"], "%s · Max 20x · read %s" % (EMAIL, hhmm(n - 3 * MIN)))
        self.assertEqual(claude["blocks"], [
            {"type": "meter", "label": "Session", "percent": 90, "value": "90% left", "tone": "fine", "mark": 66.7,
             "resetsAt": n + 200 * MIN, "note": "23% in reserve · Lasts until reset · 4.5x headroom"},
            {"type": "meter", "label": "Weekly", "percent": 10, "value": "10% left", "tone": "alert", "mark": 28.6,
             "resetsAt": n + 2 * DAY, "note": "19% in deficit · Runs out in 13 h 20 min"},
            {"type": "meter", "label": "Fable weekly", "percent": 100, "value": "100% left", "tone": "fine", "mark": 28.6,
             "resetsAt": n + 2 * DAY, "note": "71% in reserve · Lasts until reset"}])
        codex = card(blocks, "Codex")
        self.assertTrue(codex["subtitle"].startswith("%s · Plus · updated " % EMAIL), codex["subtitle"])
        self.assertEqual(codex["blocks"][:2], [
            {"type": "meter", "label": "Session", "percent": 89, "value": "89% left", "tone": "fine", "mark": 50.0,
             "resetsAt": n + 150 * MIN, "note": "39% in reserve · Lasts until reset · 8.1x headroom"},
            {"type": "meter", "label": "Weekly", "percent": 87, "value": "87% left", "tone": "fine", "mark": 57.1,
             "resetsAt": n + 4 * DAY, "note": "30% in reserve · Lasts until reset · 5.0x headroom"}])
        day = lambda t: time.strftime("%-d %b %H:%M", time.localtime(t))
        self.assertEqual(codex["blocks"][2:], [{"type": "markdown", "text": "**Limit reset credits:** 2 available · expire "
                                                "%s, %s" % (day(n + 19 * DAY), day(n + 27 * DAY))}],
                         "a zero credit balance is left out, as CodexBar does")
        self.assertEqual(blocks[-1]["text"], "Codex's numbers come from Codex itself. Pilot reads no sign-in tokens.")
        self.assertEqual(self.codex_calls(), [str(self.box.home / ".codex")])
        bar = self.bar(home=self.box.home)
        self.assertEqual((bar["text"], bar["tone"], bar.get("gauges")), ("✳90% ⊛89%", "alert", None))
        self.assertEqual(bar["blocks"], blocks[:-1], "the bar line carries the popover's cards until it answers")
        self.assertEqual(len(self.codex_calls()), 1, "a fresh answer is not asked again")
        self.assertEqual(self.hud(), "Claude: Session 90% left, Weekly 10% left, Fable weekly 100% left · "
                                     "Codex: Session 89% left, Weekly 87% left")

    def test_claude_takes_the_newer_read_and_the_model_weeks_from_claude_code(self):
        n = self.now
        self.status("s", statusline(n - 20 * MIN, (10, n + HOUR), (20, n + DAY)), age=20 * MIN)
        self.claude_cache(claude_json(n - 5 * MIN, (30, n + HOUR), (40, n + DAY), (5, n + DAY)))
        claude = card(self.popover(home=self.box.home), "Claude")
        self.assertEqual([(m["label"], m["value"]) for m in claude["blocks"]],
                         [("Session", "70% left"), ("Weekly", "60% left"), ("Fable weekly", "95% left")])
        self.assertTrue(claude["subtitle"].endswith("read " + hhmm(n - 5 * MIN)))
        self.claude_cache(claude_json(n - 5 * MIN, (30, n + HOUR), (40, n + DAY), (5, n + DAY), cached_for="someone-else"))
        claude = card(self.popover(home=self.box.home), "Claude")
        self.assertEqual([(m["label"], m["value"]) for m in claude["blocks"]],
                         [("Session", "90% left"), ("Weekly", "80% left")], "another account's cached read is not used")
        self.assertEqual(claude["subtitle"], "%s · Max 20x · read %s" % (EMAIL, hhmm(n - 20 * MIN)))

    def test_the_newest_status_line_with_limits_wins(self):
        n = self.now
        self.status("old", statusline(n - DAY, (90, n + HOUR), (90, n + DAY)), age=DAY)
        self.status("mid", statusline(n - 10 * MIN, (12, n + HOUR), (34, n + DAY)), age=10 * MIN)
        self.status("newer-no-limits", statusline(n - 5 * MIN), age=5 * MIN)
        self.status("newest-half-written", '{"version":1,"payl', age=0)
        self.assertEqual(self.bar(home=self.box.home)["text"], "✳88%")

    def test_claude_unknown_without_ghostex_or_a_reply(self):
        for setup in (lambda: None, lambda: self.status("bare", statusline(self.now))):
            setup()
            claude = card(self.popover(home=self.box.home), "Claude")
            self.assertEqual(claude["blocks"], [{"type": "markdown", "text": "Unknown until your next Claude Code reply."}])
            self.assertNotIn("✳", self.bar(home=self.box.home)["text"])
        self.assertTrue(self.hud().startswith("Claude unknown"))

    def test_a_window_that_reset_since_shows_all_left_with_what_it_was(self):
        n = self.now
        self.status("s", statusline(n - 6 * HOUR, (88, n - HOUR), (61, n + DAY)), age=6 * HOUR)
        claude = card(self.popover(home=self.box.home), "Claude")
        self.assertEqual(claude["blocks"][0], {"type": "meter", "label": "Session", "percent": 100, "value": "100% left",
                                               "note": "reset since your last Claude Code reply (was 88% used)"})
        self.assertEqual(claude["subtitle"], "read %s" % hhmm(n - 6 * HOUR)
                         if time.localtime(n - 6 * HOUR)[:3] == time.localtime(n)[:3]
                         else "read yesterday %s" % hhmm(n - 6 * HOUR))
        bar = self.bar(home=self.box.home)
        self.assertEqual((bar["text"], bar["tone"]), ("✳100%", "warn"))

    def test_pace_used_up_and_unused(self):
        n = self.now
        self.status("s", statusline(n, (100, n + HOUR), (0, n + DAY)))
        session, week = card(self.popover(home=self.box.home), "Claude")["blocks"]
        self.assertEqual((session["value"], session["tone"], session["note"]), ("0% left", "alert", "Used up until the reset"))
        self.assertEqual((week["mark"], week["note"]), (round(100 - 600 / 7.0, 1), "86% in reserve · Lasts until reset"),
                         "nothing used: all of the expected use is in reserve, as CodexBar says")

    def codexbar_accounts(self, accounts, version=3):
        """CodexBar's managed-codex-accounts.json (shape read live 2026-10-11, v3) and each account's folder, holding a
        login Pilot must never read."""
        root = self.box.home / "Library" / "Application Support" / "CodexBar"
        rows = []
        for uuid, email in accounts:
            folder = root / "managed-codex-homes" / uuid
            if email is not None:
                self.write(folder / "auth.json", json.dumps({"bot": SECRET_SAMPLE}))
            rows.append({"id": uuid, "email": email or "gone@example.com", "providerAccountID": "acc-" + uuid,
                         "workspaceLabel": "Personal", "workspaceAccountID": None, "authFingerprint": SECRET_SAMPLE,
                         "managedHomePath": str(folder), "createdAt": 1.0, "updatedAt": 1.0, "lastAuthenticatedAt": 1.0})
        self.write(root / "managed-codex-accounts.json", json.dumps({"version": version, "accounts": rows}))
        return "Library/Application Support/CodexBar/managed-codex-homes/"

    def test_codexbar_accounts_asked_like_codexbar_each_shown_once(self):
        n = self.now
        managed = self.codexbar_accounts([("A-1", OTHER), ("B-2", EMAIL.upper()), ("C-3", "quiet@example.com"),
                                          ("D-4", None)])
        self.codex_says(codex_answer(EMAIL, "plus", live_window(20, 300, n + HOUR), live_window(30, 10080, n + DAY)))
        self.codex_says(codex_answer(OTHER, "plus", live_window(26, 300, n + HOUR), live_window(4, 10080, n + DAY)),
                        home=managed + "A-1")
        self.codex_says(codex_answer(EMAIL, "plus", live_window(20, 300, n + HOUR), live_window(30, 10080, n + DAY)),
                        home=managed + "B-2")
        blocks = self.popover(home=self.box.home)
        self.check_quiet(json.dumps(blocks))
        self.assertEqual([(c["subtitle"].split(" · ")[:2], c["blocks"][0].get("value")) for c in cards(blocks, "Codex")],
                         [([EMAIL, "Plus"], "80% left"), ([OTHER, "Plus"], "74% left"),
                          (["CodexBar", "quiet@example.com"], None)],
                         "CodexBar's copy of the account above is shown once; a folder that is gone is skipped")
        quiet = cards(blocks, "Codex")[2]
        self.assertEqual(quiet["subtitle"], "CodexBar · quiet@example.com · unavailable")
        self.assertIn("Reauthenticate it in CodexBar", quiet["blocks"][0]["text"])
        home = lambda rel: str(self.box.home / rel)
        self.assertEqual(sorted(self.codex_calls()), sorted([home(".codex"), home(managed + "A-1"), home(managed + "B-2"),
                                                             home(managed + "C-3")]))
        for rel in (".codex", managed + "A-1", managed + "B-2", managed + "C-3"):  # every answer a minute old
            cached = json.loads(self.live_cache(rel).read_text())
            cached["at"] -= MIN
            self.live_cache(rel).write_text(json.dumps(cached))
        self.popover(home=self.box.home)
        self.assertEqual(sorted(self.codex_calls()[4:]), sorted([home(".codex"), home(managed + "A-1"),
                                                                 home(managed + "C-3")]),
                         "the account ~/.codex answered for is not asked again through CodexBar's folder")
        self.assertEqual(len(cards(self.popover(home=self.box.home, preferences={"codexBar": False}), "Codex")), 1)

    def test_codexbar_list_unreadable_or_of_another_version_is_left_alone(self):
        self.codex_says(codex_answer(EMAIL, "plus", live_window(20, 300, self.now + HOUR), None))
        for version in (4, "3"):
            self.codexbar_accounts([("A-1", OTHER)], version=version)
            self.assertEqual(len(cards(self.popover(home=self.box.home), "Codex")), 1, version)
        self.write(self.box.home / "Library" / "Application Support" / "CodexBar" / "managed-codex-accounts.json", "{")
        self.assertEqual(len(cards(self.popover(home=self.box.home), "Codex")), 1)

    def test_tones_amber_from_60_red_from_85_used(self):
        n = self.now
        self.status("s", statusline(n, (59.5, n + HOUR), (12, n + DAY)))
        self.codex_says(codex_answer(EMAIL, "pro", live_window(60, 300, n + HOUR), live_window(12, 10080, n + DAY)))
        self.popover(home=self.box.home)
        bar = self.bar(home=self.box.home)
        self.assertEqual((bar["text"], bar["tone"]), ("✳40% ⊛40%", "warn"))
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
        self.assertEqual(codex["subtitle"], "~/.codex · last run %s" % hhmm(n - 10 * MIN))
        self.assertEqual(self.codex_calls(), [str(self.box.home / ".codex")], "asked, and it failed")

    def test_a_newer_log_beats_an_older_answer(self):
        n = self.now
        self.write(self.live_cache(), json.dumps({"at": n - HOUR, "account": {"email": EMAIL, "plan": "Plus"},
                                                  "limits": {"primary": live_window(5, 300, n + HOUR)}}))
        self.codex_log("now", [codex_event(n - MIN, window(70.0, 300, n + HOUR), window(10.0, 10080, n + DAY))])
        self.assertEqual(self.bar(home=self.box.home)["text"], "⊛30%")

    def test_codex_signed_out_absent_or_quiet_for_8_days(self):
        self.codex_says({"account": None, "limits": None})
        codex = card(self.popover(home=self.box.home), "Codex")
        self.assertEqual((codex["subtitle"], codex["blocks"][0]["text"]),
                         ("~/.codex · signed out", "Codex is signed out here. Run `codex login` in a terminal."))
        (self.box.home / ".codex" / "fake-codex.json").unlink()
        self.live_cache().unlink()
        codex = card(self.popover(home=self.box.home), "Codex")
        self.assertEqual(codex["blocks"][0]["text"], "Codex didn't answer and has no logs here. Run Codex once.")
        self.assertNotIn("⊛", self.bar(home=self.box.home)["text"])
        self.codex_log("ancient", [codex_event(self.now - 9 * DAY, window(80.0, 300, self.now), None)], age=9 * DAY)
        codex = card(self.popover(home=self.box.home), "Codex")
        self.assertEqual(codex["subtitle"], "~/.codex · no run in 8 days")

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
        self.assertEqual([c["subtitle"].split(" · ")[:2] for c in cards(blocks, "Codex")],
                         [[OTHER, "Team"], [EMAIL, "Plus"], ["~/.codex-dead", "unavailable"], ["~/.codex-pro", "last run %s" % hhmm(n - HOUR)]])
        self.assertEqual(sorted(self.codex_calls()), [str(self.box.home / ".codex"), str(self.box.home / ".codex-work")],
                         "a folder not in the setting is never asked: asking makes Codex use its login")
        self.assertEqual(self.bar(home=self.box.home, preferences=prefs)["text"], "⊛30%", "the first listed account")
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
        self.assertEqual(bar["text"], "AI")
        deadline = time.monotonic() + 10
        while not self.live_cache().exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.bar(home=self.box.home)["text"], "⊛80%", "the refresh behind it saved Codex's answer")
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

    def test_settings_used_instead_of_left_and_what_the_bar_shows(self):
        self.sven_like()
        n = self.now
        used = {"show": "used"}
        session = card(self.popover(home=self.box.home, preferences=used), "Claude")["blocks"][0]
        self.assertEqual((session["percent"], session["value"], session["mark"]), (10, "10% used", 33.3),
                         "the bar fills with what is used and the mark sits where use should be by now")
        self.assertEqual(self.bar(home=self.box.home, preferences=used)["text"], "✳10% ⊛11%")
        self.assertTrue(self.run_ext("summary", home=self.box.home, preferences=used).of_type("hud")[0]["text"]
                        .startswith("Claude: Session 10% used, Weekly 90% used"))
        self.assertEqual(self.bar(home=self.box.home, preferences={"barWindow": "tightest"})["text"], "✳10% ⊛87%")
        bar = self.bar(home=self.box.home, preferences={"barProviders": "codex"})
        self.assertEqual((bar["text"], bar["tone"]), ("⊛89%", None), "the tone follows only the providers shown")
        self.assertEqual(self.bar(home=self.box.home, preferences={"barProviders": "claude"})["text"], "✳90%")

    def test_settings_hide_pace_other_codex_folders_and_gemini(self):
        self.sven_like()
        self.write(self.box.home / ".codex-pro" / "auth.json", json.dumps({"bot": SECRET_SAMPLE}))
        prefs = {"pace": False, "otherCodex": False, "gemini": False}
        blocks = self.popover(home=self.box.home, preferences=prefs)
        self.assertEqual([b.get("title") for b in blocks], ["Claude", "Codex", None])
        meters = [m for c in blocks[:2] for m in c["blocks"] if m["type"] == "meter"]
        self.assertFalse([m for m in meters if "mark" in m or "note" in m], meters)
        self.assertEqual(len(cards(self.popover(home=self.box.home), "Codex")), 2, "on by default")
        d = self.run_ext("usage", home=self.box.home, preferences=prefs).detail["markdown"]
        self.assertNotIn("## Gemini", d)
        self.assertNotIn("in reserve", d)

    def test_gemini_not_set_up_until_its_cli_is_on_path(self):
        gemini = card(self.popover(home=self.box.home), "Gemini")
        self.assertEqual(gemini["subtitle"], "not set up")
        self.assertEqual(gemini["blocks"][1]["actions"],
                         [{"type": "open", "url": "pilot://open?page=ai", "title": "Set up in Pilot › AI…"}])
        self.box.shim("gemini")
        gemini = card(self.popover(home=self.box.home), "Gemini")
        self.assertEqual(gemini["subtitle"], "limit unknown")
        self.assertNotIn("gemini", " ".join(self.box.shim_calls()), "the gemini CLI is looked for, never run")

    def test_nothing_known_still_shows_an_item_to_click(self):
        bar = self.bar(home=self.box.home)
        self.assertEqual((bar["text"], bar["tone"]), ("AI", None))
        self.assertEqual([c["title"] for c in bar["blocks"]], ["Claude", "Codex", "Gemini"])

    def test_page_cards_then_the_token_footnote(self):
        self.sven_like()
        self.claude_fixture()
        blocks = self.page(home=self.box.home)
        self.assertEqual([b.get("title") for b in blocks[:3]], ["Claude", "Codex", "Gemini"])
        self.assertEqual(blocks[3]["text"], "Claude Code tokens: 1.2M in the last 7 days, 1.2M in this 5-hour block. "
                                            "They come from Claude Code's own logs and aren't your limit. Codex's numbers "
                                            "come from Codex itself. Pilot reads no sign-in tokens.")
        self.assertEqual(blocks[4]["actions"][0]["text"], "Claude: Session 90% left, Weekly 10% left, Fable weekly 100% "
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
        self.assertIn("4k in the last 7 days, none in this 5-hour block", self.page(home=self.box.home)[3]["text"])

    def test_no_claude_logs_no_token_counts(self):
        self.assertIn("No Claude Code logs on this Mac", self.page(home=self.box.home)[3]["text"])

    def test_a_rerun_reads_only_the_new_bytes(self):
        main = self.claude_fixture()
        self.assertIn("1.2M in this 5-hour block", self.page(home=self.box.home)[3]["text"])
        cache = json.loads((self.box.cache / "claude-usage.json").read_text())["files"]
        self.assertEqual(cache[str(main)]["offset"], main.stat().st_size)
        self.assertNotIn(str(self.projects / "-Users-x-old" / "s0.jsonl"), cache)
        # Same length, different numbers: a re-read from the start would change the total; reading on does not.
        text = main.read_text()
        main.write_text(text.replace('"cache_read_input_tokens":698900', '"cache_read_input_tokens":111111'))
        self.assertEqual(len(main.read_text()), len(text))
        self.write(main, [claude_line(self.now - 5 * MIN, "msg_c", out=300000)], append=True)
        self.assertIn("1.5M in this 5-hour block", self.page(home=self.box.home)[3]["text"])

    def test_while_another_refresh_runs_it_answers_from_the_cache_at_once(self):
        self.claude_fixture()
        self.page(home=self.box.home)
        with open(self.box.cache / "refresh.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            text = self.page(home=self.box.home)[3]["text"]
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
    """CodexBar's pace at fixed times (UsagePace.weekly and UsagePaceText in CodexBar 0.73.0): the mark sits on the
    draining bar where the share left should be by now, and only when the words say reserve or deficit."""
    NOW = 1800000000

    def setUp(self):
        self.view = dict(ai_usage.VIEW)

    def tearDown(self):
        ai_usage.VIEW.clear()
        ai_usage.VIEW.update(self.view)

    def pace(self, used, minutes, left_minutes, show="left"):
        ai_usage.configure({"preferences": {"show": show}})
        w = ai_usage.window("w", used, self.NOW + left_minutes * MIN, self.NOW, minutes)
        return ai_usage.pace(w, self.NOW)

    def test_codexbars_session_in_deficit(self):
        """CodexBar's 2026-10-11 menu: a session 46% used 50 min in (resets in 4 h 10 min) said "29% in deficit ·
        Projected empty in 1h" with its tick at 83% of a bar 54% full."""
        self.assertEqual(self.pace(46, 300, 250), (83.3, "29% in deficit · Projected empty in 58 min"))
        self.assertEqual(self.pace(46, 300, 250, show="used"), (16.7, "29% in deficit · Projected empty in 58 min"))

    def test_a_week_in_deficit_runs_out(self):
        self.assertEqual(self.pace(61, 10080, 3.5 * 1440), (50.0, "11% in deficit · Runs out in 2 d 5 h"))

    def test_reserve_lasts_with_its_headroom(self):
        self.assertEqual(self.pace(10, 300, 200), (66.7, "23% in reserve · Lasts until reset · 4.5x headroom"))

    def test_unused_is_all_reserve(self):
        self.assertEqual(self.pace(0, 10080, 3.5 * 1440), (50.0, "50% in reserve · Lasts until reset"))

    def test_on_pace_within_2_points_has_no_mark(self):
        self.assertEqual(self.pace(17, 300, 250), (None, "On pace · Projected empty in 4 h 4 min"))
        self.assertEqual(self.pace(48, 10080, 3.5 * 1440), (None, "On pace · Lasts until reset · 1.1x headroom"))
        self.assertEqual(self.pace(47.9, 10080, 3.5 * 1440)[0], 50.0, "past 2 points the mark shows")

    def test_no_pace_under_3_percent_in_or_outside_the_window(self):
        self.assertEqual(self.pace(10, 300, 295), (None, None))
        self.assertEqual(self.pace(10, 300, 400), (None, None))
        self.assertEqual(self.pace(10, 300, 0), (None, None))

    def test_used_up(self):
        self.assertEqual(self.pace(100, 300, 150), (50.0, "Used up until the reset"))
