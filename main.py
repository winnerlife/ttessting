import os
import re
import html
import time
import signal
import logging
import asyncio
from urllib.parse import urlparse

import aiohttp
import httpx
from aiohttp import web
from supabase import create_async_client, AsyncClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError as TelegramNetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────
# Configuration & Constants
# ─────────────────────────────────────────────
# Required env vars : TELEGRAM_TOKEN, SUPABASE_KEY (service_role key)
# Recommended       : SUPABASE_URL (e.g. https://<project-ref>.supabase.co)
# Optional          : PORT, APP_URL, ADMIN_ID, RECORDING_GROUP_ID, MUSIC_CHANNEL_ID,
#                     DB_KEEPALIVE_SECONDS (default 21600 = 6 hours)

def clean_supabase_url(raw: str | None) -> str:
    """Normalise SUPABASE_URL: strip quotes/whitespace, force https://, drop /rest/v1 and trailing slash."""
    url = (raw or "").strip().strip('"').strip("'").strip()
    if url and not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    url = url.rstrip("/")
    for suffix in ("/rest/v1", "/rest"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url.rstrip("/")

BOT_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
PORT = int(os.environ.get("PORT", "8080"))
APP_URL = (os.environ.get("APP_URL") or "").strip() or None

ADMIN_ID = int(os.environ.get("ADMIN_ID", "7429996344"))
SUPABASE_URL_FROM_ENV = "SUPABASE_URL" in os.environ
SUPABASE_URL = clean_supabase_url(os.environ.get("SUPABASE_URL", "https://zrwbwtzduhgmtszzleot.supabase.co"))
SUPABASE_KEY = (os.environ.get("SUPABASE_KEY") or "").strip()
SUPABASE_HOST = urlparse(SUPABASE_URL).hostname or "(invalid URL)"

# How often to run a tiny query so the free-tier project never looks inactive.
# Supabase pauses free projects after ~7 days of low activity, so 6h is a safe margin.
DB_KEEPALIVE_SECONDS = max(60, int(os.environ.get("DB_KEEPALIVE_SECONDS", "21600")))

# Stop a broadcast after this many consecutive failures instead of looping forever.
MAX_BROADCAST_ERRORS = 5

def format_tg_id(raw_id: str | int) -> int:
    val = int(raw_id)
    if val < 0 and not str(val).startswith("-100"):
        return int(f"-100{abs(val)}")
    return val

RECORDING_GROUP_ID = format_tg_id(os.environ.get("RECORDING_GROUP_ID", "-5309919588"))
MUSIC_CHANNEL_ID = format_tg_id(os.environ.get("MUSIC_CHANNEL_ID", "-4448938519"))

CHANNEL_USERNAME = "Yourveryownplaylist"
CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME}"


# ─────────────────────────────────────────────
# Logging (with secret redaction)
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")

class RedactSecretsFilter(logging.Filter):
    """Safety net: mask anything that looks like a Telegram bot token before it is written."""
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = _TOKEN_RE.sub("bot<REDACTED>", msg)
            if redacted != msg:
                record.msg = redacted
                record.args = ()
        except Exception:
            pass
        return True

for _handler in logging.getLogger().handlers:
    _handler.addFilter(RedactSecretsFilter())

logger = logging.getLogger(__name__)

# Global Runtime State
BROADCAST_ACTIVE = False
ADMIN_MODE_USERS = set()
BOT_USERNAME = None
db: AsyncClient = None
BACKGROUND_TASKS: set[asyncio.Task] = set()
_ALERT_TIMES: dict[str, float] = {}

esc = html.escape


# ─────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────
def normalize_key(artist: str | None, title: str | None) -> str:
    text = f"{artist or ''}_{title or ''}".lower()
    return re.sub(r"[^a-z0-9]", "", text)

def make_hashtag(val: str | None) -> str:
    if not val:
        return ""
    clean = re.sub(r"[^a-zA-Z0-9]", "", val.title())
    return f"#{clean}" if clean else ""

def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "Unknown"
    mins, secs = divmod(seconds, 60)
    return f"{mins}:{secs:02d}"

def generate_caption(artist: str | None, album: str | None, genre: str) -> str:
    tags = [make_hashtag(genre), make_hashtag(artist)]
    if album:
        tags.append(make_hashtag(album))
    tags.append("#Music")
    return " ".join([t for t in tags if t]).strip()

def is_recording_group(chat_id: int) -> bool:
    cid_str = str(chat_id)
    target_str = str(RECORDING_GROUP_ID).replace("-100", "-")
    return cid_str == str(RECORDING_GROUP_ID) or cid_str == target_str or cid_str.replace("-100", "-") == target_str

