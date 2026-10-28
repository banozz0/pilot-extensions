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
| 🍅 Pomodoro | Focus, Break, Custom focus…, Pause timer, Resume timer, Skip block, Stop timer, Pomodoro today | Focus blocks and breaks counted down in Pilot's Timer item, the long break every few blocks, an alert or the next block when one ends; a page (and Home block) with the countdown, today's blocks and its buttons. |
| 🔌 Ports | Kill port | Every listening TCP port with the program holding it; Quit, Force quit, open or copy its localhost URL, copy the PID. Never Pilot, never pid 1. |
| 🌿 lazygit (id `git`) | lazygit repos | Which of your repos (Pilot's Repos folder, default ~/code) have changes to commit or commits to push or pull: a menu-bar item counting them, a popover and a page where a click on a repo opens lazygit in it (in Pilot's terminal), with Pull (fast-forward only) and Push on each and a plain terminal, Finder or GitHub under ⋯; Fetch all, and a fetch when the popover or page opens, at most every 10 min. Replaces Repos (kept on the `parked/repos` branch). |
| 🤖 Claude Sessions | Claude sessions | Your 60 newest Claude Code sessions by title, folder and age; copy or paste the command that resumes one in its folder, open the folder, or see its facts (branch, start, messages, model) with Show details. Read only; a prompt shows only as a short title. |
| 🧰 Dev Utils | Transform clipboard, Decode JWT from clipboard, Copy new UUID | The clipboard as base64, URL encoding, pretty or minified JSON, epoch to ISO date and back, SHA-256, MD5, lower and upper case, slug, a new UUID; only the rows that fit what you copied; Return copies the result, Cmd+K pastes it. A JWT opens a detail with its header, payload and expiry (not verified). |
| 🖥️ SSH hosts | SSH hosts | Connects to a server you named in your SSH config, in a new terminal window, without typing the ssh command. |
| 🪨 Obsidian search | Search Obsidian notes | Find a note in your Obsidian vault by its title or by what it says, without opening Obsidian first. |
| 📂 Easy-CD folders | Easy-CD folders | Open the folders you pinned or visited lately with easy-cd (ecd) in a terminal, without typing a path. |
| 🐙 GitHub | My pull requests, GitHub issues, Failing CI runs | See what on GitHub needs you: pull requests waiting on your review, your own with their checks, issues given to you and CI runs that failed. |
| 👻 Ghostex sessions | Ghostex sessions | Your Ghostex agent sessions: a menu-bar ghost counting the working ones (amber when one asks you something), a popover grouped Need you, Working, Idle, and a page per project. A session opens its chat (last six messages, tool work folded, your own last message always shown) with a reply box: Return queues the reply until the agent stops, Option+Return interrupts, a sleeping one is woken first. Rename, tag, sleep, wake, open in Ghostex or close (asked first), one session at a time. |
| 🌤️ Weather | Weather | The temperature now in the menu bar; its popover, page and Hyper+Space view show feels like, high and low, rain chance and wind, the next hours and the next 7 days. Your City setting, else your Mac's time zone city, so no location permission; °C or °F. Data from Open-Meteo (free, no key), kept 15 min; offline it shows the last answer and when it was read. |
| ☑️ To-do | To-do | A plain to-do list: a menu-bar item counting the open ones, a popover to add one (Return), tick it off or delete it, done ones folded under "N done", and a page listing them all. In Hyper+Space, type the to-do in To-do and Return adds it. Kept in a JSON file on this Mac, no account, no sync. |
| 📊 AI usage | AI usage, AI usage summary | See how much of your Claude and Codex limits is left this session and this week, how your pace compares, and when each resets, in the menu bar. |

## Making one

An extension is a folder with `extension.json` and small programs that read one JSON line on stdin and print JSON
lines; see `AGENTS.md` for the rules and `tests/harness.py` for how Pilot runs them. Check and test everything with:

```sh
python3 scripts/build-index.py
python3 scripts/check.py && python3 -m unittest discover -s tests -v
```

## Files

