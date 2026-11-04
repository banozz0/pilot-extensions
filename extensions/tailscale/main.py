#!/usr/bin/env python3
"""Tailscale for Pilot: every device on your tailnet with its OS, Tailscale IP and online state; Return copies the IP.
Its page lists them online first with counts, and its menu-bar item shows how many are online, its click lists them.

Read only (Pilot 1.1, T8): it runs `tailscale status --json` and nothing else, so it never changes a setting, logs in or
connects anything. The CLI is the one Tailscale.app installs at /usr/local/bin/tailscale (or one on PATH); it only answers
while Tailscale.app runs, and a failed, slow or unreadable answer becomes one plain row saying Tailscale isn't running.
Device names and IPs are shown only to you, never logged.
"""
import datetime
import json
import os
import shutil
import signal
import subprocess
import sys

CLI = "/usr/local/bin/tailscale"
APP = "/Applications/Tailscale.app"
WAIT = {"page": 3.0, "popover": 3.0}  # seconds; Pilot stops a page or popover after 5, a list or bar line after 2
LIST_WAIT = 1.2  # a cold python3 takes some of those 2 s
MOST_ROWS = 50  # Pilot draws 50 rows of a list block at most
NOT_RUNNING = ("Tailscale isn't running", "Open Tailscale.app, then look again")
OFFLINE_STATES = {
    "Stopped": ("Tailscale is turned off", "Turn it on in Tailscale's menu-bar item, then look again"),
    "NeedsLogin": ("Tailscale is signed out", "Sign in from Tailscale's menu-bar item, then look again"),
    "NeedsMachineAuth": ("This Mac waits for approval", "An admin of your tailnet approves it in the admin console"),
    "Starting": ("Tailscale is starting", "Look again in a moment"),
}


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")


