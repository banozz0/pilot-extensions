#!/usr/bin/env python3
"""Now playing for Pilot: whatever the Mac is playing, as Control Center's Now Playing shows it (Music, Spotify, a
YouTube tab in Chrome, Safari or Zen, any app that tells macOS), as a menu-bar item ("♫ Artist – Title", a lone ♫ while
paused, nothing when nothing has played), its popover with the track, a progress meter and play or pause, previous and
next (Surfaces v2, Pilot story 165), and the same controls in Hyper+Space.

macOS keeps Now Playing in its MediaRemote framework, private and since macOS 15.4 closed to apps Apple did not sign;
osascript is Apple's, so a JavaScript for Automation script run by it reads MRNowPlayingRequest and sends
MRMediaRemoteSendCommand (2 play or pause, 4 next, 5 previous). No Apple events, so no Automation prompt, and no player
is ever started. Should a macOS update close that door, the item says so with a warn ♫ rather than vanishing.

Favourite is the one thing Now Playing can't do: it shows only while Music plays, and asks Music itself (its favorited
track property, «class pLov»), which raises macOS's Automation prompt once. A Spotify track carries its link instead.
"""
import json
import re
import shutil
import subprocess
import sys
import time

BUDGET = {"bar": 1.6, "detail": 1.6, "popover": 4.0, "control": 20.0}  # seconds; Pilot's limits 2, 2, 5 and 30
SETTLE = 0.4  # a player's new track shows this long after a control
REFRESH = {"playing": 5, "idle": 15}  # an open popover asks again; the bar is Pilot's 60 s
GLYPH = "♫"
BAR_ROOM = 24 - 2  # Pilot's bar text is 24 characters; "♫ " takes two
MUSIC = "com.apple.Music"
COMMANDS = {"playpause": 2, "next": 4, "previous": 5}  # MediaRemote's MRMediaRemoteCommand
VERBS = {"playpause": "play or pause", "next": "skip", "previous": "go back to", "favourite": "favourite"}
# A bar title without what only clutters it (Raycast's Spotify Player cleans the same way): (feat. …), […], remasters.
CLUTTER = re.compile(r"\s*(\((feat|ft|with)\.?\s[^)]*\)|\[[^\]]*\]|\([^)]*remaster[^)]*\)|\s-\s[^-]*remaster.*$)", re.I)

# The read, after an optional command (argv[0], then SETTLE s for the player to move): one JSON object, {} when nothing
# has played, {"error"} when Now Playing can't be read.
NOW_PLAYING = '''function run(argv) {
	ObjC.import('Foundation');
	if (!$.NSBundle.bundleWithPath('/System/Library/PrivateFrameworks/MediaRemote.framework/').load)
		return JSON.stringify({error: 'MediaRemote did not load'});
	const request = $.NSClassFromString('MRNowPlayingRequest');
	if (!request || request.isNil()) return JSON.stringify({error: 'no MRNowPlayingRequest'});
	if (argv.length) {
		ObjC.bindFunction('MRMediaRemoteSendCommand', ['bool', ['int', 'id']]);
		$.MRMediaRemoteSendCommand(Number(argv[0]), $());
		delay(%s);
	}
	const item = request.localNowPlayingItem;
	const info = item.isNil() ? item : item.nowPlayingInfo;
	if (info.isNil()) return JSON.stringify({});
	const field = name => {
		const value = info.valueForKey('kMRMediaRemoteNowPlayingInfo' + name);
		return value.isNil() ? null : value;
	};
	const stamp = field('Timestamp');
	const found = {playing: request.localIsPlaying, now: $.NSDate.date.timeIntervalSince1970,
		stamp: stamp ? stamp.timeIntervalSince1970 : null};
	['Title', 'Artist', 'Album', 'Duration', 'ElapsedTime', 'PlaybackRate', 'ContentItemIdentifier'].forEach(name => {
		const value = field(name);
		found[name] = value ? ObjC.unwrap(value) : null;
	});
	try {
		const client = request.localNowPlayingPlayerPath.client;
		found.app = ObjC.unwrap(client.displayName);
		found.bundle = ObjC.unwrap(client.bundleIdentifier);
	} catch (e) {}
	return JSON.stringify(found);
}''' % SETTLE

FAVOURITE = '''if application "Music" is running then
	tell application "Music"
		set f to not («class pLov» of current track)
		set «class pLov» of current track to f
		return («class pLov» of current track) as text
	end tell
end if'''


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")


class Trouble(Exception):
    """osascript did not answer: `why` is denied (macOS said no), slow (a prompt waits, or it hung), failed, or closed
    (Now Playing can't be read)."""

    def __init__(self, why):
        Exception.__init__(self, why)
        self.why = why


