#!/usr/bin/env python3
"""To-do for Pilot: a plain list in Hyper+Space, a menu-bar item counting the open ones with a popover to add, tick off
and delete, and a page listing them all (Surfaces v2, Pilot story 165).

The list is one JSON file, todos.json in the extension's data folder (`paths.data`): no account, no sync. Every change
reads it, changes it and writes it back under a lock (todos.lock), through a temp file and a rename, so a popover's add
and a bar's read never see half a file. A to-do is named by its id, never by its place in the list.
"""
import fcntl
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager

STORE = "todos.json"
LOCK = "todos.lock"
LONGEST = 500  # characters kept of a to-do's text
MOST_ROWS = 50  # Pilot draws 50 rows of a list block at most
ID_LENGTH = 8
OPEN, DONE = "⬜", "✅"  # the checkbox


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def ago(seconds):
    if seconds < 60:
        return "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return "%d %s ago" % (seconds // size, unit)


# ---- the store ----

def data_dir(run):
    """Pilot's data folder for this extension, which it makes on the first run."""
    folder = (run.get("paths") or {}).get("data")
    if not isinstance(folder, str) or not folder:
        send({"type": "error", "text": "Pilot sent no data folder"})
        sys.exit(0)
    os.makedirs(folder, exist_ok=True)
    return folder


def load(folder):
    """The to-dos, oldest first; none when the file is missing or unreadable."""
    try:
        with open(os.path.join(folder, STORE), encoding="utf-8") as f:
            items = json.load(f).get("items")
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(items, list):
        return []
    return [t for t in items if isinstance(t, dict) and isinstance(t.get("id"), str) and isinstance(t.get("text"), str)]


def save(folder, items):
    path = os.path.join(folder, STORE)
    temp = "%s.%d.tmp" % (path, os.getpid())
    with open(temp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "items": items}, f, ensure_ascii=False, indent=1)
    os.replace(temp, path)


