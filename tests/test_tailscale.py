import json

from harness import ExtensionTestCase

# A synthetic `tailscale status --json` (Tailscale 1.104 shape, trimmed to the fields read): made-up names, the
# example.ts.net tailnet and addresses from Tailscale's 100.64.0.0/10 and fd7a:115c:a1e0::/48 ranges. Never a real one.
NODE = {"Relay": "fra", "Addrs": None, "Active": False, "ExitNode": False, "ExitNodeOption": False, "Tags": None}
FIXTURE = {
    "Version": "1.104.5", "BackendState": "Running", "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0::1"],
    "MagicDNSSuffix": "example.ts.net",
    "CurrentTailnet": {"Name": "someone@example.com", "MagicDNSSuffix": "example.ts.net", "MagicDNSEnabled": True},
    "Self": dict(NODE, ID="n1", HostName="Studio Mac", DNSName="studio-mac.example.ts.net.", OS="macOS", Online=True,
                 TailscaleIPs=["100.64.0.1", "fd7a:115c:a1e0::1"], LastSeen="0001-01-01T00:00:00Z"),
    "Peer": {
        "nodekey:aa": dict(NODE, ID="n2", HostName="raspberrypi", DNSName="raspberrypi.example.ts.net.", OS="linux",
                           Online=True, ExitNode=True, TailscaleIPs=["100.64.0.2", "fd7a:115c:a1e0::2"],
                           LastSeen="0001-01-01T00:00:00Z"),
        "nodekey:bb": dict(NODE, ID="n3", HostName="Pocket", DNSName="pocket.example.ts.net.", OS="iOS", Online=False,
                           TailscaleIPs=["100.64.0.3", "fd7a:115c:a1e0::3"], LastSeen="2026-01-02T10:00:00Z"),
        "nodekey:cc": dict(NODE, ID="n4", HostName="", DNSName="gaming-pc.example.ts.net.", OS="windows", Online=False,
                           TailscaleIPs=["fd7a:115c:a1e0::4"], LastSeen="0001-01-01T00:00:00Z"),
    },
}


class TailscaleTests(ExtensionTestCase):
    EXTENSION = "tailscale"

    def answer(self, status=None, code=0, stdout=None):
        """A fake tailscale on PATH: logs its arguments, prints `status` as JSON (or `stdout`) and exits `code`."""
        out = self.box.root / "status.json"
        out.write_text(stdout if stdout is not None else json.dumps(status if status is not None else FIXTURE))
        self.box.shim("tailscale", 'printf "%%s\\n" "tailscale $*" >> "%s"\ncat "%s"\nexit %d' % (self.box.log, out, code))

    def devices(self):
        r = self.run_ext("devices")
        self.assertItemShape(r.items)
        return r.items

    def test_lists_this_mac_then_online_then_offline_devices(self):
        self.answer()
        items = self.devices()
        self.assertEqual([i["title"] for i in items], ["Studio Mac", "raspberrypi", "gaming-pc", "Pocket"])
        self.assertEqual([i["icon"] for i in items], ["🟢", "🟢", "⚪", "⚪"])
        self.assertEqual(items[0]["subtitle"], "macOS · 100.64.0.1 · this Mac")
        self.assertEqual(items[1]["subtitle"], "linux · 100.64.0.2 · online, your exit node")
        self.assertEqual(items[2]["subtitle"], "windows · fd7a:115c:a1e0::4 · offline")
        self.assertEqual(items[3]["subtitle"], "iOS · 100.64.0.3 · last seen 2026-01-02")
        self.assertIn("offline", items[3]["keywords"])
        self.assertIn("pocket.example.ts.net", items[3]["keywords"])

    def test_return_copies_the_ip_and_the_rest_copy_ipv6_and_name(self):
        self.answer()
        pi = self.devices()[1]
        self.assertEqual(pi["actions"][0], {"type": "copy", "text": "100.64.0.2", "title": "Copy IP 100.64.0.2"})
        self.assertEqual([a["text"] for a in pi["actions"][1:]], ["fd7a:115c:a1e0::2", "raspberrypi.example.ts.net"])

    def test_runs_only_status_json(self):
        self.answer()
        self.devices()
        self.assertEqual(self.box.shim_calls(), ["tailscale status --json"])

    def test_says_plainly_when_tailscale_app_is_not_running(self):
        self.answer(stdout="failed to connect to local Tailscale service; is Tailscale running?\n", code=1)
        items = self.devices()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Tailscale isn't running")
        self.assertEqual(items[0]["actions"], [{"type": "open", "url": "/Applications/Tailscale.app",
                                                "title": "Open Tailscale"}])

    def test_a_hung_cli_answers_in_time(self):
        self.box.shim("tailscale", "sleep 5\ntrue")  # a child, as /usr/local/bin/tailscale has
        r = self.box.run(self.EXTENSION, "devices")
        self.assertLess(r.elapsed, 2)
        self.assertEqual(r.items[0]["title"], "Tailscale isn't running")

    def test_stopped_and_signed_out_say_what_to_do(self):
        for state, title in (("Stopped", "Tailscale is turned off"), ("NeedsLogin", "Tailscale is signed out")):
            with self.subTest(state=state):
                self.answer(dict(FIXTURE, BackendState=state, Peer=None))
                self.assertEqual(self.devices()[0]["title"], title)

    def test_no_peers_lists_this_mac_alone(self):
        self.answer(dict(FIXTURE, Peer=None))
        self.assertEqual([i["title"] for i in self.devices()], ["Studio Mac"])

    def test_page_counts_and_lists_online_then_offline(self):
        self.answer()
        blocks = self.page()
        self.assertEqual({s["label"]: s["value"] for s in blocks[0]["items"]}, {"Online": "2", "Offline": "2", "Devices": "4"})
        lists = [(b["title"], [i["title"] for i in b["items"]]) for b in blocks if b["type"] == "list"]
        self.assertEqual(lists, [("Online", ["Studio Mac", "raspberrypi"]), ("Offline", ["gaming-pc", "Pocket"])])
        pocket = blocks[2]["items"][1]
        self.assertEqual(pocket["subtitle"], "iOS · 100.64.0.3 · last seen 2026-01-02")
        self.assertEqual([a["title"] for a in pocket["actions"]],
                         ["Copy IP 100.64.0.3", "Copy IPv6 fd7a:115c:a1e0::3", "Copy name pocket.example.ts.net"])
        self.assertEqual(self.box.shim_calls(), ["tailscale status --json"])

    def test_bar_counts_online_devices_and_its_click_lists_them(self):
        self.answer()
        line = self.bar()
        self.assertEqual(line["text"], "🛜 2")
        self.assertNotIn("tone", line)
        popover = self.popover()
        self.assertEqual([b["title"] for b in popover], ["Online", "Offline"])
        self.assertEqual([i["title"] for i in popover[0]["items"]], ["Studio Mac", "raspberrypi"])

    def test_bar_and_page_say_when_tailscale_is_not_running(self):
        self.answer(stdout="failed to connect\n", code=1)
        line = self.bar()
        self.assertEqual((line["text"], line["tone"]), ("🛜", "warn"))
        self.assertEqual(line["blocks"][0]["items"][0]["title"], "Tailscale isn't running")
        self.assertEqual(self.page()[0]["items"][0]["title"], "Tailscale isn't running")