def spawn_task(coro) -> asyncio.Task:
    """create_task that keeps a strong reference (so it can't be garbage-collected) and logs crashes."""
    task = asyncio.create_task(coro)
    BACKGROUND_TASKS.add(task)

    def _done(t: asyncio.Task):
        BACKGROUND_TASKS.discard(t)
        if not t.cancelled() and t.exception():
            logger.error(f"Background task crashed: {t.exception()!r}")

    task.add_done_callback(_done)
    return task

def is_db_connectivity_error(err: BaseException | None) -> bool:
    """True for network-level failures talking to Supabase (DNS, refused, timeout)."""
    return isinstance(err, httpx.TransportError)

def is_dns_error(err: BaseException | None) -> bool:
    text = str(err or "").lower()
    return "name or service not known" in text or "name resolution" in text or "getaddrinfo" in text

async def alert_admin(bot, key: str, text: str, cooldown: float = 1800):
    """DM the admin, at most once per `cooldown` seconds for a given alert key."""
    now = time.monotonic()
    last = _ALERT_TIMES.get(key)
    if last is not None and now - last < cooldown:
        return
    _ALERT_TIMES[key] = now
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Could not alert admin ({key}): {e}")

async def get_bot_username(bot) -> str:
    global BOT_USERNAME
    if not BOT_USERNAME:
        me = await bot.get_me()
        BOT_USERNAME = me.username
    return BOT_USERNAME

async def is_user_subscribed(bot, user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id=MUSIC_CHANNEL_ID, user_id=user_id)
        return member.status in ("creator", "administrator", "member", "restricted")
    except Exception as e:
        logger.warning(f"Subscription check error for {user_id}: {e}")
        return False

async def send_fsub_gate(message_or_query, pending_song_id: int | None = None, edit: bool = False):
    retry_data = f"check_sub_{pending_song_id}" if pending_song_id else "check_sub"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Music Channel", url=CHANNEL_URL)],
        [InlineKeyboardButton("🔄 I've Joined / Try Again", callback_data=retry_data, api_kwargs={"style": "primary"})]
    ])
    text = (
        "👋 <b>Welcome to the Music Hub!</b>\n\n"
        "To save songs, listen in high quality, and access your personal playlist, "
        "please join our channel first.\n\n"
        f"1️⃣ Tap <b>Join Music Channel</b> (@{CHANNEL_USERNAME})\n"
        "2️⃣ Tap <b>I've Joined / Try Again</b> below"
    )
    if edit:
        await message_or_query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await message_or_query.reply_text(text, reply_markup=keyboard, parse_mode="HTML")

async def register_user(user_id: int, username: str | None = None, first_name: str | None = None):
    """Upsert the user. Logs and RE-RAISES on failure so callers never continue as if it worked;
    the global error handler tells the user to try again."""
    try:
        await db.table("users").upsert({
            "user_id": user_id,
            "username": username,
            "first_name": first_name
        }).execute()
    except Exception as e:
        logger.error(f"Error registering user {user_id}: {e!r}")
        raise

async def is_user_registered(user_id: int) -> bool:
    """Returns True/False for a real answer. Database failures PROPAGATE (they are not 'not registered')."""
    res = await db.table("users").select("user_id").eq("user_id", user_id).limit(1).execute()
    return len(res.data) > 0


# ─────────────────────────────────────────────
# Database Health: startup check + keep-alive
# ─────────────────────────────────────────────
async def verify_db_connection(retries: int = 3) -> bool:
    """create_async_client() makes no network call, so run a real query to prove the DB is reachable."""
    for attempt in range(1, retries + 1):
        try:
            await db.table("users").select("user_id").limit(1).execute()
            return True
        except Exception as e:
            logger.error(f"Database check {attempt}/{retries} failed: {e!r}")
            if is_dns_error(e):
                logger.error(
                    f"Cannot resolve '{SUPABASE_HOST}'. Check that the Supabase project is not paused/deleted "
                    f"and that SUPABASE_URL is correct."
                )
            if attempt < retries:
                await asyncio.sleep(2 * attempt)
    return False

