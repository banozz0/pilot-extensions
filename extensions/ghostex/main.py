#!/usr/bin/env python3
"""Ghostex sessions for Pilot: Sven's Ghostex sessions, focused, slept or woken through the `ghostex` CLI.

The list is `ghostex sessions --json --full`, read only, which answers in about 0.2 s, so it is asked on every open
within 1.2 s of starting and never cached. A session is named to the CLI by its globalRef (S..:P..:G..), passed with
--session-id, so a context can never say `all`. The hidden focus, sleep and wake commands take that ref in their
context; focus then brings Ghostex to the front with `open -a Ghostex`.
"""
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

STARTED = time.monotonic()
LIST_BUDGET = 1.2  # from this program's start; the CLI answers in about 0.2 s, Pilot stops a list at 2 s
ACTION_TIMEOUT = 20
REF = re.compile(r"[A-Za-z0-9_-]+:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+\Z")
AGENTS = {"claude": "Claude", "codex": "Codex", "hermes-agent": "Hermes"}
# Each state's place in the list (what asks Sven something first), icon and words.
STATES = {"question": (0, "❓", "asks you something"), "working": (1, "🟢", "working"),
          "idle": (2, "⚪", "awake, idle"), "asleep": (3, "💤", "asleep"), "gone": (4, "⬛", "not running")}


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")


def first_line(text, limit=160):
    line = next((l.strip() for l in (text or "").splitlines() if l.strip()), "")
    return line[:limit]


def ghostex(arguments, timeout):
    """(parsed JSON or None, error text or None) for one ghostex call."""
    binary = shutil.which("ghostex")
    if not binary:
        return None, "ghostex is not installed: open Ghostex once, or put its CLI on your PATH"
    try:
        proc = subprocess.Popen([binary] + arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except OSError as e:
        return None, "could not run ghostex: %s" % e.strerror
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Its own process group, so a child still holding the pipe goes too and nothing waits on it.
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return None, "Ghostex did not answer in time: is it running?"
    stdout, stderr = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    try:
        answer = json.loads(stdout) if stdout.strip() else None
    except ValueError:
        answer = None
    failed = proc.returncode != 0 or (isinstance(answer, dict) and answer.get("ok") is False)
    if failed:
        said = None
        if isinstance(answer, dict):
            said = answer.get("error") or answer.get("message")
            if isinstance(said, dict):
                said = said.get("message")
        return None, first_line(said if isinstance(said, str) else stderr) or "ghostex failed"
    return answer, None


def agent_names(answer):
    """agentId → its name, from every project's custom agents (designer, moody…)."""
    names = {}
    for project in answer.get("projects") or []:
        for agent in (project or {}).get("customAgents") or []:
            if isinstance(agent, dict) and isinstance(agent.get("agentId"), str) and isinstance(agent.get("name"), str):
                names.setdefault(agent["agentId"], agent["name"])
    return names


def agent_of(session, names):
    if session.get("kind") == "terminal":
        return "terminal"
    agent = session.get("agentId") or session.get("agentName")
    if not isinstance(agent, str) or not agent:
        return None
    return names.get(agent) or AGENTS.get(agent) or agent


def state_of(session):
    status = session.get("status")
    if status == "sleep" or session.get("isSleeping"):
        return "asleep"
    if status != "running":
        return "gone"
    if (session.get("pendingQuestionCount") or 0) > 0:
        return "question"
    return "working" if session.get("activity") == "working" else "idle"


def seconds_since(stamp, now):
    if not isinstance(stamp, str):
        return None
    try:
        moment = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, now - moment.timestamp())