def osascript(args, deadline):
    binary = shutil.which("osascript") or "/usr/bin/osascript"
    left = deadline - time.monotonic()
    if left < 0.2:
        raise Trouble("slow")
    try:
        done = subprocess.run([binary] + args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=left)
    except subprocess.TimeoutExpired:
        raise Trouble("slow")
    except OSError:
        raise Trouble("failed")
    if done.returncode != 0:
        raise Trouble("denied" if b"-1743" in done.stderr else "failed")
    return done.stdout.decode("utf-8", "replace").strip()


def text(value):
    return value.strip() if isinstance(value, str) else ""


def seconds(value):
    return max(float(value), 0.0) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def now_playing(deadline, command=None):
    """What the Mac plays as a dict (app, bundle, playing, title, artist, album, duration and position in seconds or
    None, item), or None when nothing has played; `command`, one of COMMANDS, is sent first."""
    args = ["-l", "JavaScript", "-e", NOW_PLAYING] + ([str(COMMANDS[command])] if command else [])
    try:
        found = json.loads(osascript(args, deadline) or "{}")
    except ValueError:
        raise Trouble("failed")
    if not isinstance(found, dict):
        raise Trouble("failed")
    if found.get("error"):
        raise Trouble("closed")
    title, playing = text(found.get("Title")), found.get("playing") is True
    if not title and not playing:
        return None
    duration, elapsed = seconds(found.get("Duration")), seconds(found.get("ElapsedTime")) or 0.0
    stamp, now = seconds(found.get("stamp")), seconds(found.get("now"))
    if playing and stamp and now:
        rate = seconds(found.get("PlaybackRate")) or 1.0  # Now Playing's elapsed time is as of its stamp
        elapsed += (now - stamp) * rate
    if duration:
        elapsed = min(elapsed, duration)
    return {"app": text(found.get("app")) or "Now Playing", "bundle": text(found.get("bundle")), "playing": playing,
            "title": title, "artist": text(found.get("Artist")), "album": text(found.get("Album")),
            "duration": int(duration) if duration else None, "position": int(elapsed),
            "item": text(found.get("ContentItemIdentifier"))}


# ---- words ----

