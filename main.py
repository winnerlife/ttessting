import os
import re
import signal
import sqlite3
import logging
import asyncio
from aiohttp import web
import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
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
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", "8080"))
APP_URL = os.environ.get("APP_URL")

ADMIN_ID = int(os.environ.get("ADMIN_ID", "7429996344"))

def format_tg_id(raw_id: str | int) -> int:
    """Ensures supergroups and channels have the required -100 prefix."""
    val = int(raw_id)
    if val < 0 and not str(val).startswith("-100"):
        return int(f"-100{abs(val)}")
    return val

RECORDING_GROUP_ID = format_tg_id(os.environ.get("RECORDING_GROUP_ID", "-5309919588"))
MUSIC_CHANNEL_ID = format_tg_id(os.environ.get("MUSIC_CHANNEL_ID", "-4448938519"))

# Hardcoded Channel Username & Link
CHANNEL_USERNAME = "Yourveryownplaylist"
CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME}"

DB_PATH = "music_bot.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# State tracking
BROADCAST_ACTIVE = False
ADMIN_MODE_USERS = set()
BOT_USERNAME = None


# ─────────────────────────────────────────────
# Database Layer (SQLite with WAL mode)
# ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            genre TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'recording', -- 'recording', 'publishing', 'completed', 'stopped'
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER,
            file_id TEXT NOT NULL,
            file_unique_id TEXT NOT NULL,
            artist TEXT,
            title TEXT,
            album TEXT,
            duration INTEGER,
            norm_key TEXT,
            status TEXT NOT NULL DEFAULT 'queued', -- 'queued', 'duplicate_pending', 'skipped', 'published'
            order_index INTEGER,
            channel_message_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(batch_id) REFERENCES batches(id)
        );

        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_playlists (
            user_id INTEGER NOT NULL,
            song_id INTEGER NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, song_id),
            FOREIGN KEY(song_id) REFERENCES songs(id)
        );
        """)
    logger.info("✅ Database initialized successfully.")

def register_user(user_id: int, username: str | None = None, first_name: str | None = None):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
            (user_id, username, first_name)
        )

def is_user_registered(user_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None


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

def generate_caption(artist: str | None, title: str | None, album: str | None, duration: int | None, genre: str) -> str:
    art = artist or "Unknown Artist"
    tit = title or "Unknown Track"
    
    caption = (
        f"🎵 <b>Track:</b> {tit}\n"
        f"👤 <b>Artist:</b> {art}\n"
    )
    if album:
        caption += f"💿 <b>Album:</b> {album}\n"
    caption += f"⏱ <b>Duration:</b> {format_duration(duration)}\n\n"

    tags = [make_hashtag(genre), make_hashtag(art)]
    if album:
        tags.append(make_hashtag(album))
    tags.append("#Music")
    
    caption += " ".join([t for t in tags if t])
    return caption

def is_recording_group(chat_id: int) -> bool:
    cid_str = str(chat_id)
    target_str = str(RECORDING_GROUP_ID).replace("-100", "-")
    return cid_str == str(RECORDING_GROUP_ID) or cid_str == target_str or cid_str.replace("-100", "-") == target_str

async def get_bot_username(bot) -> str:
    global BOT_USERNAME
    if not BOT_USERNAME:
        me = await bot.get_me()
        BOT_USERNAME = me.username
    return BOT_USERNAME

async def is_user_subscribed(bot, user_id: int) -> bool:
    """Checks if a user is an active member/admin of the music channel."""
    if user_id == ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id=MUSIC_CHANNEL_ID, user_id=user_id)
        return member.status in ("creator", "administrator", "member", "restricted")
    except Exception as e:
        logger.warning(f"Subscription verification error for user {user_id}: {e}")
        return False

async def send_fsub_gate(message_or_query, pending_song_id: int | None = None, edit: bool = False):
    """Sends the forced subscription gate with the hardcoded join link and retry button."""
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


# ─────────────────────────────────────────────
# Dynamic Web Dashboard & Keep-Alive Pinger
# ─────────────────────────────────────────────
async def web_index(request):
    with get_db() as conn:
        active_batch = conn.execute("SELECT id, genre FROM batches WHERE status = 'recording'").fetchone()
        total_songs = conn.execute("SELECT COUNT(*) as c FROM songs").fetchone()["c"]
        published = conn.execute("SELECT COUNT(*) as c FROM songs WHERE status = 'published'").fetchone()["c"]
        users_count = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]

    batch_str = f"Batch #{active_batch['id']} ({active_batch['genre']})" if active_batch else "None (Idle)"
    broadcast_badge = "🟢 Broadcasting" if BROADCAST_ACTIVE else "⚪ Idle"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Music Bot Status</title>
    <style>
        body {{ background:#0f172a; color:#e2e8f0; font-family:system-ui,sans-serif; display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
        .card {{ background:#1e293b; padding:40px; border-radius:16px; text-align:center; box-shadow:0 20px 40px rgba(0,0,0,.5); max-width:440px; border-top:4px solid #38bdf8; }}
        h1 {{ color:#38bdf8; margin:0 0 10px; font-size:1.6rem; }}
        .status {{ color:#4ade80; font-weight:700; margin:14px 0; display:inline-flex; align-items:center; gap:8px; font-size:1.05em; }}
        .dot {{ width:10px; height:10px; background-color:#4ade80; border-radius:50%; box-shadow:0 0 8px #4ade80; }}
        p {{ color:#94a3b8; line-height:1.6; margin-bottom:20px; font-size:0.95em; }}
        .stats {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:20px; }}
        .stat-box {{ background:#334155; padding:12px; border-radius:10px; text-align:center; }}
        .stat-val {{ font-size:1.3em; font-weight:bold; color:#f8fafc; }}
        .stat-lbl {{ font-size:0.8em; color:#94a3b8; }}
        .tag {{ display:inline-block; background:#1e1b4b; color:#c7d2fe; padding:6px 14px; border-radius:20px; font-size:0.85em; font-weight:600; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>🎧 Music Service Bot</h1>
        <div class="status"><div class="dot"></div> Online & Polling Telegram</div>
        <p>Zero-download music management, deduplication pipeline, and automated channel broadcast engine.</p>
        
        <div class="stats">
            <div class="stat-box">
                <div class="stat-val">{total_songs}</div>
                <div class="stat-lbl">Songs Tracked</div>
            </div>
            <div class="stat-box">
                <div class="stat-val">{published}</div>
                <div class="stat-lbl">Published Tracks</div>
            </div>
            <div class="stat-box" style="grid-column: span 2;">
                <div class="stat-val">{users_count}</div>
                <div class="stat-lbl">Active Bot Subscribers</div>
            </div>
        </div>

        <div class="tag">Active Batch: {batch_str}</div>
        <div class="tag" style="margin-top:8px;background:#334155;color:#f1f5f9;">Queue: {broadcast_badge}</div>
    </div>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")

async def keep_alive_pinger():
    await asyncio.sleep(15)
    target_url = APP_URL or f"http://127.0.0.1:{PORT}/"
    logger.info(f"🔄 Keep-alive pinger started. Target: {target_url}")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(target_url, timeout=10) as resp:
                    logger.info(f"💓 Keep-alive ping sent (HTTP {resp.status})")
            except Exception as e:
                logger.warning(f"⚠️ Keep-alive ping notice: {e}")
            await asyncio.sleep(600)


# ─────────────────────────────────────────────
# Admin Mode & Session Handlers
# ─────────────────────────────────────────────
async def adminmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enables admin mode in private chat."""
    user = update.effective_user
    if user.id != ADMIN_ID:
        return

    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ Please send <code>/adminmode</code> in my private DM.", parse_mode="HTML")
        return

    ADMIN_MODE_USERS.add(user.id)
    await update.message.reply_text(
        "🛠️ <b>Admin Mode: Activated</b>\n\n"
        "You can now manage the bot directly from here:\n"
        "• <code>/record &lt;genre&gt;</code> - Open a recording session (e.g. <code>/record rnb</code>)\n"
        "• <b>Forward Songs Here</b> - Send/forward tracks directly to this chat!\n"
        "• <code>/over</code> - Close session & begin automatic publishing\n"
        "• <code>/stopbroadcast</code> - Pause or stop publishing\n"
        "• <code>/pin</code> - Reply to any message to post & pin it to the channel with a 'Go listen' button\n"
        "• <code>/status</code> - View current batch & stats\n"
        "• <code>/adminmodeoff</code> - Exit admin mode",
        parse_mode="HTML"
    )

