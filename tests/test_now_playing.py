import json
import sys

from harness import ExtensionTestCase

# A fake osascript: as JavaScript it answers scenario.json's `now` as Now Playing would (a command, the argument after the
# script, logged and `after` swapped in), as AppleScript it answers Music's favourite; every call is logged (calls.jsonl)
# and nothing reaches a real player.
FAKE = r'''
import json, os, sys, time
root = os.environ["FAKE_ROOT"]
args = sys.argv[1:]
with open(os.path.join(root, "calls.jsonl"), "a") as f:
    f.write(json.dumps(args) + "\n")
with open(os.path.join(root, "scenario.json")) as f:
    scene = json.load(f)
time.sleep(scene.get("sleep", 0))
if args[:2] == ["-l", "JavaScript"]:
    if scene.get("fails"):
        sys.stderr.write("execution error: Error: something went wrong (-2700)\n")
        sys.exit(1)
    if len(args) > 4 and "after" in scene:
        scene["now"] = scene.pop("after")
        with open(os.path.join(root, "scenario.json"), "w") as f:
            json.dump(scene, f)
    print(json.dumps(scene.get("now", {})))
else:
    if scene.get("deny"):
        sys.stderr.write("execution error: Not authorized to send Apple events to Music. (-1743)\n")
        sys.exit(1)
    if scene.get("favouriteFails"):
        sys.stderr.write("execution error: Music got an error: Can't set favorited of current track. (-10006)\n")
        sys.exit(1)
    print(scene.get("favouriteAnswer", "true"))
'''


def playing(title="Voyager", artist="Daft Punk", album="Discovery", app="Music", bundle="com.apple.Music", duration=320.4,
            elapsed=70.2, stamp=1000.0, now=1010.0, rate=1.0, item="ABC", is_playing=True):
    """Now Playing's answer as the read script prints it: elapsed is as of its stamp."""
    return {"playing": is_playing, "now": now, "stamp": stamp, "Title": title, "Artist": artist, "Album": album,
            "Duration": duration, "ElapsedTime": elapsed, "PlaybackRate": rate if is_playing else 0,
            "ContentItemIdentifier": item, "app": app, "bundle": bundle}


def youtube(**kwargs):
    return playing(**dict(dict(title="Lokko - Min fejn jien ( official music video )", artist="LOKKO MUSIK", album="",
                               app="Google Chrome", bundle="com.google.Chrome", duration=172.8,
                               item="68B81662-F7AA-494E-B754-A15D91B82712"), **kwargs))


