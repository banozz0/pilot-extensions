import json
import time
from datetime import datetime, timezone
from unittest import mock

import harness
from harness import ExtensionTestCase


def stamp(seconds_ago):
    return datetime.fromtimestamp(time.time() - seconds_ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def session(ref, title, project, status, activity="idle", agent="claude", kind="agent", **extra):
    s = {"globalRef": ref, "sessionId": ref.rsplit(":", 1)[-1], "title": title, "displayTitle": title,
         "projectName": project, "kind": kind, "status": status, "activity": activity,
         "isSleeping": status == "sleep", "lastInteractionAt": stamp(extra.pop("age", 60)),
         "actions": None if status == "unknown" else {"focus": status == "running", "sleep": status == "running",
                                                       "wake": status == "sleep"}}
    if kind == "agent":
        s["agentId"] = s["agentName"] = agent
    s.update(extra)
    return s


SESSIONS = {"ok": True, "projects": [{"customAgents": [{"agentId": "custom-designer-x1", "name": "designer"}]}],
            "sessions": [
                session("S1:P1:G1", "Asleep long ago", "pilot", "sleep", age=86400 * 3),
                session("S1:P1:G2", "Idle one", "pilot", "running", age=600),
                session("S1:P2:G3", "Busy build", "agents", "running", "working", age=5),
                session("S1:P1:G4", "Design pass", "pilot", "running", "working", agent="custom-designer-x1", age=30),
                session("S1:P2:G5", "Needs an answer", "agents", "running", pendingQuestionCount=1, age=120),
                session("S1:P3:G6", "old-terminal", "dotfiles", "unknown", kind="terminal", age=86400 * 30),
                session("S1:P1:G7", "Parked", "pilot", "sleep", isParked=True, age=86400 * 9),
                {"title": "no ref, skipped"},
                session("all", "bad ref, skipped", "pilot", "running"),
            ]}


class GhostexTests(ExtensionTestCase):
    EXTENSION = "ghostex"

    def setUp(self):
        super().setUp()
        self.fixture = self.box.root / "sessions.json"
        self.fixture.write_text(json.dumps(SESSIONS))
        self.fake_ghostex()
        self.box.shim("open")

    def fake_ghostex(self, sessions_body=None, action_body=None):
        """A ghostex that logs every call and never reaches the real app: `sessions` prints the fixture, any other
        verb prints {"ok": true}."""
        log = 'printf "%%s\\n" "ghostex $*" >> "%s"\n' % self.box.log
        sessions_body = sessions_body or 'cat "%s"' % self.fixture
        action_body = action_body or "echo '{\"ok\":true}'"
        self.box.shim("ghostex", log + 'case "$1" in\n  sessions) %s ;;\n  *) %s ;;\nesac' % (sessions_body, action_body))

    def items(self, **kwargs):
        r = self.run_ext("sessions", **kwargs)
        self.assertItemShape(r.items)
        self.assertLess(r.elapsed, 2)
        return r.items

    def test_lists_sessions_by_what_needs_you_with_project_agent_and_state(self):
        items = self.items()
        self.assertEqual([i["title"] for i in items],
                         ["Needs an answer", "Busy build", "Design pass", "Idle one", "Asleep long ago", "Parked",
                          "old-terminal"])
        rows = {i["title"]: i for i in items}
        self.assertEqual(rows["Busy build"]["subtitle"], "agents · Claude · working · just now")
        self.assertEqual(rows["Design pass"]["subtitle"], "pilot · designer · working · just now")
        self.assertEqual(rows["Idle one"]["subtitle"], "pilot · Claude · awake, idle · 10 min ago")
        self.assertEqual(rows["Asleep long ago"]["subtitle"], "pilot · Claude · asleep · 3 d ago")
        self.assertEqual(rows["Parked"]["subtitle"], "pilot · Claude · asleep, parked · 9 d ago")
        self.assertEqual(rows["Needs an answer"]["icon"], "❓")
        self.assertEqual(rows["old-terminal"]["subtitle"], "dotfiles · terminal · not running · 30 d ago")
        self.assertIn("designer", rows["Design pass"]["keywords"])
        self.assertEqual(self.box.shim_calls(), ["ghostex sessions --json --full"])

    def test_awake_rows_focus_then_sleep_asleep_rows_wake_and_focus_then_wake(self):
        rows = {i["title"]: i for i in self.items()}
        awake = rows["Idle one"]["actions"]
        self.assertEqual(awake, [
            {"type": "run", "command": "focus", "title": "Focus in Ghostex", "context": {"ref": "S1:P1:G2", "wake": False}},
            {"type": "run", "command": "sleep", "title": "Sleep", "context": {"ref": "S1:P1:G2"}}])
        asleep = rows["Asleep long ago"]["actions"]
        self.assertEqual(asleep, [
            {"type": "run", "command": "focus", "title": "Wake and focus in Ghostex",
             "context": {"ref": "S1:P1:G1", "wake": True}},
            {"type": "run", "command": "wake", "title": "Wake", "context": {"ref": "S1:P1:G1"}}])
        for item in rows.values():
            for action in item["actions"]:
                self.assertActionShape(action)

    def test_focus_names_the_session_by_its_ref_and_brings_ghostex_forward(self):
        r = self.run_ext("focus", context={"ref": "S1:P1:G2", "wake": False})
        self.assertEqual(self.box.shim_calls(), ["ghostex focus --session-id S1:P1:G2 --json", "open -a Ghostex"])
        self.assertEqual(r.of_type("hud")[0]["text"], "Focused in Ghostex")

    def test_focus_on_a_sleeping_session_wakes_it_first(self):
        r = self.run_ext("focus", context={"ref": "S1:P1:G1", "wake": True})
        self.assertEqual(self.box.shim_calls(), ["ghostex wake --session-id S1:P1:G1 --json",
                                                 "ghostex focus --session-id S1:P1:G1 --json", "open -a Ghostex"])
        self.assertEqual(r.of_type("hud")[0]["text"], "Woken and focused in Ghostex")

    def test_sleep_and_wake(self):
        r = self.run_ext("sleep", context={"ref": "S1:P1:G2"})
        self.assertEqual(r.of_type("hud")[0]["text"], "Session asleep")
        r = self.run_ext("wake", context={"ref": "S1:P1:G1"})
        self.assertEqual(r.of_type("hud")[0]["text"], "Session awake")
        self.assertEqual(self.box.shim_calls(), ["ghostex sleep --session-id S1:P1:G2 --json",
                                                 "ghostex wake --session-id S1:P1:G1 --json"])

    def test_a_context_that_is_not_one_session_ref_is_refused_so_all_can_never_be_slept(self):
        for context in ({"ref": "all"}, {"ref": "S1:P1:G2 all"}, {"ref": "--help"}, {}, None):
            r = self.run_ext("sleep", context=context)
            self.assertEqual(r.of_type("error")[0]["text"], "got no Ghostex session to act on")
        self.assertEqual(self.box.shim_calls(), [])

    def test_a_failing_action_says_why(self):
        self.fake_ghostex(action_body='echo \'{"ok":false,"error":"No running terminal sessions matched."}\'; exit 1')
        r = self.run_ext("focus", context={"ref": "S1:P1:G2", "wake": False})
        self.assertEqual(r.of_type("error")[0]["text"], "could not focus it: No running terminal sessions matched.")
        self.assertNotIn("open -a Ghostex", self.box.shim_calls())

    def test_ghostex_failing_shows_one_honest_row(self):
        self.fake_ghostex(sessions_body='echo "Error: gxserver is not running" >&2; exit 1')
        items = self.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Ghostex sessions are not available")
        self.assertEqual(items[0]["subtitle"], "Error: gxserver is not running")

    def test_a_hung_ghostex_still_answers_inside_two_seconds(self):
        self.fake_ghostex(sessions_body="sleep 5")
        items = self.items()
        self.assertEqual(items[0]["subtitle"], "Ghostex did not answer in time: is it running?")

    def test_ghostex_missing_shows_one_honest_row(self):
        (self.box.shims / "ghostex").unlink()
        # the real CLI lives in /opt/homebrew/bin
        hide = mock.patch.object(harness, "SYSTEM_PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        hide.start()
        self.addCleanup(hide.stop)
        items = self.items()
        self.assertEqual(len(items), 1)
        self.assertIn("ghostex is not installed", items[0]["subtitle"])

    def test_no_sessions(self):
        self.fixture.write_text(json.dumps({"ok": True, "sessions": []}))
        items = self.items()
        self.assertEqual([i["title"] for i in items], ["No Ghostex sessions"])

    def test_page_counts_states_then_lists_each_project_with_its_actions(self):
        blocks = self.page()
        self.assertEqual(blocks[0]["items"], [
            {"label": "Ask you something", "value": "1", "tone": "warn"}, {"label": "Working", "value": "2", "tone": "fine"},
            {"label": "Idle", "value": "1"}, {"label": "Asleep", "value": "2"}])
        lists = [(b["title"], [i["title"] for i in b["items"]]) for b in blocks[1:]]
        self.assertEqual(lists, [("agents", ["Needs an answer", "Busy build"]), ("pilot", ["Design pass", "Idle one", "Asleep long ago", "Parked"]),
                                 ("dotfiles", ["old-terminal"])])
        self.assertEqual(self.box.shim_calls(), ["ghostex sessions --json --full"])

    def test_page_says_why_ghostex_is_not_there(self):
        self.fake_ghostex(sessions_body='echo "Error: gxserver is not running" >&2; exit 1')
        self.assertEqual(self.page(), [{"type": "markdown", "text": "Ghostex sessions are not available: Error: gxserver is not running"}])
