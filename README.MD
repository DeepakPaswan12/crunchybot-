# Crunchy Checker

A Telegram bot that validates Crunchyroll accounts. Paste credentials, get tiered results — valid, invalid, premium, free, retry, error — split into files you can download straight from chat.

Whitelist-gated. Per-user jobs. Live progress. Proxy pool with preflight. Runs headless on Windows, Linux, or a cloud host.

---

## What it does

- Accepts accounts as `email:password` lines, pasted text, or a `.txt` file
- Logs into Crunchyroll's API and classifies each account:
  - **Valid** — login works, with subscription tier detected (`Free`, `Fan Pack`, `Premium`, `Mega Pack`)
  - **Invalid** — login rejected
  - **Retry** — rate-limited or IP-flagged; account state unknown
  - **Error** — network / proxy / unexpected failure
- Writes results into separate files per bucket
- Sends those files back as Telegram documents on `/results`
- Optionally routes traffic through an HTTP proxy pool, preflighted before every job
- Isolates every job under its own working directory, so concurrent users never collide

---

## Features

| | |
|---|---|
| **Per-user isolation** | Each job gets its own `input/` and `output/` folder, thread pool, and proxy pool |
| **Live status card** | The job message edits in real time — progress bar, counters, speed, ETA, current account, tail of recent results |
| **Proxy preflight** | Dead proxies are dropped before checking starts; alive count shown in the banner |
| **Multi-tier detection** | Distinguishes Free vs Fan Pack vs Premium vs Mega Pack |
| **Full capture output** | One line per valid account with username, token, account ID, profile count, external ID, country |
| **Whitelist gate** | Only Telegram user IDs on the list can run commands; owner manages the list from chat |
| **System log channel** | Owner DM (or a separate chat) receives job starts, completions, preflight results, crashes, and a 60-second heartbeat |
| **Auto-cleanup** | Job directories older than N hours are deleted automatically |
| **Debug modes** | `--test-auth` verifies the API client is still valid; `--test-proxy <n>` probes a single proxy |

---

## Requirements

