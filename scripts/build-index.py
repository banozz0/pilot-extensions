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
        "examples": list(manifest.get("examples") or []),
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
    if not EXTENSIONS.is_dir():
        return []
    return sorted(p for p in EXTENSIONS.iterdir() if p.is_dir() and not p.name.startswith("."))


def listed(folder):
    """Whether the catalogue offers `folder`: one whose manifest says `"unlisted": true` stays on Sven's Mac only, never in
    index.json (Video downloader, Q470). Pilot ignores the key; it is installed from its folder."""
    try:
        return json.loads((folder / "extension.json").read_text(encoding="utf-8")).get("unlisted") is not True
    except (OSError, ValueError, AttributeError):
        return True


def build():
    return {"schema": SCHEMA, "extensions": [entry(f) for f in folders() if listed(f)]}


def render(index):
    return json.dumps(index, indent=2, ensure_ascii=False) + "\n"


def main():
    index = build()
    INDEX.write_text(render(index), encoding="utf-8")
    count = len(index["extensions"])
    print("index.json: %d extension%s" % (count, "" if count == 1 else "s"))


if __name__ == "__main__":
    sys.exit(main())