async def adminmodeoff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disables admin mode in private chat."""
    user = update.effective_user
    if user.id != ADMIN_ID:
        return

    ADMIN_MODE_USERS.discard(user.id)
    await update.message.reply_text("🔒 <b>Admin Mode: Deactivated</b>. Back to standard user mode.", parse_mode="HTML")

async def record_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts a new recording batch for a specified genre."""
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Please specify a genre. Example:\n<code>/record rnb</code>", parse_mode="HTML")
        return

    genre = " ".join(args).replace('"', '').strip()

    with get_db() as conn:
        active = conn.execute("SELECT id, genre FROM batches WHERE status = 'recording'").fetchone()
        if active:
            await update.message.reply_text(
                f"⚠️ Batch #{active['id']} (<b>{active['genre']}</b>) is currently recording.\n"
                f"Send <code>/over</code> to finish it first.",
                parse_mode="HTML"
            )
            return

        cur = conn.execute("INSERT INTO batches (genre, status) VALUES (?, 'recording')", (genre,))
        batch_id = cur.lastrowid

    await update.message.reply_text(
        f"🎙️ <b>Recording Session #{batch_id} Started!</b>\n"
        f"• <b>Genre:</b> #{make_hashtag(genre)[1:]}\n\n"
        f"👉 <b>Forward songs directly here</b> (or into the group).\n"
        f"👉 Send <code>/over</code> when finished to begin channel broadcast.",
        parse_mode="HTML"
    )

