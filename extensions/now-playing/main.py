#!/usr/bin/env python3
"""Now playing for Pilot: what Music or Spotify plays as a menu-bar item ("♫ Artist – Title", a lone ♫ while paused,
nothing when no player is open), its popover with the track, a progress meter and play or pause, previous, next and
favourite (Surfaces v2, Pilot story 165), and the same four controls as commands in Hyper+Space.

Nothing ever starts a player. One osascript asks System Events which of Music and Spotify run, by process name only, so a
Mac without Spotify never compiles a script naming it; only then does a second osascript tell the running ones, each
tell inside `if application "X" is running`, so a player quit in between is not started again either. The tell reads
the track and, for a control, sends that player its own command (playpause, next track, previous track, Music's
favorited): the same target the read already needs, so macOS asks once per player (Automation) and never again.

With both players open the one playing wins; with neither or both playing, the Prefer setting (by default the last one
seen playing, kept in state.json) breaks the tie.

Apple Music on macOS 26 hides a streamed track that is not in the library from scripts (`current track` errors; Apple
developer forums thread 801357): the bar then says ♫ Music and the popover says why, the controls still work. Spotify's
dictionary has no like or save, so its Favourite slot copies the song's link instead.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

PLAYERS = ("Music", "Spotify")
FIELDS = ("player", "state", "title", "artist", "album", "duration", "position", "favourite", "key")
UNIT, RECORD = "\x1f", "\x1e"  # AppleScript's character id 31 and 30: no title holds them
BUDGET = {"bar": 1.6, "detail": 1.6, "popover": 4.0, "control": 20.0}  # seconds; Pilot's limits 2, 2, 5 and 30
SETTLE = 0.4  # a player's new track shows this long after a control
REFRESH = {"playing": 5, "idle": 15}  # an open popover asks again; the bar is Pilot's 60 s
STATE = "state.json"
VERBS = {"playpause": "play or pause", "next": "skip", "previous": "go back to", "favourite": "favourite"}  # the controls
GLYPH = "♫"
BAR_ROOM = 24 - 2  # Pilot's bar text is 24 characters; "♫ " takes two
SETTINGS = "System Settings › Privacy & Security › Automation › Pilot"
# A bar title without what only clutters it (Raycast's Spotify Player cleans the same way): (feat. …), […], remasters.
CLUTTER = re.compile(r"\s*(\((feat|ft|with)\.?\s[^)]*\)|\[[^\]]*\]|\([^)]*remaster[^)]*\)|\s-\s[^-]*remaster.*$)", re.I)

RUNNING = '''tell application "System Events" to set found to name of every process whose name is "Music" or name is "Spotify"
set AppleScript's text item delimiters to character id 31
return found as text'''

JOIN = '''on joined(fields)
	set AppleScript's text item delimiters to character id 31
	set joinedText to fields as text
	set AppleScript's text item delimiters to ""
	return joinedText
end joined'''

# Each player's read, as one record: state, title, artist, album, duration and position in whole seconds, favourited
# (Music's «class pLov», named favorited since macOS 14 and loved before), and Spotify's spotify:track: id.
READ = {
    "Music": '''if application "Music" is running then
	tell application "Music"
		set {s, t, a, al, d, p, f} to {player state as text, "", "", "", "", "", ""}
		if s is not "stopped" then
			try
				set t to name of current track
				try
					set a to artist of current track
				end try
				try
					set al to album of current track
				end try
				try
					set d to (round (duration of current track)) as text
				end try
				try
					set f to («class pLov» of current track) as text
				end try
			on error
				try
					set t to current stream title
				end try
			end try
			try
				set p to (round player position) as text
			end try
		end if
	end tell
	set end of records to my joined({"Music", s, t, a, al, d, p, f, ""})
end if''',
    "Spotify": '''if application "Spotify" is running then
	tell application "Spotify"
		set {s, t, a, al, d, p, k} to {player state as text, "", "", "", "", "", ""}
		if s is not "stopped" then
			try
				set t to name of current track
				set a to artist of current track
				set al to album of current track
				set d to (duration of current track) as text
				set k to spotify url of current track
			end try
			try
				set p to (round player position) as text
			end try
		end if
	end tell
	set end of records to my joined({"Spotify", s, t, a, al, d, p, "", k})
end if''',
}

# Each control, sent inside its player's tell, the same words for both. Only Music can favourite (Spotify's dictionary
# has no like); it answers what the track is now.
CONTROL = {"playpause": "playpause", "next": "next track", "previous": "previous track"}
FAVOURITE = '''set f to not («class pLov» of current track)
		set «class pLov» of current track to f
		return («class pLov» of current track) as text'''
STREAMING = "macOS keeps a streamed track that is not in your library hidden from Pilot; add it to your library to see it here."


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")


class Trouble(Exception):
    """osascript did not answer: `why` is denied (macOS said no), slow (a prompt waits, or it hung) or failed; `who` the
    app it was telling, when known."""

    def __init__(self, why, who=None):
        Exception.__init__(self, why)
        self.why, self.who = why, who


def told(player, body):
    """`body` inside `player`'s tell, sent only while it runs, so nothing starts it."""
    return 'if application "%s" is running then\n\ttell application "%s"\n\t\t%s\n\tend tell\nend if' % (player, player, body)


