# pilot-extensions — agent brief

Private repo. Pilot's extension catalogue: each folder under `extensions/` is one Pilot extension, and `index.json`
at the root is what Pilot's Extensions page → Catalogue reads to install and update them (from `main`, through
raw.githubusercontent.com or `gh api`). Pilot itself lives in `~/code/pilot`; never edit it from here.

## Layout

- `extensions/<id>/extension.json` + its programs. The folder name is the id. Each folder is self-contained:
  no shared code outside it, since Pilot installs one folder at a time.
- `scripts/build-index.py` writes `index.json` (sha256, size and executable bit per file, hidden commands left out).
- `scripts/check.py` checks every manifest against Pilot's rules and that `index.json` is fresh.
- `tests/` stdlib `unittest`; `tests/harness.py` runs a command exactly as Pilot does.

## Protocol

Pilot's ADR 0010 (`~/code/pilot/docs/adr/0010-extensions-speak-json-lines-pilot-draws.md`) is the contract: one
JSON run message on stdin (command, query, context, preferences, paths.data, paths.cache), JSON lines on stdout,
a fixed environment, list and detail answers within 2 s, no-view within 30 s. Manifest rules mirror
`~/code/pilot/Sources/PilotCore/Extension.swift`. Added 2026-10-10: `"hidden": true` commands, several actions per
no-view run, `hud` and `alert` with `sound`, the `timer` action.

## Adding or changing an extension

1. Make `extensions/<id>/` with `extension.json` (`id`, `title`, emoji `icon`, `version` x.y.z, `description`,
   `requires`, `permissions`, `preferences`, `commands`) and its programs.
2. Programs: Python 3 stdlib only, `#!/usr/bin/env python3`, executable, and they must run on macOS's own
   `/usr/bin/python3` (3.9). Print only protocol lines on stdout. Never log item titles, clipboard text or secrets.
3. Write `tests/test_<id>.py` on the harness; shim `open`, `pbcopy`, `pbpaste`, `gh` so nothing touches the screen.
4. Bump `version` on every change Pilot should offer as an update, then `python3 scripts/build-index.py`.

## Proof

`python3 scripts/check.py && python3 -m unittest discover -s tests -v`, and again with
`PILOT_EXT_PYTHON=/usr/bin/python3` before a commit. Never install into `~/Library/Application Support/Pilot/`
(Sven's live Pilot reads it); try an install in a temp folder.
Licence: ideas only, never code, from Raycast, Vicinae, ClipBook or Alfred extensions. Commit each verified
step with `~/code/dotfiles/automation/bin/step-commit -m "what + why" <paths> --land`.
