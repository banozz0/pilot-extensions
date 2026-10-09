"""Runs an extension command the way Pilot does (ADR 0010, Sources/PilotCore/ExtensionRun.swift).

The program runs in its own folder with a small fixed environment (HOME, USER, TMPDIR, PATH of its folder, Homebrew's
and the system's, LANG, PILOT_EXTENSION_ID), gets one JSON run message on stdin, which is then closed, and must answer in
JSON lines within Pilot's limits: 2 s for a list or detail, 30 s for a no-view command.

Two test-only additions, both prepended to PATH after the program's folder: a `shims` folder a test fills with fake
`open`, `pbcopy`, `pbpaste`, `gh`… so nothing reaches Sven's screen, clipboard or accounts; and, when PILOT_EXT_PYTHON
names an interpreter (e.g. /usr/bin/python3, macOS's 3.9), a `python3` that runs it, so every program is proven on
the oldest Python a Mac ships.
"""
import functools
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS = ROOT / "extensions"
SYSTEM_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TIMEOUTS = {"list": 2.0, "detail": 2.0, "no-view": 30.0}
# Each action type and the field it needs; an item carries the first seven, a no-view run may also print the last two.
ITEM_ACTIONS = {"open": "url", "copy": "text", "paste": "text", "push": "command", "run": "command", "hud": "text",
                "pilot": "action"}
ACTIONS = dict(ITEM_ACTIONS, timer="seconds", alert="text")

_spec = importlib.util.spec_from_file_location("check", ROOT / "scripts" / "check.py")
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


@functools.lru_cache(maxsize=None)
def _manifest(folder):
    return json.loads((Path(folder) / "extension.json").read_text(encoding="utf-8"))


def manifest(ext_id, folder=None):
    return _manifest(str(folder or EXTENSIONS / ext_id))


def resolved_preferences(m, saved):
    """Pilot's Preference.resolved: the saved value when it fits, else the default; a checkbox with neither is false,
    any other type left out."""
    values = {}
    for p in m.get("preferences", []):
        value = saved.get(p["name"])
        kind = p["type"]
        if value is not None and not check.fits(kind, p.get("data") or [], value):
            value = None
        if value is None:
            value = p.get("default")
        if value is None and kind == "checkbox":
            value = False
        if value is not None:
            values[p["name"]] = value
    return values


class Result:
    def __init__(self, stdout, stderr, code, elapsed):
        self.stdout, self.stderr, self.code, self.elapsed = stdout, stderr, code, elapsed
        self.messages = []
        self.plain = []
        for line in (line for line in stdout.splitlines() if line.strip()):
            try:
                message = json.loads(line)
            except ValueError:
                self.plain.append(line)
                continue
            if isinstance(message, dict):
                self.messages.append(message)
            else:
                self.plain.append(line)

    def of_type(self, kind):
        return [m for m in self.messages if m.get("type") == kind]

    @property
    def items(self):
        lists = self.of_type("list")
        return lists[-1]["items"] if lists else None

    @property
    def detail(self):
        details = self.of_type("detail")
        return details[-1] if details else None

    @property
    def actions(self):
        """The no-view actions, in printed order."""
        return [m["action"] for m in self.of_type("action")]


class Sandbox:
    """Temporary home, data, cache and shim folders for one test."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="pilot-ext-test-"))
        self.home = self.root / "home"
        self.data = self.root / "data"
        self.cache = self.root / "cache"
        self.tmp = self.root / "tmp"
        self.shims = self.root / "shims"
        self.log = self.root / "shim.log"
        for p in (self.home, self.data, self.cache, self.tmp, self.shims):
            p.mkdir()
        python = os.environ.get("PILOT_EXT_PYTHON")
        if python:
            self.shim("python3", 'exec "%s" "$@"' % python)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def shim(self, name, body=None):
        """A fake command on PATH; by default it logs its name and arguments, one line each, and prints nothing."""
        if body is None:
            body = 'printf "%%s\\n" "%s $*" >> "%s"' % (name, self.log)
        path = self.shims / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        return path

    def shim_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def run(self, ext_id, command, query=None, context=None, preferences=None, home=None, timeout=None, folder=None):
        """Runs `command`, from `folder` when given (an installed copy), else from the repo."""
        folder = Path(folder or EXTENSIONS / ext_id)
        m = manifest(ext_id, folder)
        spec = next(c for c in m["commands"] if c["name"] == command)
        program = (folder / spec["run"]).resolve()
        message = {"type": "run", "extension": ext_id, "command": command,
                   "preferences": resolved_preferences(m, preferences or {}),
                   "paths": {"data": str(self.data), "cache": str(self.cache)}}
        if query is not None or spec["mode"] in ("list", "detail"):
            message["query"] = query or ""
        if context is not None:
            message["context"] = context
        env = {"HOME": str(home or self.home), "USER": os.environ.get("USER", "sven"), "TMPDIR": str(self.tmp) + "/",
               "PATH": ":".join([str(program.parent), str(self.shims), SYSTEM_PATH]),
               "LANG": "en_US.UTF-8", "PILOT_EXTENSION_ID": ext_id}
        limit = TIMEOUTS[spec["mode"]] if timeout is None else timeout
        started = time.monotonic()
        proc = subprocess.Popen([str(program)], cwd=str(program.parent), env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        line = json.dumps(message, sort_keys=True, separators=(",", ":")) + "\n"
        try:
            out, err = proc.communicate(line.encode(), timeout=limit)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise AssertionError("%s %s did not answer in %g s: Pilot would stop it" % (ext_id, command, limit))
        return Result(out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), proc.returncode,
                      time.monotonic() - started)


class ExtensionTestCase(unittest.TestCase):
    """Gives each test a Sandbox and the protocol checks every answer must pass."""

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def run_ext(self, command, **kwargs):
        result = self.box.run(self.EXTENSION, command, **kwargs)
        self.assertEqual(result.plain, [], "printed lines outside the protocol: %r" % result.plain)
        self.assertEqual(result.code, 0, "exit %d: %s" % (result.code, result.stderr[-500:]))
        return result

    def assertItemShape(self, items):
        """Every item as Pilot decodes it: a title string, optional strings, actions of known types with their field."""
        for item in items:
            self.assertIsInstance(item.get("title"), str)
            for key in ("id", "subtitle", "icon", "keywords"):
                if key in item:
                    self.assertIsInstance(item[key], str, key)
            for action in item.get("actions", []):
                self.assertActionShape(action, ITEM_ACTIONS)

    def assertActionShape(self, action, fields=ACTIONS):
        self.assertIn(action.get("type"), fields, action)
        self.assertIn(fields[action["type"]], action, action)
        if action["type"] in ("run", "push"):
            names = {c["name"]: c for c in manifest(self.EXTENSION)["commands"]}
            self.assertIn(action["command"], names, "names a command the extension lacks")
            wanted = "no-view" if action["type"] == "run" else ("list", "detail")
            self.assertIn(names[action["command"]]["mode"], wanted, action)
