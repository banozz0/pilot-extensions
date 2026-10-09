# Pilot extensions

The catalogue of extensions for [Pilot](https://github.com/banozz0/pilot), the Mac app that runs your keyboard,
windows and launcher. Each extension adds commands to Pilot's Hyper+Space menu; Pilot draws every list and detail
itself, the extension only sends data.

## Installing

Open Pilot's Extensions page and pick the **Catalogue** tab (or search Hyper+Space for "Extension catalogue"), then
**Install**. Pilot downloads the extension's files listed in `index.json`, checks each one's SHA-256, and puts the
folder in `~/Library/Application Support/Pilot/Extensions/<id>`. **Update** shows when the catalogue has a newer
version; **Remove** moves the folder to the Trash and keeps its settings and data. Pilot only connects to GitHub
while the Catalogue tab is open. This repo is private, so Pilot reads it through the `gh` CLI when you are signed in.

Every extension here needs `python3`, which macOS has.

## Extensions

| Extension | Commands | What it does |
| --- | --- | --- |
| 🍅 Pomodoro | Focus, Break, Custom focus…, Pause timer, Resume timer, Skip block, Stop timer, Pomodoro today | Focus blocks and breaks counted down in Pilot's Timer item, the long break every few blocks, an alert or the next block when one ends. |
| 🔌 Ports | Kill port | Every listening TCP port with the program holding it; Quit, Force quit, open or copy its localhost URL, copy the PID. Never Pilot, never pid 1. |
| 🌿 Repos | Git repos | The git repos under a folder (default ~/code, three levels deep) with branch and uncommitted changes; open one in your terminal (Ghostty when installed), in lazygit, in Finder or on GitHub, or copy its path. |
| 🤖 Claude Sessions | Claude sessions, Last Claude session | Your 60 newest Claude Code sessions by title, folder and age; copy or paste the command that resumes one in its folder, open the folder, or see its facts (branch, start, messages, model). Read only; a prompt shows only as a short title. |
| 🧰 Dev Utils | Transform clipboard, Decode JWT from clipboard, Copy new UUID | The clipboard as base64, URL encoding, pretty or minified JSON, epoch to ISO date and back, SHA-256, MD5, lower and upper case, slug, a new UUID; only the rows that fit what you copied; Return copies the result, Cmd+K pastes it. A JWT opens a detail with its header, payload and expiry (not verified). |

## Making one

An extension is a folder with `extension.json` and small programs that read one JSON line on stdin and print JSON
lines; see `AGENTS.md` for the rules and `tests/harness.py` for how Pilot runs them. Check and test everything with:

```sh
python3 scripts/build-index.py
python3 scripts/check.py && python3 -m unittest discover -s tests -v
```
