#!/usr/bin/env python3
"""Easy-CD folders for Pilot: easy-cd's (`ecd`) pinned folders, then its recent ones, read only.

easy-cd keeps both in its state folder, $XDG_STATE_HOME/ecd or ~/.local/state/ecd: `pins` is one folder per line in
pin order, `visits` one `<unix time>\t<folder>` line per landing. Recents are ranked as ecd ranks them (zoxide-style
frecency: a visit weighs 4 within the hour, 2 within the day, 1/2 within the week, 1/4 after), pins and folders that
no longer exist left out. Nothing is ever written to easy-cd's files. The hidden open-terminal command opens a window
through `open`, so the program never holds it.
"""
import itertools
import json
import os
import subprocess
import sys
import time

RECENTS = 30
GHOSTTY = ["/Applications/Ghostty.app", os.path.expanduser("~/Applications/Ghostty.app")]


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")


def state_dir():
    xdg = os.environ.get("XDG_STATE_HOME", "")
    return os.path.join(xdg if os.path.isabs(xdg) else os.path.expanduser("~/.local/state"), "ecd")


def read_lines(path):
    """The file's lines, or None when it does not exist."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().split("\n")
    except FileNotFoundError:
        return None
    except OSError:
        return []


def pins(lines):
    seen, out = set(), []
    for d in lines or []:
        if d and d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def recents(lines, now):
    """[(folder, last visit)] best frecency first, as ecd ranks them (frecency() and visited() in easy-cd's
    internal/picker/state.go: keep the weights and tie-break in step with it); gone folders are left to the caller."""
    score, last = {}, {}
    for line in lines or []:
        stamp, sep, d = line.partition("\t")
        if not sep or not d:
            continue
        try:
            t = int(stamp)
        except ValueError:
            continue
        age = now - t
        score[d] = score.get(d, 0) + (4 if age < 3600 else 2 if age < 86400 else 0.5 if age < 7 * 86400 else 0.25)
        last[d] = max(last.get(d, t), t)
    return [(d, last[d]) for d in sorted(score, key=lambda d: (-score[d], d))]


def tilde(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def ago(stamp, now):
    s = max(0, now - stamp)
    if s < 60:
        return "just now"
    if s < 3600:
        return "%d min ago" % (s // 60)
    if s < 86400:
        return "%d h ago" % (s // 3600)
    if s < 2 * 86400:
        return "yesterday"
    if s < 7 * 86400:
        return "%d days ago" % (s // 86400)
    return time.strftime("%-d %b %Y", time.localtime(stamp))


def terminal_app(preferences):
    """The chosen app's path, else Ghostty when installed, else Terminal."""
    chosen = preferences.get("terminal")
    if isinstance(chosen, str) and chosen and os.path.isdir(chosen):
        return chosen
    return next((g for g in GHOSTTY if os.path.isdir(g)), "Terminal")


def app_name(app):
    return os.path.basename(app)[:-4] if app.endswith(".app") else app


def row(folder, icon, note, terminal):
    return {"id": folder, "title": os.path.basename(folder) or folder, "subtitle": "%s · %s" % (tilde(folder), note),
            "icon": icon, "keywords": tilde(folder),
            "actions": [{"type": "run", "command": "open-terminal", "title": "Open in %s" % terminal,
                         "context": {"path": folder}},
                        {"type": "open", "url": folder, "title": "Open in Finder"},
                        {"type": "copy", "text": folder, "title": "Copy path"}]}


def list_folders(run):
    state = state_dir()
    pin_lines, visit_lines = read_lines(os.path.join(state, "pins")), read_lines(os.path.join(state, "visits"))
    now = int(time.time())
    pinned = pins(pin_lines)
    terminal = app_name(terminal_app(run.get("preferences") or {}))
    items = [row(d, "📌", "pinned", terminal) for d in pinned]
    recent = (r for r in recents(visit_lines, now) if r[0] not in pinned and os.path.isdir(r[0]))
    items += [row(d, "🕘", ago(last, now), terminal) for d, last in itertools.islice(recent, RECENTS)]
    if not items:
        none = pin_lines is None and visit_lines is None
        items = [{"title": "easy-cd has no pins or recent folders yet" if none else "easy-cd's folders are all gone",
                  "subtitle": "Run ecd in a terminal: Enter lands in a folder, Ctrl+P pins it" if none
                  else "The folders it pinned or visited no longer exist", "icon": "📂"}]
    send({"type": "list", "items": items})


def open_terminal(run):
    """Opens a terminal window in the folder the context names: Ghostty as a new window there, another terminal by
    opening the folder with it. The window belongs to the terminal, never to this program."""
    context = run.get("context") if isinstance(run.get("context"), dict) else {}
    folder = context.get("path")
    if not (isinstance(folder, str) and os.path.isdir(folder)):
        send({"type": "error", "text": "got no folder that exists"})
        return
    app = terminal_app(run.get("preferences") or {})
    if app_name(app).lower() == "ghostty":
        arguments = ["-na", app, "--args", "--working-directory=" + folder, "--window-save-state=never"]
    else:
        arguments = ["-a", app, folder]
    done = subprocess.run(["open"] + arguments, capture_output=True, text=True, timeout=10)
    if done.returncode != 0:
        send({"type": "error", "text": (done.stderr.strip() or "open failed")[:160]})
        return
    send({"type": "hud", "text": "%s in %s" % (os.path.basename(folder) or folder, app_name(app))})


def main():
    try:
        run = json.loads(sys.stdin.readline())
    except ValueError:
        send({"type": "error", "text": "got a run message it could not read"})
        return
    command = run.get("command")
    if command == "folders":
        list_folders(run)
    elif command == "open-terminal":
        open_terminal(run)
    else:
        send({"type": "error", "text": "has no command %s" % command})


if __name__ == "__main__":
    main()