def clock(value):
    if value is None:
        return "–"
    hours, rest = divmod(value, 3600)
    return "%d:%02d:%02d" % (hours, rest // 60, rest % 60) if hours else "%d:%02d" % (rest // 60, rest % 60)


def fit(words, room):
    return words if len(words) <= room else words[:max(room - 1, 1)].rstrip() + "…"


def bar_text(track, mode):
    """"♫ Artist – Title" playing, the title cleaned, when it fits Pilot's 24 characters, else the title alone; a lone ♫
    paused."""
    if not track["playing"] or mode == "glyph":
        return GLYPH
    title = CLUTTER.sub("", track["title"]).strip() or track["title"] or track["app"]
    both = "%s – %s" % (track["artist"], title)
    if mode == "artist-title" and track["artist"] and track["title"] and len(both) <= BAR_ROOM:
        return "%s %s" % (GLYPH, both)
    return "%s %s" % (GLYPH, fit(title, BAR_ROOM))


def byline(track):
    return " · ".join(p for p in (track["artist"], track["album"]) if p)


def spoken(track):
    """"Title – Artist" for a HUD."""
    return " – ".join(p for p in (track["title"], track["artist"]) if p) or track["app"]


def share_link(track):
    """A Spotify track's spotify:track:ID as the web link anyone can open; None for anything else."""
    match = re.match(r"spotify:track:([A-Za-z0-9]+)\Z", track["item"])
    return "https://open.spotify.com/track/%s" % match.group(1) if match else None


def trouble_words(trouble):
    if trouble.why == "closed":
        return "This macOS doesn't share what's playing with Pilot (Now Playing is closed to it)."
    if trouble.why == "denied":
        return "macOS doesn't let Pilot control Music: allow it in System Settings › Privacy & Security › Automation › Pilot."
    if trouble.why == "slow":
        return "Now Playing did not answer in time; if macOS asks whether Pilot may control Music, choose Allow."
    return "Now Playing did not answer."


# ---- the views ----

def buttons(track, words=False):
    """Play or pause first, the main press; then previous, next, and Favourite while Music plays (a Spotify track: Copy
    link). The popover's are glyphs, `words` Hyper+Space's Cmd+K titles."""
    playing = track["playing"]
    if words:
        titles = ("Pause" if playing else "Play", "Previous track", "Next track", "Favourite or unfavourite")
    else:
        titles = ("⏸\ufe0e Pause" if playing else "▶\ufe0e Play", "⏮\ufe0e", "⏭\ufe0e", "♡ Favourite")
    commands = ("playpause", "previous", "next") + (("favourite",) if track["bundle"] == MUSIC else ())
    found = [{"type": "run", "command": command, "title": title} for command, title in zip(commands, titles)]
    link = share_link(track)
    if link:
        found.append({"type": "copy", "text": link, "title": "Copy link"})
    return found


def track_section(track):
    section = {"type": "section", "title": track["title"] or "Playing", "subtitle": byline(track), "aside": track["app"]}
    if not track["playing"]:
        section["asideNote"] = "Paused"
    inner = []
    if track["duration"]:
        inner.append({"type": "meter", "label": clock(track["position"]), "value": "/ %s" % clock(track["duration"]),
                      "percent": round(100.0 * track["position"] / track["duration"], 1)})
    section["blocks"] = inner + [{"type": "buttons", "actions": buttons(track)}]
    return section


def drawable(run, blocks):
    """`blocks` as this Pilot draws them: a note (Q138) is plain markdown where its run message's `pilot.blocks` lacks it."""
    pilot = run.get("pilot")
    listed = [k.strip() for k in str(pilot.get("blocks") or "").split(",")] if isinstance(pilot, dict) else []
    return [b if b["type"] != "note" or "note" in listed else {"type": "markdown", "text": b["text"]} for b in blocks]


def answer(run, budget):
    """(what plays or None, its blocks, the trouble or None)."""
    try:
        track = now_playing(time.monotonic() + budget)
    except Trouble as trouble:
        return None, drawable(run, [{"type": "note", "text": trouble_words(trouble), "icon": "info"}]), trouble
    if track is None:
        return None, [{"type": "markdown", "text": "Nothing is playing: play something in any app or browser tab."}], None
    return track, [track_section(track)], None


def bar(run):
    popover = run.get("surface") == "popover"
    track, blocks, trouble = answer(run, BUDGET["popover" if popover else "bar"])
    if popover:
        playing = track is not None and track["playing"]
        send({"type": "blocks", "blocks": blocks, "refresh": REFRESH["playing" if playing else "idle"]})
    elif trouble:
        send({"type": "bar", "text": GLYPH, "tone": "warn", "blocks": blocks})
    elif track is None:
        send({"type": "bar", "text": "", "blocks": blocks})  # no text hides the item
    else:
        mode = str((run.get("preferences") or {}).get("bar") or "artist-title")
        send({"type": "bar", "text": bar_text(track, mode), "blocks": blocks})


def detail(run):
    """Hyper+Space's Now playing: the track, how far in, and the controls on Return and Cmd+K."""
    track, blocks, trouble = answer(run, BUDGET["detail"])
    if trouble or track is None:
        send({"type": "detail", "markdown": blocks[0]["text"]})
        return
    lines = ["# %s" % (track["title"] or "Playing"), "", byline(track)]
    metadata = [{"label": "Playing in", "text": track["app"]},
                {"label": "State", "text": "Playing" if track["playing"] else "Paused"}]
    if track["duration"]:
        metadata.append({"label": "Position", "text": "%s / %s" % (clock(track["position"]), clock(track["duration"]))})
    send({"type": "detail", "markdown": "\n".join(lines), "metadata": metadata, "actions": buttons(track, words=True)})


# ---- the controls ----

def control(run, verb):
    """Sends `verb` to whatever plays, then says what plays now; Favourite asks Music, only while it is what plays."""
    deadline = time.monotonic() + BUDGET["control"]
    try:
        if verb == "favourite":
            track = now_playing(deadline)
            if track is None or track["bundle"] != MUSIC:
                send({"type": "hud", "text": "Favourite works while Music plays; %s can't be favourited from Pilot."
                                             % (track["app"] if track else "nothing")})
                return
            said = osascript(["-e", FAVOURITE], deadline)
            send({"type": "hud", "text": "♥ Favourited" if said == "true" else "♡ No longer a favourite"})
            return
        if now_playing(deadline) is None:
            send({"type": "hud", "text": "Nothing to %s: nothing has played yet." % VERBS[verb]})
            return
        track = now_playing(deadline, verb)
    except Trouble as trouble:
        if verb == "favourite" and trouble.why == "failed":
            send({"type": "hud", "text": "Music can't favourite this track: add it to your library first."})
        else:
            send({"type": "error", "text": trouble_words(trouble)})
        return
    if track is None:
        send({"type": "hud", "text": "Stopped"})
    elif track["playing"]:
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