- **Python 3.9 – 3.12** (3.11 recommended)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your numeric Telegram user ID (message [@userinfobot](https://t.me/CyberiousCheckerBot) to find it)
- Optional: a list of HTTP proxies

> **Windows note:** `tls_client` ships prebuilt wheels for 3.9–3.12. On 3.13 it tries to build from source and needs Visual Studio Build Tools. Use 3.11 or 3.12 to avoid this.

---

## Install

### 1. Clone

```bash
git clone https://github.com/YOURUSER/crunchybot.git
cd crunchybot
```

### 2. Create the venv

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (cmd):**
```bat
python -m venv .venv
.venv\Scripts\activate.bat
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

`requirements.txt`:
```
tls_client
toml
python-telegram-bot==21.6
psutil
```

### 4. Create the config files

The bot needs two JSON files under `storage/`.

**`storage/config.json`**
```json
{
  "bot_token": "PASTE_YOUR_BOT_TOKEN_HERE",
  "owner_id": 123456789,
  "default_threads": 4,
  "default_preflight_workers": 50,
  "default_preflight_timeout": 10,
  "max_accounts_per_job": 100000,
  "job_retention_hours": 72,
  "syslog_chat_id": 0
}
```

- `bot_token` — from @BotFather
- `owner_id` — your numeric Telegram ID. Auto-added to the whitelist on first boot.
- `syslog_chat_id` — optional. `0` or missing → syslog goes to the owner's DM. Set to a chat/channel ID to route logs elsewhere.

**`storage/whitelist.json`**
```json
{}
```

Empty. The owner is added automatically on first launch. Add others later via `/wl_add`.

### 5. (Optional) proxy list

Create `input/proxies.txt`, one proxy per line. Supported formats:

```
http://user:pass@host:port
host:port
user:pass:host:port
```

Leave the file empty or set `Proxyless = true` in `input/config.toml` to run without proxies.

### 6. (Optional) checker config

`input/config.toml` tunes the checker. Defaults are fine for most setups.

```toml
[dev]
Debug = false
Threads = 8
Proxyless = false
SkipPreflight = false
PreflightWorkers = 50
PreflightTimeout = 10

[auth]
BasicAuth = "Basic ..."
AppVersion = "3.70.0 (22358)"
```

---

## Run

**Linux / macOS:**
```bash
./run.sh
```

**Windows:**
```bat
run.bat
```

Or directly:
```bash
python bot.py
```

On a healthy boot you'll see:
```
bot running. owner=123456789. whitelist size=1
```

If the window closes immediately, run it from an open terminal so you can read the traceback.

---

## Using the bot

Open a chat with your bot on Telegram and send `/start`.

### User commands

| Command | What it does |
|---|---|
| `/start` | Show help and your access status |
| `/check` | Start a check job. Three ways to feed it accounts:<br>1. Reply to a `.txt` file with `/check`<br>2. Reply to a text message with `/check`<br>3. Type `/check` then paste creds on the next lines in one message |
| `/status` | Progress of your running (or last) job |
| `/stop` | Cancel your running job |
| `/results [job_id]` | Download result files. Without a job_id, sends the most recent job's output |
| `/history` | List your last 10 jobs |

### Owner commands

| Command | What it does |
|---|---|
| `/wl_add <user_id> [note]` | Whitelist a Telegram user |
| `/wl_remove <user_id>` | Remove a user from the whitelist |
| `/wl_list` | Show everyone on the whitelist |
| `/whitelist` | Show whitelist command help |

### Account input format

One per line:
```
email@example.com:password123
another@example.com:secret
```

Optional metadata after ` | ` is preserved in the output:
```
email@example.com:password123 | some-note
```

---

## Output files

Each job produces files under `storage/jobs/<user_id>/<job_id>/output/`:

```
output/
├── valid/
│   ├── valid.txt                  # email:pass (all valid)
│   ├── premium_accounts.txt       # email:pass|tier
│   ├── free.txt                   # valid but Free tier
│   └── full_valid_capture.txt     # full account dump, one line each
├── invalid/
│   └── invalid.txt                # email:pass
├── retry/
│   └── retry.txt                  # email:pass | reason
└── errors/
    └── errors.txt                 # email:pass | reason
```

`full_valid_capture.txt` line format:
```
username:email:password:access_token:account_id:subscription:profile_count:external_id:country
```

---

## Deploying

The bot uses **long polling**, which means it never receives inbound HTTP. That matters for host choice.

### Render — free Web Service (webhook mode)

Free tier requires webhook mode. You'll need to convert `bot.py` to bind an HTTP port and receive updates via webhook, plus a health endpoint so an external pinger can keep the service from sleeping. Full walkthrough in the deployment section of the repo wiki.

Quick shape:
1. Push the repo to GitHub
2. Render → **New +** → **Web Service**
3. Build: `pip install -r requirements.txt`
4. Start: `python bot.py`
5. Plan: Free
6. Env vars: `BOT_TOKEN`, `OWNER_ID`, `PYTHON_VERSION=3.11.0`
7. UptimeRobot pings `https://your-service.onrender.com/health` every 5 minutes

### Render — paid Background Worker (polling mode)

The current code runs unchanged.

1. Render → **New +** → **Background Worker**
2. Same build and start commands
3. Plan: Starter (~$7/mo)
4. Same env vars

Background workers have no spin-down and no HTTP requirements. This is the simplest deployment path if you're willing to pay.

### Other hosts

- **PythonAnywhere** — free tier works but restrictive; run in a bash console with `nohup python bot.py &`
- **A cheap VPS** (Hetzner, DigitalOcean, Oracle Cloud free tier) — most flexible. Use `systemd` or `tmux`.
- **Your own machine** — works fine for personal use. Keep the terminal open, or use `nssm` on Windows to run it as a service.

**Avoid:** Vercel, AWS Lambda, Cloudflare Workers — all serverless and ephemeral, which kills long-polling bots and persistent sessions.

---

## Project structure

```
crunchybot/
├── bot.py                 # Telegram frontend — commands, whitelist, job orchestration
├── checker/
│   ├── __init__.py
│   ├── core.py            # AccountChecker, Job, proxy preflight, credential parsing
│   ├── ui.py              # EventBus + JobUI (counters, snapshot)
│   ├── config.py          # config.toml loader
│   └── misc.py            # proxy normalization, header builder, user-agent
├── storage/
│   ├── config.json        # bot token, owner id (gitignored)
│   ├── whitelist.json     # whitelisted user IDs (gitignored)
│   └── jobs/              # per-user job working directories (gitignored)
├── input/
│   ├── proxies.txt        # optional proxy list (gitignored)
│   └── config.toml        # checker defaults
├── requirements.txt
├── run.sh
├── run.bat
└── README.md
```

---

## How it works

The bot is a thin frontend over a self-contained checker core.

- **`EventBus`** — thread-safe fan-out. The checker emits `valid`, `invalid`, `retry`, `error`, `info`, `warn`, `fatal`, `done`. The bot registers handlers and renders them into the live status message and the system log channel.
- **`JobUI`** — per-job counters, current email, progress snapshot. Polled by the status updater task every 2 seconds.
- **`Job`** — owns the accounts list, proxy pool, thread pool, cancel event, and `SafeWriter`. Runs in a background thread so the Telegram event loop stays responsive.
- **`AccountChecker`** — one instance per account. Performs the login POST, `/me` lookup, `/me/multiprofile` capture, and subscription benefits lookup. Retries on 403/429/5xx, rotates proxies, classifies 400/401 errors.
- **`SafeWriter`** — lock-guarded file appends under each job's `output/` directory.

Concurrency is per-job. Each job gets its own thread pool sized from `Threads` in the config. Multiple users can run jobs in parallel without interfering.

---

## Troubleshooting

**Window closes instantly on Windows**
Run from an already-open terminal: `cd path\to\crunchybot` then `run.bat`. The `pause` at the end keeps the window alive.

**`TypeError: unsupported operand type(s) for |` on Python 3.9**
Add `from __future__ import annotations` at the top of `checker/config.py`, `checker/core.py`, `checker/misc.py`, and `bot.py`.

**`ImportError: tls_client`**
Python version issue. Use 3.11 or 3.12. On Windows, ensure you have the venv activated and rerun `pip install --only-binary=:all: tls_client`.

**`Set bot_token in storage/config.json`**
You didn't fill in the config file, or the token still reads `PASTE_YOUR_BOT_TOKEN_HERE`. Edit and rerun.

**Only one account checked when I pasted a batch**
Fixed in current version. If you're on an older copy, the bug was in `cmd_check` using `ctx.args` instead of `msg.text`. Update to the current `bot.py`.

**All accounts invalid but credentials are correct**
Check `input/config.toml` — the `BasicAuth` header may be stale. Run `python main.py --test-auth` to verify the API client is still accepted by Crunchyroll.

**Proxies all dead**
Run preflight once with `SkipPreflight = true` to see if the checker works proxyless, then diagnose the proxy source separately. `--test-proxy <index>` probes a single proxy and prints the raw response.

---

## Security

- `storage/config.json` contains your bot token. Never commit it. The provided `.gitignore` excludes it.
- `storage/whitelist.json` and `storage/jobs/` contain account data. Also gitignored.
- The bot is whitelist-gated. Anyone not on the list gets `Access denied` and their Telegram ID, so they can ask you for a slot.
- All account data stays on the machine running the bot. Nothing is sent anywhere except Crunchyroll's API and Telegram.

---

## License

Personal use. No warranty. Use at your own risk and in accordance with Crunchyroll's terms of service.
