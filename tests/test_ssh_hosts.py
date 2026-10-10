import json
import os

from harness import ExtensionTestCase


class SSHHostsTests(ExtensionTestCase):
    EXTENSION = "ssh-hosts"

    def setUp(self):
        super().setUp()
        ssh = self.box.home / ".ssh"
        (ssh / "config.d").mkdir(parents=True)
        (ssh / "config").write_text(
            "# mine\n"
            "Include config.d/*\n"
            "Host prod prod-alias\n"
            "    HostName 203.0.113.7\n"
            "    User deploy\n"
            "    Port 2222\n"
            "Host *.internal !bastion\n"
            "    User ops\n"
            "Host mac\n"
            "    HostName=127.0.0.1\n"
            "    User sven\n"
            "Match host prod exec true\n"
            "    User nobody\n"
            "Host *\n"
            "    ServerAliveInterval 30\n")
        (ssh / "config.d" / "work").write_text("Host gitbox\n  HostName git.example.com\n  User git\nHost prod\n  User overridden\n")
        self.box.shim("open")

    def hosts(self):
        r = self.run_ext("hosts", home=self.box.home)
        self.assertItemShape(r.items)
        return {i["title"]: i for i in r.items}

    def test_lists_named_hosts_from_config_and_includes_never_wildcards(self):
        rows = self.hosts()
        self.assertEqual(sorted(rows), ["gitbox", "mac", "prod", "prod-alias"])
        self.assertEqual(rows["prod-alias"]["subtitle"], "deploy@203.0.113.7:2222 · ~/.ssh/config")
        self.assertEqual(rows["mac"]["subtitle"], "sven@127.0.0.1 · ~/.ssh/config", "Key=Value reads too; port 22 left out")
        self.assertEqual(rows["gitbox"]["subtitle"], "git@git.example.com · ~/.ssh/config.d/work")
        self.assertIn("203.0.113.7", rows["prod"]["keywords"])

    def test_first_value_wins_as_in_ssh(self):
        # config.d/work is included before the main file's own prod block, so its User wins, as ssh reads it.
        self.assertEqual(self.hosts()["prod"]["subtitle"], "overridden@203.0.113.7:2222 · ~/.ssh/config.d/work")

    def test_actions_connect_copy_and_open_config(self):
        prod = self.hosts()["prod"]
        actions = prod["actions"]
        self.assertEqual([a["type"] for a in actions], ["run", "copy", "open"])
        self.assertEqual(actions[0]["context"], {"host": "prod"})
        self.assertTrue(actions[0]["title"].startswith("Connect in "))
        self.assertEqual(actions[1]["text"], "ssh prod")
        self.assertEqual(os.path.realpath(actions[2]["url"]), os.path.realpath(self.box.home / ".ssh" / "config.d" / "work"))
        for a in actions:
            self.assertActionShape(a)

    def test_connect_opens_ghostty_with_ssh(self):
        ghostty = self.box.root / "Ghostty.app"
        ghostty.mkdir()
        r = self.run_ext("connect", context={"host": "prod"}, preferences={"terminal": str(ghostty)}, home=self.box.home)
        self.assertEqual(r.of_type("hud"), [{"type": "hud", "text": "ssh prod in Ghostty"}])
        self.assertEqual(self.box.shim_calls(), ["open -na %s --args --command=ssh prod --window-save-state=never" % ghostty])

    def test_connect_in_terminal_writes_a_command_file(self):
        r = self.run_ext("connect", context={"host": "gitbox"}, preferences={"terminal": "/nonexistent"}, home=self.box.home)
        calls = self.box.shim_calls()
        if calls[0].startswith("open -na"):  # Ghostty is installed on this Mac, the default wins
            self.assertEqual(r.of_type("hud")[0]["text"], "ssh gitbox in Ghostty")
            return
        script = calls[0].split()[-1]
        self.assertEqual(open(script).read(), "#!/bin/sh\nexec ssh gitbox\n")
        self.assertEqual(r.of_type("hud")[0]["text"], "ssh gitbox in Terminal")

    def test_connect_refuses_a_host_the_config_does_not_name(self):
        for host in ("evil; rm -rf ~", "nothere", "*.internal", None):
            with self.subTest(host=host):
                r = self.run_ext("connect", context={"host": host}, home=self.box.home)
                self.assertEqual(len(r.of_type("error")), 1)
        self.assertEqual(self.box.shim_calls(), [])

    def test_no_config_says_how_to_add_one(self):
        empty = self.box.root / "empty-home"
        empty.mkdir()
        r = self.run_ext("hosts", home=empty)
        self.assertEqual(len(r.items), 1)
        self.assertTrue(r.items[0]["title"].startswith("No hosts in"))
        self.assertNotIn("actions", r.items[0])

    def test_include_loops_and_unreadable_files_do_not_hang(self):
        ssh = self.box.home / ".ssh"
        (ssh / "config.d" / "loop").write_text("Include ~/.ssh/config\nHost looped\n")
        rows = self.hosts()
        self.assertIn("looped", rows)
        self.assertLess(self.box.run(self.EXTENSION, "hosts", home=self.box.home).elapsed, 2)