def osascript(source, deadline, who=None):
    binary = shutil.which("osascript") or "/usr/bin/osascript"
    left = deadline - time.monotonic()
    if left < 0.2:
        raise Trouble("slow")
    try:
        done = subprocess.run([binary, "-e", source], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=left)
    except subprocess.TimeoutExpired:
        raise Trouble("slow")
    except OSError:
        raise Trouble("failed")
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace")
        who = who or next((name for name in PLAYERS if name in said), None)
        raise Trouble("denied" if "-1743" in said else "failed", who)
    return done.stdout.decode("utf-8", "replace").rstrip("\n")


def running(deadline):
    """The players open now, by process name; a missing one is never named to AppleScript."""
    found = osascript(RUNNING, deadline, who="System Events").split(UNIT)
    return [name for name in PLAYERS if name in found]


def number(text):
    try:
        return max(int(text), 0)
    except (TypeError, ValueError):
        return None


def spotify_seconds(duration, position):
    """Spotify's track duration in seconds: its dictionary says seconds, yet it answers milliseconds (Raycast's and
    others' reports); a figure a thousand times past where the track is, and past 30 000, is milliseconds."""
    if duration and duration >= 1000 * max(position or 0, 30):
        return duration // 1000
    return duration


def read(names, deadline, first=None):
    """Each running player's track as a dict: player, state (playing, paused or stopped), title, artist, album,
    duration and position in seconds or None, favourite True, False or None, key. `first`, a control, runs in the same
    script SETTLE s before the read, so its new track shows."""
    if not names:
        return []
    ahead = [first, "delay %s" % SETTLE] if first else []
    source = "\n".join([JOIN, "set records to {}"] + ahead + [READ[name] for name in names]
                       + ["set AppleScript's text item delimiters to character id 30", "return records as text"])
    tracks = []
    for record in osascript(source, deadline).split(RECORD):
        values = (record.split(UNIT) + [""] * len(FIELDS))[:len(FIELDS)]
        track = dict(zip(FIELDS, (v.strip() for v in values)))
        if track["player"] not in PLAYERS:
            continue
        state = track["state"].lower()
        track["state"] = "playing" if state in ("playing", "fast forwarding", "rewinding") else \
            "stopped" if state == "stopped" else "paused"
        track["duration"], track["position"] = number(track["duration"]), number(track["position"])
        if track["player"] == "Spotify":
            track["duration"] = spotify_seconds(track["duration"], track["position"])
        track["favourite"] = {"true": True, "false": False}.get(track["favourite"])
        tracks.append(track)
    return tracks


# ---- which player ----

def load_state(run):
    folder = (run.get("paths") or {}).get("cache")
    if not isinstance(folder, str) or not folder:
        return None, {}
    try:
        with open(os.path.join(folder, STATE), encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    return folder, state if isinstance(state, dict) else {}


def remember(folder, state, track):
    """Keeps the player last seen playing, for Prefer's default."""
    if not folder or track is None or track["state"] != "playing" or state.get("last") == track["player"]:
        return
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, STATE)
        temp = "%s.%d.tmp" % (path, os.getpid())
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(dict(state, last=track["player"]), f)
        os.replace(temp, path)
    except OSError:
        pass


def preferred(run, state):
    """The player that wins a tie: the Prefer setting, else the last one seen playing, else Music."""
    pick = str((run.get("preferences") or {}).get("player") or "auto")
    if pick in ("music", "spotify"):
        return pick.capitalize()
    return state.get("last") if state.get("last") in PLAYERS else "Music"


def choose(tracks, first):
    """The track to show: one playing over one paused, then `first`'s; None when no player has a track loaded."""
    loaded = [t for t in tracks if t["state"] != "stopped"]
    if not loaded:
        return None
    return min(loaded, key=lambda t: (t["state"] != "playing", t["player"] != first))


# ---- words ----

