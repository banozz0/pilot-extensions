#!/usr/bin/env python3
"""Claude Sessions for Pilot: recent Claude Code sessions from ~/.claude/projects/*/*.jsonl, read only.

Each session's folder, title and times come from the first 512 KB and the last 128 KB of its transcript, cached in
paths.cache by file size and modification time, so the list stays fast however long a session ran. A title is the
session's own AI title when it has one, else its first prompt cut to one short line; no more of any prompt is shown.
"""
import glob
import json
import os
import re
import shlex
import sys
import time

LIMIT = 60
HEAD, TAIL = 512 * 1024, 128 * 1024
TITLE_LENGTH = 80
COUNT_LIMIT = 64 * 1024 * 1024
TAG = re.compile(r"<[^>]{1,200}>")
COMMAND = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")


def projects():
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def transcripts():
    """The newest session files first, LIMIT of them."""
    found = []
    for path in glob.glob(os.path.join(projects(), "*", "*.jsonl")):
        if os.path.basename(path).startswith("agent-"):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        found.append((st.st_mtime, st.st_size, path))
    found.sort(reverse=True)
    return found[:LIMIT]


def one_line(text):
    text = " ".join(TAG.sub(" ", text).split())
    return text if len(text) <= TITLE_LENGTH else text[:TITLE_LENGTH - 1].rstrip() + "…"