<!-- file-map:start depth=3 fold=tests,fixtures — generated by agents automation/bin/file-map; edit only a folder's # comment -->
```text
pilot-extensions/
├── extensions/             # one self-contained folder per extension; the folder name is the id
│   ├── ai-usage/           # 2 files
│   │   ├── extension.json
│   │   └── main.py         # AI usage for Pilot: how much of each AI plan's limits is left, CodexBar's way, from this…
│   ├── claude-sessions/    # 2 files
│   │   ├── extension.json
│   │   └── main.py         # Claude Sessions for Pilot: recent Claude Code sessions from ~/.claude/projects/*/*.jsonl,…
│   ├── dev-utils/          # 2 files
│   │   ├── extension.json
│   │   └── main.py         # Dev Utils for Pilot: the clipboard's text turned into what a developer needs, read with p…
│   ├── easy-cd/            # 2 files
│   │   ├── extension.json
│   │   └── main.py         # Easy-CD folders for Pilot: easy-cd's (`ecd`) pinned folders, then its recent ones, read o…
│   ├── ghostex/            # 2 files
│   │   ├── extension.json
│   │   └── main.py         # Ghostex sessions for Pilot: Sven's Ghostex sessions in Hyper+Space, a menu-bar item with…
│   ├── git/                # 2 files
│   │   ├── extension.json
│   │   └── main.py         # lazygit for Pilot (the extension id stays git): every repo under the repos folder with it…
│   ├── github/             # 2 files
│   │   ├── extension.json
│   │   └── main.py         # GitHub for Pilot: pull requests, issues and failing CI runs that need Sven, through the s…
│   ├── markets/            # 2 files
│   │   ├── extension.json
│   │   └── main.py         # Markets for Pilot: stocks, ETFs and coins and their day's change in the menu bar; its pop…
│   ├── now-playing/        # 2 files
│   │   ├── extension.json
│   │   └── main.py         # Now playing for Pilot: whatever the Mac is playing, as Control Center's Now Playing shows…
│   ├── obsidian-search/    # 2 files
│   │   ├── extension.json
│   │   └── main.py         # Obsidian search for Pilot: find a note in an Obsidian vault by title, then by what it say…
│   ├── pomodoro/           # 2 files
│   │   ├── extension.json
│   │   └── main.py         # Pomodoro for Pilot: focus blocks and breaks counted down on Pilot's one timer.
│   ├── ports/              # 2 files
│   │   ├── extension.json
│   │   └── main.py         # Ports for Pilot: the listening TCP ports (lsof) and quitting the program that holds one.
│   ├── ssh-hosts/          # 2 files
│   │   ├── extension.json
│   │   └── main.py         # SSH hosts for Pilot: the hosts named in ~/.ssh/config, and every file it Includes, each o…
│   ├── summarise/          # 2 files
│   │   ├── extension.json
│   │   └── main.py         # Summarise for Pilot (Sven, 2026-10-25: Q478, Q480 to Q482): a YouTube video's transcript,…
│   ├── todo/               # 2 files
│   │   ├── extension.json
│   │   └── main.py         # To-do for Pilot: Hyper+Space is its home (To-do 2.0, Sven's pick B, Q242): type to filter…
│   ├── video-downloader/   # unlisted: Sven's Mac only, never in index.json
│   │   ├── extension.json
│   │   └── main.py         # Video downloader for Pilot: a YouTube, X or Twitch link, from the search or the clipboard…
│   └── weather/            # 2 files
│       ├── extension.json
│       └── main.py         # Weather for Pilot: the temperature now as a menu-bar item, a popover and a page with now,…
├── scripts/                # build-index.py (writes index.json), check.py
│   ├── build-index.py      # Write index.json, the catalogue Pilot reads, from every extensions/<id>/ folder.
│   └── check.py            # Check every extension against the rules Pilot's Extension.swift applies, plus the catalog…
├── tests/                  # stdlib unittest; harness.py runs a command as Pilot does
├── .gitignore
├── AGENTS.md               # pilot-extensions — agent brief
├── index.json
└── README.md               # Pilot extensions
```
<!-- file-map:end -->