async def over_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Closes the recording session and starts publishing to the channel."""
    global BROADCAST_ACTIVE
    if update.effective_user.id != ADMIN_ID:
        return

    with get_db() as conn:
        batch = conn.execute("SELECT id, genre FROM batches WHERE status = 'recording'").fetchone()
        if not batch:
            await update.message.reply_text("ℹ️ No active recording session found. Start one with <code>/record &lt;genre&gt;</code>.", parse_mode="HTML")
            return

        batch_id = batch["id"]
        genre = batch["genre"]

        queued_count = conn.execute("SELECT COUNT(*) as c FROM songs WHERE batch_id = ? AND status = 'queued'", (batch_id,)).fetchone()["c"]
        pending_count = conn.execute("SELECT COUNT(*) as c FROM songs WHERE batch_id = ? AND status = 'duplicate_pending'", (batch_id,)).fetchone()["c"]

        conn.execute("UPDATE batches SET status = 'publishing' WHERE id = ?", (batch_id,))

    await update.message.reply_text(
        f"🏁 <b>Recording Session #{batch_id} Closed</b>\n"
        f"• <b>Queued songs:</b> {queued_count}\n"
        f"• <b>Pending duplicates:</b> {pending_count}\n\n"
        f"🚀 <b>Starting automatic broadcast to channel...</b>\n"
        f"<i>(Send <code>/stopbroadcast</code> at any time to pause)</i>",
        parse_mode="HTML"
    )

    BROADCAST_ACTIVE = True
    asyncio.create_task(broadcast_worker(context.application, batch_id, genre, notify_chat_id=update.effective_chat.id))

async def stopbroadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stops the active channel broadcast."""
    global BROADCAST_ACTIVE
    if update.effective_user.id != ADMIN_ID:
        return

    if not BROADCAST_ACTIVE:
        await update.message.reply_text("ℹ️ There is no broadcast running right now.")
        return

    BROADCAST_ACTIVE = False
    await update.message.reply_text("🛑 <b>Broadcast stopping...</b> Remaining tracks remain queued.", parse_mode="HTML")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays current system and batch information."""
    if update.effective_user.id != ADMIN_ID:
        return

    with get_db() as conn:
        active_rec = conn.execute("SELECT * FROM batches WHERE status = 'recording'").fetchone()
        total_songs = conn.execute("SELECT COUNT(*) as c FROM songs").fetchone()["c"]
        published_songs = conn.execute("SELECT COUNT(*) as c FROM songs WHERE status = 'published'").fetchone()["c"]
        total_users = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]

    is_adm_mode = "🟢 Active" if update.effective_user.id in ADMIN_MODE_USERS else "⚪ Inactive"

    status_msg = (
        f"⚙️ <b>Bot System Status</b>\n\n"
        f"• <b>Admin Mode in DM:</b> {is_adm_mode}\n"
        f"• <b>Active Broadcast:</b> {'🟢 Running' if BROADCAST_ACTIVE else '⚪ Idle'}\n"
        f"• <b>Recording Group:</b> <code>{RECORDING_GROUP_ID}</code>\n"
        f"• <b>Music Channel:</b> <code>{MUSIC_CHANNEL_ID}</code> (@{CHANNEL_USERNAME})\n"
        f"• <b>Total Songs in DB:</b> {total_songs}\n"
        f"• <b>Total Published:</b> {published_songs}\n"
        f"• <b>Registered Users:</b> {total_users}\n\n"
    )

    if active_rec:
        status_msg += f"🎙️ <b>Active Batch:</b> #{active_rec['id']} (Genre: {active_rec['genre']})"
    else:
        status_msg += "🎙️ <b>Active Batch:</b> None (Use /record <genre>)"

    await update.message.reply_text(status_msg, parse_mode="HTML")


# ─────────────────────────────────────────────
# Channel Pin Command (Admin only)
# ─────────────────────────────────────────────
async def pin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: copies the replied message to the channel with a 'Go listen' button and pins it."""
    if update.effective_user.id != ADMIN_ID:
        return

    replied_msg = update.message.reply_to_message
    if not replied_msg:
        await update.message.reply_text(
            "⚠️ <b>Usage:</b> Reply to any message you want to post and pin in the channel with <code>/pin</code>.",
            parse_mode="HTML"
        )
        return

    bot_user = await get_bot_username(context.bot)
    listen_markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                text="🎧 Go listen",
                url=f"https://t.me/{bot_user}",
                api_kwargs={"style": "primary"}
            )
        ]
    ])

    try:
        sent_channel_msg = await context.bot.copy_message(
            chat_id=MUSIC_CHANNEL_ID,
            from_chat_id=update.effective_chat.id,
            message_id=replied_msg.message_id,
            reply_markup=listen_markup
        )

        await context.bot.pin_chat_message(
            chat_id=MUSIC_CHANNEL_ID,
            message_id=sent_channel_msg.message_id
        )

        await update.message.reply_text(
            "✅ <b>Message successfully posted and pinned in the channel!</b>",
            parse_mode="HTML"
        )
    except BadRequest as e:
        logger.error(f"Failed to post/pin message: {e}")
        await update.message.reply_text(f"❌ <b>Telegram API Error:</b> {e.message}", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Unexpected error in /pin: {e}")
        await update.message.reply_text(f"❌ <b>Error:</b> {e}", parse_mode="HTML")


# ─────────────────────────────────────────────
# Audio Ingestion (Group + DM Support)
# ─────────────────────────────────────────────
async def handle_audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ingests audio sent either in the recording group OR directly in DM during admin mode."""
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
    elif doc and ((doc.mime_type and "audio" in doc.mime_type) or doc.file_name.lower().endswith(('.mp3', '.m4a', '.flac', '.wav', '.ogg'))):
        file_id = doc.file_id
        file_unique_id = doc.file_unique_id
        title = doc.file_name
    else:
        return

    with get_db() as conn:
        batch = conn.execute("SELECT id FROM batches WHERE status = 'recording'").fetchone()
        if not batch:
            if is_dm_admin:
                await msg.reply_text("⚠️ No active recording session! Send <code>/record &lt;genre&gt;</code> first.", parse_mode="HTML")
            return

        batch_id = batch["id"]
        norm = normalize_key(artist, title)

        existing = conn.execute(
            "SELECT id, artist, title FROM songs WHERE norm_key = ? AND status != 'skipped'",
            (norm,)
        ).fetchone()

        order_row = conn.execute(
            "SELECT MAX(order_index) as max_order FROM songs WHERE batch_id = ?",
            (batch_id,)
        ).fetchone()
        next_order = (order_row["max_order"] or 0) + 1

        if existing:
            cur = conn.execute(
                """INSERT INTO songs (batch_id, file_id, file_unique_id, artist, title, album, duration, norm_key, status, order_index)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'duplicate_pending', ?)""",
                (batch_id, file_id, file_unique_id, artist, title, album, duration, norm, next_order)
            )
            song_id = cur.lastrowid

            buttons = [
                [
                    InlineKeyboardButton("🚫 Skip", callback_data=f"dup_skip_{song_id}", api_kwargs={"style": "danger"}),
                    InlineKeyboardButton("⚠️ Record Anyway", callback_data=f"dup_keep_{song_id}", api_kwargs={"style": "primary"}),
                ]
            ]
            await msg.reply_text(
                f"⚠️ <b>Duplicate Detected</b>\n"
                f"🎵 <b>{artist} - {title}</b> already exists in DB.\n"
                f"Action for track #{next_order}:",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="HTML"
            )
        else:
            conn.execute(
                """INSERT INTO songs (batch_id, file_id, file_unique_id, artist, title, album, duration, norm_key, status, order_index)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
                (batch_id, file_id, file_unique_id, artist, title, album, duration, norm, next_order)
            )
            logger.info(f"✅ Queued track #{next_order}: {artist} - {title}")

