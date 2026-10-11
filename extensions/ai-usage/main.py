#!/usr/bin/env python3
"""AI usage for Pilot: how much of each AI plan's limits is left, CodexBar's way, from this Mac and the providers' own CLIs.

Claude, two sources, the newer winning for the session and the week:
- Claude Code hands its status-line command `rate_limits` after every reply (`five_hour` and `seven_day`, each
  `used_percentage` and `resets_at` in epoch seconds), and Ghostex keeps that per session in
  ~/.local/state/ghostex/agent-hooks/claude-statusline/<session>.json as `{"version", "updatedAt", "payload": {…}}`.
- Claude Code caches its own usage read in ~/.claude.json: `cachedUsageUtilization` (`fetchedAtMs`, `accountUuid` and
  `utilization.limits`, each `kind` session, weekly_all or weekly_scoped with a model's `display_name`, `percent`,
  `resets_at` ISO), the only place a per-model week such as "Fable weekly" shows; `oauthAccount` there gives the email
  and plan (`organizationType`, `organizationRateLimitTier`). Only those two keys are used; the file holds no token.

Codex, per account (a CODEX_HOME folder): the folders in the `codexHomes` preference are asked through Codex's own
`codex app-server` (`account/read`: email and plan; `account/rateLimits/read`: the 5-hour and weekly windows, credits and
limit reset credits). Codex signs in with its own login; Pilot never reads it. The bar never waits for that: it shows
the last answer (paths.cache/codex-<hash>.json) and hands a stale one to one detached refresh; a popover or page asks
again inline when the answer is over 30 s old. Without an answer, and for any other ~/.codex-* folder (listed but never
asked, since asking makes Codex use that folder's login), the numbers come from the tail of the newest session logs
(sessions/YYYY/MM/DD/*.jsonl, archived_sessions): `token_count` events whose `payload.rate_limits` holds `primary` and
`secondary`, each `used_percent`, `window_minutes` and `resets_at` (older Codex: `resets_in_seconds`), plus `credits`.

CodexBar's own Codex accounts (each a CODEX_HOME it keeps under ~/Library/Application Support/CodexBar/managed-codex-homes)
are asked the same way, after the preference's folders: only each account's `email` and `managedHomePath` are read from
CodexBar's managed-codex-accounts.json, never a file inside the folder. One whose email an account above already
answered with is shown once (and not asked again while that answer is known).

Either way, a window whose reset time has passed since the numbers were read has reset: it shows 100% left, with what
it was. Each window's pace compares its use with the share of the window gone: in reserve or in deficit, whether it lasts
until the reset (and the headroom) or when it runs out, and a mark where its share left should be by now. As CodexBar:
no pace until 3% of the window has gone, "On pace" and no mark within 2 points, a session "Projected empty", a week
"Runs out".

Gemini keeps no local limit, and its quota is only asked with its sign-in token, which Pilot never reads: "limit
unknown" once the gemini CLI is on PATH, "Not set up" before.

Claude Code tokens (a footnote on the page and in the detail, not a limit): every assistant line in
~/.claude/projects/**/*.jsonl carries `message.usage`. Logs changed in the last 8 days are read from where the last run
stopped and summed per minute into paths.cache, keyed by path, size and modification time. A run that cannot finish in
its time hands the rest to one detached refresh (a lock keeps two from overlapping).

Settings (the manifest's preferences): show the share left (CodexBar's way, the default) or used; which providers the
bar shows and whether its figure is the session or the most used window; pace on or off; other Codex folders and Gemini
shown or hidden.

Nothing else is read: no auth.json, credentials, keychain items or cookies of any provider.
"""
import calendar
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time