async def supabase_keepalive(app: Application):
    """Runs a tiny query on a schedule so the free-tier project never looks inactive,
    and DMs the admin when the database goes down / comes back."""
    db_was_down = False
    await asyncio.sleep(30)
    while True:
        try:
            await db.table("users").select("user_id").limit(1).execute()
            logger.info("💓 Supabase keep-alive query OK")
            if db_was_down:
                db_was_down = False
                await alert_admin(app.bot, "db_recovered", "✅ <b>Database is reachable again.</b>", cooldown=0)
        except Exception as e:
            logger.error(f"Supabase keep-alive failed: {e!r}")
            if not db_was_down:
                db_was_down = True
                hint = (
                    "\nThe hostname could not be resolved. Check whether the Supabase project is paused."
                    if is_dns_error(e) else ""
                )
                await alert_admin(
                    app.bot, "db_down",
                    f"🚨 <b>Database unreachable</b>\n<code>{esc(repr(e)[:300])}</code>{hint}",
                    cooldown=0,
                )
        await asyncio.sleep(DB_KEEPALIVE_SECONDS)


# ─────────────────────────────────────────────
# Global Error Handler
# ─────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error

    # Telegram-side network blips (polling timeouts, etc.): log quietly, never spam users.
    if isinstance(err, TelegramNetworkError):
        logger.warning(f"Telegram network error: {err}")
        return

    db_down = is_db_connectivity_error(err)
    if db_down:
        logger.error(f"Database connection error: {err!r}")
        await alert_admin(
            context.bot, "db_error",
            f"🚨 <b>Database connection error</b>\n<code>{esc(repr(err)[:300])}</code>",
        )
    else:
        logger.error("Unhandled exception while handling an update", exc_info=(type(err), err, err.__traceback__))

    if not isinstance(update, Update):
        return

    text = (
        "⚠️ The service is temporarily unavailable. Please try again in a few minutes."
        if db_down else
        "⚠️ Something went wrong. Please try again."
    )
    try:
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif update.effective_message and update.effective_chat and update.effective_chat.type == "private":
            await update.effective_message.reply_text(text)
    except Exception as e:
        logger.debug(f"Could not notify user about error: {e}")


# ─────────────────────────────────────────────
# Admin Mode & Recording Session Handlers
# ─────────────────────────────────────────────
async def adminmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        return

    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ Please send <code>/adminmode</code> in my private DM.", parse_mode="HTML")
        return

    ADMIN_MODE_USERS.add(user.id)
    await update.message.reply_text(
        "🛠️ <b>Admin Mode: Activated</b>\n\n"
        "• <code>/record &lt;genre&gt;</code> - Open a recording session (e.g. <code>/record rnb</code>)\n"
        "• <b>Forward Songs Here</b> - Send/forward tracks directly to this chat!\n"
        "• <code>/skipall</code> - Skip all pending duplicate tracks at once\n"
        "• <code>/over</code> - Close session & begin automatic publishing\n"
        "• <code>/stopbroadcast</code> - Pause or stop publishing\n"
        "• <code>/pin</code> - Reply to any message to post & pin it with a 'Go listen' button\n"
        "• <code>/status</code> - View current batch & stats\n"
        "• <code>/adminmodeoff</code> - Exit admin mode",
        parse_mode="HTML"
    )

async def adminmodeoff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    ADMIN_MODE_USERS.discard(update.effective_user.id)
    await update.message.reply_text("🔒 <b>Admin Mode: Deactivated</b>.", parse_mode="HTML")

async def record_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Please specify a genre. Example:\n<code>/record rnb</code>", parse_mode="HTML")
        return

    genre = " ".join(args).replace('"', '').strip()

    active = await db.table("batches").select("id, genre").eq("status", "recording").limit(1).execute()
    if active.data:
        cur = active.data[0]
        await update.message.reply_text(
            f"⚠️ Batch #{cur['id']} (<b>{esc(str(cur['genre']))}</b>) is currently recording.\n"
            f"Send <code>/over</code> to finish it first.",
            parse_mode="HTML"
        )
        return

    res = await db.table("batches").insert({"genre": genre, "status": "recording"}).execute()
    batch_id = res.data[0]["id"]

    await update.message.reply_text(
        f"🎙️ <b>Recording Session #{batch_id} Started!</b>\n"
        f"• <b>Genre:</b> #{esc(make_hashtag(genre)[1:])}\n\n"
        f"👉 Forward songs directly here or in the group.\n"
        f"👉 Send <code>/skipall</code> to skip duplicate tracks.\n"
        f"👉 Send <code>/over</code> when finished to begin channel broadcast.",
        parse_mode="HTML"
    )

async def skipall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    res = await db.table("songs").update({"status": "skipped"}).eq("status", "duplicate_pending").execute()
    count = len(res.data) if res.data else 0
    await update.message.reply_text(f"🚫 <b>Skipped {count} duplicate song(s).</b>", parse_mode="HTML")

