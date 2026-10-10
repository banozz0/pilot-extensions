#!/usr/bin/env python3
"""AI usage for Pilot: Codex limits and Claude Code tokens from this Mac's own logs, read only.

Codex: its session logs (~/.codex/sessions/YYYY/MM/DD/*.jsonl, and archived_sessions) carry `token_count` events whose
`payload.rate_limits` holds `primary` (the 5-hour window) and `secondary` (the weekly one), each with `used_percent`,
`window_minutes` and `resets_at` (epoch seconds; older Codex wrote `resets_in_seconds`, counted from the event's
timestamp). Only the tail of the newest few logs is read.

Claude Code: every assistant line in ~/.claude/projects/**/*.jsonl carries `message.usage`. Logs changed in the last
8 days are read from where the last run stopped and summed per minute into paths.cache, keyed by path, size and
modification time, so a re-run reads only new bytes. A run that cannot finish in its time hands the rest to one
detached refresh (a lock keeps two from overlapping). Claude's percentage of its limit is in no local log: unknown.

Nothing else is read: no credentials, tokens or auth files, and nothing goes over the network. `usage()` returns the
numbers as a dict, so a top-bar item can reuse them.
"""
import calendar
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

START = time.monotonic()
DAY = 86400
SCAN_DAYS = 8
BLOCK = 5 * 3600
CACHE_FILE = "claude-usage.json"
LOCK_FILE = "refresh.lock"
CACHE_VERSION = 1
SEEN_KEEP = 512  # message ids remembered per log to fold repeated lines into one; repeats sit close together
DETAIL_SCAN = 1.0  # seconds from start a detail may spend reading Claude logs; the rest goes to the background
SUMMARY_SCAN = 25.0
PAGE_SCAN = 3.0  # a page's command may take 5 s (Pilot story 161); a bar item's 2 s, so it reads less
BAR_SCAN = 0.8
SUMMARY_WAIT = 15.0
REFRESH_SCAN = 120.0
CODEX_TAILS = (256 * 1024, 4 * 1024 * 1024, 32 * 1024 * 1024)
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
BAR = 20


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")


def parse_time(stamp):
    """An ISO timestamp from a log (UTC, `Z`) as epoch seconds, or None."""
    if not isinstance(stamp, str) or len(stamp) < 19:
        return None
    try:
        if stamp.endswith("Z") or len(stamp) == 19:
            return calendar.timegm((int(stamp[0:4]), int(stamp[5:7]), int(stamp[8:10]), int(stamp[11:13]),
                                    int(stamp[14:16]), int(stamp[17:19]), 0, 0, 0))
        from datetime import datetime
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


# Codex


def codex_files(home, now):
    """Codex logs changed in the last 8 days, newest first."""
    found = []
    for sub in ("sessions", "archived_sessions"):
        for folder, _, names in os.walk(os.path.join(home, ".codex", sub)):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(folder, name)
                try:
                    mtime = os.stat(path).st_mtime
                except OSError:
                    continue
                if now - mtime < SCAN_DAYS * DAY:
                    found.append((mtime, path))
    found.sort(reverse=True)
    return [path for _, path in found]


def last_rate_limits(path):
    """(event time, rate_limits) of the last token_count event with a window in a Codex log, or None."""
    size = os.path.getsize(path)
    for tail in CODEX_TAILS:
        start = max(0, size - tail)
        with open(path, "rb") as f:
            f.seek(start)
            lines = f.read(size - start).split(b"\n")
        for raw in reversed(lines[1:] if start else lines):
            if b'"token_count"' not in raw or b'"rate_limits"' not in raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            payload = event.get("payload") if isinstance(event, dict) else None
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            limits = payload.get("rate_limits")
            if not isinstance(limits, dict) or limits.get("limit_id") not in (None, "codex"):
                continue
            if any(isinstance(limits.get(k), dict) for k in ("primary", "secondary")):
                return parse_time(event.get("timestamp")), limits
        if not start:
            break
    return None