async def duplicate_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Admin only.", show_alert=True)
        return

    data = query.data
    action, song_id_str = data.rsplit("_", 1)
    song_id = int(song_id_str)

    with get_db() as conn:
        if action == "dup_skip":
            conn.execute("UPDATE songs SET status = 'skipped' WHERE id = ?", (song_id,))
            await query.answer("Track skipped.")
            await query.edit_message_text("🚫 <b>Track Skipped.</b>", parse_mode="HTML")
        elif action == "dup_keep":
            conn.execute("UPDATE songs SET status = 'queued' WHERE id = ?", (song_id,))
            await query.answer("Track queued for publishing.")
            await query.edit_message_text("✅ <b>Track Kept and Queued.</b>", parse_mode="HTML")


# ─────────────────────────────────────────────
# Automated Channel Publishing Queue
# ─────────────────────────────────────────────
async def broadcast_worker(app: Application, batch_id: int, genre: str, notify_chat_id: int):
    global BROADCAST_ACTIVE
    logger.info(f"Starting broadcast for Batch #{batch_id}")
    published_count = 0

    while BROADCAST_ACTIVE:
        with get_db() as conn:
            song = conn.execute(
                """SELECT * FROM songs 
                   WHERE batch_id = ? AND status = 'queued' 
                   ORDER BY order_index ASC LIMIT 1""",
                (batch_id,)
            ).fetchone()

        if not song:
            logger.info(f"Batch #{batch_id} completed.")
            break

        song_id = song["id"]
        caption = generate_caption(
            artist=song["artist"],
            title=song["title"],
            album=song["album"],
            duration=song["duration"],
            genre=genre
        )

        channel_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    text="➕ Add to Playlist",
                    callback_data=f"pl_add_{song_id}",
                    api_kwargs={"style": "success"}
                )
            ]
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

            with get_db() as conn:
                conn.execute(
                    "UPDATE songs SET status = 'published', channel_message_id = ? WHERE id = ?",
                    (sent_msg.message_id, song_id)
                )
            published_count += 1
            await asyncio.sleep(2.5)

        except Exception as e:
            logger.error(f"Error publishing song #{song_id}: {e}")
            await asyncio.sleep(5)

    with get_db() as conn:
        status_to_set = "completed" if not conn.execute("SELECT 1 FROM songs WHERE batch_id = ? AND status = 'queued'", (batch_id,)).fetchone() else "stopped"
        conn.execute("UPDATE batches SET status = ? WHERE id = ?", (status_to_set, batch_id))

    BROADCAST_ACTIVE = False
    
    await app.bot.send_message(
        chat_id=notify_chat_id,
        text=f"🏁 <b>Broadcast finished for Batch #{batch_id}!</b>\n"
             f"• <b>Published:</b> {published_count} tracks\n"
             f"• <b>Status:</b> <i>{status_to_set}</i>",
        parse_mode="HTML"
    )