def status(wait=LIST_WAIT):
    """The parsed `tailscale status --json`, or a (title, subtitle) row saying why there is none."""
    cli = shutil.which("tailscale") or CLI
    try:
        # Its own process group: /usr/local/bin/tailscale is a shell script whose child would outlive a plain kill.
        proc = subprocess.Popen([cli, "status", "--json"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except FileNotFoundError:
        return None, ("Tailscale isn't installed", "Install Tailscale.app from tailscale.com, then look again")
    except OSError:
        return None, NOT_RUNNING
    try:
        out, _ = proc.communicate(timeout=wait)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return None, NOT_RUNNING
    try:
        data = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        data = None
    if proc.returncode != 0 or not isinstance(data, dict):
        return None, NOT_RUNNING
    state = data.get("BackendState")
    if state != "Running":
        return None, OFFLINE_STATES.get(state, ("Tailscale isn't connected (%s)" % state, "Open Tailscale.app"))
    return data, None


def ipv4(ips):
    return next((ip for ip in ips if "." in ip), None)


def ipv6(ips):
    return next((ip for ip in ips if ":" in ip), None)


def ago(stamp, now):
    """"last seen 5 min ago" from an RFC 3339 time; a date past a week; None when Tailscale never saw it (year 1)."""
    try:
        seen = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None
    if seen.year < 2000:
        return None
    minutes = int((now - seen).total_seconds() // 60)
    if minutes < 1:
        return "last seen just now"
    if minutes < 60:
        return "last seen %d min ago" % minutes
    if minutes < 24 * 60:
        return "last seen %d h ago" % (minutes // 60)
    if minutes < 7 * 24 * 60:
        return "last seen %d d ago" % (minutes // (24 * 60))
    return "last seen %s" % seen.strftime("%Y-%m-%d")


def device_item(node, me, now):
    name = node.get("HostName") or (node.get("DNSName") or "").split(".")[0] or "unnamed device"
    dns = (node.get("DNSName") or "").rstrip(".")
    ips = [ip for ip in node.get("TailscaleIPs") or [] if isinstance(ip, str)]
    v4, v6 = ipv4(ips), ipv6(ips)
    online = bool(node.get("Online")) or me
    state = "this Mac" if me else "online" if online else (ago(node.get("LastSeen"), now) or "offline")
    if node.get("ExitNode"):
        state += ", your exit node"
    item = {"id": node.get("ID") or dns or name, "title": name, "icon": "🟢" if online else "⚪",
            "subtitle": " · ".join(x for x in (node.get("OS"), v4 or v6, state) if x),
            "keywords": " ".join(x for x in [dns, "online" if online else "offline"] + ips if x)}
    actions = []
    if v4 or v6:
        actions.append({"type": "copy", "text": v4 or v6, "title": "Copy IP %s" % (v4 or v6)})
    if v4 and v6:
        actions.append({"type": "copy", "text": v6, "title": "Copy IPv6 %s" % v6})
    if dns:
        actions.append({"type": "copy", "text": dns, "title": "Copy name %s" % dns})
    if actions:
        item["actions"] = actions
    return item, (0 if me else 1 if online else 2, name.lower())


def reason_row(why):
    return {"title": why[0], "subtitle": why[1], "icon": "🛜",
            "actions": [{"type": "open", "url": APP, "title": "Open Tailscale"}]}


def device_rows(data, now=None):
    """(item, online) per device: this Mac, then the online ones, then the offline ones, each by name."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    rows = []
    if isinstance(data.get("Self"), dict):
        rows.append(device_item(data["Self"], True, now))
    peers = data.get("Peer") if isinstance(data.get("Peer"), dict) else {}
    rows += [device_item(p, False, now) for p in peers.values() if isinstance(p, dict)]
    rows.sort(key=lambda row: row[1])
    return [(item, rank < 2) for item, (rank, _) in rows]


def devices(now=None):
    data, why = status()
    if why:
        return [reason_row(why)]
    return [item for item, _ in device_rows(data, now)] or [{"title": "No devices on your tailnet", "icon": "🛜"}]


def lists(rows, most):
    """An Online list and an Offline list, at most `most` rows in all."""
    online = [item for item, on in rows if on][:most]
    offline = [item for item, on in rows if not on][:max(0, most - len(online))]
    blocks = [{"type": "list", "title": title, "items": items}
              for title, items in (("Online", online), ("Offline", offline)) if items]
    return blocks or [{"type": "list", "items": [{"title": "No devices on your tailnet", "icon": "🛜"}]}]


def page(run):
    """Counts, then every device, online ones first."""
    data, why = status(WAIT["page"])
    if why:
        send({"type": "blocks", "blocks": [{"type": "list", "items": [reason_row(why)]}]})
        return
    rows = device_rows(data)
    online = sum(1 for _, on in rows if on)
    stats = {"type": "stats", "items": [{"label": "Online", "value": str(online), "tone": "fine"},
                                        {"label": "Offline", "value": str(len(rows) - online)},
                                        {"label": "Devices", "value": str(len(rows))}]}
    send({"type": "blocks", "blocks": [stats] + lists(rows, MOST_ROWS)})


def bar(run):
    """🛜 and how many devices are online, this Mac counted; a click (surface popover) lists them."""
    popover = run.get("surface") == "popover"
    data, why = status(WAIT["popover"] if popover else LIST_WAIT)
    if why:
        blocks = [{"type": "list", "items": [reason_row(why)]}]
        line = {"type": "bar", "text": "🛜", "tone": "warn", "blocks": blocks}
    else:
        rows = device_rows(data)
        blocks = lists(rows, MOST_ROWS)
        line = {"type": "bar", "text": "🛜 %d" % sum(1 for _, on in rows if on), "blocks": blocks}
    send({"type": "blocks", "blocks": blocks} if popover else line)


def main():
    try:
        run = json.loads(sys.stdin.readline())
    except ValueError:
        send({"type": "error", "text": "got a run message it could not read"})
        return
    command = run.get("command")
    if command == "devices":
        send({"type": "list", "items": devices()})
    elif command in ("page", "bar"):
        (page if command == "page" else bar)(run)
    else:
        send({"type": "error", "text": "has no command %s" % run.get("command")})


if __name__ == "__main__":
    main()
