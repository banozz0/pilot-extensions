import json
import re
import sys

from harness import ExtensionTestCase

# A fake osascript: it answers from scenario.json as System Events, Music and Spotify would, logs every script it is
# handed (calls.jsonl), and never reaches a real player. A control (one with a delay before its read) swaps in the
# scenario's `after` tracks.
FAKE = r'''
import json, os, sys, time
root = os.environ["FAKE_ROOT"]
source = sys.argv[sys.argv.index("-e") + 1]
with open(os.path.join(root, "calls.jsonl"), "a") as f:
    f.write(json.dumps(source) + "\n")
with open(os.path.join(root, "scenario.json")) as f:
    scene = json.load(f)
time.sleep(scene.get("sleep", 0))
def deny(who):
    sys.stderr.write("execution error: Not authorized to send Apple events to %s. (-1743)\n" % who)
    sys.exit(1)
if "System Events" in source:
    if scene.get("deny") == "System Events":
        deny("System Events")
    print("\x1f".join(scene.get("running", [])))
elif "set records to" in source:
    if "delay" in source and "after" in scene:
        scene["tracks"] = scene.pop("after")
        with open(os.path.join(root, "scenario.json"), "w") as f:
            json.dump(scene, f)
    records = []
    for player in ("Music", "Spotify"):
        if 'tell application "%s"' % player in source:
            if scene.get("deny") == player:
                deny(player)
            records.append("\x1f".join([player] + scene["tracks"][player]))
    print("\x1e".join(records))
else:
    if scene.get("controlFails"):
        sys.stderr.write("execution error: Music got an error: Can't set favorited of current track. (-10006)\n")
        sys.exit(1)
    if "favorited" in source or "pLov" in source:
        print(scene.get("favouriteAnswer", "true"))
'''


def music(state="playing", title="Voyager", artist="Daft Punk", album="Discovery", duration="320", position="80",
          favourite="false"):
    return [state, title, artist, album, duration, position, favourite, "ABC123"]


def spotify(state="playing", title="Midnight City", artist="M83", album="Hurry Up, We're Dreaming", duration="243000",
            position="60"):
    return [state, title, artist, album, duration, position, "", "spotify:track:6GyFP1nfCDB8lbD2bG0Hq9"]