async def over_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BROADCAST_ACTIVE
    if update.effective_user.id != ADMIN_ID:
        return

    active = await db.table("batches").select("*").eq("status", "recording").limit(1).execute()
    if not active.data:
        await update.message.reply_text("ℹ️ No active recording session. Start one with <code>/record &lt;genre&gt;</code>.", parse_mode="HTML")
        return

    batch = active.data[0]
    batch_id = batch["id"]
    genre = batch["genre"]

    q_res = await db.table("songs").select("id", count="exact").eq("batch_id", batch_id).eq("status", "queued").execute()
    queued_count = q_res.count or 0

    await db.table("batches").update({"status": "publishing"}).eq("id", batch_id).execute()

    await update.message.reply_text(
        f"🏁 <b>Recording Session #{batch_id} Closed</b>\n"
        f"• <b>Queued songs:</b> {queued_count}\n\n"
        f"🚀 <b>Starting automatic broadcast to channel...</b>\n"
        f"<i>(Send <code>/stopbroadcast</code> at any time to pause)</i>",
        parse_mode="HTML"
    )

    BROADCAST_ACTIVE = True
    spawn_task(broadcast_worker(context.application, batch_id, genre, notify_chat_id=update.effective_chat.id))

async def stopbroadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BROADCAST_ACTIVE
    if update.effective_user.id != ADMIN_ID:
        return

    if not BROADCAST_ACTIVE:
        await update.message.reply_text("ℹ️ There is no broadcast running right now.")
        return

    BROADCAST_ACTIVE = False
    await update.message.reply_text("🛑 <b>Broadcast stopping...</b> Remaining tracks remain queued.", parse_mode="HTML")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    total_songs = (await db.table("songs").select("id", count="exact").execute()).count or 0
    published_songs = (await db.table("songs").select("id", count="exact").eq("status", "published").execute()).count or 0
    total_users = (await db.table("users").select("user_id", count="exact").execute()).count or 0
    active_rec = await db.table("batches").select("*").eq("status", "recording").limit(1).execute()

    is_adm_mode = "🟢 Active" if update.effective_user.id in ADMIN_MODE_USERS else "⚪ Inactive"

    status_msg = (
        f"⚙️ <b>Bot System Status</b>\n\n"
        f"• <b>Admin Mode:</b> {is_adm_mode}\n"
        f"• <b>Active Broadcast:</b> {'🟢 Running' if BROADCAST_ACTIVE else '⚪ Idle'}\n"
        f"• <b>Music Channel:</b> <code>{MUSIC_CHANNEL_ID}</code> (@{CHANNEL_USERNAME})\n"
        f"• <b>Database:</b> <code>{esc(SUPABASE_HOST)}</code>\n"
        f"• <b>Total Songs in DB:</b> {total_songs}\n"
        f"• <b>Published Songs:</b> {published_songs}\n"
        f"• <b>Registered Users:</b> {total_users}\n\n"
    )

    if active_rec.data:
        status_msg += f"🎙️ <b>Active Batch:</b> #{active_rec.data[0]['id']} (Genre: {esc(str(active_rec.data[0]['genre']))})"
    else:
        # NOTE: must be escaped, a raw <genre> breaks Telegram's HTML parser.
        status_msg += "🎙️ <b>Active Batch:</b> None (Use /record &lt;genre&gt;)"

    await update.message.reply_text(status_msg, parse_mode="HTML")