START = time.monotonic()
DAY = 86400
SCAN_DAYS = 8
BLOCK = 5 * 3600
CACHE_FILE = "claude-usage.json"
LOCK_FILE = "refresh.lock"
CODEX_LOCK = "codex.lock"
CACHE_VERSION = 1
SEEN_KEEP = 512  # message ids remembered per log to fold repeated lines into one; repeats sit close together
DETAIL_SCAN = 1.0  # seconds from start a detail may spend reading Claude logs; the rest goes to the background
PAGE_SCAN = 3.0  # a page's command may take 5 s; the bar and popover read no Claude logs at all
REFRESH_SCAN = 120.0
LIVE_FRESH = 30  # a popover or page asks Codex again when its answer is older
LIVE_STALE = 120  # the bar and detail hand an older answer to a background refresh
LIVE_WAIT = 3.0  # seconds a popover waits for Codex; a page waits 2 s, leaving time to count Claude Code tokens
PAGE_WAIT = 2.0
CODEX_TAILS = (256 * 1024, 4 * 1024 * 1024, 32 * 1024 * 1024)
CODEXBAR_ACCOUNTS = os.path.join("Library", "Application Support", "CodexBar", "managed-codex-accounts.json")
CODEX_HOME_NAME = re.compile(r"\.codex(-[A-Za-z0-9._-]+)?\Z")
STATUSLINE = os.path.join(".local", "state", "ghostex", "agent-hooks", "claude-statusline")
STATUSLINE_TRIES = 40  # newest status-line files read for one with rate_limits
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
SESSION, WEEK = 300, 10080  # window lengths in minutes
WARN, ALERT = 60, 85  # percent used
BAR = 20
SET_UP_AI = {"type": "open", "url": "pilot://open?page=ai", "title": "Set up in Pilot › AI…"}
# The settings (the manifest's preferences), read once per run by configure().
VIEW = {"show": "left", "barProviders": "both", "barWindow": "session", "pace": True, "codexBar": True, "otherCodex": True,
        "gemini": True}
CLAUDE_PLANS = {"claude_max": "Max", "claude_pro": "Pro", "claude_team": "Team", "claude_enterprise": "Enterprise"}


def configure(run):
    """The run's settings over the defaults; a value of the wrong type keeps the default."""
    prefs = run.get("preferences") or {}
    for key, default in VIEW.items():
        if isinstance(prefs.get(key), type(default)):
            VIEW[key] = prefs[key]


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")


def parse_time(stamp):
    """An ISO timestamp (UTC `Z`, or with an offset) as epoch seconds, or None."""
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


def window(name, percent, resets_at, now, minutes=None):
    """One limit window; reset_since when its reset time has passed since it was read."""
    return {"name": name, "percent": float(percent), "resets_at": resets_at,
            "minutes": minutes if isinstance(minutes, int) and minutes > 0 else None,
            "reset_since": resets_at is not None and resets_at <= now}


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# Claude


def claude_statusline(home, now):
    """(read time, session and week) from the newest Ghostex status-line file that carries rate_limits, or None."""
    folder = os.path.join(home, STATUSLINE)
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".json")]
    except OSError:
        return None
    found = []
    for name in names:
        try:
            found.append((os.stat(os.path.join(folder, name)).st_mtime, name))
        except OSError:
            continue
    found.sort(reverse=True)
    for mtime, name in found[:STATUSLINE_TRIES]:
        data = read_json(os.path.join(folder, name))
        payload = data.get("payload") if isinstance(data, dict) else None
        limits = payload.get("rate_limits") if isinstance(payload, dict) else None
        if not isinstance(limits, dict):
            continue
        windows = []
        for key, label, minutes in (("five_hour", "Session", SESSION), ("seven_day", "Weekly", WEEK)):
            raw = limits.get(key)
            percent = number(raw.get("used_percentage")) if isinstance(raw, dict) else None
            if percent is not None:
                windows.append(window(label, percent, number(raw.get("resets_at")), now, minutes))
        if windows:
            return parse_time(data.get("updatedAt")) or mtime, windows
    return None


def claude_plan(account):
    plan = CLAUDE_PLANS.get(account.get("organizationType"))
    tier = re.search(r"_(\d+)x\Z", account.get("organizationRateLimitTier") or "")
    return "%s %sx" % (plan, tier.group(1)) if plan and tier else plan


def claude_account(home, now):
    """(email, plan, cached read) from ~/.claude.json: the account Claude Code signed in with and its own last usage read
    as (read time, windows), the per-model weeks among them; None for what is missing or another account's."""
    data = read_json(os.path.join(home, ".claude.json"))
    if not isinstance(data, dict):
        return None, None, None
    account = data.get("oauthAccount") if isinstance(data.get("oauthAccount"), dict) else {}
    email = account.get("emailAddress") if isinstance(account.get("emailAddress"), str) else None
    cached = data.get("cachedUsageUtilization")
    fetched = number(cached.get("fetchedAtMs")) if isinstance(cached, dict) else None
    if fetched is None or cached.get("accountUuid") != account.get("accountUuid"):
        return email, claude_plan(account), None
    usage = cached.get("utilization") if isinstance(cached.get("utilization"), dict) else {}
    windows = []
    for raw in usage.get("limits") or []:
        if not isinstance(raw, dict) or number(raw.get("percent")) is None:
            continue
        kind, scope = raw.get("kind"), raw.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        if kind == "session":
            name, minutes = "Session", SESSION
        elif kind == "weekly_all":
            name, minutes = "Weekly", WEEK
        elif kind == "weekly_scoped" and isinstance(model, dict) and isinstance(model.get("display_name"), str):
            name, minutes = "%s weekly" % model["display_name"], WEEK
        else:
            continue
        windows.append(window(name, raw["percent"], parse_time(raw.get("resets_at")), now, minutes))
    return email, claude_plan(account), (fetched / 1000.0, windows) if windows else None


