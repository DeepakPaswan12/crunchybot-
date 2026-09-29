import asyncio
import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from checker import load_config, EventBus, JobUI, Job, Miscellaneous, preflight_proxies

# ── paths ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
STORAGE = ROOT / "storage"
CONFIG_FILE = STORAGE / "config.json"
WHITELIST_FILE = STORAGE / "whitelist.json"
JOBS_DIR = STORAGE / "jobs"
DEFAULT_JOB_CFG = ROOT / "input" / "config.toml"

STORAGE.mkdir(exist_ok=True)
JOBS_DIR.mkdir(exist_ok=True)

# ── config load ──────────────────────────────────────────────────────────
if not CONFIG_FILE.exists():
    raise SystemExit(f"Missing {CONFIG_FILE}. Copy storage/config.json template.")

BOT_CFG = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
BOT_TOKEN = BOT_CFG["bot_token"]
OWNER_ID = int(BOT_CFG["owner_id"])
MAX_ACCOUNTS = int(BOT_CFG.get("max_accounts_per_job", 100000))
RETENTION_H = int(BOT_CFG.get("job_retention_hours", 72))

if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
    raise SystemExit("Set bot_token in storage/config.json")

# ── whitelist ────────────────────────────────────────────────────────────
def _load_whitelist() -> dict:
    if not WHITELIST_FILE.exists():
        return {}
    try:
        return json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_whitelist(wl: dict):
    WHITELIST_FILE.write_text(
        json.dumps(wl, indent=2, sort_keys=True), encoding="utf-8"
    )

_wl_lock = threading.Lock()
WHITELIST = _load_whitelist()

if OWNER_ID and str(OWNER_ID) not in WHITELIST:
    WHITELIST[str(OWNER_ID)] = {
        "added_by": "system",
        "added_at": time.time(),
        "note": "owner",
    }
    _save_whitelist(WHITELIST)

def is_whitelisted(user_id: int) -> bool:
    return str(user_id) in WHITELIST

def is_owner(user_id: int) -> bool:
    return int(user_id) == OWNER_ID

def add_to_whitelist(user_id: int, by: int, note: str = ""):
    with _wl_lock:
        WHITELIST[str(user_id)] = {
            "added_by": str(by),
            "added_at": time.time(),
            "note": note,
        }
        _save_whitelist(WHITELIST)

def remove_from_whitelist(user_id: int) -> bool:
    with _wl_lock:
        if str(user_id) in WHITELIST:
            del WHITELIST[str(user_id)]
            _save_whitelist(WHITELIST)
            return True
        return False

# ── app reference (for cross-thread syslog) ──────────────────────────────
app_ref: dict = {"app": None, "loop": None}

SYSLOG_CHAT_ID = BOT_CFG.get("syslog_chat_id") or OWNER_ID