class NowPlayingTests(ExtensionTestCase):
    EXTENSION = "now-playing"

    def setUp(self):
        super().setUp()
        (self.box.root / "fake.py").write_text(FAKE)
        self.box.shim("osascript", 'FAKE_ROOT="%s" exec "%s" "%s" "$@"' % (self.box.root, sys.executable,
                                                                          self.box.root / "fake.py"))
        self.scene()

    def run_ext(self, command, **kwargs):
        """As today's Pilot runs it: its run message names every block it draws, notes among them."""
        kwargs.setdefault("pilot", {"blocks": ",".join(sorted(self.BLOCK_TYPES))})
        return ExtensionTestCase.run_ext(self, command, **kwargs)

    def scene(self, **scene):
        (self.box.root / "scenario.json").write_text(json.dumps(scene))

    def calls(self):
        path = self.box.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def commands(self):
        """The MediaRemote commands sent, in order."""
        return [c[4] for c in self.calls() if c[:2] == ["-l", "JavaScript"] and len(c) > 4]

    def apple_events(self):
        return [c for c in self.calls() if c[:2] != ["-l", "JavaScript"]]

    def section(self, blocks):
        return next(b for b in blocks if b["type"] == "section")

    def actions(self, blocks):
        return next(b for b in self.section(blocks)["blocks"] if b["type"] == "buttons")["actions"]

    # ---- the bar ----

    def test_nothing_played_hides_the_item(self):
        self.assertEqual(self.bar()["text"], "")
        self.assertEqual(self.apple_events(), [], "no Apple event, so no prompt and no player started")

    def test_a_youtube_tab_counts(self):
        self.scene(now=youtube())
        self.assertEqual(self.bar()["text"], "♫ Lokko - Min fejn jien…", "too long with the artist: the title alone, cut")
        section = self.section(self.popover())
        self.assertEqual((section["title"], section["subtitle"], section["aside"]),
                         ("Lokko - Min fejn jien ( official music video )", "LOKKO MUSIK", "Google Chrome"))

    def test_playing_shows_artist_and_title(self):
        self.scene(now=playing())
        self.assertEqual(self.bar()["text"], "♫ Daft Punk – Voyager")
        self.assertEqual(self.apple_events(), [])

    def test_paused_is_a_lone_note(self):
        self.scene(now=youtube(is_playing=False))
        self.assertEqual(self.bar()["text"], "♫")

    def test_bar_cleans_titles_and_follows_its_setting(self):
        self.scene(now=playing(title="Voyager (feat. Someone) - 2011 Remaster"))
        self.assertEqual(self.bar()["text"], "♫ Daft Punk – Voyager", "clutter dropped")
        self.assertEqual(self.bar(preferences={"bar": "title"})["text"], "♫ Voyager")
        self.assertEqual(self.bar(preferences={"bar": "glyph"})["text"], "♫")

    def test_now_playing_closed_warns_rather_than_vanishing(self):
        self.scene(now={"error": "no MRNowPlayingRequest"})
        answer = self.bar()
        self.assertEqual((answer["text"], answer["tone"]), ("♫", "warn"))
        self.assertIn("doesn't share what's playing", answer["blocks"][0]["text"])
        self.scene(fails=True)
        self.assertEqual(self.bar()["tone"], "warn")

    def test_a_hung_osascript_still_answers_in_time(self):
        self.scene(now=playing(), sleep=5)
        self.assertEqual(self.bar()["tone"], "warn")

    def test_an_older_pilot_gets_its_notes_as_markdown(self):
        self.scene(now={"error": "closed"})
        self.assertEqual(self.bar(pilot={})["blocks"][0]["type"], "markdown")

    # ---- the popover ----

    def test_popover_shows_track_progress_and_controls(self):
        self.scene(now=playing())
        message = self.blocks_message("bar", "popover")
        self.assertEqual(message["refresh"], 5)
        section = self.section(message["blocks"])
        self.assertEqual((section["title"], section["subtitle"], section["aside"]), ("Voyager", "Daft Punk · Discovery", "Music"))
        self.assertNotIn("asideNote", section)
        meter = section["blocks"][0]
        self.assertEqual((meter["label"], meter["value"], meter["percent"]), ("1:20", "/ 5:20", 25.0),
                         "elapsed 70 s as of its stamp, 10 s ago")
        self.assertEqual([a["title"] for a in self.actions(message["blocks"])],
                         ["⏸︎ Pause", "⏮︎", "⏭︎", "♡ Favourite"])

    def test_paused_popover_says_so_holds_its_place_and_asks_less_often(self):
        self.scene(now=youtube(is_playing=False, elapsed=35.4, stamp=0, now=900))
        message = self.blocks_message("bar", "popover")
        self.assertEqual(message["refresh"], 15)
        section = self.section(message["blocks"])
        self.assertEqual(section["asideNote"], "Paused")
        self.assertEqual(section["blocks"][0]["label"], "0:35", "paused: no time added since its stamp")
        self.assertEqual([a["title"] for a in self.actions(message["blocks"])], ["▶︎ Play", "⏮︎", "⏭︎"],
                         "Favourite only while Music plays")

    def test_a_spotify_track_offers_its_link(self):
        self.scene(now=playing(app="Spotify", bundle="com.spotify.client", item="spotify:track:6GyFP1nfCDB8lbD2bG0Hq9"))
        self.assertEqual(self.actions(self.popover())[-1], {"type": "copy", "title": "Copy link",
                                                            "text": "https://open.spotify.com/track/6GyFP1nfCDB8lbD2bG0Hq9"})

    def test_a_live_stream_has_no_meter(self):
        self.scene(now=youtube(duration=None))
        self.assertEqual(self.section(self.popover())["blocks"][0]["type"], "buttons")

    def test_nothing_played_popover_says_so(self):
        self.assertIn("Nothing is playing", self.popover()[0]["text"])

    # ---- the controls ----

    def test_next_sends_now_playing_its_command_and_says_what_plays(self):
        self.scene(now=youtube(), after=youtube(title="Next video", artist="Someone"))
        result = self.run_ext("next")
        self.assertEqual(result.of_type("hud")[0]["text"], "▶︎ Next video – Someone")
        self.assertEqual(self.commands(), ["4"])
        self.assertEqual(self.apple_events(), [])

    def test_play_pause_and_previous(self):
        self.scene(now=youtube(), after=youtube(is_playing=False))
        self.assertEqual(self.run_ext("playpause").of_type("hud")[0]["text"], "⏸︎ Paused")
        self.run_ext("previous")
        self.assertEqual(self.commands(), ["2", "5"])

    def test_a_control_with_nothing_played_sends_nothing(self):
        result = self.run_ext("next")
        self.assertIn("nothing has played", result.of_type("hud")[0]["text"])
        self.assertEqual(self.commands(), [])

    def test_favourite_asks_music_only_while_music_plays(self):
        self.scene(now=playing())
        self.assertEqual(self.run_ext("favourite").of_type("hud")[0]["text"], "♥ Favourited")
        self.assertEqual(len(self.apple_events()), 1)
        self.assertIn('if application "Music" is running then', self.apple_events()[0][1], "never starts Music")
        self.scene(now=youtube())
        self.assertIn("Google Chrome can't be favourited", self.run_ext("favourite").of_type("hud")[0]["text"])
        self.assertEqual(len(self.apple_events()), 1, "no Apple event for Chrome")

    def test_favourite_failures_say_why(self):
        self.scene(now=playing(), favouriteFails=True)
        self.assertIn("add it to your library", self.run_ext("favourite").of_type("hud")[0]["text"])
        self.scene(now=playing(), deny=True)
        self.assertIn("Automation", self.run_ext("favourite").of_type("error")[0]["text"])

    # ---- Hyper+Space ----

    def test_now_playing_view_shows_the_track_and_its_controls(self):
        self.scene(now=youtube())
        detail = self.run_ext("now").detail
        self.assertTrue(detail["markdown"].startswith("# Lokko - Min fejn jien"), detail["markdown"])
        self.assertIn({"label": "Playing in", "text": "Google Chrome"}, detail["metadata"])
        self.assertIn({"label": "Position", "text": "1:20 / 2:52"}, detail["metadata"])
        self.assertEqual([a["title"] for a in detail["actions"]], ["Pause", "Previous track", "Next track"])
        for action in detail["actions"]:
            self.assertActionShape(action)

    def test_now_playing_view_with_nothing_played_says_so(self):
        self.assertIn("Nothing is playing", self.run_ext("now").detail["markdown"])