@contextmanager
def changing(folder):
    """The to-dos to change in place, written back as the block ends, one writer at a time."""
    with open(os.path.join(folder, LOCK), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        items = load(folder)
        yield items
        save(folder, items)


def split(items):
    """(open ones, newest first; done ones, latest done first)."""
    open_ = sorted((t for t in items if not t.get("done")), key=lambda t: -(t.get("created") or 0))
    done = sorted((t for t in items if t.get("done")), key=lambda t: -(t.get("doneAt") or 0))
    return open_, done


# ---- rows ----

def row(todo, now, surface=True):
    """A to-do as a row: inline ⬜/✅ ticks it off (or back on) and ✕ deletes it, Cmd+K copies its text. Pilot runs a row's
    first action on a click, so on a surface a click shows it (Sven, 2026-10-12: a click ticked it off); in Hyper+Space
    Return ticks it."""
    toggle, subtitle, icon = ticking(todo, now)
    actions = buttons(todo, toggle)
    inline = actions[:2]
    if surface:
        actions = [{"type": "push", "command": "show", "title": "Show", "context": {"id": todo["id"]}}] + actions
    return {"id": todo["id"], "title": todo["text"], "subtitle": subtitle, "icon": icon, "actions": actions, "inline": inline}


def ticking(todo, now):
    """Its Mark done (or Mark not done), its subtitle and its checkbox."""
    if todo.get("done"):
        command, title, icon, subtitle = "undone", "Mark not done", DONE, "done %s" % ago(now - (todo.get("doneAt") or now))
    else:
        command, title, icon, subtitle = "done", "Mark done", OPEN, "added %s" % ago(now - (todo.get("created") or now))
    return {"type": "run", "command": command, "title": title, "icon": icon, "context": {"id": todo["id"]}}, subtitle, icon


def buttons(todo, toggle):
    delete = {"type": "run", "command": "delete", "title": "Delete", "icon": "xmark", "context": {"id": todo["id"]}}
    return [toggle, delete, {"type": "copy", "text": todo["text"], "title": "Copy text"}]


def rows(todos, now, most=MOST_ROWS):
    return [row(t, now) for t in todos[:most]]


def add_input():
    return {"type": "input", "placeholder": "Add a to-do",
            "action": {"type": "run", "command": "add", "title": "Add"}}


def done_blocks(done, now):
    """The done ones, then Delete them (asked first)."""
    count = plural(len(done), "done to-do")
    return [{"type": "list", "title": "Done", "items": rows(done, now)},
            {"type": "buttons", "actions": [{"type": "run", "command": "clear-done", "title": "Delete %s" % count,
                                             "icon": "xmark", "ask": {"confirm": "Delete %s?" % count},
                                             "destructive": True}]}]


# ---- surfaces ----

def popover_blocks(open_, done, now):
    """Add a to-do, the open ones, the done ones folded under N done."""
    blocks = [add_input()]
    found = rows(open_, now, MOST_ROWS - 1)
    if done:
        found.append({"title": "%d done ›" % len(done), "icon": DONE,
                      "actions": [{"type": "push", "command": "done-list", "title": "Show done to-dos"}]})
    if found:
        blocks.append({"type": "list", "items": found})
    if not open_:
        blocks.append({"type": "markdown", "text": "Nothing to do." if not done else "All done."})
    return blocks


def bar(run):
    open_, done = split(load(data_dir(run)))
    blocks = popover_blocks(open_, done, time.time())
    if run.get("surface") == "popover":
        send({"type": "blocks", "blocks": blocks})
    else:
        send({"type": "bar", "text": "☑️ %d" % len(open_), "blocks": blocks})


def page(run):
    """Every to-do: counts, Add a to-do, the open ones, the done ones, Delete the done ones."""
    now = time.time()
    open_, done = split(load(data_dir(run)))
    today = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    blocks = [{"type": "stats", "items": [
        {"label": "Open", "value": str(len(open_))},
        {"label": "Done today", "value": str(sum(1 for t in done if (t.get("doneAt") or 0) >= today)), "tone": "fine"},
        {"label": "Done", "value": str(len(done))}]}, add_input()]
    if open_:
        blocks.append({"type": "list", "title": "To do", "items": rows(open_, now)})
    else:
        blocks.append({"type": "markdown", "text": "Nothing to do."})
    if done:
        blocks += done_blocks(done, now)
    send({"type": "blocks", "blocks": blocks})


def done_list(run):
    done = split(load(data_dir(run)))[1]
    if not run.get("surface"):
        send({"type": "hud", "text": plural(len(done), "done to-do")})
        return
    if not done:
        send({"type": "blocks", "blocks": [{"type": "markdown", "text": "Nothing done yet."}]})
        return
    send({"type": "blocks", "blocks": done_blocks(done, time.time())})


def show(run):
    """One to-do, its whole text and when it was added or done, then Mark done, Delete and Copy text."""
    todo_id = (run.get("context") or {}).get("id")
    todo = next((t for t in load(data_dir(run)) if t["id"] == todo_id), None)
    if todo is None:
        send({"type": "blocks", "blocks": [{"type": "markdown", "text": "That to-do is gone."}]})
        return
    toggle, subtitle, icon = ticking(todo, time.time())
    send({"type": "blocks", "blocks": [{"type": "markdown", "text": "%s %s\n\n%s" % (icon, todo["text"], subtitle)},
                                       {"type": "buttons", "actions": buttons(todo, toggle)}]})


# ---- Hyper+Space ----

def list_todos(run):
    """The search typed is a new to-do: Add “…” first, then the to-dos whose text holds it; empty, every open to-do,
    then the done ones."""
    query = (run.get("query") or "").strip()
    items = load(data_dir(run))
    found = []
    if query:
        text = query[:LONGEST]
        found.append({"title": "Add “%s”" % text, "icon": "➕", "subtitle": "New to-do",
                     "actions": [{"type": "run", "command": "add", "title": "Add", "context": {"text": text}}]})
        words = query.lower().split()
        items = [t for t in items if all(w in t["text"].lower() for w in words)]
    open_, done = split(items)
    found += [row(t, time.time(), surface=False) for t in open_ + done]
    if not found:
        found = [{"title": "Nothing to do", "subtitle": "Type a to-do and press Return to add it", "icon": "☑️"}]
    send({"type": "list", "items": found})


# ---- changes ----

def typed(run):
    """The to-do's text: what was typed in Add a to-do, else what the row handed on."""
    text = run.get("input")
    if not isinstance(text, str) or not text.strip():
        text = (run.get("context") or {}).get("text")
    text = " ".join(text.split()) if isinstance(text, str) else ""
    return text[:LONGEST]


def add(run):
    text = typed(run)
    if not text:
        send({"type": "error", "text": "type a to-do first"})
        return
    folder = data_dir(run)
    with changing(folder) as items:
        ids = {t["id"] for t in items}
        new = uuid.uuid4().hex[:ID_LENGTH]
        while new in ids:
            new = uuid.uuid4().hex[:ID_LENGTH]
        items.append({"id": new, "text": text, "done": False, "created": time.time()})
    send({"type": "hud", "text": "Added: %s" % text})


def change(run, verb):
    """Marks the to-do the context names done or not done, or deletes it."""
    todo_id = (run.get("context") or {}).get("id")
    if not isinstance(todo_id, str):
        send({"type": "error", "text": "was not told which to-do"})
        return
    with changing(data_dir(run)) as items:
        todo = next((t for t in items if t["id"] == todo_id), None)
        if todo is None:
            send({"type": "error", "text": "that to-do is gone"})
            return
        if verb == "delete":
            items.remove(todo)
        elif verb == "done":
            todo["done"], todo["doneAt"] = True, time.time()
        else:
            todo["done"] = False
            todo.pop("doneAt", None)
    send({"type": "hud", "text": {"delete": "Deleted", "done": "Done", "undone": "Back on the list"}[verb]})


def clear_done(run):
    with changing(data_dir(run)) as items:
        gone = [t for t in items if t.get("done")]
        items[:] = [t for t in items if not t.get("done")]
    send({"type": "hud", "text": "Deleted %s" % plural(len(gone), "done to-do")})


def main():
    try:
        run = json.loads(sys.stdin.readline())
    except ValueError:
        send({"type": "error", "text": "got a run message it could not read"})
        return
    command = run.get("command")
    if command == "todo":
        list_todos(run)
    elif command == "bar":
        bar(run)
    elif command == "page":
        page(run)
    elif command == "done-list":
        done_list(run)
    elif command == "show":
        show(run)
    elif command == "add":
        add(run)
    elif command in ("done", "undone", "delete"):
        change(run, command)
    elif command == "clear-done":
        clear_done(run)
    else:
        send({"type": "error", "text": "has no command %s" % command})


if __name__ == "__main__":
    main()