def ago(seconds):
    if seconds is None:
        return None
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return "%d min ago" % (seconds // 60)
    if seconds < 86400:
        return "%d h ago" % (seconds // 3600)
    return "%d d ago" % (seconds // 86400)


def sessions():
    """Every session as a list row, what asks Sven first, with its state and project; or why there are none."""
    answer, error = ghostex(["sessions", "--json", "--full"], max(0.1, STARTED + LIST_BUDGET - time.monotonic()))
    if error:
        return None, error
    sessions = answer.get("sessions") if isinstance(answer, dict) else None
    if not isinstance(sessions, list):
        return None, "ghostex sessions gave no session list"
    names = agent_names(answer)
    now = time.time()
    rows = []
    for s in sessions:
        if not isinstance(s, dict) or not isinstance(s.get("globalRef"), str) or not REF.match(s["globalRef"]):
            continue
        rows.append((s, state_of(s), seconds_since(s.get("lastInteractionAt") or s.get("updatedAt"), now)))
    rows.sort(key=lambda r: (STATES[r[1]][0], float("inf") if r[2] is None else r[2]))
    items = []
    for s, state, idle_for in rows:
        _, icon, state_words = STATES[state]
        ref = s["globalRef"]
        title = s.get("displayTitle") or s.get("title") or "Session %s" % ref.rsplit(":", 1)[-1]
        agent = agent_of(s, names)
        project = s.get("projectName") or ""
        words = state_words + (", parked" if s.get("isParked") else "")
        allowed = s.get("actions") if isinstance(s.get("actions"), dict) else {}
        asleep = state == "asleep"
        actions = [{"type": "run", "command": "focus", "title": "Wake and focus in Ghostex" if asleep else
                    "Focus in Ghostex", "context": {"ref": ref, "wake": asleep}}]
        if allowed.get("sleep") and not asleep:
            actions.append({"type": "run", "command": "sleep", "title": "Sleep", "context": {"ref": ref}})
        if allowed.get("wake") or asleep:
            actions.append({"type": "run", "command": "wake", "title": "Wake", "context": {"ref": ref}})
        items.append(({"id": ref, "title": title,
                       "subtitle": " · ".join(x for x in (project, agent, words, ago(idle_for)) if x),
                       "icon": icon,
                       "keywords": " ".join(x for x in (project, agent or "", state_words,
                                                        str(s.get("alias") or "")) if x),
                       "actions": actions}, state, project))
    return items, None


def list_sessions():
    rows, error = sessions()
    if error:
        send({"type": "list", "items": [{"title": "Ghostex sessions are not available", "subtitle": error, "icon": "⚠️"}]})
        return
    items = [row for row, _, _ in rows]
    if not items:
        items = [{"title": "No Ghostex sessions", "subtitle": "Start an agent in Ghostex and it shows here",
                  "icon": "👻"}]
    send({"type": "list", "items": items})


def page():
    """The page (Pilot story 161): how many sessions are in each state, then one list per project, its sessions in the
    list's order; a click focuses, the ⋯ menu sleeps or wakes."""
    rows, error = sessions()
    if error:
        send({"type": "blocks", "blocks": [{"type": "markdown", "text": "Ghostex sessions are not available: %s" % error}]})
        return
    count = {state: sum(1 for _, s, _ in rows if s == state) for state in STATES}
    stats = [{"label": "Ask you something", "value": str(count["question"]), "tone": "warn" if count["question"] else None},
             {"label": "Working", "value": str(count["working"]), "tone": "fine" if count["working"] else None},
             {"label": "Idle", "value": str(count["idle"])}, {"label": "Asleep", "value": str(count["asleep"])}]
    blocks = [{"type": "stats", "items": stats}]
    projects = []
    for _, _, project in rows:
        if project not in projects:
            projects.append(project)
    for project in projects:
        blocks.append({"type": "list", "title": project or "No project",
                       "items": [row for row, _, p in rows if p == project]})
    if not rows:
        blocks.append({"type": "markdown", "text": "No Ghostex sessions. Start an agent in Ghostex and it shows here."})
    send({"type": "blocks", "blocks": blocks})


def context_ref(run):
    context = run.get("context") if isinstance(run.get("context"), dict) else {}
    ref = context.get("ref")
    if not (isinstance(ref, str) and REF.match(ref)):
        send({"type": "error", "text": "got no Ghostex session to act on"})
        return None, context
    return ref, context


def focus(run):
    ref, context = context_ref(run)
    if not ref:
        return
    wake = context.get("wake") is True
    if wake:
        _, error = ghostex(["wake", "--session-id", ref, "--json"], ACTION_TIMEOUT)
        if error:
            send({"type": "error", "text": "could not wake it: %s" % error})
            return
    _, error = ghostex(["focus", "--session-id", ref, "--json"], ACTION_TIMEOUT)
    if error:
        send({"type": "error", "text": "could not focus it: %s" % error})
        return
    try:
        subprocess.run(["open", "-a", "Ghostex"], capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        pass
    send({"type": "hud", "text": "Woken and focused in Ghostex" if wake else "Focused in Ghostex"})


def set_sleep(run, verb):
    """`verb` is sleep or wake."""
    ref, _ = context_ref(run)
    if not ref:
        return
    _, error = ghostex([verb, "--session-id", ref, "--json"], ACTION_TIMEOUT)
    if error:
        send({"type": "error", "text": "could not %s it: %s" % (verb, error)})
        return
    send({"type": "hud", "text": "Session asleep" if verb == "sleep" else "Session awake"})


def main():
    try:
        run = json.loads(sys.stdin.readline())
    except ValueError:
        send({"type": "error", "text": "got a run message it could not read"})
        return
    command = run.get("command")
    if command == "sessions":
        list_sessions()
    elif command == "page":
        page()
    elif command == "focus":
        focus(run)
    elif command in ("sleep", "wake"):
        set_sleep(run, command)
    else:
        send({"type": "error", "text": "has no command %s" % command})


if __name__ == "__main__":
    main()