async def pin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    replied = update.message.reply_to_message
    if not replied:
        await update.message.reply_text("⚠️ Reply to any message you want to post and pin in the channel with <code>/pin</code>.", parse_mode="HTML")
        return

    bot_user = await get_bot_username(context.bot)
    listen_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎧 Go listen", url=f"https://t.me/{bot_user}", api_kwargs={"style": "primary"})]
    ])

    try:
        sent_msg = await context.bot.copy_message(
            chat_id=MUSIC_CHANNEL_ID,
            from_chat_id=update.effective_chat.id,
            message_id=replied.message_id,
            reply_markup=listen_markup
        )
        await context.bot.pin_chat_message(chat_id=MUSIC_CHANNEL_ID, message_id=sent_msg.message_id)
        await update.message.reply_text("✅ Message successfully posted and pinned in the channel!", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error in /pin: {e}")
        await update.message.reply_text(f"❌ Error: {esc(str(e))}", parse_mode="HTML")


# ─────────────────────────────────────────────
# Audio Ingestion & Duplicate Handling
# ─────────────────────────────────────────────
async def handle_audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    is_group = is_recording_group(msg.chat_id)
    is_dm_admin = msg.chat.type == "private" and msg.from_user.id == ADMIN_ID

    if not (is_group or is_dm_admin):
        return

    audio = msg.audio
    doc = msg.document
    file_id = None
    file_unique_id = None
    duration = 0
    artist = "Unknown Artist"
    title = "Unknown Track"
    album = None

    if audio:
        file_id = audio.file_id
        file_unique_id = audio.file_unique_id
        duration = audio.duration
        artist = audio.performer or "Unknown Artist"
        title = audio.title or audio.file_name or "Track"
    elif doc and ((doc.mime_type and "audio" in doc.mime_type) or (doc.file_name and doc.file_name.lower().endswith(('.mp3', '.m4a', '.flac', '.wav', '.ogg')))):
        file_id = doc.file_id
        file_unique_id = doc.file_unique_id
        title = doc.file_name or "Track"
    else:
        return

    active = await db.table("batches").select("id").eq("status", "recording").limit(1).execute()
    if not active.data:
        if is_dm_admin:
            await msg.reply_text("⚠️ No active recording session! Send <code>/record &lt;genre&gt;</code> first.", parse_mode="HTML")
        return

    batch_id = active.data[0]["id"]
    norm = normalize_key(artist, title)

    existing = await db.table("songs").select("id, artist, title").or_(f"norm_key.eq.{norm},file_unique_id.eq.{file_unique_id}").neq("status", "skipped").limit(1).execute()

    order_res = await db.table("songs").select("order_index").eq("batch_id", batch_id).order("order_index", desc=True).limit(1).execute()
    next_order = (order_res.data[0]["order_index"] + 1) if order_res.data and order_res.data[0]["order_index"] else 1

    if existing.data:
        res = await db.table("songs").insert({
            "batch_id": batch_id,
            "file_id": file_id,
            "file_unique_id": file_unique_id,
            "artist": artist,
            "title": title,
            "album": album,
            "duration": duration,
            "norm_key": norm,
            "status": "duplicate_pending",
            "order_index": next_order
        }).execute()
        song_id = res.data[0]["id"]

        buttons = [
            [
                InlineKeyboardButton("🚫 Skip", callback_data=f"dup_skip_{song_id}", api_kwargs={"style": "danger"}),
                InlineKeyboardButton("⚠️ Record Anyway", callback_data=f"dup_keep_{song_id}", api_kwargs={"style": "primary"}),
            ]
        ]
        await msg.reply_text(
            f"⚠️ <b>Duplicate Detected</b>\n"
            f"🎵 <b>{esc(artist)} - {esc(title)}</b> already exists in DB.\n"
            f"Action for track #{next_order} (or send <code>/skipall</code>):",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML"
        )
    else:
        await db.table("songs").insert({
            "batch_id": batch_id,
            "file_id": file_id,
            "file_unique_id": file_unique_id,
            "artist": artist,
            "title": title,
            "album": album,
            "duration": duration,
            "norm_key": norm,
            "status": "queued",
            "order_index": next_order
        }).execute()
        logger.info(f"✅ Queued track #{next_order}: {artist} - {title}")

async def duplicate_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Admin only.", show_alert=True)
        return

    action, song_id_str = query.data.rsplit("_", 1)
    song_id = int(song_id_str)

    if action == "dup_skip":
        await db.table("songs").update({"status": "skipped"}).eq("id", song_id).execute()
        await query.answer("Track skipped.")
        await query.edit_message_text("🚫 <b>Track Skipped.</b>", parse_mode="HTML")
    elif action == "dup_keep":
        await db.table("songs").update({"status": "queued"}).eq("id", song_id).execute()
        await query.answer("Track queued.")
        await query.edit_message_text("✅ <b>Track Kept and Queued.</b>", parse_mode="HTML")


# ─────────────────────────────────────────────
# Channel Publishing Queue
# ─────────────────────────────────────────────
async def broadcast_worker(app: Application, batch_id: int, genre: str, notify_chat_id: int):
    global BROADCAST_ACTIVE
    logger.info(f"Starting broadcast for Batch #{batch_id}")
    published_count = 0
    errors_in_row = 0
    final_status = "stopped"
    warning = ""

    try:
        while BROADCAST_ACTIVE:
            if errors_in_row >= MAX_BROADCAST_ERRORS:
                logger.error(f"Batch #{batch_id}: {errors_in_row} consecutive errors, stopping broadcast.")
                warning = f"⚠️ Stopped after {errors_in_row} consecutive errors. Check the logs, then resume."
                break

            try:
                res = await db.table("songs").select("*").eq("batch_id", batch_id).eq("status", "queued").order("order_index").limit(1).execute()
            except Exception as e:
                errors_in_row += 1
                logger.error(f"Broadcast DB error (batch #{batch_id}): {e!r}")
                await asyncio.sleep(10)
                continue

            if not res.data:
                break

            song = res.data[0]
            song_id = song["id"]
            caption = generate_caption(artist=song["artist"], album=song["album"], genre=genre)

            channel_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add to Playlist", callback_data=f"pl_add_{song_id}", api_kwargs={"style": "success"})]
            ])

            try:
                sent_msg = await app.bot.send_audio(
                    chat_id=MUSIC_CHANNEL_ID,
                    audio=song["file_id"],
                    caption=caption,
                    parse_mode="HTML",
                    duration=song["duration"],
                    performer=song["artist"],
                    title=song["title"],
                    reply_markup=channel_markup
                )
            except Exception as e:
                errors_in_row += 1
                logger.error(f"Error publishing song #{song_id}: {e}")
                await asyncio.sleep(5)
                continue

            published_count += 1

            # The song is already in the channel. Mark it published, retrying briefly; if the DB is down,
            # halt instead of looping (a still-'queued' song would be posted to the channel again).
            marked = False
            for _ in range(3):
                try:
                    await db.table("songs").update({
                        "status": "published",
                        "channel_message_id": sent_msg.message_id
                    }).eq("id", song_id).execute()
                    marked = True
                    break
                except Exception as e:
                    logger.error(f"Could not mark song #{song_id} as published: {e!r}")
                    await asyncio.sleep(5)

            if not marked:
                warning = (
                    f"⚠️ Song #{song_id} was posted but could not be marked as published (database error). "
                    f"Broadcast halted to avoid posting it twice. Fix it manually before resuming."
                )
                break

            errors_in_row = 0
            await asyncio.sleep(2.5)

        try:
            remaining = await db.table("songs").select("id").eq("batch_id", batch_id).eq("status", "queued").limit(1).execute()
            final_status = "stopped" if remaining.data else "completed"
            await db.table("batches").update({"status": final_status}).eq("id", batch_id).execute()
        except Exception as e:
            logger.error(f"Could not finalise batch #{batch_id}: {e!r}")
            final_status = "unknown (database error)"
    finally:
        BROADCAST_ACTIVE = False

    try:
        await app.bot.send_message(
            chat_id=notify_chat_id,
            text=f"🏁 <b>Broadcast finished for Batch #{batch_id}!</b>\n"
                 f"• <b>Published:</b> {published_count} tracks\n"
                 f"• <b>Status:</b> <i>{esc(final_status)}</i>"
                 + (f"\n\n{esc(warning)}" if warning else ""),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Could not send broadcast summary: {e}")


# ─────────────────────────────────────────────
# Subscriber Playlist (10 Tracks Per Page & Styled Buttons)
# ─────────────────────────────────────────────
async def add_to_playlist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    song_id = int(query.data.replace("pl_add_", ""))
    bot_user = await get_bot_username(context.bot)

    if not await is_user_registered(user.id):
        await query.answer(url=f"https://t.me/{bot_user}?start=save_{song_id}")
        return

    check = await db.table("user_playlists").select("song_id").eq("user_id", user.id).eq("song_id", song_id).limit(1).execute()
    if check.data:
        await query.answer("Already in your playlist! 🎧", show_alert=False)
        return

    await db.table("user_playlists").insert({"user_id": user.id, "song_id": song_id}).execute()
    await query.answer("Added to your playlist! ⭐ Check bot DM.", show_alert=False)

async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data

    pending_song_id = None
    if data.startswith("check_sub_"):
        try:
            pending_song_id = int(data.replace("check_sub_", ""))
        except ValueError:
            pending_song_id = None

    if not await is_user_subscribed(context.bot, user.id):
        await query.answer("⚠️ You haven't joined the channel yet!", show_alert=True)
        return

    await register_user(user.id, user.username, user.first_name)
    await query.answer("✅ Channel membership verified!")

    if pending_song_id:
        await db.table("user_playlists").upsert({"user_id": user.id, "song_id": pending_song_id}).execute()
        song_data = await db.table("songs").select("artist, title").eq("id", pending_song_id).limit(1).execute()
        info = f"<b>{esc(song_data.data[0]['artist'])} - {esc(song_data.data[0]['title'])}</b>" if song_data.data else "the track"
        await query.edit_message_text(
            f"🎉 <b>Welcome!</b>\n\n✅ Saved {info} to your playlist!\nSend <code>/playlist</code> to stream.",
            parse_mode="HTML"
        )
    else:
        await query.edit_message_text(
            "👋 <b>Welcome to the Music Hub!</b>\n\n• Tap <b>Add to Playlist</b> under channel songs.\n• Send <code>/playlist</code> to stream.",
            parse_mode="HTML"
        )

async def user_playlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please check your playlist in my private chat! 🎧")
        return

    user = update.effective_user
    await register_user(user.id, user.username, user.first_name)

    if not await is_user_subscribed(context.bot, user.id):
        await send_fsub_gate(update.message)
        return

    await render_playlist_page(update.message, user.id, page=1)

async def _send_or_edit(message, text: str, reply_markup, edit: bool):
    if edit:
        try:
            await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        except BadRequest as e:
            # Happens when the page is re-rendered with identical content; harmless.
            if "message is not modified" not in str(e).lower():
                raise
    else:
        await message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")

async def render_playlist_page(message, user_id: int, page: int, edit=False):
    limit = 10  # 10 songs per page
    offset = (page - 1) * limit

    count_res = await db.table("user_playlists").select("song_id", count="exact").eq("user_id", user_id).execute()
    total = count_res.count or 0

    if total == 0:
        text = "🎧 <b>Your playlist is empty!</b>\n\nTap <b>Add to Playlist</b> under any song in our channel to save it."
        await _send_or_edit(message, text, None, edit)
        return

    res = await db.table("user_playlists").select(
        "song_id, songs(id, artist, title, duration)"
    ).eq("user_id", user_id).order("added_at", desc=True).range(offset, offset + limit - 1).execute()

    total_pages = max(1, (total + limit - 1) // limit)
    text = f"🎧 <b>Your Saved Playlist</b> (Page {page}/{total_pages})\n\n"
    keyboard = []

    for item in res.data:
        s = item["songs"]
        if not s:
            continue
        artist = s["artist"] or "Unknown"
        title = s["title"] or "Track"
        text += f"• 🎵 <b>{esc(artist)}</b> - {esc(title)} <i>({format_duration(s['duration'])})</i>\n"

        # Blue Play button, Red Danger Remove button
        keyboard.append([
            InlineKeyboardButton(f"▶️ Play {title[:16]}", callback_data=f"pl_play_{s['id']}", api_kwargs={"style": "primary"}),
            InlineKeyboardButton("❌ Remove", callback_data=f"pl_rem_{s['id']}_{page}", api_kwargs={"style": "danger"})
        ])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"pl_page_{page-1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"pl_page_{page+1}"))
    if nav:
        keyboard.append(nav)

    await _send_or_edit(message, text, InlineKeyboardMarkup(keyboard), edit)

async def playlist_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if data.startswith("pl_page_"):
        page = int(data.replace("pl_page_", ""))
        await render_playlist_page(query.message, user_id, page, edit=True)
        await query.answer()

    elif data.startswith("pl_play_"):
        song_id = int(data.replace("pl_play_", ""))
        res = await db.table("songs").select("file_id, artist, title").eq("id", song_id).limit(1).execute()
        if res.data:
            song = res.data[0]
            await query.answer("Delivering audio...")
            await context.bot.send_audio(
                chat_id=user_id,
                audio=song["file_id"],
                title=song["title"],
                performer=song["artist"]
            )
        else:
            await query.answer("Audio not found.", show_alert=True)

    elif data.startswith("pl_rem_"):
        parts = data.split("_")
        song_id = int(parts[2])
        page = int(parts[3])

        await db.table("user_playlists").delete().eq("user_id", user_id).eq("song_id", song_id).execute()
        await query.answer("Removed from playlist 🗑️")

        total = (await db.table("user_playlists").select("song_id", count="exact").eq("user_id", user_id).execute()).count or 0
        limit = 10  # 10 songs per page
        max_page = max(1, (total + limit - 1) // limit)
        await render_playlist_page(query.message, user_id, min(page, max_page), edit=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    pending_song_id = None
    if args and args[0].startswith("save_"):
        try:
            pending_song_id = int(args[0].replace("save_", ""))
        except ValueError:
            pending_song_id = None

    if user.id == ADMIN_ID:
        await register_user(user.id, user.username, user.first_name)
        await update.message.reply_text(
            "👋 <b>Welcome Admin!</b>\n\n"
            "• <code>/adminmode</code> - Open admin control panel\n"
            "• <code>/playlist</code> - View your saved music",
            parse_mode="HTML"
        )
        return

    if not await is_user_subscribed(context.bot, user.id):
        await send_fsub_gate(update.message, pending_song_id=pending_song_id)
        return

    await register_user(user.id, user.username, user.first_name)

    if pending_song_id:
        await db.table("user_playlists").upsert({"user_id": user.id, "song_id": pending_song_id}).execute()
        song = await db.table("songs").select("artist, title").eq("id", pending_song_id).limit(1).execute()
        info = f"<b>{esc(song.data[0]['artist'])} - {esc(song.data[0]['title'])}</b>" if song.data else "the song"
        await update.message.reply_text(
            f"🎉 <b>Welcome!</b>\n\n✅ Added {info} to your playlist.\nSend <code>/playlist</code> to stream.",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            "👋 <b>Welcome to the Music Hub!</b>\n\n• Tap <b>Add to Playlist</b> under channel songs.\n• Send <code>/playlist</code> to stream.",
            parse_mode="HTML"
        )


# ─────────────────────────────────────────────
# Web Health Check & Keep-Alive
# ─────────────────────────────────────────────
async def web_index(request):
    return web.Response(
        text="<h1>Music Bot Online</h1><p>Running with persistent Supabase backend.</p>",
        content_type="text/html"
    )

async def keep_alive_pinger():
    """Pings the bot's own web server so the HOST doesn't sleep.
    (This does NOT touch Supabase, see supabase_keepalive() for that.)"""
    await asyncio.sleep(15)
    target_url = APP_URL or f"http://127.0.0.1:{PORT}/"
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(target_url, timeout=timeout) as resp:
                    logger.info(f"💓 Keep-alive ping sent (HTTP {resp.status})")
            except Exception as e:
                logger.warning(f"Keep-alive ping notice: {e}")
            await asyncio.sleep(600)


# ─────────────────────────────────────────────
# Main Application Runtime
# ─────────────────────────────────────────────
async def main():
    global db
    if not BOT_TOKEN:
        logger.error("TELEGRAM_TOKEN is missing!")
        raise SystemExit(1)
    if not SUPABASE_KEY:
        logger.error("SUPABASE_KEY is missing! Set your service_role key.")
        raise SystemExit(1)
    if not SUPABASE_URL:
        logger.error("SUPABASE_URL is empty or invalid!")
        raise SystemExit(1)

    if not SUPABASE_URL_FROM_ENV:
        logger.warning("SUPABASE_URL env var is not set, using the hardcoded default. Set it explicitly in your deployment.")
    logger.info(f"Supabase host: {SUPABASE_HOST}")

    db = await create_async_client(SUPABASE_URL, SUPABASE_KEY)

    # create_async_client() makes no network request, so prove the database is really reachable.
    if not await verify_db_connection():
        logger.critical(
            f"Database is unreachable at {SUPABASE_HOST}. If this is a free-tier project, "
            f"it may be paused: open the Supabase dashboard and click 'Resume project'. "
            f"Also double-check the SUPABASE_URL env var."
        )
        raise SystemExit(1)
    logger.info("✅ Supabase database is reachable.")

    app = Application.builder().token(BOT_TOKEN).build()

    # Global error handler (logs, tells users, alerts the admin on DB outages)
    app.add_error_handler(error_handler)

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("adminmode", adminmode_command))
    app.add_handler(CommandHandler("adminmodeoff", adminmodeoff_command))
    app.add_handler(CommandHandler("record", record_command))
    app.add_handler(CommandHandler("skipall", skipall_command))
    app.add_handler(CommandHandler("over", over_command))
    app.add_handler(CommandHandler("stopbroadcast", stopbroadcast_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("pin", pin_command))
    app.add_handler(CommandHandler("playlist", user_playlist_command))

    app.add_handler(CallbackQueryHandler(duplicate_callback_handler, pattern=r"^dup_"))
    app.add_handler(CallbackQueryHandler(add_to_playlist_callback, pattern=r"^pl_add_"))
    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern=r"^check_sub"))
    app.add_handler(CallbackQueryHandler(playlist_page_callback, pattern=r"^pl_(page|play|rem)_"))

    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.AUDIO | filters.Document.FileExtension("mp3"), handle_audio_message))

    await app.initialize()

    web_app = web.Application()
    web_app.router.add_get("/", web_index)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🚀 Health check server running on port {PORT}")

    await app.start()
    await app.updater.start_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)
    logger.info("🤖 Music Management Bot is actively polling!")

    pinger_task = asyncio.create_task(keep_alive_pinger())
    db_keepalive_task = asyncio.create_task(supabase_keepalive(app))

    stop_signal = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_signal.set)

    await stop_signal.wait()

    logger.info("Shutting down bot...")
    pinger_task.cancel()
    db_keepalive_task.cancel()
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    await site.stop()
    await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