def prompt_text(entry):
    """The words of a prompt Sven typed; None for a tool result, a meta line or a local command's output."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    if not isinstance(content, str) or not content.strip():
        return None
    command = COMMAND.search(content)
    if command:
        args = ARGS.search(content)
        return one_line("%s %s" % (command.group(1), args.group(1) if args else ""))
    if content.lstrip().startswith(("<local-command-stdout>", "<local-command-stderr>", "Caveat:")):
        return None
    line = one_line(content)
    return line or None


def entries(chunk, skip_first):
    lines = chunk.split(b"\n")
    if skip_first:
        lines = lines[1:]
    for raw in lines:
        if not raw.startswith(b"{"):
            continue
        try:
            entry = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def read_meta(path, size):
    meta = {"id": os.path.splitext(os.path.basename(path))[0], "cwd": None, "title": None, "prompt": None,
            "started": None, "branch": None, "model": None}
    with open(path, "rb") as f:
        head = f.read(HEAD)
        if size > HEAD:
            f.seek(max(HEAD, size - TAIL))
            tail = f.read()
        else:
            tail = b""
    for part, skip in ((head, False), (tail, True)):
        for e in entries(part, skip):
            if not meta["cwd"] and isinstance(e.get("cwd"), str):
                meta["cwd"] = e["cwd"]
            if not meta["started"] and isinstance(e.get("timestamp"), str):
                meta["started"] = e["timestamp"]
            if isinstance(e.get("gitBranch"), str) and e["gitBranch"]:
                meta["branch"] = e["gitBranch"]
            if e.get("type") == "ai-title" and isinstance(e.get("aiTitle"), str):
                meta["title"] = one_line(e["aiTitle"])
            if e.get("type") == "assistant":
                model = (e.get("message") or {}).get("model")
                if isinstance(model, str) and not model.startswith("<"):
                    meta["model"] = model
            if not meta["prompt"]:
                meta["prompt"] = prompt_text(e)
    return meta


def metas(cache_dir):
    """(mtime, size, path, meta) for the newest sessions, from the cache when the file did not change."""
    cache_path = os.path.join(cache_dir, "sessions.json") if cache_dir else None
    cache = {}
    if cache_path:
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
    out, kept, changed = [], {}, False
    for mtime, size, path in transcripts():
        hit = cache.get(path)
        if isinstance(hit, dict) and hit.get("mtime") == mtime and hit.get("size") == size:
            meta = hit["meta"]
        else:
            try:
                meta = read_meta(path, size)
            except OSError:
                continue
            changed = True
        kept[path] = {"mtime": mtime, "size": size, "meta": meta}
        out.append((mtime, size, path, meta))
    if cache_path and (changed or len(kept) != len(cache)):
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(kept, f)
            os.replace(cache_path + ".tmp", cache_path)
        except OSError:
            pass
    return out


def tilde(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path and (path == home or path.startswith(home + "/")) else path


def ago(mtime, now):
    s = max(0, now - mtime)
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
    return time.strftime("%-d %b %Y", time.localtime(mtime))


def resume(meta):
    command = "claude --resume %s" % meta["id"]
    return "cd %s && %s" % (shlex.quote(meta["cwd"]), command) if meta.get("cwd") else command


def title(meta):
    return meta.get("title") or meta.get("prompt") or "Session %s" % meta["id"][:8]


def session_actions(meta, details=None):
    """Copy and paste the resume command, then Show details (the transcript `details` names, in the list), the folder
    and the id."""
    command = resume(meta)
    actions = [{"type": "copy", "text": command, "title": "Copy resume command"},
               {"type": "paste", "text": command, "title": "Paste resume command"}]
    if details:
        actions.append({"type": "push", "command": "session", "title": "Show details", "context": {"file": details}})
    if meta.get("cwd") and os.path.isdir(meta["cwd"]):
        actions.append({"type": "open", "url": meta["cwd"], "title": "Open folder"})
    actions.append({"type": "copy", "text": meta["id"], "title": "Copy session id"})
    return actions


def list_sessions(run):
    now = time.time()
    items = []
    for mtime, size, path, meta in metas((run.get("paths") or {}).get("cache")):
        actions = session_actions(meta, details=path)
        folder = tilde(meta.get("cwd")) or "unknown folder"
        items.append({"id": meta["id"], "title": title(meta), "subtitle": "%s · %s" % (folder, ago(mtime, now)),
                      "icon": "🤖", "keywords": "%s %s %s" % (os.path.basename(meta.get("cwd") or ""),
                                                            meta.get("branch") or "", meta["id"]),
                      "actions": actions})
    if not items:
        items = [{"title": "No Claude Code sessions yet", "subtitle": "None in %s" % tilde(projects())}]
    send({"type": "list", "items": items})


def counts(path, size):
    if size > COUNT_LIMIT:
        return None
    with open(path, "rb") as f:
        data = f.read()
    return data.count(b'"type":"user"') + data.count(b'"type":"assistant"')


def when(stamp):
    """An ISO timestamp from the transcript as local time."""
    if not stamp:
        return "unknown"
    try:
        from datetime import datetime, timezone
        moment = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return time.strftime("%-d %b %Y %H:%M", time.localtime(moment.timestamp()))
    except ValueError:
        return stamp


def show_session(run):
    context = run.get("context") if isinstance(run.get("context"), dict) else {}
    wanted = context.get("file")
    if isinstance(wanted, str):
        real = os.path.realpath(wanted)
        if not (real.startswith(os.path.realpath(projects()) + os.sep) and real.endswith(".jsonl") and os.path.isfile(real)):
            send({"type": "error", "text": "that session is gone"})
            return
        st = os.stat(real)
        chosen = (st.st_mtime, st.st_size, real, read_meta(real, st.st_size))
    else:
        found = metas((run.get("paths") or {}).get("cache"))
        if not found:
            send({"type": "error", "text": "found no Claude Code sessions in %s" % tilde(projects())})
            return
        chosen = found[0]
    mtime, size, path, meta = chosen
    n = counts(path, size)
    markdown = "# %s\n\nResume it in a terminal:\n\n```sh\n%s\n```\n" % (title(meta), resume(meta))
    metadata = [
        {"label": "Folder", "text": tilde(meta.get("cwd")) or "unknown"},
        {"label": "Branch", "text": meta.get("branch") or "none"},
        {"label": "Started", "text": when(meta.get("started"))},
        {"label": "Last activity", "text": "%s (%s)" % (time.strftime("%-d %b %Y %H:%M", time.localtime(mtime)),
                                                        ago(mtime, time.time()))},
        {"label": "Messages", "text": str(n) if n is not None else "too many to count"},
        {"label": "Size", "text": "%.1f MB" % (size / 1e6)},
        {"label": "Model", "text": meta.get("model") or "unknown"},
        {"label": "Session", "text": meta["id"]},
    ]
    send({"type": "detail", "markdown": markdown, "metadata": metadata, "actions": session_actions(meta)})


def main():
    try:
        run = json.loads(sys.stdin.readline())
    except ValueError:
        send({"type": "error", "text": "got a run message it could not read"})
        return
    command = run.get("command")
    if command == "sessions":
        list_sessions(run)
    elif command == "session":
        show_session(run)
    else:
        send({"type": "error", "text": "has no command %s" % command})


if __name__ == "__main__":
    main()