def claude_limits(home, now):
    """Claude's windows: the session and the week from whichever read is newer, the status line's or Claude Code's
    cached one, then the cached per-model weeks."""
    email, plan, cached = claude_account(home, now)
    line = claude_statusline(home, now)
    if line is None and cached is None:
        return {"state": "unknown", "who": email, "plan": plan}
    if cached is None or (line is not None and line[0] >= cached[0]):
        read_at, windows = line
        windows = windows + [w for w in (cached[1] if cached else []) if w["name"] not in ("Session", "Weekly")]
    else:
        read_at, windows = cached
    return {"state": "ok", "who": email, "plan": plan, "read_at": read_at, "windows": windows}


# Codex


def tilde(path, home):
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def codexbar_homes(home):
    """{folder: email} of the Codex accounts CodexBar added: from its account list, each account's email and folder."""
    data = read_json(os.path.join(home, CODEXBAR_ACCOUNTS))
    if not isinstance(data, dict) or data.get("version") not in (1, 2, 3) or not isinstance(data.get("accounts"), list):
        return {}
    found = {}
    for account in data["accounts"]:
        path = account.get("managedHomePath") if isinstance(account, dict) else None
        if isinstance(path, str) and os.path.isabs(path) and os.path.isdir(path):
            email = account.get("email")
            found[os.path.normpath(path)] = email if isinstance(email, str) and email else None
    return found


def codex_homes(home, run):
    """(the preference's folders, CodexBar's accounts as {folder: email}, other folders): the first two asked live, the
    others (any ~/.codex-* holding a login or logs) read from their logs only."""
    raw = (run.get("preferences") or {}).get("codexHomes")
    asked = []
    for part in (raw if isinstance(raw, str) and raw.strip() else "~/.codex").split(","):
        path = os.path.normpath(os.path.expanduser(part.strip())) if part.strip() else ""
        if os.path.isabs(path) and path not in asked:
            asked.append(path)
    codexbar = {p: e for p, e in codexbar_homes(home).items() if p not in asked} if VIEW["codexBar"] else {}
    try:
        names = sorted(os.listdir(home))
    except OSError:
        names = []
    others = []
    for name in names:
        path = os.path.join(home, name)
        if CODEX_HOME_NAME.match(name) and path not in asked and (
                os.path.exists(os.path.join(path, "auth.json")) or os.path.isdir(os.path.join(path, "sessions"))):
            others.append(path)
    return asked, codexbar, others


