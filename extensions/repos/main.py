#!/usr/bin/env python3
"""Repos for Pilot: the git repos under a folder with their branch and whether they have uncommitted changes.

The branch and remote are read from each repo's .git files, no git process. The changes come from
`git --no-optional-locks status --porcelain`, eight at a time under a deadline that keeps the list inside Pilot's 2 s;
a repo that does not answer in time shows what paths.cache/repos.json last saw. The hidden open-terminal and lazygit
commands open a window through `open`, so the program never holds it.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait

MAX_DEPTH = 3
SKIP = {"node_modules", "build", "dist", "target", "DerivedData", "Pods", "venv", "__pycache__"}
STATUS_BUDGET = 1.2
WORKERS = 8
GHOSTTY = ["/Applications/Ghostty.app", os.path.expanduser("~/Applications/Ghostty.app")]


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")


def find_repos(root):
    """Folders holding .git, at most MAX_DEPTH below `root` (or `root` itself); a repo's insides are not searched."""
    if os.path.exists(os.path.join(root, ".git")):
        return [root]
    found = []

    def walk(path, depth):
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            if e.name.startswith(".") or e.name in SKIP or not e.is_dir(follow_symlinks=False):
                continue
            if os.path.exists(os.path.join(e.path, ".git")):
                found.append(e.path)
            elif depth < MAX_DEPTH:
                walk(e.path, depth + 1)

    walk(root, 1)
    return found


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def git_dirs(repo):
    """The repo's own git folder and the common one (they differ in a linked worktree)."""
    dot = os.path.join(repo, ".git")
    if os.path.isdir(dot):
        return dot, dot
    line = read(dot).strip()
    own = line[len("gitdir:"):].strip() if line.startswith("gitdir:") else dot
    own = os.path.normpath(os.path.join(repo, own))
    common = read(os.path.join(own, "commondir")).strip()
    return own, os.path.normpath(os.path.join(own, common)) if common else own


def branch(own):
    head = read(os.path.join(own, "HEAD")).strip()
    if head.startswith("ref:"):
        return head.split("refs/heads/", 1)[-1]
    return (head[:7] + " (detached)") if head else "no branch"


def origin(common):
    """The origin remote's URL from the repo's config; None without one."""
    section = None
    for raw in read(os.path.join(common, "config")).splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line
        elif section == '[remote "origin"]' and line.replace(" ", "").startswith("url="):
            return line.split("=", 1)[1].strip()
    return None


def web_url(remote):
    """An https page for a git remote: git@host:o/r.git, ssh://git@host/o/r, https://user@host/o/r.git."""
    if not remote:
        return None
    url = remote
    if url.startswith("git@") and ":" in url:
        host, path = url[4:].split(":", 1)
    elif url.startswith(("ssh://", "https://", "http://", "git://")):
        rest = url.split("://", 1)[1]
        host, _, path = rest.partition("/")
        host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    else:
        return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return "https://%s/%s" % (host, path) if host and path else None


def activity(own):
    times = []
    for name in ("index", "HEAD"):
        try:
            times.append(os.stat(os.path.join(own, name)).st_mtime)
        except OSError:
            pass
    return max(times, default=0)


def status(repo, deadline):
    """True when `repo` has uncommitted changes, False when clean, None when git did not answer by `deadline`."""
    left = deadline - time.monotonic()
    if left <= 0.05:
        return None
    try:
        done = subprocess.run(["git", "--no-optional-locks", "-C", repo, "status", "--porcelain",
                               "--ignore-submodules=dirty"], capture_output=True, timeout=left)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return bool(done.stdout.strip()) if done.returncode == 0 else None


def changes(repos, deadline):
    """{repo: True when it has uncommitted changes, False when clean}; repos not answered by `deadline` left out."""
    if not repos:
        return {}
    pool = ThreadPoolExecutor(WORKERS)
    futures = {pool.submit(status, repo, deadline): repo for repo in repos}
    wait(futures, timeout=max(0, deadline - time.monotonic()) + 0.2)
    pool.shutdown(wait=False, cancel_futures=True)
    return {futures[f]: f.result() for f in futures if f.done() and not f.cancelled() and f.result() is not None}