class NowPlayingTests(ExtensionTestCase):
    EXTENSION = "now-playing"

    def setUp(self):
        super().setUp()
        (self.box.root / "fake.py").write_text(FAKE)
        self.box.shim("osascript", 'FAKE_ROOT="%s" exec "%s" "%s" "$@"' % (self.box.root, sys.executable,
                                                                          self.box.root / "fake.py"))
        self.scene(running=[])

    def run_ext(self, command, **kwargs):
        """As today's Pilot runs it: its run message names every block it draws, notes among them."""
        kwargs.setdefault("pilot", {"blocks": ",".join(sorted(self.BLOCK_TYPES))})
        return ExtensionTestCase.run_ext(self, command, **kwargs)

    def scene(self, **scene):
        (self.box.root / "scenario.json").write_text(json.dumps(scene))

    def calls(self):
        path = self.box.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def told(self, player):
        return [c for c in self.calls() if 'tell application "%s"' % player in c]

    def controls(self, player, words):
        """The scripts that sent `player` a control holding `words`."""
        return [c for c in self.told(player) if words in c]

    def section(self, blocks):
        return next(b for b in blocks if b["type"] == "section")

    def actions(self, blocks):
        inner = self.section(blocks)["blocks"]
        return next(b for b in inner if b["type"] == "buttons")["actions"]

    # ---- the bar ----

    def test_no_player_open_hides_the_item_and_tells_no_player(self):
        self.assertEqual(self.bar()["text"], "")
        self.assertEqual(len(self.calls()), 1, "only System Events is asked")
        self.assertEqual(self.told("Music") + self.told("Spotify"), [])

    def test_playing_shows_artist_and_title(self):
        self.scene(running=["Music"], tracks={"Music": music()})
        self.assertEqual(self.bar()["text"], "♫ Daft Punk – Voyager")
        self.assertEqual(self.told("Spotify"), [], "Spotify is not open, so no script names it")

    def test_paused_is_a_lone_note_and_stopped_hides_it(self):
        self.scene(running=["Music"], tracks={"Music": music(state="paused")})
        self.assertEqual(self.bar()["text"], "♫")
        self.scene(running=["Music"], tracks={"Music": music(state="stopped", title="")})
        self.assertEqual(self.bar()["text"], "")

    def test_long_names_fit_the_bar_title_first_without_clutter(self):
        self.scene(running=["Music"], tracks={"Music": music(title="Voyager (feat. Someone) - 2011 Remaster")})
        self.assertEqual(self.bar()["text"], "♫ Daft Punk – Voyager", "clutter dropped")
        self.scene(running=["Music"], tracks={"Music": music(title="One More Time [Radio Edit]")})
        self.assertEqual(self.bar()["text"], "♫ One More Time", "too long with the artist: the title alone")
        self.scene(running=["Music"], tracks={"Music": music(artist="X", title="A" * 40)})
        self.assertEqual(len(self.bar()["text"]), 24)

    def test_bar_setting_title_or_glyph(self):
        self.scene(running=["Music"], tracks={"Music": music()})
        self.assertEqual(self.bar(preferences={"bar": "title"})["text"], "♫ Voyager")
        self.assertEqual(self.bar(preferences={"bar": "glyph"})["text"], "♫")

    def test_streamed_track_music_hides_still_shows_and_says_why(self):
        self.scene(running=["Music"], tracks={"Music": music(title="", artist="", album="", duration="", favourite="")})
        self.assertEqual(self.bar()["text"], "♫ Music")
        blocks = self.popover()
        section = self.section(blocks)
        self.assertEqual(section["title"], "Music is streaming")
        self.assertIn("library", section["blocks"][0]["text"])
        self.assertEqual([a["command"] for a in self.actions(blocks)], ["playpause", "previous", "next"])

    def test_denied_warns_and_says_where_to_allow(self):
        self.scene(running=["Music"], tracks={"Music": music()}, deny="Music")
        answer = self.bar()
        self.assertEqual((answer["text"], answer["tone"]), ("♫", "warn"))
        self.assertIn("Automation", answer["blocks"][0]["text"])
        self.assertIn("Music", answer["blocks"][0]["text"])

    def test_a_hung_osascript_still_answers_in_time(self):
        self.scene(running=["Music"], tracks={"Music": music()}, sleep=5)
        answer = self.bar()
        self.assertEqual(answer["tone"], "warn")
        self.assertIn("Allow", answer["blocks"][0]["text"])

    # ---- the popover ----

    def test_popover_shows_track_progress_and_controls(self):
        self.scene(running=["Music"], tracks={"Music": music()})
        message = self.blocks_message("bar", "popover")
        self.assertEqual(message["refresh"], 5)
        section = self.section(message["blocks"])
        self.assertEqual((section["title"], section["subtitle"], section["aside"]), ("Voyager", "Daft Punk · Discovery", "Music"))
        self.assertNotIn("asideNote", section)
        meter = section["blocks"][0]
        self.assertEqual((meter["type"], meter["label"], meter["value"], meter["percent"]), ("meter", "1:20", "/ 5:20", 25.0))
        actions = self.actions(message["blocks"])
        self.assertEqual([a["title"] for a in actions], ["⏸\ufe0e Pause", "⏮\ufe0e", "⏭\ufe0e", "♡ Favourite"])
        self.assertTrue(all(a["context"] == {"player": "Music"} for a in actions))

    def test_paused_popover_says_so_and_asks_less_often(self):
        self.scene(running=["Music"], tracks={"Music": music(state="paused", favourite="true")})
        message = self.blocks_message("bar", "popover")
        self.assertEqual(message["refresh"], 15)
        self.assertEqual(self.section(message["blocks"])["asideNote"], "Paused")
        titles = [a["title"] for a in self.actions(message["blocks"])]
        self.assertEqual((titles[0], titles[-1]), ("▶\ufe0e Play", "♥ Favourite"))

    def test_the_playing_player_wins_and_spotify_offers_its_link(self):
        self.scene(running=["Music", "Spotify"], tracks={"Music": music(state="paused"), "Spotify": spotify()})
        self.assertEqual(self.bar()["text"], "♫ M83 – Midnight City")
        blocks = self.popover()
        section = self.section(blocks)
        self.assertEqual(section["aside"], "Spotify")
        self.assertEqual(section["blocks"][0]["value"], "/ 4:03", "Spotify's milliseconds read as seconds")
        link = self.actions(blocks)[-1]
        self.assertEqual(link, {"type": "copy", "text": "https://open.spotify.com/track/6GyFP1nfCDB8lbD2bG0Hq9",
                                "title": "Copy link"})
        note = next(b for b in blocks if b["type"] == "note")
        self.assertEqual((note["text"], note["aside"]), ("Music also open: Voyager – Daft Punk", "paused"))

    def test_a_tie_goes_to_the_setting_else_the_last_one_playing(self):
        both = {"Music": music(state="paused"), "Spotify": spotify(state="paused")}
        self.scene(running=["Music", "Spotify"], tracks=both)
        self.assertEqual(self.section(self.popover())["aside"], "Music", "no history: Music")
        self.assertEqual(self.section(self.popover(preferences={"player": "spotify"}))["aside"], "Spotify")
        self.scene(running=["Spotify"], tracks={"Spotify": spotify()})
        self.bar()
        self.scene(running=["Music", "Spotify"], tracks=both)
        self.assertEqual(self.section(self.popover())["aside"], "Spotify", "Spotify played last")

    def test_an_older_pilot_gets_its_notes_as_markdown(self):
        self.scene(running=["Music", "Spotify"], tracks={"Music": music(state="paused"), "Spotify": spotify()})
        blocks = self.popover(pilot={})
        self.assertEqual(blocks[-1], {"type": "markdown", "text": "Music also open: Voyager – Daft Punk"})
        self.scene(running=["Music"], tracks={"Music": music()}, deny="Music")
        self.assertEqual(self.bar(pilot={})["blocks"][0]["type"], "markdown")

    def test_nothing_loaded_popover_says_so(self):
        blocks = self.popover()
        self.assertIn("No player is open", blocks[0]["text"])

    # ---- the controls ----

    def test_next_tells_the_shown_player_and_says_what_plays(self):
        self.scene(running=["Music", "Spotify"], tracks={"Music": music(), "Spotify": spotify(state="paused")},
                   after={"Music": music(title="Aerodynamic"), "Spotify": spotify(state="paused")})
        result = self.run_ext("next", context={"player": "Music"})
        self.assertEqual(result.of_type("hud")[0]["text"], "▶\ufe0e Aerodynamic – Daft Punk")
        controls = self.controls("Music", "next track")
        self.assertEqual(len(controls), 1)
        self.assertIn('if application "Music" is running then\n\ttell application "Music"\n\t\tnext track', controls[0],
                      "never starts a player")
        self.assertEqual(self.controls("Spotify", "next track"), [])

    def test_play_pause_from_hyper_space_picks_the_shown_player(self):
        self.scene(running=["Spotify"], tracks={"Spotify": spotify()}, after={"Spotify": spotify(state="paused")})
        result = self.run_ext("playpause")
        self.assertEqual(result.of_type("hud")[0]["text"], "⏸\ufe0e Paused")
        self.assertEqual(len(self.controls("Spotify", "playpause")), 1)

    def test_a_control_with_no_player_starts_nothing(self):
        result = self.run_ext("previous")
        self.assertIn("no player has a track", result.of_type("hud")[0]["text"])
        self.assertEqual(self.told("Music") + self.told("Spotify"), [])

    def test_a_quit_player_in_the_context_is_not_started_again(self):
        self.scene(running=["Spotify"], tracks={"Spotify": spotify()})
        result = self.run_ext("next", context={"player": "Music"})
        self.assertEqual(result.of_type("hud")[0]["text"], "Music is no longer open.")
        self.assertEqual(self.told("Music"), [])
        self.assertEqual(self.controls("Spotify", "next track"), [], "nor is the other one skipped")

    def test_favourite_toggles_music_and_says_so(self):
        self.scene(running=["Music"], tracks={"Music": music()})
        result = self.run_ext("favourite")
        self.assertEqual(result.of_type("hud")[0]["text"], "♥ Favourited")
        self.assertTrue(any(re.search(r"set favorited|pLov", c) for c in self.told("Music")))
        self.scene(running=["Music"], tracks={"Music": music()}, controlFails=True)
        result = self.run_ext("favourite")
        self.assertIn("add it to your library", result.of_type("hud")[0]["text"])

    def test_favourite_on_spotify_says_it_cannot(self):
        self.scene(running=["Spotify"], tracks={"Spotify": spotify()})
        result = self.run_ext("favourite")
        self.assertIn("Spotify lets no script like a song", result.of_type("hud")[0]["text"])
        self.assertEqual([c for c in self.told("Spotify") if "set records to" not in c], [], "no control sent")

    def test_a_denied_control_is_an_error_naming_the_setting(self):
        self.scene(running=["Music"], tracks={"Music": music()}, deny="System Events")
        result = self.run_ext("next")
        self.assertIn("System Events", result.of_type("error")[0]["text"])
        self.assertIn("Automation", result.of_type("error")[0]["text"])

    # ---- Hyper+Space ----

    def test_now_playing_view_shows_the_track_and_its_controls(self):
        self.scene(running=["Music", "Spotify"], tracks={"Music": music(), "Spotify": spotify(state="paused")})
        detail = self.run_ext("now").detail
        self.assertTrue(detail["markdown"].startswith("# Voyager\n\nDaft Punk · Discovery"), detail["markdown"])
        self.assertIn("*Spotify also open: Midnight City – M83*", detail["markdown"])
        self.assertIn({"label": "Position", "text": "1:20 / 5:20"}, detail["metadata"])
        self.assertEqual([a["title"] for a in detail["actions"]], ["Pause", "Previous track", "Next track", "Favourite"])
        for action in detail["actions"]:
            self.assertActionShape(action)

    def test_now_playing_view_with_no_player_says_so(self):
        self.assertIn("No player is open", self.run_ext("now").detail["markdown"])