def window_name(minutes, fallback):
    if not isinstance(minutes, int) or minutes <= 0:
        return fallback
    if minutes == 10080:
        return "week"
    if minutes % 1440 == 0:
        return "%d d" % (minutes // 1440)
    if minutes % 60 == 0:
        return "%d h" % (minutes // 60)
    return "%d min" % minutes


def codex_usage(home, now):
    if not any(os.path.isdir(os.path.join(home, ".codex", sub)) for sub in ("sessions", "archived_sessions")):
        return {"state": "missing", "limits": []}
    best = None
    for path in codex_files(home, now)[:3]:
        try:
            found = last_rate_limits(path)
        except OSError:
            continue
        if found and (best is None or (found[0] or 0) > (best[0] or 0)):
            best = found
    if best is None:
        return {"state": "none", "limits": []}
    stamp, raw = best
    limits = []
    for key, fallback in (("primary", "5 h"), ("secondary", "week")):
        window = raw.get(key)
        if not isinstance(window, dict) or not isinstance(window.get("used_percent"), (int, float)):
            continue
        resets = window.get("resets_at")
        if not isinstance(resets, (int, float)):
            seconds = window.get("resets_in_seconds")
            resets = stamp + seconds if isinstance(seconds, (int, float)) and stamp else None
        limits.append({"name": window_name(window.get("window_minutes"), fallback),
                       "window_minutes": window.get("window_minutes"), "used_percent": float(window["used_percent"]),
                       "resets_at": resets, "reset_since": resets is not None and resets <= now})
    return {"state": "ok" if limits else "none", "as_of": stamp, "limits": limits}


# Claude Code


def claude_root(home):
    return os.path.join(home, ".claude", "projects")


def claude_files(home, now):
    """(mtime, size, path) of Claude Code logs changed in the last 8 days, subagents' included, newest first."""
    found = []
    for folder, _, names in os.walk(claude_root(home)):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(folder, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if now - st.st_mtime < SCAN_DAYS * DAY:
                found.append((st.st_mtime, st.st_size, path))
    found.sort(reverse=True)
    return found


def load_cache(cache_dir):
    if not cache_dir:
        return {}
    try:
        with open(os.path.join(cache_dir, CACHE_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) and data.get("version") == CACHE_VERSION else None
    return files if isinstance(files, dict) else {}


def save_cache(cache_dir, files, now):
    if not cache_dir:
        return
    oldest = (now - SCAN_DAYS * DAY) // 60
    for entry in files.values():
        entry["minutes"] = {k: v for k, v in entry["minutes"].items() if int(k) >= oldest}
        if len(entry["seen"]) > SEEN_KEEP:
            entry["seen"] = dict(list(entry["seen"].items())[-SEEN_KEEP:])
    path = os.path.join(cache_dir, CACHE_FILE)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": CACHE_VERSION, "files": files}, f, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def add_line(entry, raw):
    """Folds one assistant line's usage into the entry's per-minute sums. Claude Code writes a line per content block,
    each repeating the message's usage while its output count grows, so a message id is counted once and only the
    growth of its output is added later."""
    try:
        event = json.loads(raw)
    except ValueError:
        return
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return
    message = event.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    stamp = parse_time(event.get("timestamp"))
    if not isinstance(usage, dict) or stamp is None:
        return
    counts = [usage.get(k) if isinstance(usage.get(k), int) else 0 for k in USAGE_KEYS]
    key = message.get("id") or event.get("requestId") or event.get("uuid")
    seen = entry["seen"]
    if key and key in seen:
        minute, counted = seen[key]
        if counts[1] > counted:
            entry["minutes"].setdefault(str(minute), [0, 0, 0, 0])[1] += counts[1] - counted
            seen[key] = [minute, counts[1]]
        return
    minute = int(stamp // 60)
    if key:
        seen[key] = [minute, counts[1]]
    sums = entry["minutes"].setdefault(str(minute), [0, 0, 0, 0])
    for i, n in enumerate(counts):
        sums[i] += n


def scan_file(path, size, mtime, old, deadline):
    """Reads a log from where its cached entry stopped to its last whole line; returns (entry, finished)."""
    if isinstance(old, dict) and isinstance(old.get("offset"), int) and old["offset"] <= size:
        entry = {"offset": old["offset"], "minutes": old.get("minutes") or {}, "seen": old.get("seen") or {}}
    else:
        entry = {"offset": 0, "minutes": {}, "seen": {}}
    finished = True
    with open(path, "rb") as f:
        f.seek(entry["offset"])
        pos = entry["offset"]
        for n, raw in enumerate(f):
            if pos >= size or not raw.endswith(b"\n"):
                break
            pos += len(raw)
            if b'"assistant"' in raw and b'"usage"' in raw:
                add_line(entry, raw)
            if n % 2000 == 1999 and time.monotonic() >= deadline:
                finished = pos >= size
                break
    entry["offset"] = pos
    entry["size"], entry["mtime"] = (size, mtime) if finished else (None, None)
    return entry, finished


def fresh(entry, size, mtime):
    return isinstance(entry, dict) and entry.get("size") == size and entry.get("mtime") == mtime


def refresh_claude(home, cache_dir, now, deadline):
    """Brings the cache up to date until the deadline: (entries by path, every log read, logs read, logs)."""
    files = claude_files(home, now)
    cache = load_cache(cache_dir)
    kept, changed, done = {}, False, 0
    for mtime, size, path in files:
        old = cache.get(path)
        if fresh(old, size, mtime):
            kept[path] = old
            done += 1
            continue
        if time.monotonic() >= deadline:
            if isinstance(old, dict):
                kept[path] = old
            continue
        try:
            entry, finished = scan_file(path, size, mtime, old, deadline)
        except OSError:
            continue
        kept[path], changed = entry, True
        done += finished
    if changed or len(kept) != len(cache):
        save_cache(cache_dir, kept, now)
    return kept, done == len(files), done, len(files)


def take_lock(cache_dir, wait):
    """The refresh lock, held until its file is closed; None when another refresh holds it past `wait` seconds."""
    os.makedirs(cache_dir, exist_ok=True)
    handle = open(os.path.join(cache_dir, LOCK_FILE), "a")
    until = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.monotonic() >= until:
                handle.close()
                return None
            time.sleep(0.1)


def spawn_refresh(cache_dir):
    """One detached refresh that finishes reading the logs; it quits at once when another holds the lock."""
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--refresh", cache_dir],
                         cwd=os.path.dirname(os.path.abspath(__file__)), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def blocks(minutes):
    """Five-hour blocks the way ccusage counts them: a block starts at the hour of the first message after a gap of
    5 h or more (or after the previous block's 5 h ran out) and ends 5 h after that hour. Only 8 days of logs are
    read, so a block chain with no 5 h gap in 8 days starts at the oldest message read."""
    out = []
    last = None
    for minute in sorted(minutes):
        t = minute * 60
        if not out or t >= out[-1]["start"] + BLOCK or t - last >= BLOCK:
            out.append({"start": t - t % 3600, "tokens": [0, 0, 0, 0]})
        for i, n in enumerate(minutes[minute]):
            out[-1]["tokens"][i] += n
        out[-1]["last"] = last = t
    return out


def tokens_dict(sums):
    return {"input": sums[0], "output": sums[1], "cache_write": sums[2], "cache_read": sums[3], "total": sum(sums)}


def claude_usage(home, cache_dir, now, deadline, wait=0.0):
    if not os.path.isdir(claude_root(home)):
        return {"state": "missing"}
    busy = False
    lock = take_lock(cache_dir, wait) if cache_dir else None
    if cache_dir and lock is None:
        entries, complete, busy = load_cache(cache_dir), False, True
        read = total = None
    else:
        try:
            entries, complete, read, total = refresh_claude(home, cache_dir, now, deadline)
        finally:
            if lock:
                lock.close()
        if not complete and cache_dir:
            spawn_refresh(cache_dir)
    minutes = {}
    since = now - SCAN_DAYS * DAY
    for entry in entries.values():
        for key, sums in (entry.get("minutes") or {}).items():
            minute = int(key)
            if since <= minute * 60 <= now + 60:
                acc = minutes.setdefault(minute, [0, 0, 0, 0])
                for i, n in enumerate(sums):
                    acc[i] += n
    week = [0, 0, 0, 0]
    for minute, sums in minutes.items():
        if minute * 60 >= now - 7 * DAY:
            for i, n in enumerate(sums):
                week[i] += n
    found = blocks(minutes)
    current = found[-1] if found and now < found[-1]["start"] + BLOCK else None
    result = {"state": "ok", "complete": complete, "busy": busy, "files_read": read, "files": total,
              "week": tokens_dict(week), "percent": None, "block": None,
              "last_block_end": found[-1]["start"] + BLOCK if found and not current else None}
    if current:
        result["block"] = {"start": current["start"], "resets_at": current["start"] + BLOCK, "last": current["last"],
                           "tokens": tokens_dict(current["tokens"])}
    return result


def usage(home=None, cache_dir=None, now=None, deadline=None, wait=0.0):
    """Codex limits and Claude Code tokens as a dict; reads Claude logs until `deadline` (time.monotonic())."""
    home = home or os.path.expanduser("~")
    now = time.time() if now is None else now
    deadline = START + DETAIL_SCAN if deadline is None else deadline
    return {"now": now, "codex": codex_usage(home, now), "claude": claude_usage(home, cache_dir, now, deadline, wait)}


# Words


def short_tokens(n):
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= size:
            value = n / size
            text = ("%.1f" % value) if value < 100 else str(int(round(value)))
            return (text[:-2] if text.endswith(".0") else text) + unit
    return str(n)


def span(seconds):
    seconds = int(abs(seconds))
    if seconds < 60:
        return "under a minute"
    minutes = seconds // 60
    if minutes < 60:
        return "%d min" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%d h %d min" % (hours, minutes) if minutes else "%d h" % hours
    days, hours = divmod(hours, 24)
    return "%d d %d h" % (days, hours) if hours else "%d d" % days


def relative(moment, now):
    return "in " + span(moment - now) if moment > now else span(now - moment) + " ago"


def clock(moment, now):
    """Local time with its day: today 18:33, tomorrow 09:00, yesterday 22:35, or Mon 13 Oct 09:00."""
    day = time.localtime(moment)
    days = (time.mktime(day[:3] + (12, 0, 0, 0, 0, -1)) - time.mktime(time.localtime(now)[:3] + (12, 0, 0, 0, 0, -1)))
    days = int(round(days / DAY))
    name = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days) or time.strftime("%a %-d %b", day)
    return "%s %s" % (name, time.strftime("%H:%M", day))


def when(moment, now):
    return "%s (%s)" % (clock(moment, now), relative(moment, now))


def bar(fraction):
    filled = max(0, min(BAR, int(round(fraction * BAR))))
    return "`%s%s`" % ("▓" * filled, "░" * (BAR - filled))


def breakdown(tokens):
    return "in %s · out %s · cache write %s · cache read %s" % tuple(
        short_tokens(tokens[k]) for k in ("input", "output", "cache_write", "cache_read"))


def summary_line(data):
    codex = data["codex"]
    if codex["state"] == "ok":
        parts = ["%s %s" % (l["name"], "reset" if l["reset_since"] else "%d%%" % round(l["used_percent"]))
                 for l in codex["limits"]]
        line = "Codex " + " · ".join(parts)
    elif codex["state"] == "none":
        line = "Codex no use in 8 days"
    else:
        line = "Codex no logs"
    claude = data["claude"]
    if claude["state"] == "missing":
        tail = "Claude no logs"
    elif claude["block"]:
        tail = "Claude block %s tokens" % short_tokens(claude["block"]["tokens"]["total"])
    else:
        tail = "Claude no block running"
    if claude["state"] == "ok" and not claude["complete"]:
        tail += " (still counting)"
    return "%s · %s" % (line, tail)


def codex_section(codex, now, md, rows):
    md.append("## Codex")
    if codex["state"] == "missing":
        md.append("No Codex logs on this Mac (~/.codex/sessions).")
        return
    if codex["state"] == "none":
        md.append("No Codex use logged in the last 8 days, so every limit has reset.")
        return
    for limit in codex["limits"]:
        name, pct, resets = limit["name"], limit["used_percent"], limit["resets_at"]
        if limit["reset_since"]:
            md.append("- **%s** %s reset %s, nothing logged since" % (name, bar(0), when(resets, now)))
            rows += [{"label": "Codex %s used" % name, "text": "0%% (was %d%%, reset since)" % round(pct)},
                     {"label": "Codex %s reset" % name, "text": when(resets, now)}]
            continue
        reset_text = when(resets, now) if resets else "unknown"
        md.append("- **%s** %s %d%% · resets %s" % (name, bar(pct / 100.0), round(pct), reset_text))
        rows += [{"label": "Codex %s used" % name, "text": "%d%%" % round(pct)},
                 {"label": "Codex %s resets" % name, "text": reset_text}]
    if codex.get("as_of"):
        md.append("")
        md.append("From the newest Codex log, %s." % when(codex["as_of"], now))
        rows.append({"label": "Codex as of", "text": when(codex["as_of"], now)})


def claude_section(claude, now, md, rows):
    md.append("## Claude Code")
    if claude["state"] == "missing":
        md.append("No Claude Code logs on this Mac (~/.claude/projects).")
        return
    if not claude["complete"]:
        if claude["files"]:
            md.append("Still counting: %d of %d logs read; open again in a few seconds. Numbers so far:"
                      % (claude["files_read"], claude["files"]))
        else:
            md.append("Still counting in the background; open again in a few seconds. Numbers so far:")
        md.append("")
    block = claude["block"]
    if block:
        tokens = block["tokens"]
        elapsed = now - block["start"]
        md.append("- **This block** %s tokens since %s · resets %s"
                  % (short_tokens(tokens["total"]), time.strftime("%H:%M", time.localtime(block["start"])),
                     when(block["resets_at"], now)))
        md.append("- **Block time** %s %s of 5 h" % (bar(elapsed / BLOCK), span(elapsed)))
        rows += [{"label": "Claude block tokens", "text": "{:,}".format(tokens["total"])},
                 {"label": "Claude block split", "text": breakdown(tokens)},
                 {"label": "Claude block resets", "text": when(block["resets_at"], now)}]
    else:
        ended = claude["last_block_end"]
        md.append("- **This block** none running%s" % (", the last ended %s" % when(ended, now) if ended else ""))
        rows.append({"label": "Claude block tokens", "text": "0 (no block running)"})
    week = claude["week"]
    md.append("- **Last 7 days** %s tokens" % short_tokens(week["total"]))
    rows += [{"label": "Claude 7 days tokens", "text": "{:,}".format(week["total"])},
             {"label": "Claude 7 days split", "text": breakdown(week)},
             {"label": "Claude % of limit", "text": "unknown (not in local logs)"}]
    md.append("")
    md.append("Tokens count input, output and cache, as ccusage does. How much of Claude's limit that is "
              "is not in any local log, so it is unknown.")


def show_usage(run):
    data = usage(cache_dir=(run.get("paths") or {}).get("cache"))
    now = data["now"]
    md, rows = [], []
    codex_section(data["codex"], now, md, rows)
    md.append("")
    claude_section(data["claude"], now, md, rows)
    actions = [{"type": "copy", "text": summary_line(data), "title": "Copy summary"}]
    if data["claude"]["state"] == "ok" and not data["claude"]["complete"]:
        actions.insert(0, {"type": "push", "command": "usage", "title": "Count again"})
    send({"type": "detail", "markdown": "\n".join(md) + "\n", "metadata": rows, "actions": actions})


def show_summary(run):
    data = usage(cache_dir=(run.get("paths") or {}).get("cache"), deadline=START + SUMMARY_SCAN, wait=SUMMARY_WAIT)
    send({"type": "hud", "text": summary_line(data)})


# Surfaces (Pilot story 161): the page in Pilot's app window and the top-bar item.


def tone(percent):
    return "alert" if percent >= 90 else "warn" if percent >= 70 else "fine"


def live_limits(data):
    """Codex's limits that have not reset since the log, as (name, percent, resets_at)."""
    codex = data["codex"]
    if codex["state"] != "ok":
        return []
    return [(l["name"], l["used_percent"], l["resets_at"]) for l in codex["limits"] if not l["reset_since"]]


def limit_meters(data, now):
    return [{"type": "meter", "label": "Codex %s" % name, "percent": round(pct, 1), "tone": tone(pct),
             "note": "resets %s" % when(resets, now) if resets else "reset time unknown"}
            for name, pct, resets in live_limits(data)]


def show_page(run):
    data = usage(cache_dir=(run.get("paths") or {}).get("cache"), deadline=START + PAGE_SCAN)
    now, codex, claude = data["now"], data["codex"], data["claude"]
    stats = [{"label": "Codex %s" % name, "value": "%d%%" % round(pct), "tone": tone(pct)} for name, pct, _ in live_limits(data)]
    if claude["state"] == "ok":
        block = claude["block"]
        stats.append({"label": "Claude block", "value": short_tokens(block["tokens"]["total"]) if block else "none"})
        stats.append({"label": "Claude 7 days", "value": short_tokens(claude["week"]["total"])})
    blocks = [{"type": "stats", "items": stats}] if stats else []
    blocks += limit_meters(data, now)
    if codex["state"] != "ok":
        blocks.append({"type": "markdown", "text": "No Codex logs on this Mac (~/.codex/sessions)." if codex["state"] == "missing"
                       else "No Codex use logged in the last 8 days, so every limit has reset."})
    if claude["state"] == "missing":
        blocks.append({"type": "markdown", "text": "No Claude Code logs on this Mac (~/.claude/projects)."})
    else:
        block = claude["block"]
        if block:
            elapsed = now - block["start"]
            blocks.append({"type": "meter", "label": "Claude block time", "percent": round(100 * min(elapsed / BLOCK, 1), 1),
                           "note": "%s tokens since %s · resets %s" % (short_tokens(block["tokens"]["total"]),
                                                                       time.strftime("%H:%M", time.localtime(block["start"])),
                                                                       when(block["resets_at"], now))})
        words = "Claude tokens count input, output and cache. Claude's share of its limit is in no local log, so it is unknown."
        if not claude["complete"]:
            words = "Still counting Claude's logs; this page asks again on its own. " + words
        blocks.append({"type": "markdown", "text": words})
    blocks.append({"type": "buttons", "actions": [{"type": "copy", "text": summary_line(data), "title": "Copy summary"}]})
    send({"type": "blocks", "blocks": blocks})


def show_bar(run):
    data = usage(cache_dir=(run.get("paths") or {}).get("cache"), deadline=START + BAR_SCAN)
    limits = live_limits(data)
    if limits:
        top = max(pct for _, pct, _ in limits)
        text, level = "AI %d%%" % round(top), tone(top)
    elif data["claude"]["state"] == "ok" and data["claude"]["block"]:
        text, level = "AI %s" % short_tokens(data["claude"]["block"]["tokens"]["total"]), None
    else:
        text, level = "", None
    send({"type": "bar", "text": text, "tone": level, "blocks": limit_meters(data, data["now"])})


def background_refresh(cache_dir):
    signal.alarm(int(REFRESH_SCAN) + 30)
    lock = take_lock(cache_dir, 0)
    if lock is None:
        return
    try:
        refresh_claude(os.path.expanduser("~"), cache_dir, time.time(), time.monotonic() + REFRESH_SCAN)
    finally:
        lock.close()


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--refresh":
        background_refresh(sys.argv[2])
        return
    try:
        run = json.loads(sys.stdin.readline())
    except ValueError:
        send({"type": "error", "text": "got a run message it could not read"})
        return
    command = run.get("command")
    if command == "usage":
        show_usage(run)
    elif command == "summary":
        show_summary(run)
    elif command == "page":
        show_page(run)
    elif command == "bar":
        show_bar(run)
    else:
        send({"type": "error", "text": "has no command %s" % command})


if __name__ == "__main__":
    main()
