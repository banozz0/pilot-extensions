import fcntl
import json
import os
import subprocess
import threading
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


def codex_event(t, primary, secondary, limit_id="codex"):
    """A token_count line as Codex writes it (shape read from a real 2026-10 log)."""
    usage = {"input_tokens": 1000, "cached_input_tokens": 900, "cache_write_input_tokens": 0, "output_tokens": 10,
             "reasoning_output_tokens": 0, "total_tokens": 1010}
    return {"timestamp": iso(t), "ordinal": 1, "type": "event_msg",
            "payload": {"type": "token_count",
                        "info": {"total_token_usage": usage, "last_token_usage": usage, "model_context_window": 258400},
                        "rate_limits": {"limit_id": limit_id, "limit_name": None, "primary": primary,
                                        "secondary": secondary,
                                        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                                        "individual_limit": None, "spend_control_reached": None, "plan_type": None,
                                        "rate_limit_reached_type": None}}}


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


class AIUsageTests(ExtensionTestCase):
    EXTENSION = "ai-usage"

    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.codex = self.box.home / ".codex" / "sessions" / "2026" / "10" / "10"
        self.projects = self.box.home / ".claude" / "projects"
        for secret in (self.box.home / ".codex" / "auth.json", self.box.home / ".claude" / ".credentials.json"):
            secret.parent.mkdir(parents=True, exist_ok=True)
            secret.write_text(json.dumps({"bot": SECRET_SAMPLE}))

    def write(self, path, lines, age=0, append=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a" if append else "w") as f:
            f.write("".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines))
        os.utime(path, (self.now - age, self.now - age))
        return path

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

    def detail(self, **kwargs):
        r = self.run_ext("usage", home=self.box.home, **kwargs)
        self.assertLess(r.elapsed, 2)
        self.assertNotIn(SECRET_SAMPLE, r.stdout)
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
        self.assertNotIn(SECRET_SAMPLE, r.stdout)
        huds = r.of_type("hud")
        self.assertEqual(len(huds), 1, r.stdout)
        return huds[0]["text"]

    def test_codex_limits_from_the_newest_log_with_bars_and_reset_times(self):
        n = self.now
        self.codex_log("older", [codex_event(n - 2 * DAY, window(90.0, 300, n + HOUR), window(95.0, 10080, n + DAY))],
                       age=2 * DAY)
        self.codex_log("newest", [
            codex_event(n - 20 * MIN, window(40.0, 300, n + HOUR), window(10.0, 10080, n + DAY)),
            codex_event(n - 10 * MIN, window(52.0, 300, n + 2 * HOUR + 10 * MIN + 30),
                        window(18.0, 10080, n + 3 * DAY + 2 * HOUR + 30)),
            codex_event(n - 5 * MIN, None, None, limit_id="premium")])
        d, facts = self.detail()
        self.assertIn("▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░` 52%", d["markdown"])
        self.assertIn("▓▓▓▓░░░░░░░░░░░░░░░░` 18%", d["markdown"])
        self.assertEqual(facts["Codex 5 h used"], "52%")
        self.assertEqual(facts["Codex week used"], "18%")
        local = time.strftime("%H:%M", time.localtime(n + 2 * HOUR + 10 * MIN + 30))
        self.assertTrue(facts["Codex 5 h resets"].endswith("%s (in 2 h 10 min)" % local), facts["Codex 5 h resets"])
        self.assertTrue(facts["Codex week resets"].endswith("(in 3 d 2 h)"), facts["Codex week resets"])
        self.assertIn("10 min ago", facts["Codex as of"])
        self.assertTrue(self.hud().startswith("Codex 5 h 52% · week 18% · "))

    def test_codex_older_shape_and_a_window_that_reset_since(self):
        n = self.now
        self.codex_log("one", [codex_event(n - 10 * MIN, window(10.0, 300, resets_at=n - HOUR),
                                           window(30.0, 10080, resets_in=10 * MIN + HOUR + 30))])
        d, facts = self.detail()
        self.assertEqual(facts["Codex 5 h used"], "0% (was 10%, reset since)")
        self.assertIn("nothing logged since", d["markdown"])
        self.assertTrue(facts["Codex week resets"].endswith("(in 1 h)"), facts["Codex week resets"])
        self.assertTrue(self.hud().startswith("Codex 5 h reset · week 30% · "))

    def test_codex_quiet_for_8_days_or_absent(self):
        d, _ = self.detail()
        self.assertIn("No Codex logs on this Mac", d["markdown"])
        self.codex_log("ancient", [codex_event(self.now - 9 * DAY, window(80.0, 300, self.now), None)], age=9 * DAY)
        d, facts = self.detail()
        self.assertIn("No Codex use logged in the last 8 days", d["markdown"])
        self.assertNotIn("Codex 5 h used", facts)
        self.assertTrue(self.hud().startswith("Codex no use in 8 days · "))

    def test_claude_block_and_week_tokens(self):
        self.claude_fixture()
        d, facts = self.detail()
        self.assertEqual(facts["Claude block tokens"], "1,200,000")
        self.assertEqual(facts["Claude block split"], "in 100 · out 300k · cache write 1k · cache read 899k")
        self.assertEqual(facts["Claude 7 days tokens"], "1,205,000")
        self.assertEqual(facts["Claude % of limit"], "unknown (not in local logs)")
        start = (self.now - 90 * MIN) // HOUR * HOUR
        self.assertIn("since %s" % time.strftime("%H:%M", time.localtime(start)), d["markdown"])
        self.assertTrue(facts["Claude block resets"].startswith("today ") or facts["Claude block resets"].startswith(
            "tomorrow "))
        self.assertIn(time.strftime("%H:%M", time.localtime(start + 5 * HOUR)), facts["Claude block resets"])
        self.assertIn("**Last 7 days** 1.2M tokens", d["markdown"])
        self.assertNotIn("Still counting", d["markdown"])
        self.assertEqual([a["title"] for a in d["actions"]], ["Copy summary"])
        self.assertTrue(self.hud().endswith(" · Claude block 1.2M tokens"))

    def test_page_and_bar_show_the_limits_and_the_block(self):
        n = self.now
        self.codex_log("newest", [codex_event(n - 10 * MIN, window(72.0, 300, n + 2 * HOUR + 30), window(18.0, 10080, n + 3 * DAY))])
        self.claude_fixture()
        blocks = self.page(home=self.box.home)
        self.assertEqual(blocks[0]["items"], [
            {"label": "Codex 5 h", "value": "72%", "tone": "warn"}, {"label": "Codex week", "value": "18%", "tone": "fine"},
            {"label": "Claude block", "value": "1.2M"}, {"label": "Claude 7 days", "value": "1.2M"}])
        meters = [b for b in blocks if b["type"] == "meter"]
        self.assertEqual([(m["label"], m["percent"]) for m in meters][:2], [("Codex 5 h", 72.0), ("Codex week", 18.0)])
        self.assertTrue(meters[0]["note"].endswith("(in 2 h)"), meters[0]["note"])
        self.assertEqual(meters[2]["label"], "Claude block time")
        self.assertEqual(blocks[-1]["actions"][0]["title"], "Copy summary")
        bar = self.bar(home=self.box.home)
        self.assertEqual((bar["text"], bar["tone"]), ("AI 72%", "warn"))
        self.assertEqual([b["label"] for b in bar["blocks"]], ["Codex 5 h", "Codex week"])

    def test_bar_hides_with_nothing_to_say(self):
        bar = self.bar(home=self.box.home)
        self.assertEqual((bar["text"], bar["blocks"]), ("", []))
        self.assertIn("No Codex logs", json.dumps(self.page(home=self.box.home)))

    def test_no_block_running_after_a_quiet_5_hours(self):
        self.write(self.projects / "p" / "s.jsonl", [claude_line(self.now - 6 * HOUR, "msg_1", read=4000)])
        d, facts = self.detail()
        self.assertEqual(facts["Claude block tokens"], "0 (no block running)")
        self.assertIn("none running, the last ended", d["markdown"])
        self.assertEqual(facts["Claude 7 days tokens"], "4,000")
        self.assertTrue(self.hud().endswith("Claude no block running"))

    def test_no_logs_at_all(self):
        d, _ = self.detail()
        self.assertIn("No Claude Code logs on this Mac", d["markdown"])
        self.assertEqual(self.hud(), "Codex no logs · Claude no logs")

    def test_a_rerun_reads_only_the_new_bytes(self):
        main = self.claude_fixture()
        _, facts = self.detail()
        self.assertEqual(facts["Claude block tokens"], "1,200,000")
        cache = json.loads((self.box.cache / "claude-usage.json").read_text())["files"]
        self.assertEqual(cache[str(main)]["offset"], main.stat().st_size)
        self.assertNotIn(str(self.projects / "-Users-x-old" / "s0.jsonl"), cache)
        # Same length, different numbers: a re-read from the start would change the total; reading on does not.
        text = main.read_text()
        main.write_text(text.replace('"cache_read_input_tokens":698900', '"cache_read_input_tokens":111111'))
        self.assertEqual(len(main.read_text()), len(text))
        self.write(main, [claude_line(self.now - 5 * MIN, "msg_c", out=1000)], append=True)
        _, facts = self.detail()
        self.assertEqual(facts["Claude block tokens"], "1,201,000")

    def test_while_another_refresh_runs_it_answers_from_the_cache_at_once(self):
        self.claude_fixture()
        self.detail()
        with open(self.box.cache / "refresh.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            d, facts = self.detail()
            self.assertIn("Still counting in the background", d["markdown"])
            self.assertEqual(facts["Claude block tokens"], "1,200,000")
            self.assertEqual(d["actions"][0], {"type": "push", "command": "usage", "title": "Count again"})
            threading.Timer(1.0, lock.close).start()
            started = time.monotonic()
            self.assertTrue(self.hud().endswith("Claude block 1.2M tokens"), "the summary waits for the refresh")
            self.assertGreater(time.monotonic() - started, 0.9)

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