def load_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
        return cache if isinstance(cache, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(path, cache):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def terminal_app(preferences):
    """The chosen app's path, else Ghostty when installed, else Terminal."""
    chosen = preferences.get("terminal")
    if isinstance(chosen, str) and chosen and os.path.isdir(chosen):
        return chosen
    return next((g for g in GHOSTTY if os.path.isdir(g)), "Terminal")


def app_name(app):
    return os.path.basename(app)[:-4] if app.endswith(".app") else app


def is_ghostty(app):
    return app_name(app).lower() == "ghostty"


def tilde(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def list_repos(run, started):
    preferences = run.get("preferences") or {}
    root = os.path.abspath(os.path.expanduser(preferences.get("folder") or "~/code"))
    if not os.path.isdir(root):
        send({"type": "error", "text": "has no folder %s: set Repos folder on the Extensions page" % tilde(root)})
        return
    repos = find_repos(root)
    cache_path = os.path.join(cache_dir(run), "repos.json")
    cache = load_cache(cache_path)
    cache.update(changes(repos, started + STATUS_BUDGET))
    save_cache(cache_path, {r: cache[r] for r in repos if r in cache})
    terminal = app_name(terminal_app(preferences))
    has_lazygit = shutil.which("lazygit") is not None
    rows = []
    for repo in repos:
        own, common = git_dirs(repo)
        rows.append((activity(own), repo, own, common))
    rows.sort(key=lambda r: -r[0])
    items = []
    for _, repo, own, common in rows:
        name = os.path.relpath(repo, root) if repo != root else os.path.basename(repo)
        on = branch(own)
        dirty = cache.get(repo)
        state = "uncommitted changes" if dirty else ("clean" if dirty is False else None)
        url = web_url(origin(common))
        actions = [{"type": "run", "command": "open-terminal", "title": "Open in %s" % terminal, "context": {"path": repo}}]
        if has_lazygit:
            actions.append({"type": "run", "command": "lazygit", "title": "Open lazygit", "context": {"path": repo}})
        actions.append({"type": "open", "url": repo, "title": "Open in Finder"})
        if url:
            host = url.split("/")[2]
            label = "GitHub" if host == "github.com" else host
            actions.append({"type": "open", "url": url, "title": "Open on %s" % label})
        actions.append({"type": "copy", "text": repo, "title": "Copy path"})
        items.append({"id": repo, "title": name, "subtitle": " · ".join(x for x in (on, state, tilde(repo)) if x),
                      "icon": "✏️" if dirty else "🌿", "keywords": "%s %s" % (on, os.path.basename(repo)),
                      "actions": actions})
    if not items:
        items = [{"title": "No git repos in %s" % tilde(root), "subtitle": "Set Repos folder on the Extensions page"}]
    send({"type": "list", "items": items})


def cache_dir(run):
    return (run.get("paths") or {}).get("cache") or os.environ.get("TMPDIR", "/tmp")


def open_with(arguments):
    done = subprocess.run(["open"] + arguments, capture_output=True, text=True, timeout=10)
    return None if done.returncode == 0 else (done.stderr.strip() or "open failed")[:160]


def launch(run, command=None):
    """Opens a terminal window in the repo the context names, running `command` (a path) when given: Ghostty as a new
    window there; another terminal by opening the folder, or for a command a .command file Terminal runs. The window
    belongs to the terminal, never to this program. Returns (repo, app name), or None after sending the error."""
    context = run.get("context") if isinstance(run.get("context"), dict) else {}
    repo = context.get("path")
    if not (isinstance(repo, str) and os.path.isdir(repo)):
        send({"type": "error", "text": "got no repo folder that exists"})
        return None
    app = terminal_app(run.get("preferences") or {})
    if is_ghostty(app):
        arguments = ["-na", app, "--args", "--working-directory=" + repo, "--window-save-state=never"]
        failed = open_with(arguments + (["--command=" + command] if command else []))
    elif command:
        os.makedirs(cache_dir(run), exist_ok=True)
        script = os.path.join(cache_dir(run), "%s.command" % os.path.basename(command))
        with open(script, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\ncd %s && exec %s\n" % (shlex.quote(repo), shlex.quote(command)))
        os.chmod(script, 0o755)
        app = "Terminal"
        failed = open_with(["-a", app, script])
    else:
        failed = open_with(["-a", app, repo])
    if failed:
        send({"type": "error", "text": failed})
        return None
    return repo, app_name(app)


def open_terminal(run):
    opened = launch(run)
    if opened:
        send({"type": "hud", "text": "%s in %s" % (os.path.basename(opened[0]), opened[1])})


def lazygit(run):
    binary = shutil.which("lazygit")
    if not binary:
        send({"type": "error", "text": "lazygit is not installed: brew install lazygit"})
        return
    opened = launch(run, binary)
    if opened:
        send({"type": "hud", "text": "lazygit on %s" % os.path.basename(opened[0])})


def main():
    started = time.monotonic()
    try:
        run = json.loads(sys.stdin.readline())
    except ValueError:
        send({"type": "error", "text": "got a run message it could not read"})
        return
    command = run.get("command")
    if command == "repos":
        list_repos(run, started)
    elif command == "open-terminal":
        open_terminal(run)
    elif command == "lazygit":
        lazygit(run)
    else:
        send({"type": "error", "text": "has no command %s" % command})


if __name__ == "__main__":
    main()