async def _syslog(app, text: str):
    if not app:
        return
    try:
        await app.bot.send_message(
            chat_id=SYSLOG_CHAT_ID,
            text=f"🛠️ {text}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass

# ── job registry ─────────────────────────────────────────────────────────
class JobHandle:
    def __init__(self, job_id, user_id, chat_id, ui, bus, job, thread, dir):
        self.job_id = job_id
        self.user_id = user_id
        self.chat_id = chat_id
        self.ui = ui
        self.bus = bus
        self.job = job
        self.thread = thread
        self.dir = dir
        self.created_at = time.time()
        self.status_msg_id = None
        self.last_edit = 0.0
        self.live_buffer = []  # recent events for the live status message

JOBS: dict[str, JobHandle] = {}
JOBS_LOCK = threading.Lock()

# ── helpers ──────────────────────────────────────────────────────────────
def user_job_dir(user_id: int, job_id: str) -> Path:
    p = JOBS_DIR / str(user_id) / job_id
    (p / "input").mkdir(parents=True, exist_ok=True)
    (p / "output").mkdir(parents=True, exist_ok=True)
    return p

def _sanitize_accounts(text: str) -> list[str]:
    out, seen = [], set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out

def _escape(s: str) -> str:
    # minimal markdown-v1 escaping for email/status strings
    return s.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")

def _progress_bar(
    processed: int, total: int, width: int = 14, style: str = "block"
) -> str:
    if total <= 0:
        return "░" * width
    filled = int(width * processed / total)
    if style == "block":
        return "█" * filled + "░" * (width - filled)
    return "▰" * filled + "▱" * (width - filled)


def _fmt_duration(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _render_status(handle, snap, style: str = "running") -> str:
    bar = _progress_bar(snap["processed"], snap["total"], width=20)
    pct = (snap["processed"] / snap["total"] * 100) if snap["total"] else 0

    header_icon = {
        "running": "⏳",
        "done": "✅",
        "aborted": "🛑",
        "fatal": "❌",
    }.get(style, "⏳")

    header_label = {
        "running": "Checking…",
        "done": "Complete",
        "aborted": "Cancelled",
        "fatal": "Fatal error",
    }.get(style, "Checking…")

    lines = []
    lines.append(f"{header_icon} *Job* `{handle.job_id}` — {header_label}")
    lines.append("")
    lines.append(f"`{bar}` {pct:5.1f}%")
    lines.append(
        f"`{snap['processed']:>5}/{snap['total']:<5}` "
        f"· {snap['speed']:.1f}/s · eta {_fmt_duration(snap['eta'])}"
    )
    lines.append("")
    lines.append(
        f"✅ `{snap['valid']:<4}`   "
        f"❌ `{snap['invalid']:<4}`   "
        f"⚠️ `{snap['retry'] + snap['error']:<4}`"
    )

    if style == "running" and snap["current"]:
        cur = snap["current"]
        if len(cur) > 42:
            cur = cur[:39] + "..."
        lines.append("")
        lines.append(f"→ `{_escape(cur)}`")

    tail = [e for e in handle.live_buffer[-4:] if e.get("type") == "evt"]
    if tail:
        lines.append("")
        rendered = []
        for ev in tail:
            if "email" in ev:
                e = ev["email"]
                if len(e) > 28:
                    e = e[:25] + "..."
                e = _escape(e)
                kind = ev.get("_kind")
                if kind == "valid":
                    rendered.append(f"  ✅ `{e}` — {ev.get('tier', '')}")
                elif kind == "invalid":
                    rendered.append(f"  ❌ `{e}`")
                elif kind == "retry":
                    rendered.append(f"  ⚠️ `{e}` — {ev.get('reason', '')[:20]}")
                elif kind == "error":
                    rendered.append(f"  ⚠️ `{e}` — {ev.get('reason', '')[:20]}")
            elif "msg" in ev:
                m = ev["msg"]
                if len(m) > 48:
                    m = m[:45] + "..."
                rendered.append(f"  · {_escape(m)}")
        lines.extend(rendered)

    return "\n".join(lines)

async def _typing(bot, chat_id):
    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        pass

# ── guard decorator ──────────────────────────────────────────────────────
def whitelist_required(handler):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return
        if not is_whitelisted(user.id):
            msg = update.effective_message
            if msg:
                await msg.reply_text(
                    f"Access denied.\nYour ID: `{user.id}`\nAsk the owner to whitelist you.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            return
        return await handler(update, ctx)
    return wrapper

def owner_only(handler):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not is_owner(user.id):
            msg = update.effective_message
            if msg:
                await msg.reply_text("Owner only.")
            return
        return await handler(update, ctx)
    return wrapper

# ── /start ───────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"*Crunchyroll Checker Bot*\n"
        f"Your ID: `{user.id}`\n"
        f"Whitelisted: {'yes' if is_whitelisted(user.id) else 'no'}\n\n"
        f"*Commands*\n"
        f"/check — run a check job\n"
        f"/status — current job status\n"
        f"/stop — cancel current job\n"
        f"/results — fetch output files\n"
        f"/history — list recent jobs\n"
    )
    if is_owner(user.id):
        text += (
            f"\n*Owner*\n"
            f"/whitelist — manage whitelist\n"
            f"/wl\\_add `<id> [note]`\n"
            f"/wl\\_remove `<id>`\n"
            f"/wl\\_list\n"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# ── /whitelist commands ──────────────────────────────────────────────────
@owner_only
async def cmd_wl_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /wl_add <user_id> [note]")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("user_id must be an integer.")
        return
    note = " ".join(args[1:]) if len(args) > 1 else ""
    add_to_whitelist(uid, update.effective_user.id, note)
    await update.message.reply_text(f"Added `{uid}` to whitelist.", parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_wl_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /wl_remove <user_id>")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("user_id must be an integer.")
        return
    if uid == OWNER_ID:
        await update.message.reply_text("Cannot remove owner.")
        return
    ok = remove_from_whitelist(uid)
    await update.message.reply_text("Removed." if ok else "Not in whitelist.")

@owner_only
async def cmd_wl_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not WHITELIST:
        await update.message.reply_text("Whitelist empty.")
        return
    lines = ["*Whitelist*"]
    for uid, meta in sorted(WHITELIST.items(), key=lambda x: int(x[0])):
        note = meta.get("note") or ""
        lines.append(f"`{uid}` — {_escape(note)}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cmd_whitelist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/wl_add `<id> [note]`\n/wl_remove `<id>`\n/wl_list",
        parse_mode=ParseMode.MARKDOWN,
    )

# ── /check ───────────────────────────────────────────────────────────────
async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message

    accounts_text = ""

    # 1. reply to a .txt file
    if msg.reply_to_message and msg.reply_to_message.document:
        doc = msg.reply_to_message.document
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await msg.reply_text("File too large (20MB cap).")
            return
        f = await doc.get_file()
        raw = await f.download_as_bytearray()
        accounts_text = raw.decode("utf-8", errors="ignore")

    # 2. reply to a text message
    elif msg.reply_to_message and msg.reply_to_message.text:
        accounts_text = msg.reply_to_message.text

    # 3. inline: preserve raw message text and strip only the /check token
    else:
        raw = msg.text or ""
        first_line = raw.split("\n", 1)
        if first_line[0].lower().startswith("/check"):
            remainder = first_line[1] if len(first_line) > 1 else ""
            same_line = first_line[0].split(" ", 1)
            same_line_rest = same_line[1] if len(same_line) > 1 else ""
            parts = []
            if same_line_rest.strip():
                parts.append(same_line_rest)
            if remainder.strip():
                parts.append(remainder)
            accounts_text = "\n".join(parts)
        else:
            accounts_text = raw

    if not accounts_text.strip():
        ctx.user_data["awaiting_accounts"] = True
        await msg.reply_text(
            "Send the accounts now.\n"
            "Format: `email:password` one per line, or reply to a .txt file with /check.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await _start_job_from_text(update, ctx, accounts_text)

async def _start_job_from_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, accounts_text: str):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message

    accounts = _sanitize_accounts(accounts_text)
    if not accounts:
        await msg.reply_text("No valid accounts parsed.")
        return
    if len(accounts) > MAX_ACCOUNTS:
        await msg.reply_text(f"Too many accounts ({len(accounts)} > {MAX_ACCOUNTS}).")
        return

    # prevent concurrent jobs from same user
    with JOBS_LOCK:
        for h in JOBS.values():
            if h.user_id == user.id and not h.ui.finished:
                await msg.reply_text("You already have a running job. /stop it first.")
                return

    job_id = uuid.uuid4().hex[:8]
    job_dir = user_job_dir(user.id, job_id)

    # write input
    (job_dir / "input" / "accounts.txt").write_text(
        "\n".join(accounts) + "\n", encoding="utf-8"
    )

    # copy config.toml template if present, else use defaults
    cfg = load_config(DEFAULT_JOB_CFG)

    # proxies
    proxyless = cfg["dev"].get("Proxyless", False)
    proxies_file = job_dir / "input" / "proxies.txt"
    if not proxies_file.exists():
        proxies_file.write_text("", encoding="utf-8")
    proxies = Miscellaneous.load_all_proxies_from(proxies_file, proxyless)

    # bus + ui
    bus = EventBus()
    ui = JobUI(bus, total=len(accounts))

    # status message
    status_msg = await msg.reply_text(
        f"*Job* `{job_id}`\n"
        f"accounts: {len(accounts)}\n"
        f"proxies: {len(proxies)}\n"
        f"threads: {cfg['dev'].get('Threads', 4)}\n"
        f"starting...",
        parse_mode=ParseMode.MARKDOWN,
    )

    handle = JobHandle(
        job_id=job_id,
        user_id=user.id,
        chat_id=chat.id,
        ui=ui,
        bus=bus,
        job=None,
        thread=None,
        dir=job_dir,
    )
    handle.status_msg_id = status_msg.message_id

    # background thread: preflight (if needed) then run job
    def _runner():
        try:
            if proxies and not cfg["dev"].get("SkipPreflight", False):
                ui.info(f"preflight: probing {len(proxies)} proxies")
                alive, elapsed = preflight_proxies(
                    proxies,
                    cfg["auth"]["AppVersion"],
                    ui,
                    workers=cfg["dev"].get("PreflightWorkers", 50),
                    timeout=cfg["dev"].get("PreflightTimeout", 10),
                )
                ui.info(f"preflight done in {elapsed}s — {len(alive)} alive")
                proxies[:] = alive

            job = Job(
                job_id=job_id,
                job_dir=job_dir,
                accounts=accounts,
                proxies=proxies,
                cfg=cfg,
                ui=ui,
            )
            handle.job = job

            # event hooks -> append tagged events to the live buffer
            def _make_hook(kind):
                def _hook(**p):
                    with JOBS_LOCK:
                        handle.live_buffer.append({"type": "evt", "_kind": kind, **p})
                        if len(handle.live_buffer) > 30:
                            handle.live_buffer = handle.live_buffer[-30:]
                    if kind in ("warn", "fatal", "info"):
                        event_message = p.get("msg", "")
                        if (
                            kind == "fatal"
                            or event_message.startswith("preflight")
                            or "crashed" in event_message
                        ):
                            loop = app_ref["loop"]
                            app = app_ref["app"]
                            if loop and app:
                                asyncio.run_coroutine_threadsafe(
                                    _syslog(
                                        app,
                                        f"[`{handle.job_id}`] {kind}: {event_message}",
                                    ),
                                    loop,
                                )

                return _hook

            for evt in ("valid", "invalid", "retry", "error", "info", "warn", "fatal"):
                bus.on(evt, _make_hook(evt))

            job.run()
        except Exception as e:
            ui.fatal(f"runner crashed: {e}")
            try:
                ui.done()
            except Exception:
                pass

    t = threading.Thread(target=_runner, daemon=True)
    handle.thread = t
    with JOBS_LOCK:
        JOBS[job_id] = handle
    t.start()

    _total_jobs_run[0] += 1
    asyncio.create_task(_syslog(
        ctx.application,
        f"*job started* `{job_id}` — user `{user.id}` — "
        f"{len(accounts)} accounts · {len(proxies)} proxies · "
        f"{cfg['dev'].get('Threads', 4)} threads",
    ))

    # live status updater
    asyncio.create_task(_status_updater(ctx.application, handle))

async def _status_updater(app: Application, handle: JobHandle):
    """Edits the status message periodically with progress."""
    tick = 0
    while True:
        await asyncio.sleep(2)
        tick += 1
        snap = handle.ui.snapshot()
        if snap["finished"]:
            break
        style = "aborted" if snap["aborted"] else "running"
        text = _render_status(handle, snap, style=style)
        try:
            await app.bot.edit_message_text(
                chat_id=handle.chat_id,
                message_id=handle.status_msg_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    snap = handle.ui.snapshot()
    style = "done"
    if snap["aborted"]:
        style = "aborted"
    text = _render_status(handle, snap, style=style)
    text += f"\n\n📥 /results {handle.job_id}"
    try:
        await app.bot.edit_message_text(
            chat_id=handle.chat_id,
            message_id=handle.status_msg_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass

    asyncio.create_task(_syslog(
        app,
        f"job `{handle.job_id}` {style} — "
        f"user `{handle.user_id}` — "
        f"{snap['processed']}/{snap['total']} "
        f"(✓{snap['valid']} ✗{snap['invalid']} "
        f"⚠{snap['retry'] + snap['error']}) "
        f"in {_fmt_duration(snap['elapsed'])}",
    ))

# ── text handler: paste creds after /check ───────────────────────────────
@whitelist_required
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.pop("awaiting_accounts", False):
        await _start_job_from_text(update, ctx, update.message.text)
        return
    # also accept "email:pass" blocks without a command when it looks like creds
    text = update.message.text or ""
    if "\n" in text and ":" in text and not text.startswith("/"):
        # only auto-start if at least 3 lines look like creds
        cred_like = sum(
            1 for ln in text.splitlines()
            if ":" in ln and not ln.strip().startswith("#")
        )
        if cred_like >= 3:
            await _start_job_from_text(update, ctx, text)
            return
    await update.message.reply_text("Unknown input. Use /help.")

# ── /status ──────────────────────────────────────────────────────────────
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with JOBS_LOCK:
        mine = [h for h in JOBS.values() if h.user_id == user.id]
    if not mine:
        await update.message.reply_text("No jobs.")
        return
    h = sorted(mine, key=lambda x: -x.created_at)[0]
    snap = h.ui.snapshot()
    bar = _progress_bar(snap["processed"], snap["total"])
    text = (
        f"*Job* `{h.job_id}`\n"
        f"`{bar}` {snap['processed']}/{snap['total']}\n"
        f"✓ {snap['valid']}  ✗ {snap['invalid']}  "
        f"⚠ {snap['retry'] + snap['error']}\n"
        f"{snap['speed']:.2f}/s  eta {snap['eta']:.0f}s\n"
        f"state: {'finished' if snap['finished'] else 'running'}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# ── /stop ────────────────────────────────────────────────────────────────
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with JOBS_LOCK:
        mine = [h for h in JOBS.values() if h.user_id == user.id and not h.ui.finished]
    if not mine:
        await update.message.reply_text("No running job.")
        return
    for h in mine:
        h.job and h.job.cancel.set()
        h.ui.aborted = True
    await update.message.reply_text("Cancel requested.")

# ── /history ─────────────────────────────────────────────────────────────
async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    udir = JOBS_DIR / str(user.id)
    if not udir.exists():
        await update.message.reply_text("No history.")
        return
    dirs = sorted(
        [d for d in udir.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )[:10]
    if not dirs:
        await update.message.reply_text("No history.")
        return
    lines = ["*Recent jobs*"]
    for d in dirs:
        ts = time.strftime("%m-%d %H:%M", time.localtime(d.stat().st_mtime))
        lines.append(f"`{d.name}` — {ts}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ── /results ─────────────────────────────────────────────────────────────
@whitelist_required
async def cmd_results(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    job_id = ctx.args[0] if ctx.args else None

    udir = JOBS_DIR / str(user.id)
    if not udir.exists():
        await update.message.reply_text("No jobs.")
        return

    if job_id:
        job_dir = udir / job_id
        if not job_dir.exists():
            await update.message.reply_text("Job not found.")
            return
    else:
        dirs = sorted(
            [d for d in udir.iterdir() if d.is_dir()],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        if not dirs:
            await update.message.reply_text("No jobs.")
            return
        job_dir = dirs[0]

    out = job_dir / "output"
    targets = [
        out / "valid" / "valid.txt",
        out / "valid" / "premium_accounts.txt",
        out / "valid" / "free.txt",
        out / "valid" / "full_valid_capture.txt",
        out / "invalid" / "invalid.txt",
        out / "retry" / "retry.txt",
        out / "errors" / "errors.txt",
    ]
    sent = 0
    await _typing(ctx.bot, update.effective_chat.id)
    for p in targets:
        if p.exists() and p.stat().st_size > 0:
            try:
                with open(p, "rb") as f:
                    await update.message.reply_document(
                        document=InputFile(f, filename=f"{job_dir.name}_{p.name}"),
                        caption=f"{p.parent.name}/{p.name}",
                    )
                sent += 1
            except Exception as e:
                await update.message.reply_text(f"failed to send {p.name}: {e}")
    if not sent:
        await update.message.reply_text(f"No output files for `{job_dir.name}`.", parse_mode=ParseMode.MARKDOWN)

# ── /help ────────────────────────────────────────────────────────────────
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

# ── cleanup loop ─────────────────────────────────────────────────────────
_startup_ts = time.time()
_total_jobs_run = [0]


def _proc_stats() -> str:
    try:
        import psutil

        process = psutil.Process()
        mem = process.memory_info().rss / (1024 * 1024)
        cpu = process.cpu_percent(interval=None)
        threads = process.num_threads()
        return f"mem {mem:.0f}MB · cpu {cpu:.0f}% · threads {threads}"
    except Exception:
        return "mem n/a"


async def _heartbeat_loop(app: Application):
    while True:
        await asyncio.sleep(60)
        with JOBS_LOCK:
            active = sum(1 for h in JOBS.values() if not h.ui.finished)
            total = len(JOBS)
        uptime = _fmt_duration(time.time() - _startup_ts)
        await _syslog(
            app,
            f"*heartbeat* — up {uptime} · jobs {active} active / {total} total · {_proc_stats()}",
        )


async def _cleanup_loop(app: Application):
    while True:
        await asyncio.sleep(3600)
        cutoff = time.time() - RETENTION_H * 3600
        if not JOBS_DIR.exists():
            continue
        for udir in JOBS_DIR.iterdir():
            if not udir.is_dir():
                continue
            for jdir in udir.iterdir():
                if not jdir.is_dir():
                    continue
                try:
                    if jdir.stat().st_mtime < cutoff:
                        shutil.rmtree(jdir, ignore_errors=True)
                except Exception:
                    pass

# ── main ─────────────────────────────────────────────────────────────────
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("check", whitelist_required(cmd_check)))
    app.add_handler(CommandHandler("status", whitelist_required(cmd_status)))
    app.add_handler(CommandHandler("stop", whitelist_required(cmd_stop)))
    app.add_handler(CommandHandler("history", whitelist_required(cmd_history)))
    app.add_handler(CommandHandler("results", cmd_results))

    app.add_handler(CommandHandler("whitelist", cmd_whitelist))
    app.add_handler(CommandHandler("wl_add", cmd_wl_add))
    app.add_handler(CommandHandler("wl_remove", cmd_wl_remove))
    app.add_handler(CommandHandler("wl_list", cmd_wl_list))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    async def _start_bg(a):
        asyncio.create_task(_cleanup_loop(a))
        asyncio.create_task(_heartbeat_loop(a))
        await _syslog(
            a,
            f"*bot online* — owner `{OWNER_ID}` · "
            f"whitelist `{len(WHITELIST)}` · pid `{os.getpid()}`",
        )

    app.post_init = _start_bg
    app_ref["app"] = app
    app_ref["loop"] = asyncio.get_event_loop()

    print(f"bot running. owner={OWNER_ID}. whitelist size={len(WHITELIST)}")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()