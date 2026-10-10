#!/usr/bin/env python3
"""AI usage for Pilot: how much of each AI plan's limits is used, from this Mac's own files, read only.

Claude: Claude Code hands its status-line command `rate_limits` after every reply (`five_hour` and `seven_day`, each
`used_percentage` and `resets_at` in epoch seconds), and Ghostex keeps that per session in
~/.local/state/ghostex/agent-hooks/claude-statusline/<session>.json as `{"version", "updatedAt", "payload": {…}}`. The
newest file that carries `rate_limits` wins; with none, Claude's share is unknown until the next reply.

Codex: its session logs (~/.codex/sessions/YYYY/MM/DD/*.jsonl, and archived_sessions) carry `token_count` events whose
`payload.rate_limits` holds `primary` (the 5-hour window) and `secondary` (the week), each with `used_percent`,
`window_minutes` and `resets_at` (older Codex wrote `resets_in_seconds`, counted from the event), plus `credits`. Only
the tail of the newest few logs is read.

Either way, a window whose reset time has passed since the numbers were read has reset: it shows 0%, with what it was.

Gemini keeps no local limit, and its quota is only asked with its sign-in token, which Pilot never reads: "limit
unknown" once the gemini CLI is on PATH, "Not set up" before.

Claude Code tokens (a footnote on the page and in the detail, not a limit): every assistant line in
~/.claude/projects/**/*.jsonl carries `message.usage`. Logs changed in the last 8 days are read from where the last run
stopped and summed per minute into paths.cache, keyed by path, size and modification time. A run that cannot finish in
its time hands the rest to one detached refresh (a lock keeps two from overlapping).

Nothing else is read: no credentials, tokens, cookies or auth files of any provider, and nothing goes over the network.
"""
import calendar
import fcntl
import json
import os
import shutil
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
PAGE_SCAN = 3.0  # a page's command may take 5 s; the bar and popover read no Claude logs at all
REFRESH_SCAN = 120.0
CODEX_TAILS = (256 * 1024, 4 * 1024 * 1024, 32 * 1024 * 1024)
STATUSLINE = os.path.join(".local", "state", "ghostex", "agent-hooks", "claude-statusline")
STATUSLINE_TRIES = 40  # newest status-line files read for one with rate_limits
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
WARN, ALERT = 60, 85
BAR = 20
SET_UP_AI = {"type": "open", "url": "pilot://open?page=ai", "title": "Set up in Pilot › AI…"}


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


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def window(name, percent, resets_at, now):
    """One limit window; reset_since when its reset time has passed since it was read."""
    return {"name": name, "percent": float(percent), "resets_at": resets_at,
            "reset_since": resets_at is not None and resets_at <= now}


# Claude