def clock(seconds):
    if seconds is None:
        return "–"
    hours, rest = divmod(seconds, 3600)
    return "%d:%02d:%02d" % (hours, rest // 60, rest % 60) if hours else "%d:%02d" % (rest // 60, rest % 60)


def fit(text, room):
    return text if len(text) <= room else text[:max(room - 1, 1)].rstrip() + "…"


def bar_text(track, mode):
    """"♫ Artist – Title" playing, the title cleaned, when it fits Pilot's 24 characters, else the title alone; a lone ♫
    paused."""
    if track["state"] != "playing" or mode == "glyph":
        return GLYPH
    title = CLUTTER.sub("", track["title"]).strip() or track["title"] or track["player"]
    both = "%s – %s" % (track["artist"], title)
    if mode == "artist-title" and track["artist"] and track["title"] and len(both) <= BAR_ROOM:
        return "%s %s" % (GLYPH, both)
    return "%s %s" % (GLYPH, fit(title, BAR_ROOM))


def byline(track):
    return " · ".join(p for p in (track["artist"], track["album"]) if p)


def spoken(track):
    """"Title – Artist" for a HUD."""
    return " – ".join(p for p in (track["title"], track["artist"]) if p) or "a track %s won't name" % track["player"]


def share_link(track):
    """Spotify's spotify:track:ID as the web link anyone can open; None for an ad, a local file or a podcast."""
    match = re.match(r"spotify:track:([A-Za-z0-9]+)\Z", track["key"])
    return "https://open.spotify.com/track/%s" % match.group(1) if match else None


# ---- the popover ----

def control_action(command, track, title):
    return {"type": "run", "command": command, "title": title, "context": {"player": track["player"]}}


def buttons(track, words=False):
    """Play or pause first, the main press; then previous, next, and favourite (Spotify: Copy link). The popover's are
    glyphs, `words` Hyper+Space's Cmd+K titles."""
    playing, liked = track["state"] == "playing", track["favourite"]
    if words:
        titles = ("Pause" if playing else "Play", "Previous track", "Next track", "Unfavourite" if liked else "Favourite")
    else:
        titles = ("⏸\ufe0e Pause" if playing else "▶\ufe0e Play", "⏮\ufe0e", "⏭\ufe0e", "♥ Favourite" if liked else "♡ Favourite")
    found = [control_action(command, track, title) for command, title in zip(("playpause", "previous", "next"), titles)]
    if track["player"] == "Music":
        if liked is not None:
            found.append(control_action("favourite", track, titles[3]))
    else:
        link = share_link(track)
        if link:
            found.append({"type": "copy", "text": link, "title": "Copy link"})
    return found


def track_section(track):
    paused = track["state"] != "playing"
    if not track["title"]:
        inner = [{"type": "note", "text": STREAMING, "icon": "info"}]
        section = {"type": "section", "title": "%s is streaming" % track["player"], "aside": track["player"]}
    else:
        inner = []
        section = {"type": "section", "title": track["title"],
                   "subtitle": byline(track), "aside": track["player"]}
        if track["duration"]:
            position = min(track["position"] or 0, track["duration"])
            inner.append({"type": "meter", "label": clock(position), "value": "/ %s" % clock(track["duration"]),
                          "percent": round(100.0 * position / track["duration"], 1)})
    if paused:
        section["asideNote"] = "Paused"
    section["blocks"] = inner + [{"type": "buttons", "actions": buttons(track)}]
    return section


def also_open(tracks, shown):
    """The other player with a track loaded too, and its line: [(track, "Spotify also open: Title – Artist")]."""
    return [(t, "%s also open: %s" % (t["player"], spoken(t) if t["title"] else "streaming"))
            for t in tracks if t is not shown and t["state"] != "stopped"]


def others(tracks, shown):
    """A quiet note for each other player with a track loaded."""
    return [{"type": "note", "text": line, "aside": "playing" if t["state"] == "playing" else "paused", "icon": "info"}
            for t, line in also_open(tracks, shown)]


def trouble_note(trouble, names):
    who = trouble.who or (names[0] if len(names) == 1 else "the player")
    if trouble.why == "denied":
        text = "macOS doesn't let Pilot read %s: allow it in %s." % (who, SETTINGS)
    elif trouble.why == "slow":
        text = "Waiting on %s: if macOS asks whether Pilot may control it, choose Allow." % who
    else:
        text = "%s did not answer." % who
    return {"type": "note", "text": text, "icon": "info"}


def drawable(run, blocks):
    """`blocks` as this Pilot draws them: a note (Q138) is plain markdown where its run message's `pilot.blocks` lacks it."""
    pilot = run.get("pilot")
    listed = [k.strip() for k in str(pilot.get("blocks") or "").split(",")] if isinstance(pilot, dict) else []
    if "note" in listed:
        return blocks
    found = []
    for block in blocks:
        if block["type"] == "note":
            block = {"type": "markdown", "text": block["text"]}
        elif block["type"] == "section":
            block = dict(block, blocks=drawable(run, block["blocks"]))
        found.append(block)
    return found


def now_playing(run, budget):
    """(the track shown or None, every track, the blocks, the trouble or None)."""
    deadline = time.monotonic() + budget
    folder, state = load_state(run)
    names = []
    try:
        names = running(deadline)
        tracks = read(names, deadline)
    except Trouble as trouble:
        return None, [], [trouble_note(trouble, names)], trouble
    track = choose(tracks, preferred(run, state))
    remember(folder, state, track)
    if track is None:
        text = "Music or Spotify is open with nothing loaded." if names else "No player is open: start Music or Spotify."
        return None, tracks, [{"type": "markdown", "text": text}], None
    return track, tracks, [track_section(track)] + others(tracks, track), None


def bar(run):
    popover = run.get("surface") == "popover"
    track, _, blocks, trouble = now_playing(run, BUDGET["popover" if popover else "bar"])
    blocks = drawable(run, blocks)
    if popover:
        playing = track is not None and track["state"] == "playing"
        send({"type": "blocks", "blocks": blocks, "refresh": REFRESH["playing" if playing else "idle"]})
        return
    if trouble:
        send({"type": "bar", "text": GLYPH, "tone": "warn", "blocks": blocks})
    elif track is None:
        send({"type": "bar", "text": "", "blocks": blocks})  # no text hides the item
    else:
        mode = str((run.get("preferences") or {}).get("bar") or "artist-title")
        send({"type": "bar", "text": bar_text(track, mode), "blocks": blocks})


def detail(run):
    """Hyper+Space's Now playing: the track, how far in, and the controls on Return and Cmd+K."""
    track, tracks, blocks, trouble = now_playing(run, BUDGET["detail"])
    if trouble or track is None:
        send({"type": "detail", "markdown": blocks[0]["text"]})
        return
    if track["title"]:
        lines = ["# %s" % track["title"], "", byline(track)]
    else:
        lines = ["# %s is streaming" % track["player"], "", STREAMING]
    for _, line in also_open(tracks, track):
        lines += ["", "*%s*" % line]
    metadata = [{"label": "Player", "text": track["player"]},
                {"label": "State", "text": "Playing" if track["state"] == "playing" else "Paused"}]
    if track["duration"]:
        metadata.append({"label": "Position", "text": "%s / %s" % (clock(track["position"]), clock(track["duration"]))})
    send({"type": "detail", "markdown": "\n".join(lines), "metadata": metadata, "actions": buttons(track, words=True)})


# ---- the controls ----

def control(run, verb):
    """Sends `verb` to the player the popover showed (its context), else to the one shown now, then says what plays."""
    deadline = time.monotonic() + BUDGET["control"]
    folder, state = load_state(run)
    names = []
    try:
        names = running(deadline)
        context = run.get("context") if isinstance(run.get("context"), dict) else {}
        player = context.get("player")
        if player in PLAYERS and player not in names:
            send({"type": "hud", "text": "%s is no longer open." % player})
            return
        if player not in PLAYERS:
            track = choose(read(names, deadline), preferred(run, state))
            player = track["player"] if track else None
        if player is None:
            send({"type": "hud", "text": "Nothing to %s: no player has a track." % VERBS[verb]})
            return
        if verb == "favourite":
            if player != "Music":
                send({"type": "hud", "text": "Spotify lets no script like a song: use Copy link, or like it in Spotify."})
                return
            said = osascript(told(player, FAVOURITE), deadline)
            send({"type": "hud", "text": "♥ Favourited" if said == "true" else "♡ No longer a favourite"})
            return
        track = next(iter(read([player], deadline, first=told(player, CONTROL[verb]))), None)
    except Trouble as trouble:
        if verb == "favourite" and trouble.why == "failed":
            send({"type": "hud", "text": "Music can't favourite this track: add it to your library first."})
        else:
            send({"type": "error", "text": trouble_note(trouble, names)["text"]})
        return
    remember(folder, state, track)
    if track is None or track["state"] == "stopped":
        send({"type": "hud", "text": "%s stopped" % player})
    elif track["state"] == "playing":
        send({"type": "hud", "text": "▶\ufe0e %s" % spoken(track)})
    else:
        send({"type": "hud", "text": "⏸\ufe0e Paused"})


def main():
    try:
        run = json.loads(sys.stdin.readline() or "{}")
    except ValueError:
        run = {}
    command = run.get("command")
    if command == "bar":
        bar(run)
    elif command == "now":
        detail(run)
    elif command in VERBS:
        control(run, command)
    else:
        send({"type": "error", "text": "Now playing has no command %s" % command})


if __name__ == "__main__":
    main()
