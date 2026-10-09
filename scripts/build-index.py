#!/usr/bin/env python3
"""Write index.json, the catalogue Pilot reads, from every extensions/<id>/ folder.

Run after every change to an extension. scripts/check.py fails when index.json is stale.
"""
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS = ROOT / "extensions"
INDEX = ROOT / "index.json"
SCHEMA = 1


def files(folder):
    """Every file Pilot installs from `folder`: regular files, hidden ones and Python caches left out, by path."""
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
        for name in filenames:
            if name.startswith(".") or name.endswith(".pyc"):
                continue
            path = Path(dirpath) / name
            if not path.is_file() or path.is_symlink():
                continue
            data = path.read_bytes()
            found.append({
                "path": path.relative_to(folder).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "executable": bool(path.stat().st_mode & stat.S_IXUSR),
            })
    return sorted(found, key=lambda f: f["path"])


def entry(folder):
    """One extension's index entry, read from its manifest."""
    manifest = json.loads((folder / "extension.json").read_text(encoding="utf-8"))
    permissions = manifest.get("permissions") or {}
    return {
        "id": manifest["id"],
        "title": manifest["title"],
        "icon": manifest.get("icon"),
        "version": manifest["version"],
        "description": manifest["description"],
        "requires": list(manifest.get("requires") or []),
        "commands": [{"name": c["name"], "title": c["title"], "mode": c["mode"]}
                     for c in manifest["commands"] if not c.get("hidden")],
        "permissions": {
            "network": list(permissions.get("network") or []),
            "clipboard": bool(permissions.get("clipboard", False)),
            "files": list(permissions.get("files") or []),
            "actions": list(permissions.get("actions") or []),
        },
        "files": files(folder),
    }


def folders():
    return sorted(p for p in EXTENSIONS.iterdir() if p.is_dir() and not p.name.startswith("."))


def build():
    return {"schema": SCHEMA, "extensions": [entry(f) for f in folders()]}


def render(index):
    return json.dumps(index, indent=2, ensure_ascii=False) + "\n"


def main():
    text = render(build())
    INDEX.write_text(text, encoding="utf-8")
    count = len(json.loads(text)["extensions"])
    print("index.json: %d extension%s" % (count, "" if count == 1 else "s"))


if __name__ == "__main__":
    sys.exit(main())
