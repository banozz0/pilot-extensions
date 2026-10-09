"""Installs every catalogue entry the way Pilot's catalogue does (PLAN section 3) into a temporary Pilot-like folder and
proves the result is an extension Pilot reads: files from index.json only, sha256 checked, executables 0755 and the
rest 0644, the folder named by its id."""
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import EXTENSIONS, ROOT, SYSTEM_PATH

spec = importlib.util.spec_from_file_location("check", ROOT / "scripts" / "check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class CatalogueInstallTests(unittest.TestCase):
    def setUp(self):
        self.index = json.loads((ROOT / "index.json").read_text(encoding="utf-8"))
        self.pilot = Path(tempfile.mkdtemp(prefix="pilot-catalogue-"))
        self.addCleanup(shutil.rmtree, self.pilot, True)

    def install(self, entry):
        folder = self.pilot / "Extensions" / entry["id"]
        for f in entry["files"]:
            self.assertFalse(f["path"].startswith("/") or ".." in f["path"].split("/"), f["path"])
            data = (EXTENSIONS / entry["id"] / f["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), f["sha256"], "%s/%s" % (entry["id"], f["path"]))
            self.assertEqual(len(data), f["size"])
            target = folder / f["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o755 if f["executable"] else 0o644)
        return folder

    def test_index_lists_every_extension_folder(self):
        self.assertEqual(self.index["schema"], 1)
        self.assertEqual(sorted(e["id"] for e in self.index["extensions"]),
                         sorted(p.name for p in EXTENSIONS.iterdir() if p.is_dir()))

    def test_every_entry_installs_into_a_folder_pilot_reads(self):
        for entry in self.index["extensions"]:
            with self.subTest(entry["id"]):
                folder = self.install(entry)
                self.assertEqual(check.check(folder), [])
                manifest = json.loads((folder / "extension.json").read_text(encoding="utf-8"))
                shown = [c["name"] for c in manifest["commands"] if not c.get("hidden")]
                self.assertEqual([c["name"] for c in entry["commands"]], shown)
                self.assertEqual(entry["version"], manifest["version"])
                for c in manifest["commands"]:
                    mode = (folder / c["run"]).stat().st_mode
                    self.assertTrue(mode & stat.S_IXUSR, c["run"])

    def test_an_installed_program_answers_from_its_new_folder(self):
        """One command of each installed extension runs from the temp folder, not from the repo."""
        for entry in self.index["extensions"]:
            with self.subTest(entry["id"]):
                folder = self.install(entry)
                manifest = json.loads((folder / "extension.json").read_text(encoding="utf-8"))
                command = next(c for c in manifest["commands"] if c["mode"] == "detail" or c["mode"] == "list")
                data = self.pilot / "ExtensionData" / entry["id"] / "data"
                cache = self.pilot / "Caches" / entry["id"]
                data.mkdir(parents=True)
                cache.mkdir(parents=True)
                shims = self.pilot / "shims"
                shims.mkdir(exist_ok=True)
                for name, body in (("pbpaste", "echo hello"), ("pbcopy", "cat >/dev/null"), ("open", "exit 0"), ("gh", "exit 1")):
                    (shims / name).write_text("#!/bin/sh\n" + body + "\n")
                    (shims / name).chmod(0o755)
                program = folder / command["run"]
                message = {"type": "run", "extension": entry["id"], "command": command["name"], "query": "",
                           "preferences": {}, "paths": {"data": str(data), "cache": str(cache)}}
                env = {"HOME": str(self.pilot), "USER": os.environ.get("USER", "sven"), "TMPDIR": str(self.pilot) + "/",
                       "PATH": ":".join([str(folder), str(shims), SYSTEM_PATH]), "LANG": "en_US.UTF-8", "PILOT_EXTENSION_ID": entry["id"]}
                out = subprocess.run([str(program)], cwd=str(folder), env=env, input=json.dumps(message) + "\n",
                                     capture_output=True, text=True, timeout=2).stdout
                kinds = [json.loads(line)["type"] for line in out.splitlines() if line.strip()]
                self.assertTrue(set(kinds) & {"list", "detail", "error"}, out)