# ─────────────────────────────────────────────
# Subscriber Playlist & Membership Gate Handlers
# ─────────────────────────────────────────────
async def add_to_playlist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles 'Add to Playlist' button clicks in the channel.
    If the user has not started the bot, redirects them via deep link.
    """
    query = update.callback_query
    user = query.from_user
    user_id = user.id
    song_id = int(query.data.replace("pl_add_", ""))
    bot_user = await get_bot_username(context.bot)

    # If the user has never started the bot in DM, redirect them with a deep link
    if not is_user_registered(user_id):
        redirect_url = f"https://t.me/{bot_user}?start=save_{song_id}"
        await query.answer(url=redirect_url)
        return

    # User is registered: save immediately and give in-app feedback
    with get_db() as conn:
        already_saved = conn.execute(
            "SELECT 1 FROM user_playlists WHERE user_id = ? AND song_id = ?",
            (user_id, song_id)
        ).fetchone()

        if already_saved:
            await query.answer("Already in your playlist! 🎧", show_alert=False)
            return

        conn.execute(
            "INSERT INTO user_playlists (user_id, song_id) VALUES (?, ?)",
            (user_id, song_id)
        )

    await query.answer("Added to your playlist! ⭐ Check your private chat with the bot.", show_alert=False)

async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'Try Again' button when a user verifies their channel membership."""
    query = update.callback_query
    user = query.from_user
    data = query.data

    pending_song_id = None
    if data.startswith("check_sub_"):
        try:
            pending_song_id = int(data.replace("check_sub_", ""))
        except ValueError:
            pending_song_id = None

    is_sub = await is_user_subscribed(context.bot, user.id)
    if not is_sub:
        await query.answer("⚠️ You haven't joined the channel yet! Please join first.", show_alert=True)
        return

    register_user(user.id, user.username, user.first_name)
    await query.answer("✅ Channel membership verified!")

    if pending_song_id:
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_playlists (user_id, song_id) VALUES (?, ?)",
                (user.id, pending_song_id)
            )
            song = conn.execute("SELECT artist, title FROM songs WHERE id = ?", (pending_song_id,)).fetchone()

        song_info = f"<b>{song['artist']} - {song['title']}</b>" if song else "the selected track"
        await query.edit_message_text(
            f"🎉 <b>Welcome!</b>\n\n"
            f"✅ We saved {song_info} to your personal playlist!\n\n"
            f"Send <code>/playlist</code> anytime to view and stream your tracks.",
            parse_mode="HTML"
        )
    else:
        await query.edit_message_text(
            "👋 <b>Welcome to the Music Hub!</b>\n\n"
            "• Tap <b>Add to Playlist</b> under any track in our channel to save it.\n"
            "• Send <code>/playlist</code> here to view and play your saved songs.",
            parse_mode="HTML"
        )