def claude_limits(home, now):
    """Claude's session and week from the newest Ghostex status-line file that carries rate_limits."""
    folder = os.path.join(home, STATUSLINE)
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".json")]
    except OSError:
        return {"state": "unknown"}
    found = []
    for name in names:
        try:
            found.append((os.stat(os.path.join(folder, name)).st_mtime, name))
        except OSError:
            continue
    found.sort(reverse=True)
    for mtime, name in found[:STATUSLINE_TRIES]:
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        payload = data.get("payload") if isinstance(data, dict) else None
        limits = payload.get("rate_limits") if isinstance(payload, dict) else None
        if not isinstance(limits, dict):
            continue
        windows = []
        for key, label in (("five_hour", "Session"), ("seven_day", "Week")):
            raw = limits.get(key)
            percent = number(raw.get("used_percentage")) if isinstance(raw, dict) else None
            if percent is not None:
                windows.append(window(label, percent, number(raw.get("resets_at")), now))
        if windows:
            return {"state": "ok", "read_at": parse_time(data.get("updatedAt")) or mtime, "windows": windows}
    return {"state": "unknown"}


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
        return "Week"
    if minutes % 1440 == 0:
        return "%d days" % (minutes // 1440)
    if minutes % 60 == 0:
        return "%d hours" % (minutes // 60)
    return "%d min" % minutes


def credits_text(raw):
    if not isinstance(raw, dict):
        return None
    if raw.get("unlimited") is True:
        return "unlimited"
    balance = raw.get("balance")
    return balance if isinstance(balance, str) else None if number(balance) is None else str(balance)


def codex_limits(home, now):
    if not any(os.path.isdir(os.path.join(home, ".codex", sub)) for sub in ("sessions", "archived_sessions")):
        return {"state": "missing"}
    best = None
    for path in codex_files(home, now)[:3]:
        try:
            found = last_rate_limits(path)
        except OSError:
            continue
        if found and (best is None or (found[0] or 0) > (best[0] or 0)):
            best = found
    if best is None:
        return {"state": "none"}
    stamp, raw = best
    windows = []
    for key, fallback in (("primary", "5 hours"), ("secondary", "Week")):
        w = raw.get(key)
        percent = number(w.get("used_percent")) if isinstance(w, dict) else None
        if percent is None:
            continue
        resets = number(w.get("resets_at"))
        if resets is None:
            seconds = number(w.get("resets_in_seconds"))
            resets = stamp + seconds if seconds is not None and stamp else None
        windows.append(window(window_name(w.get("window_minutes"), fallback), percent, resets, now))
    if not windows:
        return {"state": "none"}
    plan = raw.get("plan_type")
    return {"state": "ok", "read_at": stamp, "windows": windows, "credits": credits_text(raw.get("credits")),
            "plan": plan if isinstance(plan, str) and plan else None}


# Gemini


def gemini_state():
    return "installed" if shutil.which("gemini") else "missing"


def limits(home=None, now=None):
    """Every provider's limits as a dict; reads only the status-line store, Codex's logs and PATH."""
    home = home or os.path.expanduser("~")
    now = time.time() if now is None else now
    return {"now": now, "claude": claude_limits(home, now), "codex": codex_limits(home, now), "gemini": gemini_state()}


# Claude Code tokens (footnote)


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
    """Brings the cache up to date until the deadline: (entries by path, every log read)."""
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
    return kept, done == len(files)


def take_lock(cache_dir):
    """The refresh lock, held until its file is closed; None while another refresh holds it."""
    os.makedirs(cache_dir, exist_ok=True)
    handle = open(os.path.join(cache_dir, LOCK_FILE), "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


def spawn_refresh(cache_dir):
    """One detached refresh that finishes reading the logs; it quits at once when another holds the lock."""
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--refresh", cache_dir],
                         cwd=os.path.dirname(os.path.abspath(__file__)), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def token_blocks(minutes):
    """Five-hour blocks the way ccusage counts them: a block starts at the hour of the first message after a gap of
    5 h or more (or after the previous block's 5 h ran out) and ends 5 h after that hour. Only 8 days of logs are
    read, so a block chain with no 5 h gap in 8 days starts at the oldest message read."""
    out = []
    last = None
    for minute in sorted(minutes):
        t = minute * 60
        if not out or t >= out[-1]["start"] + BLOCK or t - last >= BLOCK:
            out.append({"start": t - t % 3600, "tokens": 0})
        out[-1]["tokens"] += minutes[minute]
        last = t
    return out


def claude_tokens(run, now, deadline):
    """Claude Code tokens in the last 7 days and in the running 5-hour block, read from its logs until `deadline`."""
    home, cache_dir = os.path.expanduser("~"), (run.get("paths") or {}).get("cache")
    if not os.path.isdir(claude_root(home)):
        return {"state": "missing"}
    lock = take_lock(cache_dir) if cache_dir else None
    if cache_dir and lock is None:
        entries, complete = load_cache(cache_dir), False
    else:
        try:
            entries, complete = refresh_claude(home, cache_dir, now, deadline)
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
                minutes[minute] = minutes.get(minute, 0) + sum(sums)
    week = sum(n for minute, n in minutes.items() if minute * 60 >= now - 7 * DAY)
    found = token_blocks(minutes)
    block = found[-1]["tokens"] if found and now < found[-1]["start"] + BLOCK else None
    return {"state": "ok", "complete": complete, "week": week, "block": block}


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


def days_after(moment, now):
    """Calendar days from now's day to moment's: 0 today, 1 tomorrow, -1 yesterday (noon to noon, so DST shifts round)."""
    noon = lambda t: time.mktime(time.localtime(t)[:3] + (12, 0, 0, 0, 0, -1))
    return int(round((noon(moment) - noon(now)) / DAY))


def stamp(moment, now):
    """When numbers were read, short: 11:15 today, yesterday 17:27, Tue 17:27 this week, else 3 Oct 17:27."""
    day = time.localtime(moment)
    days = -days_after(moment, now)
    clock = time.strftime("%H:%M", day)
    if days <= 0:
        return clock
    if days == 1:
        return "yesterday " + clock
    return time.strftime("%a " if days < 7 else "%-d %b ", day) + clock


def until(moment, now):
    """A reset time with its day and how long until it: today 18:04 (in 6 h 2 min)."""
    day = time.localtime(moment)
    name = {0: "today", 1: "tomorrow"}.get(days_after(moment, now)) or time.strftime("%a %-d %b", day)
    return "%s %s (in %s)" % (name, time.strftime("%H:%M", day), span(moment - now))


def tone(percent):
    return "alert" if percent >= ALERT else "warn" if percent >= WARN else None


def shown(w):
    """What a window shows now: 0 once it reset since it was read."""
    return 0.0 if w["reset_since"] else w["percent"]


def reset_note(w, since):
    return "reset since your last %s (was %d%%)" % (since, round(w["percent"]))


def windows_text(name, provider):
    return "%s %s" % (name, " · ".join("%s %d%%" % (w["name"].lower(), round(shown(w))) for w in provider["windows"]))


CLAUDE_UNKNOWN = "Unknown until your next Claude Code reply."
CODEX_WORDS = {"missing": ("no logs", "No Codex logs on this Mac. Run Codex once."),
               "none": ("no run in 8 days", "No Codex run in 8 days, so both windows have reset.")}
GEMINI_WORDS = {"missing": ("not set up", "The gemini CLI isn't installed on this Mac."),
                "installed": ("limit unknown", "Gemini keeps no limit on this Mac, and asking Google needs your sign-in "
                                               "token, which Pilot never reads.")}


def summary_line(data):
    claude, codex = data["claude"], data["codex"]
    parts = [windows_text("Claude", claude) if claude["state"] == "ok" else "Claude unknown"]
    if codex["state"] == "ok":
        parts.append(windows_text("Codex", codex))
    elif codex["state"] == "none":
        parts.append("Codex no run in 8 days")
    return " · ".join(parts)


# Blocks: a section card per provider (Surfaces v2)


def meter(w, since):
    if w["reset_since"]:
        return {"type": "meter", "label": w["name"], "percent": 0, "note": reset_note(w, since)}
    m = {"type": "meter", "label": w["name"], "percent": round(w["percent"], 1), "tone": tone(w["percent"]) or "fine"}
    if w["resets_at"]:
        m["resetsAt"] = int(w["resets_at"])
    else:
        m["note"] = "reset time unknown"
    return m


def claude_card(claude, now):
    if claude["state"] != "ok":
        return {"type": "section", "title": "Claude", "subtitle": "unknown",
                "blocks": [{"type": "markdown", "text": CLAUDE_UNKNOWN}]}
    return {"type": "section", "title": "Claude", "subtitle": "Claude Code · read %s" % stamp(claude["read_at"], now),
            "blocks": [meter(w, "Claude Code reply") for w in claude["windows"]]}


def codex_card(codex, now):
    if codex["state"] in CODEX_WORDS:
        subtitle, text = CODEX_WORDS[codex["state"]]
        return {"type": "section", "title": "Codex", "subtitle": subtitle, "blocks": [{"type": "markdown", "text": text}]}
    subtitle = "last run %s" % stamp(codex["read_at"], now) if codex["read_at"] else "last run unknown"
    if codex["plan"]:
        subtitle = "%s · %s" % (codex["plan"], subtitle)
    blocks = [meter(w, "run") for w in codex["windows"]]
    if codex["credits"] is not None:
        blocks.append({"type": "stats", "items": [{"label": "Credits", "value": codex["credits"]}]})
    return {"type": "section", "title": "Codex", "subtitle": subtitle, "blocks": blocks}


def gemini_card(state):
    subtitle, text = GEMINI_WORDS[state]
    blocks = [{"type": "markdown", "text": text}]
    if state == "missing":
        blocks.append({"type": "buttons", "actions": [SET_UP_AI]})
    return {"type": "section", "title": "Gemini", "subtitle": subtitle, "blocks": blocks}


def cards(data):
    now = data["now"]
    return [claude_card(data["claude"], now), codex_card(data["codex"], now), gemini_card(data["gemini"])]


def token_note(tokens):
    if tokens["state"] != "ok":
        return "No Claude Code logs on this Mac (~/.claude/projects), so no token counts."
    block = tokens["block"]
    words = "Claude Code tokens: %s in the last 7 days, %s in this 5-hour block. They come from Claude Code's own " \
            "logs and aren't your limit." % (short_tokens(tokens["week"]), "none" if block is None else short_tokens(block))
    if not tokens["complete"]:
        words = "Still counting Claude Code's logs, so these grow on the next refresh. " + words
    return words


def gauges(data):
    """C and X, the session over the week, each in its loudest tone; a provider with no numbers has no gauge."""
    out = []
    for label, provider in (("C", data["claude"]), ("X", data["codex"])):
        if provider["state"] == "ok":
            values = [round(shown(w), 1) for w in provider["windows"]]
        elif label == "X" and provider["state"] == "none":
            values = [0, 0]
        else:
            continue
        g = {"label": label, "meters": values}
        level = tone(max(values))
        if level:
            g["tone"] = level
        out.append(g)
    return out


# Commands


def show_bar(run):
    data = limits()
    if run.get("surface") == "popover":
        send({"type": "blocks", "blocks": cards(data) + [{"type": "markdown", "text": "Pilot reads no sign-in tokens."}]})
        return
    found = gauges(data)
    top = max((m for g in found for m in g["meters"]), default=0)
    send({"type": "bar", "text": "" if found else "AI", "tone": tone(top), "gauges": found, "blocks": cards(data)})


def show_page(run):
    data = limits()
    tokens = claude_tokens(run, data["now"], START + PAGE_SCAN)
    send({"type": "blocks", "blocks": cards(data) + [
        {"type": "markdown", "text": token_note(tokens)},
        {"type": "buttons", "actions": [{"type": "copy", "text": summary_line(data), "title": "Copy summary"}]}]})


def bar(fraction):
    filled = max(0, min(BAR, int(round(fraction * BAR))))
    return "`%s%s`" % ("▓" * filled, "░" * (BAR - filled))


def windows_markdown(provider, since, now, md, rows, title):
    for w in provider["windows"]:
        if w["reset_since"]:
            note = reset_note(w, since)
        else:
            note = "resets " + until(w["resets_at"], now) if w["resets_at"] else "reset time unknown"
        text = "%d%% · %s" % (round(shown(w)), note)
        md.append("- **%s** %s %s" % (w["name"], bar(shown(w) / 100.0), text))
        rows.append({"label": "%s %s" % (title, w["name"].lower()), "text": text})


def show_usage(run):
    data = limits()
    now, claude, codex = data["now"], data["claude"], data["codex"]
    tokens = claude_tokens(run, now, START + DETAIL_SCAN)
    md, rows = ["## Claude"], []
    if claude["state"] == "ok":
        windows_markdown(claude, "Claude Code reply", now, md, rows, "Claude")
        md += ["", "From Claude Code's status line, read %s." % stamp(claude["read_at"], now)]
    else:
        md.append(CLAUDE_UNKNOWN)
        rows.append({"label": "Claude", "text": "unknown"})
    md += ["", "## Codex"]
    if codex["state"] == "ok":
        windows_markdown(codex, "run", now, md, rows, "Codex")
        if codex["credits"] is not None:
            md.append("- **Credits** %s" % codex["credits"])
            rows.append({"label": "Codex credits", "text": codex["credits"]})
        if codex["read_at"]:
            md += ["", "From your last Codex run, %s." % stamp(codex["read_at"], now)]
    else:
        md.append(CODEX_WORDS[codex["state"]][1])
    subtitle, text = GEMINI_WORDS[data["gemini"]]
    md += ["", "## Gemini", "**%s.** %s" % (subtitle.capitalize(), text), "", "---", "", token_note(tokens)]
    send({"type": "detail", "markdown": "\n".join(md) + "\n", "metadata": rows,
          "actions": [{"type": "copy", "text": summary_line(data), "title": "Copy summary"}]})


def show_summary(run):
    send({"type": "hud", "text": summary_line(limits())})


def background_refresh(cache_dir):
    signal.alarm(int(REFRESH_SCAN) + 30)
    lock = take_lock(cache_dir)
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
