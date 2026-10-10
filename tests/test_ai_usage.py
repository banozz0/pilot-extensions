import fcntl
import json
import os
import subprocess
import time

from harness import EXTENSIONS, ExtensionTestCase

SECRET_SAMPLE = "sk-THIS-MUST-NEVER-BE-PRINTED"
MIN, HOUR, DAY = 60, 3600, 86400


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


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


def statusline(updated, five=None, week=None):
    """A Ghostex status-line file as it stores Claude Code's status-line input (shape read live 2026-10-10)."""
    payload = {"session_id": "s", "transcript_path": "/x.jsonl", "cwd": "/x", "model": {"id": "claude-opus-5-5"},
               "context_window": {"total_input_tokens": 5, "used_percentage": 12}, "fast_mode": False}
    if five or week:
        payload["rate_limits"] = {k: {"used_percentage": v[0], "resets_at": int(v[1])}
                                  for k, v in (("five_hour", five), ("seven_day", week)) if v}
    return {"version": 1, "updatedAt": iso(updated), "payload": payload}


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


def card(blocks, title):
    return next(b for b in blocks if b["type"] == "section" and b["title"] == title)


def hhmm(t):
    return time.strftime("%H:%M", time.localtime(t))


class AIUsageTests(ExtensionTestCase):
    EXTENSION = "ai-usage"

    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.codex = self.box.home / ".codex" / "sessions" / "2026" / "10" / "10"
        self.projects = self.box.home / ".claude" / "projects"
        self.store = self.box.home / ".local" / "state" / "ghostex" / "agent-hooks" / "claude-statusline"
        for secret in (self.box.home / ".codex" / "auth.json", self.box.home / ".claude" / ".credentials.json",
                       self.box.home / ".gemini" / "oauth_creds.json"):
            secret.parent.mkdir(parents=True, exist_ok=True)
            secret.write_text(json.dumps({"bot": SECRET_SAMPLE}))

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

    def codex_log(self, name, events, age=0):
        meta = {"timestamp": iso(self.now - HOUR), "type": "session_meta", "payload": {"id": name, "cwd": "/x"}}
        return self.write(self.codex / ("rollout-%s.jsonl" % name), [meta] + events, age)

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
        """Claude at 39% and 26%; Codex last ran 4 days ago, its 5 hours reset since (was 10%), its week at 67%."""
        n = self.now
        self.status("new", statusline(n - 3 * MIN, (39, n + 40 * MIN), (26, n + 5 * DAY)), age=3 * MIN)
        self.codex_log("last", [codex_event(n - 4 * DAY, window(10.0, 300, n - 4 * DAY + 2 * HOUR),
                                            window(67.0, 10080, n + 6 * HOUR))], age=4 * DAY)

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

    def test_gauges_and_cards_from_the_status_line_and_the_codex_log(self):
        self.sven_like()
        bar = self.bar(home=self.box.home)
        self.check_quiet(json.dumps(bar))
        self.assertEqual(bar["gauges"], [{"label": "C", "meters": [39, 26]},
                                         {"label": "X", "meters": [0, 67], "tone": "warn"}])
        self.assertEqual((bar["text"], bar["tone"]), ("", "warn"))
        blocks = self.popover(home=self.box.home)
        self.assertEqual(blocks[:3], bar["blocks"], "the bar line carries the popover's cards until it answers")
        claude = card(blocks, "Claude")
        self.assertEqual(claude["subtitle"], "Claude Code · read %s" % hhmm(self.now - 3 * MIN))
        self.assertEqual(claude["blocks"], [
            {"type": "meter", "label": "Session", "percent": 39, "tone": "fine", "resetsAt": int(self.now + 40 * MIN)},
            {"type": "meter", "label": "Week", "percent": 26, "tone": "fine", "resetsAt": int(self.now + 5 * DAY)}])
        codex = card(blocks, "Codex")
        self.assertTrue(codex["subtitle"].startswith("last run ") and codex["subtitle"].endswith(hhmm(self.now - 4 * DAY)),
                        codex["subtitle"])
        self.assertEqual(codex["blocks"], [
            {"type": "meter", "label": "5 hours", "percent": 0, "note": "reset since your last run (was 10%)"},
            {"type": "meter", "label": "Week", "percent": 67, "tone": "warn", "resetsAt": int(self.now + 6 * HOUR)},
            {"type": "stats", "items": [{"label": "Credits", "value": "0"}]}])
        self.assertEqual(blocks[-1], {"type": "markdown", "text": "Pilot reads no sign-in tokens."})
        self.assertEqual(self.hud(), "Claude session 39% · week 26% · Codex 5 hours 0% · week 67%")

    def test_the_newest_status_line_with_limits_wins(self):
        n = self.now
        self.status("old", statusline(n - DAY, (90, n + HOUR), (90, n + DAY)), age=DAY)
        self.status("mid", statusline(n - 10 * MIN, (12, n + HOUR), (34, n + DAY)), age=10 * MIN)
        self.status("newer-no-limits", statusline(n - 5 * MIN), age=5 * MIN)
        self.status("newest-half-written", '{"version":1,"payl', age=0)
        self.assertEqual(self.bar(home=self.box.home)["gauges"], [{"label": "C", "meters": [12, 34]}])

    def test_claude_unknown_without_ghostex_or_a_reply(self):
        for setup in (lambda: None, lambda: self.status("bare", statusline(self.now))):
            setup()
            claude = card(self.popover(home=self.box.home), "Claude")
            self.assertEqual(claude["blocks"], [{"type": "markdown", "text": "Unknown until your next Claude Code reply."}])
            bar = self.bar(home=self.box.home)
            self.assertNotIn("C", [g["label"] for g in bar["gauges"]])
        self.assertTrue(self.hud().startswith("Claude unknown"))

    def test_a_window_that_reset_since_shows_0_with_what_it_was(self):
        n = self.now
        self.status("s", statusline(n - 6 * HOUR, (88, n - HOUR), (61, n + DAY)), age=6 * HOUR)
        claude = card(self.popover(home=self.box.home), "Claude")
        self.assertEqual(claude["blocks"][0], {"type": "meter", "label": "Session", "percent": 0,
                                               "note": "reset since your last Claude Code reply (was 88%)"})
        self.assertEqual(claude["subtitle"], "Claude Code · read %s" % hhmm(n - 6 * HOUR)
                         if time.localtime(n - 6 * HOUR)[:3] == time.localtime(n)[:3]
                         else "Claude Code · read yesterday %s" % hhmm(n - 6 * HOUR))
        self.assertEqual(self.bar(home=self.box.home)["gauges"], [{"label": "C", "meters": [0, 61], "tone": "warn"}])

    def test_tones_amber_from_60_red_from_85(self):
        n = self.now
        self.status("s", statusline(n, (85, n + HOUR), (59.5, n + DAY)))
        self.codex_log("one", [codex_event(n - MIN, window(60.0, 300, n + HOUR), window(12.0, 10080, n + DAY))])
        bar = self.bar(home=self.box.home)
        self.assertEqual(bar["gauges"], [{"label": "C", "meters": [85, 59.5], "tone": "alert"},
                                         {"label": "X", "meters": [60, 12], "tone": "warn"}])
        self.assertEqual(bar["tone"], "alert")
        tones = [m["tone"] for c in bar["blocks"][:2] for m in c["blocks"] if m["type"] == "meter"]
        self.assertEqual(tones, ["alert", "fine", "warn", "fine"])

    def test_codex_older_shape_newest_log_and_other_limit_ids(self):
        n = self.now
        self.codex_log("older", [codex_event(n - 2 * DAY, window(90.0, 300, n + HOUR), window(95.0, 10080, n + DAY))],
                       age=2 * DAY)
        self.codex_log("newest", [
            codex_event(n - 20 * MIN, window(40.0, 300, n + HOUR), window(10.0, 10080, n + DAY)),
            codex_event(n - 10 * MIN, window(52.0, 300, resets_in=2 * HOUR + 10 * MIN + 30), window(18.0, 10080, n + DAY),
                        credits={"has_credits": True, "unlimited": True, "balance": None}),
            codex_event(n - 5 * MIN, None, None, limit_id="premium")])
        codex = card(self.popover(home=self.box.home), "Codex")
        self.assertEqual([(m.get("label"), m.get("percent")) for m in codex["blocks"]][:2], [("5 hours", 52), ("Week", 18)])
        self.assertEqual(codex["blocks"][0]["resetsAt"], int(n - 10 * MIN) + 2 * HOUR + 10 * MIN + 30)
        self.assertEqual(codex["blocks"][2]["items"], [{"label": "Credits", "value": "unlimited"}])
        self.assertEqual(codex["subtitle"], "last run %s" % hhmm(n - 10 * MIN))

    def test_codex_absent_or_quiet_for_8_days(self):
        codex = card(self.popover(home=self.box.home), "Codex")
        self.assertEqual(codex["blocks"][0]["text"], "No Codex logs on this Mac. Run Codex once.")
        self.assertNotIn("X", [g["label"] for g in self.bar(home=self.box.home)["gauges"]])
        self.codex_log("ancient", [codex_event(self.now - 9 * DAY, window(80.0, 300, self.now), None)], age=9 * DAY)
        codex = card(self.popover(home=self.box.home), "Codex")
        self.assertEqual(codex["subtitle"], "no run in 8 days")
        self.assertEqual(self.bar(home=self.box.home)["gauges"], [{"label": "X", "meters": [0, 0]}])

    def test_gemini_not_set_up_until_its_cli_is_on_path(self):
        gemini = card(self.popover(home=self.box.home), "Gemini")
        self.assertEqual(gemini["subtitle"], "not set up")
        self.assertEqual(gemini["blocks"][1]["actions"],
                         [{"type": "open", "url": "pilot://open?page=ai", "title": "Set up in Pilot › AI…"}])
        self.box.shim("gemini")
        gemini = card(self.popover(home=self.box.home), "Gemini")
        self.assertEqual(gemini["subtitle"], "limit unknown")
        self.assertEqual(self.box.shim_calls(), [], "the gemini CLI is looked for, never run")

    def test_nothing_known_still_shows_an_item_to_click(self):
        bar = self.bar(home=self.box.home)
        self.assertEqual((bar["text"], bar["tone"], bar["gauges"]), ("AI", None, []))
        self.assertEqual([c["title"] for c in bar["blocks"]], ["Claude", "Codex", "Gemini"])

    def test_page_cards_then_the_token_footnote(self):
        self.sven_like()
        self.claude_fixture()
        blocks = self.page(home=self.box.home)
        self.assertEqual([b.get("title") for b in blocks[:3]], ["Claude", "Codex", "Gemini"])
        self.assertEqual(blocks[3]["text"], "Claude Code tokens: 1.2M in the last 7 days, 1.2M in this 5-hour block. "
                                            "They come from Claude Code's own logs and aren't your limit.")
        self.assertEqual(blocks[4]["actions"][0]["text"], "Claude session 39% · week 26% · Codex 5 hours 0% · week 67%")
        self.check_quiet(json.dumps(blocks))

    def test_detail_names_every_provider(self):
        self.sven_like()
        self.claude_fixture()
        d, facts = self.detail()
        self.assertTrue(facts["Claude session"].startswith("39% · resets today ") or
                        facts["Claude session"].startswith("39% · resets tomorrow "), facts["Claude session"])
        self.assertEqual(facts["Codex 5 hours"], "0% · reset since your last run (was 10%)")
        self.assertTrue(facts["Codex week"].startswith("67% · resets "), facts["Codex week"])
        self.assertEqual(facts["Codex credits"], "0")
        self.assertIn("▓▓▓▓▓▓▓▓░░░░░░░░░░░░` 39%", d["markdown"])
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