def codex_files(codex_home, now):
    """Codex logs changed in the last 8 days, newest first."""
    found = []
    for sub in ("sessions", "archived_sessions"):
        for folder, _, names in os.walk(os.path.join(codex_home, sub)):
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
    if minutes == SESSION:
        return "Session"
    if minutes == WEEK:
        return "Weekly"
    if minutes % 1440 == 0:
        return "%d days" % (minutes // 1440)
    if minutes % 60 == 0:
        return "%d hours" % (minutes // 60)
    return "%d min" % minutes


def credits_text(raw):
    """Codex credits worth showing: unlimited, or a balance above 0; None for none."""
    if not isinstance(raw, dict):
        return None
    if raw.get("unlimited") is True:
        return "unlimited"
    balance = raw.get("balance")
    try:
        return str(balance) if not isinstance(balance, bool) and float(balance) > 0 else None
    except (TypeError, ValueError):
        return None


def plan_words(plan):
    return {"prolite": "Pro Lite", "promax": "Pro Max", "edu_plus": "Edu Plus", "edu_pro": "Edu Pro"}.get(
        plan, plan.replace("_", " ").title()) if isinstance(plan, str) and plan and plan != "unknown" else None


def codex_windows(raw, keys, now, stamp=None):
    """The 5-hour and weekly windows of a Codex rate-limit snapshot, its keys (used, minutes, resets) as a log or the
    app-server spells them; an older log's `resets_in_seconds` counts from the event's `stamp`."""
    used, length, resets_key = keys
    out = []
    for key, fallback in (("primary", "Session"), ("secondary", "Weekly")):
        w = raw.get(key)
        percent = number(w.get(used)) if isinstance(w, dict) else None
        if percent is None:
            continue
        resets = number(w.get(resets_key))
        seconds = number(w.get("resets_in_seconds"))
        if resets is None and seconds is not None and stamp:
            resets = stamp + seconds
        out.append(window(window_name(w.get(length), fallback), percent, resets, now, w.get(length)))
    return out


def codex_logs(codex_home, now):
    """One account's windows from the last rate_limits in its session logs."""
    if not any(os.path.isdir(os.path.join(codex_home, sub)) for sub in ("sessions", "archived_sessions")):
        return {"state": "missing"}
    best = None
    for path in codex_files(codex_home, now)[:3]:
        try:
            found = last_rate_limits(path)
        except OSError:
            continue
        if found and (best is None or (found[0] or 0) > (best[0] or 0)):
            best = found
    if best is None:
        return {"state": "none"}
    stamp, raw = best
    windows = codex_windows(raw, ("used_percent", "window_minutes", "resets_at"), now, stamp)
    if not windows:
        return {"state": "none"}
    return {"state": "ok", "source": "logs", "read_at": stamp, "windows": windows,
            "credits": credits_text(raw.get("credits")), "plan": plan_words(raw.get("plan_type"))}


def ask_codex(codex_home, deadline):
    """Codex's own answer for one account through `codex app-server` (JSON-RPC lines on stdio), which signs in with its
    own login: {at, account: {email, plan} or None when signed out, limits, reset_credits}; None when codex is missing,
    fails or is still quiet at `deadline`."""
    program = shutil.which("codex")
    if not program:
        return None
    env = dict(os.environ, CODEX_HOME=codex_home)
    try:
        proc = subprocess.Popen([program, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    except OSError:
        return None
    answers, buffer = {}, b""
    try:
        requests = [{"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "pilot-ai-usage", "version": "3"}}},
                    {"method": "initialized"}, {"id": 2, "method": "account/read", "params": {}},
                    {"id": 3, "method": "account/rateLimits/read", "params": None}]
        proc.stdin.write("".join(json.dumps(r) + "\n" for r in requests).encode())
        proc.stdin.flush()
        out = proc.stdout.fileno()
        while len(answers) < 2:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([out], [], [], left)[0]:
                return None
            chunk = os.read(out, 65536)
            if not chunk:
                break
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") in (2, 3):
                    answers[message["id"]] = message
    except OSError:
        return None
    finally:
        proc.kill()
        proc.wait()
    account = (answers.get(2) or {}).get("result")
    limits = (answers.get(3) or {}).get("result")
    if not isinstance(account, dict):
        return None
    who, at = account.get("account"), time.time()
    if not isinstance(who, dict):
        return {"at": at, "account": None}
    found = {"at": at, "account": {"email": who.get("email") if isinstance(who.get("email"), str) else None,
                                            "plan": plan_words(who.get("planType"))}}
    if isinstance(limits, dict) and isinstance(limits.get("rateLimits"), dict):
        found["limits"] = limits["rateLimits"]
        summary = limits.get("rateLimitResetCredits")
        if isinstance(summary, dict) and number(summary.get("availableCount")) is not None:
            found["reset_credits"] = {"count": int(summary["availableCount"]), "expires": sorted(
                c["expiresAt"] for c in summary.get("credits") or []
                if isinstance(c, dict) and c.get("status") == "available" and number(c.get("expiresAt")))}
    return found


def live_path(cache_dir, codex_home):
    return os.path.join(cache_dir, "codex-%s.json" % hashlib.sha1(codex_home.encode()).hexdigest()[:12])


def load_live(cache_dir, codex_home):
    data = read_json(live_path(cache_dir, codex_home)) if cache_dir else None
    return data if isinstance(data, dict) and number(data.get("at")) is not None else None


def ask_all(cache_dir, homes, deadline):
    """Asks each account at once, saves each answer; {home: answer} for those that answered."""
    found = {}

    def one(path):
        answer = ask_codex(path, deadline)
        if answer:
            found[path] = answer
        if cache_dir:  # a failure is saved too, so the next ask waits as long as after an answer
            write_json(live_path(cache_dir, path), answer or {"at": time.time(), "failed": True})

    threads = [threading.Thread(target=one, args=(p,), daemon=True) for p in homes]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()) + 0.2)
    return dict(found)


def spawn(*args):
    """One detached run of this program with `args`; it quits at once when another holds its lock."""
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__)] + list(args),
                         cwd=os.path.dirname(os.path.abspath(__file__)), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def from_live(answer, now):
    """A provider from Codex's own answer."""
    account = answer.get("account")
    if account is None:
        return {"state": "signed_out", "source": "live", "read_at": answer["at"]}
    raw = answer.get("limits") if isinstance(answer.get("limits"), dict) else {}
    windows = codex_windows(raw, ("usedPercent", "windowDurationMins", "resetsAt"), now)
    return {"state": "ok" if windows else "none", "source": "live", "read_at": answer["at"], "windows": windows,
            "who": account.get("email"), "plan": account.get("plan") or plan_words(raw.get("planType")),
            "credits": credits_text(raw.get("credits")), "reset_credits": answer.get("reset_credits")}


def answer_email(answer):
    """The email Codex answered with, lower case; None without one."""
    who = answer.get("account") if isinstance(answer, dict) and not answer.get("failed") else None
    email = who.get("email") if isinstance(who, dict) else None
    return email.lower() if isinstance(email, str) and email else None


def codex_accounts(home, now, run, ask_until=None):
    """Every Codex account, asked ones first. `ask_until` (a monotonic deadline) asks Codex again for an answer over
    30 s old; without it an answer over 120 s old goes to one background refresh."""
    cache_dir = (run.get("paths") or {}).get("cache")
    setting, codexbar, others = codex_homes(home, run)
    answers = {p: load_live(cache_dir, p) for p in setting}
    known = {answer_email(answers[p]) for p in setting} - {None}
    codexbar = {p: e for p, e in codexbar.items() if not e or e.lower() not in known}
    answers.update((p, load_live(cache_dir, p)) for p in codexbar)
    asked = setting + list(codexbar)
    stale = [p for p in asked if not answers[p] or now - answers[p]["at"] > (LIVE_FRESH if ask_until else LIVE_STALE)]
    if stale and ask_until:
        answers.update(ask_all(cache_dir, stale, ask_until))
    elif stale and cache_dir and shutil.which("codex"):
        spawn("--codex", cache_dir, *stale)
    out = []
    for path in asked + (others if VIEW["otherCodex"] else []):
        live, answer, email = path in answers, answers.get(path), answer_email(answers.get(path))
        if path in codexbar and email and email in known:
            continue
        logs = codex_logs(path, now)
        answer = None if answer and answer.get("failed") else answer
        provider = logs
        if answer and (logs["state"] != "ok" or answer["at"] >= (logs["read_at"] or 0)):
            provider = from_live(answer, now)
        provider.update(home="CodexBar · " + (codexbar[path] or "account") if path in codexbar else tilde(path, home),
                        asked=live, codexbar=path in codexbar)
        provider["name"] = provider.get("who") or provider["home"]
        provider["since"] = "update" if provider.get("source") == "live" else "run"
        if live or provider["state"] == "ok" or os.path.exists(os.path.join(path, "auth.json")):
            out.append(provider)
            if email:
                known.add(email)
    return out


# Gemini


def gemini_state():
    return "installed" if shutil.which("gemini") else "missing"


def limits(run, now=None, ask_until=None):
    """Every provider's limits as a dict."""
    home = os.path.expanduser("~")
    now = time.time() if now is None else now
    return {"now": now, "claude": claude_limits(home, now), "codex": codex_accounts(home, now, run, ask_until),
            "gemini": gemini_state()}


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
    data = read_json(os.path.join(cache_dir, CACHE_FILE)) if cache_dir else None
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
    write_json(os.path.join(cache_dir, CACHE_FILE), {"version": CACHE_VERSION, "files": files})


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


def take_lock(cache_dir, name=LOCK_FILE):
    """A refresh lock, held until its file is closed; None while another refresh holds it."""
    os.makedirs(cache_dir, exist_ok=True)
    handle = open(os.path.join(cache_dir, name), "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


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
            spawn("--refresh", cache_dir)
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
    """A share used in its tone: amber from 60%, red from 85%."""
    return "alert" if percent >= ALERT else "warn" if percent >= WARN else None


def shown(w):
    """The share used a window shows now: 0 once it reset since it was read."""
    return 0.0 if w["reset_since"] else w["percent"]


def left(w):
    return 100.0 - shown(w)


def figure(w):
    """The share a window shows, as set: left (CodexBar's way) or used."""
    return shown(w) if VIEW["show"] == "used" else left(w)


def figure_text(w):
    return "%d%% %s" % (round(figure(w)), "used" if VIEW["show"] == "used" else "left")


def reset_note(w, since):
    return "reset since your last %s (was %d%% used)" % (since, round(w["percent"]))


def pace(w, now):
    """(mark, words) for a window, CodexBar's pace: the mark where its share left should be by now if used evenly, and
    how far ahead or behind that it is, whether it lasts until the reset at this rate (and the headroom) or when it runs
    out. (None, None) without a reset time and length, once it reset, or under 3% into the window; no mark when on pace
    (within 2 points), so the mark shows exactly when the words say reserve or deficit."""
    if w["reset_since"] or not w["resets_at"] or not w["minutes"] or not VIEW["pace"]:
        return None, None
    length = w["minutes"] * 60.0
    remaining = w["resets_at"] - now
    elapsed = length - remaining
    if remaining <= 0 or elapsed <= 0:
        return None, None
    expected = 100.0 * elapsed / length
    if expected < 3:
        return None, None
    used = w["percent"]
    mark = round(expected if VIEW["show"] == "used" else 100.0 - expected, 1)
    if used >= 100:
        return mark, "Used up until the reset"
    gap = expected - used
    if abs(gap) <= 2:
        mark, words = None, ["On pace"]
    else:
        words = ["%d%% in reserve" % round(gap) if gap > 0 else "%d%% in deficit" % round(-gap)]
    rate = used / elapsed
    if used + rate * remaining <= 100:
        words.append("Lasts until reset")
        if used > 0:
            headroom = (100 - used) / (rate * remaining)
            words.append(("%.1fx headroom" % headroom) if headroom < 10 else "10x+ headroom")
    else:
        words.append("%s in %s" % ("Projected empty" if w["minutes"] == SESSION else "Runs out", span((100 - used) / rate)))
    return mark, " · ".join(words)


def figures_text(provider):
    return ", ".join("%s %s" % (w["name"], figure_text(w)) for w in provider["windows"])


CLAUDE_UNKNOWN = "Unknown until your next Claude Code reply."
CODEX_WORDS = {"missing": ("no logs", "Codex didn't answer and has no logs here. Run Codex once."),
               "none": ("no run in 8 days", "No Codex run in 8 days, so both windows have reset."),
               "signed_out": ("signed out", "Codex is signed out here. Run `codex login` in a terminal.")}
NOT_ASKED = ("unavailable", "Not asked: Pilot asks Codex only for the folders in this extension's Codex accounts "
                            "setting, and this one has no run in 8 days.")
CODEXBAR_WORDS = ("unavailable", "Codex didn't answer for this CodexBar account. Reauthenticate it in CodexBar's "
                                  "Settings › Providers › Codex.")
GEMINI_WORDS = {"missing": ("not set up", "The gemini CLI isn't installed on this Mac."),
                "installed": ("limit unknown", "Gemini keeps no limit on this Mac, and asking Google needs your sign-in "
                                               "token, which Pilot never reads.")}
FOOTNOTE = "Codex's numbers come from Codex itself. Pilot reads no sign-in tokens."


def codex_words(codex):
    if codex["codexbar"]:
        return CODEXBAR_WORDS
    return CODEX_WORDS[codex["state"]] if codex["asked"] else NOT_ASKED


def codex_read(codex, now):
    """When an account's numbers were read: updated 16:23 (Codex's answer), last run Tue 22:35 (its logs)."""
    if not codex["read_at"]:
        return "last run unknown"
    return ("updated " if codex["source"] == "live" else "last run ") + stamp(codex["read_at"], now)


def summary_line(data):
    claude = data["claude"]
    parts = ["Claude: " + figures_text(claude) if claude["state"] == "ok" else "Claude unknown"]
    known = [c for c in data["codex"] if c["state"] == "ok"]
    for codex in known:
        parts.append("Codex%s: %s" % (" " + codex["name"] if len(known) > 1 else "", figures_text(codex)))
    return " · ".join(parts)


# Blocks: a section card per provider and account (Surfaces v2)


def window_note(w, since, now):
    """What a window's line says under its bar: that it reset, its pace, or that its reset time is unknown; None."""
    if w["reset_since"]:
        return reset_note(w, since)
    return pace(w, now)[1] or (None if w["resets_at"] else "reset time unknown")


def meter(w, since, now):
    """A window as CodexBar draws it: the share left (or used, as set), filled; the pace mark and words; the reset
    countdown."""
    m = {"type": "meter", "label": w["name"], "percent": round(figure(w), 1), "value": figure_text(w)}
    note = window_note(w, since, now)
    if not w["reset_since"]:
        m["tone"] = tone(w["percent"]) or "fine"
        mark = pace(w, now)[0]
        if mark is not None:
            m["mark"] = mark
        if w["resets_at"]:
            m["resetsAt"] = int(w["resets_at"])
    if note:
        m["note"] = note
    return m


def subtitle(*parts):
    return " · ".join(p for p in parts if p)


def claude_card(claude, now):
    if claude["state"] != "ok":
        return {"type": "section", "title": "Claude", "subtitle": subtitle(claude.get("who"), claude.get("plan"), "unknown"),
                "blocks": [{"type": "markdown", "text": CLAUDE_UNKNOWN}]}
    return {"type": "section", "title": "Claude",
            "subtitle": subtitle(claude.get("who"), claude.get("plan"), "read " + stamp(claude["read_at"], now)),
            "blocks": [meter(w, "Claude Code reply", now) for w in claude["windows"]]}


def reset_credits_text(found, now):
    if not found or found["count"] <= 0:
        return None
    text = "**Limit reset credits:** %d available" % found["count"]
    if found["expires"]:
        text += " · expire " + ", ".join(stamp_ahead(t, now) for t in found["expires"])
    return text


def stamp_ahead(moment, now):
    """A day ahead, short: 29 Oct 19:35 (with the year when it is not this one)."""
    day = time.localtime(moment)
    return time.strftime("%-d %b %H:%M" if day.tm_year == time.localtime(now).tm_year else "%-d %b %Y %H:%M", day)


def codex_card(codex, now):
    if codex["state"] != "ok":
        words, text = codex_words(codex)
        return {"type": "section", "title": "Codex", "subtitle": subtitle(codex["name"], words),
                "blocks": [{"type": "markdown", "text": text}]}
    blocks = [meter(w, codex["since"], now) for w in codex["windows"]]
    credits = reset_credits_text(codex.get("reset_credits"), now)
    if credits:
        blocks.append({"type": "markdown", "text": credits})
    if codex["credits"] is not None:
        blocks.append({"type": "stats", "items": [{"label": "Credits", "value": codex["credits"]}]})
    return {"type": "section", "title": "Codex", "subtitle": subtitle(codex["name"], codex.get("plan"), codex_read(codex, now)),
            "blocks": blocks}


def gemini_card(state):
    words, text = GEMINI_WORDS[state]
    blocks = [{"type": "markdown", "text": text}]
    if state == "missing":
        blocks.append({"type": "buttons", "actions": [SET_UP_AI]})
    return {"type": "section", "title": "Gemini", "subtitle": words, "blocks": blocks}


def cards(data):
    now = data["now"]
    out = [claude_card(data["claude"], now)] + [codex_card(c, now) for c in data["codex"]]
    return out + [gemini_card(data["gemini"])] if VIEW["gemini"] else out


def token_note(tokens):
    if tokens["state"] != "ok":
        return "No Claude Code logs on this Mac (~/.claude/projects), so no token counts."
    block = tokens["block"]
    words = "Claude Code tokens: %s in the last 7 days, %s in this 5-hour block. They come from Claude Code's own " \
            "logs and aren't your limit." % (short_tokens(tokens["week"]), "none" if block is None else short_tokens(block))
    if not tokens["complete"]:
        words = "Still counting Claude Code's logs, so these grow on the next refresh. " + words
    return words


def bar_line(data):
    """The menu bar's words, CodexBar's: each provider set to show, its mark and its session left (✳ Claude, ⊛ Codex's
    first account with numbers), or the most used window, or used, as set; in the tone of the most used window among
    them; AI when none is known."""
    parts, used = [], []
    codex = next((c for c in data["codex"] if c["state"] == "ok"), None)
    for mark, name, provider in (("✳", "claude", data["claude"]), ("⊛", "codex", codex)):
        if provider and provider["state"] == "ok" and VIEW["barProviders"] in ("both", name):
            windows = provider["windows"]
            w = max(windows, key=shown) if VIEW["barWindow"] == "tightest" else windows[0]
            parts.append("%s%d%%" % (mark, round(figure(w))))
            used += [shown(w) for w in windows]
    return (" ".join(parts) or "AI"), tone(max(used, default=0))


# Commands


def show_bar(run):
    if run.get("surface") == "popover":
        data = limits(run, ask_until=START + LIVE_WAIT)
        send({"type": "blocks", "blocks": cards(data) + [{"type": "markdown", "text": FOOTNOTE}]})
        return
    data = limits(run)
    text, level = bar_line(data)
    send({"type": "bar", "text": text, "tone": level, "blocks": cards(data)})


def show_page(run):
    data = limits(run, ask_until=START + PAGE_WAIT)
    tokens = claude_tokens(run, data["now"], START + PAGE_SCAN)
    send({"type": "blocks", "blocks": cards(data) + [
        {"type": "markdown", "text": token_note(tokens) + " " + FOOTNOTE},
        {"type": "buttons", "actions": [{"type": "copy", "text": summary_line(data), "title": "Copy summary"}]}]})


def bar(fraction):
    filled = max(0, min(BAR, int(round(fraction * BAR))))
    return "`%s%s`" % ("▓" * filled, "░" * (BAR - filled))


def windows_markdown(provider, since, now, md, rows, title):
    for w in provider["windows"]:
        reset = "resets " + until(w["resets_at"], now) if w["resets_at"] and not w["reset_since"] else None
        text = " · ".join(p for p in (figure_text(w), reset, window_note(w, since, now)) if p)
        md.append("- **%s** %s %s" % (w["name"], bar(figure(w) / 100.0), text))
        rows.append({"label": "%s %s" % (title, w["name"].lower()), "text": text})


def show_usage(run):
    data = limits(run)
    now, claude = data["now"], data["claude"]
    tokens = claude_tokens(run, now, START + DETAIL_SCAN)
    md, rows = ["## Claude"], []
    if claude["state"] == "ok":
        windows_markdown(claude, "Claude Code reply", now, md, rows, "Claude")
        md += ["", "%s, read %s." % (subtitle(claude.get("who"), claude.get("plan")) or "Claude Code",
                                     stamp(claude["read_at"], now))]
    else:
        md.append(CLAUDE_UNKNOWN)
        rows.append({"label": "Claude", "text": "unknown"})
    for codex in data["codex"]:
        md += ["", "## Codex · %s" % codex["name"]]
        if codex["state"] == "ok":
            windows_markdown(codex, codex["since"], now, md, rows, "Codex")
            credits = reset_credits_text(codex.get("reset_credits"), now)
            if credits:
                md.append("- " + credits)
            if codex["credits"] is not None:
                md.append("- **Credits** %s" % codex["credits"])
                rows.append({"label": "Codex credits", "text": codex["credits"]})
            md += ["", "%s, %s." % (codex.get("plan") or "Codex", codex_read(codex, now))]
        else:
            md.append(codex_words(codex)[1])
    if VIEW["gemini"]:
        words, text = GEMINI_WORDS[data["gemini"]]
        md += ["", "## Gemini", "**%s.** %s" % (words.capitalize(), text)]
    md += ["", "---", "", token_note(tokens), "", FOOTNOTE]
    send({"type": "detail", "markdown": "\n".join(md) + "\n", "metadata": rows,
          "actions": [{"type": "copy", "text": summary_line(data), "title": "Copy summary"}]})


def show_summary(run):
    send({"type": "hud", "text": summary_line(limits(run, ask_until=START + LIVE_WAIT))})


def background_refresh(cache_dir):
    signal.alarm(int(REFRESH_SCAN) + 30)
    lock = take_lock(cache_dir)
    if lock is None:
        return
    try:
        refresh_claude(os.path.expanduser("~"), cache_dir, time.time(), time.monotonic() + REFRESH_SCAN)
    finally:
        lock.close()


def background_codex(cache_dir, homes):
    """Asks Codex for each account the bar found stale; one at a time across runs."""
    signal.alarm(30)
    lock = take_lock(cache_dir, CODEX_LOCK)
    if lock is None:
        return
    try:
        ask_all(cache_dir, homes, time.monotonic() + 20)
    finally:
        lock.close()


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--refresh":
        background_refresh(sys.argv[2])
        return
    if len(sys.argv) >= 4 and sys.argv[1] == "--codex":
        background_codex(sys.argv[2], sys.argv[3:])
        return
    try:
        run = json.loads(sys.stdin.readline())
    except ValueError:
        send({"type": "error", "text": "got a run message it could not read"})
        return
    configure(run)
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