async def user_playlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays saved songs in user's private chat after verifying subscription."""
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please check your playlist in my private chat! 🎧")
        return

    user = update.effective_user
    register_user(user.id, user.username, user.first_name)

    # Enforce channel subscription check
    if not await is_user_subscribed(context.bot, user.id):
        await send_fsub_gate(update.message)
        return

    page = 1
    await render_playlist_page(update.message, user.id, page)

async def render_playlist_page(message, user_id: int, page: int, edit=False):
    limit = 5
    offset = (page - 1) * limit

    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) as c FROM user_playlists WHERE user_id = ?",
            (user_id,)
        ).fetchone()["c"]

        rows = conn.execute(
            """SELECT s.id, s.artist, s.title, s.duration 
               FROM user_playlists up
               JOIN songs s ON up.song_id = s.id
               WHERE up.user_id = ?
               ORDER BY up.added_at DESC
               LIMIT ? OFFSET ?""",
            (user_id, limit, offset)
        ).fetchall()

    if total == 0:
        text = "🎧 <b>Your playlist is empty!</b>\n\nTap the green <b>Add to Playlist</b> button under any song in our channel to save it here."
        if edit:
            await message.edit_text(text, parse_mode="HTML")
        else:
            await message.reply_text(text, parse_mode="HTML")
        return

    total_pages = (total + limit - 1) // limit
    text = f"🎧 <b>Your Saved Playlist</b> (Page {page}/{total_pages})\n\n"

    keyboard = []
    for row in rows:
        artist = row["artist"] or "Unknown"
        title = row["title"] or "Track"
        text += f"• 🎵 <b>{artist}</b> - {title} <i>({format_duration(row['duration'])})</i>\n"
        keyboard.append([
            InlineKeyboardButton(f"▶️ Play {title[:20]}", callback_data=f"pl_play_{row['id']}")
        ])

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"pl_page_{page-1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"pl_page_{page+1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    reply_markup = InlineKeyboardMarkup(keyboard)

    if edit:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")

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
        with get_db() as conn:
            song = conn.execute("SELECT file_id, artist, title FROM songs WHERE id = ?", (song_id,)).fetchone()

        if song:
            await query.answer("Delivering audio...")
            await context.bot.send_audio(
                chat_id=user_id,
                audio=song["file_id"],
                title=song["title"],
                performer=song["artist"]
            )
        else:
            await query.answer("Audio not found.", show_alert=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start, deep-link playlist additions, and enforces channel subscription."""
    user = update.effective_user
    args = context.args

    # Check for deep-linked song: /start save_<song_id>
    pending_song_id = None
    if args and args[0].startswith("save_"):
        try:
            pending_song_id = int(args[0].replace("save_", ""))
        except ValueError:
            pending_song_id = None

    # Admin bypass
    if user.id == ADMIN_ID:
        register_user(user.id, user.username, user.first_name)
        await update.message.reply_text(
            "👋 <b>Welcome Admin!</b>\n\n"
            "Send <code>/adminmode</code> to manage music recording & broadcasts,\n"
            "<code>/pin</code> (in reply to a message) to post & pin to the channel,\n"
            "or <code>/playlist</code> to view your saved music.",
            parse_mode="HTML"
        )
        return

    # Check if subscriber is in the channel
    subscribed = await is_user_subscribed(context.bot, user.id)
    if not subscribed:
        await send_fsub_gate(update.message, pending_song_id=pending_song_id)
        return

    # User is confirmed subscribed
    register_user(user.id, user.username, user.first_name)

    if pending_song_id:
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_playlists (user_id, song_id) VALUES (?, ?)",
                (user.id, pending_song_id)
            )
            song = conn.execute("SELECT artist, title FROM songs WHERE id = ?", (pending_song_id,)).fetchone()

        song_info = f"<b>{song['artist']} - {song['title']}</b>" if song else "the selected track"
        await update.message.reply_text(
            f"🎉 <b>Welcome to the Music Hub!</b>\n\n"
            f"✅ We added {song_info} to your personal playlist.\n"
            f"Send <code>/playlist</code> here anytime to view and play your songs.",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            "👋 <b>Welcome to the Music Hub!</b>\n\n"
            "• Tap <b>Add to Playlist</b> under any track in our channel to save it.\n"
            "• Send <code>/playlist</code> here to view and play your saved songs.",
            parse_mode="HTML"
        )


# ─────────────────────────────────────────────
# Main Application Runtime
# ─────────────────────────────────────────────
async def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_TOKEN is missing! Set it in your environment variables.")
        return

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Admin Mode Handlers
    app.add_handler(CommandHandler("adminmode", adminmode_command))
    app.add_handler(CommandHandler("adminmodeoff", adminmodeoff_command))
    app.add_handler(CommandHandler("record", record_command))
    app.add_handler(CommandHandler("over", over_command))
    app.add_handler(CommandHandler("stopbroadcast", stopbroadcast_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("pin", pin_command))

    # User Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("playlist", user_playlist_command))

    # Callbacks
    app.add_handler(CallbackQueryHandler(duplicate_callback_handler, pattern=r"^dup_"))
    app.add_handler(CallbackQueryHandler(add_to_playlist_callback, pattern=r"^pl_add_"))
    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern=r"^check_sub"))
    app.add_handler(CallbackQueryHandler(playlist_page_callback, pattern=r"^pl_(page|play)_"))

    # Audio message listener (handles native Audio & audio Files/Documents)
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.AUDIO | filters.Document.FileExtension("mp3"), handle_audio_message))

    await app.initialize()

    # Web Server for Keep-Alive
    web_app = web.Application()
    web_app.router.add_get("/", web_index)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🚀 Web Server started on port {PORT}")

    # Start Telegram Polling
    await app.start()
    await app.updater.start_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES
    )
    logger.info("🤖 Music Management Bot is actively polling!")

    # Start Keep-Alive Pinger
    pinger_task = asyncio.create_task(keep_alive_pinger())

    # Graceful exit handling
    stop_signal = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_signal.set)

    await stop_signal.wait()

    # Cleanup
    logger.info("Shutting down bot...")
    pinger_task.cancel()
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
