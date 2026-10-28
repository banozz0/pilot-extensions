# pilot-extensions — agent brief

Private repo. Pilot's extension catalogue: each folder under `extensions/` is one Pilot extension, and `index.json` at the root is what Pilot's
Extensions page → Catalogue reads to install and update them (from `main`, through raw.githubusercontent.com or `gh api`).
Pilot itself lives in `~/code/pilot`; never edit it from here.

## Commands
- While iterating: `python3 -m unittest discover -s tests -p 'test_<id>.py'`.
- Once before landing: `python3 scripts/check.py && python3 -m unittest discover -s tests -v`, and again with `PILOT_EXT_PYTHON=/usr/bin/python3`.
- After a change Pilot should offer: `python3 scripts/build-index.py`.

## Where things are

README.md's file map shows every file with its own first line; the `file-map` tool rewrites it after you add, move or rename files.

- Each extension folder is self-contained, with no shared code outside it, since Pilot installs one folder at a time. `index.json`
  carries sha256, size and executable bit per file and leaves hidden commands out.

## Protocol
Pilot's ADR 0010 (extensions speak JSON lines, Pilot draws) is the contract, with every amendment. One JSON run message on
stdin, JSON lines on stdout, a fixed environment; list and detail answers within 2 s, no-view within 30 s, a surface or popover within
5 s, a bar line within 2 s. Manifest rules mirror Pilot's `Sources/PilotCore/Extension.swift` (github.com/banozz0/pilot). Send a block kind only when `pilot.blocks`
lists it, and keep the old way in for fields an older Pilot ignores. Read the ADR before using a block, action or field.

## Adding or changing an extension
1. Make `extensions/<id>/` with `extension.json` (`id`, `title`, emoji `icon`, `version` x.y.z, `description`, `requires`, `permissions`,
   `preferences`, `commands`) and its programs.
2. Programs: Python 3 stdlib only, `#!/usr/bin/env python3`, executable, and they must run on macOS's own `/usr/bin/python3` (3.9).
   Print only protocol lines on stdout. Never log item titles, clipboard text or secrets.
3. Write `tests/test_<id>.py` on the harness; shim `open`, `pbcopy`, `pbpaste`, `gh` so nothing touches the screen.
4. Bump `version` on every change Pilot should offer as an update, then rebuild the index.
5. `"unlisted": true` in the manifest keeps an extension out of `index.json` (Sven's Mac only; Pilot ignores the key):
   it is installed from its folder with `pilot ext install`.

Never install into `~/Library/Application Support/Pilot/` (Sven's live Pilot reads it); try an install in a temp folder.
Licence: ideas only, never code, from Raycast, Vicinae, ClipBook or Alfred extensions.
Work is specced in the vault: `Projects/Pilot/specs/pilot-open-work.html` (task blocks).
