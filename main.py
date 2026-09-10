import os
import re
import signal
import logging
import asyncio
import html
from datetime import datetime
from aiohttp import web
import aiohttp
from supabase import create_async_client, AsyncClient
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
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://zrwbwtzduhgmtszzleot.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

def format_tg_id(raw_id: str | int) -> int:
    val = int(raw_id)
    if val < 0 and not str(val).startswith("-100"):
        return int(f"-100{abs(val)}")
    return val

RECORDING_GROUP_ID = format_tg_id(os.environ.get("RECORDING_GROUP_ID", "-5309919588"))
MUSIC_CHANNEL_ID = format_tg_id(os.environ.get("MUSIC_CHANNEL_ID", "-4448938519"))

CHANNEL_USERNAME = "Yourveryownplaylist"
CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME}"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Global Runtime State
BROADCAST_ACTIVE = False
ADMIN_MODE_USERS = set()
BOT_USERNAME = None
db: AsyncClient = None  # Supabase async client instance


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
        [InlineKeyboardButton("🔄 I've Joined / Try Again", callback_data=retry_data)]
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
    try:
        await db.table("users").upsert({
            "user_id": user_id,
            "username": username,
            "first_name": first_name
        }).execute()
    except Exception as e:
        logger.error(f"Error registering user {user_id}: {e}")

async def is_user_registered(user_id: int) -> bool:
    try:
        res = await db.table("users").select("user_id").eq("user_id", user_id).limit(1).execute()
        return len(res.data) > 0
    except Exception:
        return False


# ─────────────────────────────────────────────
# Channel Sync Engine (Recovers 200+ Songs)
# ─────────────────────────────────────────────
async def scanchannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Scans a range of channel message IDs by forwarding each to the admin,
    extracting track metadata/file_id to Supabase, and deleting the forwarded post.
    Usage: /scanchannel <start_id> <end_id>
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "⚠️ <b>Usage:</b> <code>/scanchannel &lt;start_id&gt; &lt;end_id&gt;</code>\n"
            "<i>Example: /scanchannel 1 350</i>\n\n"
            "💡 Tip: Copy the message link of your oldest and newest channel post to find the IDs.",
            parse_mode="HTML"
        )
        return

    try:
        start_id = int(args[0])
        end_id = int(args[1])
    except ValueError:
        await update.message.reply_text("⚠️ Start and end IDs must be valid numbers.", parse_mode="HTML")
        return

    if start_id > end_id:
        start_id, end_id = end_id, start_id

    progress_msg = await update.message.reply_text(
        f"🔍 <b>Starting Channel Sync...</b>\n"
        f"Scanning messages #{start_id} to #{end_id}\n"
        f"<i>Please wait, this will populate Supabase without spamming the chat...</i>",
        parse_mode="HTML"
    )

    imported = 0
    skipped = 0
    errors = 0

    for msg_id in range(start_id, end_id + 1):
        try:
            # Forward the message into admin DM to inspect audio metadata
            fwd = await context.bot.forward_message(
                chat_id=update.effective_chat.id,
                from_chat_id=MUSIC_CHANNEL_ID,
                message_id=msg_id
            )

            # Check if it has an audio or music document
            audio = fwd.audio
            doc = fwd.document
            file_id = None
            file_unique_id = None
            artist = "Unknown Artist"
            title = "Unknown Track"
            duration = 0
            album = None

            if audio:
                file_id = audio.file_id
                file_unique_id = audio.file_unique_id
                artist = audio.performer or "Unknown Artist"
                title = audio.title or audio.file_name or "Track"
                duration = audio.duration
            elif doc and ((doc.mime_type and "audio" in doc.mime_type) or (doc.file_name and doc.file_name.lower().endswith(('.mp3', '.m4a', '.flac')))):
                file_id = doc.file_id
                file_unique_id = doc.file_unique_id
                title = doc.file_name or "Track"

            # Always clean up the forwarded message
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=fwd.message_id)

            if file_id and file_unique_id:
                norm = normalize_key(artist, title)

                # Check if already present
                dup_check = await db.table("songs").select("id").eq("file_unique_id", file_unique_id).limit(1).execute()
                if not dup_check.data:
                    await db.table("songs").insert({
                        "file_id": file_id,
                        "file_unique_id": file_unique_id,
                        "artist": artist,
                        "title": title,
                        "album": album,
                        "duration": duration,
                        "norm_key": norm,
                        "status": "published",
                        "channel_message_id": msg_id
                    }).execute()
                    imported += 1
                else:
                    skipped += 1
            else:
                skipped += 1

        except BadRequest:
            # Message was deleted or not found
            errors += 1
        except Exception as e:
            logger.error(f"Error scanning message {msg_id}: {e}")
            errors += 1

        # Periodic progress update every 25 messages
        if (msg_id - start_id) % 25 == 0:
            try:
                await progress_msg.edit_text(
                    f"🔍 <b>Scanning in Progress...</b>\n"
                    f"• Processing: {msg_id}/{end_id}\n"
                    f"• Tracks Indexed: {imported}\n"
                    f"• Non-audio/Duplicates: {skipped}",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        await asyncio.sleep(0.35)  # Respect Telegram API limits

    await progress_msg.edit_text(
        f"✅ <b>Channel Sync Completed!</b>\n\n"
        f"• <b>New Tracks Indexed:</b> {imported}\n"
        f"• <b>Skipped / Duplicates:</b> {skipped}\n"
        f"• <b>Non-existent IDs:</b> {errors}\n\n"
        f"All indexed tracks are now available for subscribers in <code>/playlist</code>!",
        parse_mode="HTML"
    )


# ─────────────────────────────────────────────
# Admin Mode & Session Handlers
# ─────────────────────────────────────────────
async def adminmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        return

    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ Send <code>/adminmode</code> in my private DM.", parse_mode="HTML")
        return

    ADMIN_MODE_USERS.add(user.id)
    await update.message.reply_text(
        "🛠️ <b>Admin Mode: Activated (Supabase Edition)</b>\n\n"
        "• <code>/record &lt;genre&gt;</code> - Open recording session\n"
        "• <b>Forward Songs Here</b> - Send tracks directly to DM or group\n"
        "• <code>/skipall</code> - Skip all pending duplicate tracks\n"
        "• <code>/scanchannel &lt;start&gt; &lt;end&gt;</code> - Recover/sync existing channel songs\n"
        "• <code>/over</code> - Close session & start automatic publishing\n"
        "• <code>/stopbroadcast</code> - Pause broadcast\n"
        "• <code>/pin</code> - Reply to any message to post & pin it with 'Go listen'\n"
        "• <code>/status</code> - View Supabase statistics\n"
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
        await update.message.reply_text("⚠️ Specify a genre. Example:\n<code>/record rnb</code>", parse_mode="HTML")
        return

    genre = " ".join(args).replace('"', '').strip()

    # Check for ongoing recording batch
    active = await db.table("batches").select("id, genre").eq("status", "recording").limit(1).execute()
    if active.data:
        cur = active.data[0]
        await update.message.reply_text(
            f"⚠️ Batch #{cur['id']} (<b>{cur['genre']}</b>) is currently recording.\n"
            f"Send <code>/over</code> to finish it first.",
            parse_mode="HTML"
        )
        return

    res = await db.table("batches").insert({"genre": genre, "status": "recording"}).execute()
    batch_id = res.data[0]["id"]

    await update.message.reply_text(
        f"🎙️ <b>Recording Session #{batch_id} Started!</b>\n"
        f"• <b>Genre:</b> #{make_hashtag(genre)[1:]}\n\n"
        f"👉 Forward songs directly here or in the group.\n"
        f"👉 Send <code>/skipall</code> to discard duplicates.\n"
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
        await update.message.reply_text("ℹ️ No active recording session. Start with <code>/record &lt;genre&gt;</code>.", parse_mode="HTML")
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
    asyncio.create_task(broadcast_worker(context.application, batch_id, genre, notify_chat_id=update.effective_chat.id))

async def stopbroadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BROADCAST_ACTIVE
    if update.effective_user.id != ADMIN_ID:
        return

    if not BROADCAST_ACTIVE:
        await update.message.reply_text("ℹ️ No broadcast running.")
        return

    BROADCAST_ACTIVE = False
    await update.message.reply_text("🛑 <b>Broadcast stopping...</b> Remaining tracks stay queued.", parse_mode="HTML")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    total_songs = (await db.table("songs").select("id", count="exact").execute()).count or 0
    published_songs = (await db.table("songs").select("id", count="exact").eq("status", "published").execute()).count or 0
    total_users = (await db.table("users").select("user_id", count="exact").execute()).count or 0
    active_rec = await db.table("batches").select("*").eq("status", "recording").limit(1).execute()

    is_adm_mode = "🟢 Active" if update.effective_user.id in ADMIN_MODE_USERS else "⚪ Inactive"

    status_msg = (
        f"⚙️ <b>Bot System Status (Supabase)</b>\n\n"
        f"• <b>Admin Mode:</b> {is_adm_mode}\n"
        f"• <b>Active Broadcast:</b> {'🟢 Running' if BROADCAST_ACTIVE else '⚪ Idle'}\n"
        f"• <b>Music Channel:</b> <code>{MUSIC_CHANNEL_ID}</code> (@{CHANNEL_USERNAME})\n"
        f"• <b>Total Songs in DB:</b> {total_songs}\n"
        f"• <b>Published Songs:</b> {published_songs}\n"
        f"• <b>Registered Users:</b> {total_users}\n\n"
    )

    if active_rec.data:
        status_msg += f"🎙️ <b>Active Batch:</b> #{active_rec.data[0]['id']} (Genre: {active_rec.data[0]['genre']})"
    else:
        status_msg += "🎙️ <b>Active Batch:</b> None (Use /record <genre>)"

    await update.message.reply_text(status_msg, parse_mode="HTML")

async def pin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    replied = update.message.reply_to_message
    if not replied:
        await update.message.reply_text("⚠️ Reply to the message you want to post & pin in the channel with <code>/pin</code>.", parse_mode="HTML")
        return

    bot_user = await get_bot_username(context.bot)
    listen_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎧 Go listen", url=f"https://t.me/{bot_user}")]
    ])

    try:
        sent_msg = await context.bot.copy_message(
            chat_id=MUSIC_CHANNEL_ID,
            from_chat_id=update.effective_chat.id,
            message_id=replied.message_id,
            reply_markup=listen_markup
        )
        await context.bot.pin_chat_message(chat_id=MUSIC_CHANNEL_ID, message_id=sent_msg.message_id)
        await update.message.reply_text("✅ Message successfully posted and pinned in channel!", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error in /pin: {e}")
        await update.message.reply_text(f"❌ Error: {e}", parse_mode="HTML")


# ─────────────────────────────────────────────
# Audio Ingestion
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

    # Check for duplicates by normalized key or file unique ID
    existing = await db.table("songs").select("id, artist, title").or_(f"norm_key.eq.{norm},file_unique_id.eq.{file_unique_id}").neq("status", "skipped").limit(1).execute()

    # Get max order_index
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
                InlineKeyboardButton("🚫 Skip", callback_data=f"dup_skip_{song_id}"),
                InlineKeyboardButton("⚠️ Record Anyway", callback_data=f"dup_keep_{song_id}"),
            ]
        ]
        await msg.reply_text(
            f"⚠️ <b>Duplicate Detected</b>\n"
            f"🎵 <b>{artist} - {title}</b> already exists in DB.\n"
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

    while BROADCAST_ACTIVE:
        res = await db.table("songs").select("*").eq("batch_id", batch_id).eq("status", "queued").order("order_index").limit(1).execute()
        if not res.data:
            break

        song = res.data[0]
        song_id = song["id"]
        caption = generate_caption(artist=song["artist"], album=song["album"], genre=genre)

        channel_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add to Playlist", callback_data=f"pl_add_{song_id}")]
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

            await db.table("songs").update({
                "status": "published",
                "channel_message_id": sent_msg.message_id
            }).eq("id", song_id).execute()

            published_count += 1
            await asyncio.sleep(2.5)

        except Exception as e:
            logger.error(f"Error publishing song #{song_id}: {e}")
            await asyncio.sleep(5)

    remaining = await db.table("songs").select("id").eq("batch_id", batch_id).eq("status", "queued").limit(1).execute()
    final_status = "stopped" if remaining.data else "completed"
    await db.table("batches").update({"status": final_status}).eq("id", batch_id).execute()

    BROADCAST_ACTIVE = False
    await app.bot.send_message(
        chat_id=notify_chat_id,
        text=f"🏁 <b>Broadcast finished for Batch #{batch_id}!</b>\n"
             f"• <b>Published:</b> {published_count} tracks\n"
             f"• <b>Status:</b> <i>{final_status}</i>",
        parse_mode="HTML"
    )


# ─────────────────────────────────────────────
# Subscriber Playlist Handlers
# ─────────────────────────────────────────────
async def add_to_playlist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    song_id = int(query.data.replace("pl_add_", ""))
    bot_user = await get_bot_username(context.bot)

    if not await is_user_registered(user.id):
        await query.answer(url=f"https://t.me/{bot_user}?start=save_{song_id}")
        return

    # Check if already added
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
        info = f"<b>{song_data.data[0]['artist']} - {song_data.data[0]['title']}</b>" if song_data.data else "the track"
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

async def render_playlist_page(message, user_id: int, page: int, edit=False):
    limit = 5
    offset = (page - 1) * limit

    # Count total
    count_res = await db.table("user_playlists").select("song_id", count="exact").eq("user_id", user_id).execute()
    total = count_res.count or 0

    if total == 0:
        text = "🎧 <b>Your playlist is empty!</b>\n\nTap <b>Add to Playlist</b> under any song in our channel to save it."
        if edit:
            await message.edit_text(text, reply_markup=None, parse_mode="HTML")
        else:
            await message.reply_text(text, reply_markup=None, parse_mode="HTML")
        return

    # Fetch tracks with joined metadata
    res = await db.table("user_playlists").select(
        "song_id, songs(id, artist, title, duration)"
    ).eq("user_id", user_id).order("added_at", desc=True).range(offset, offset + limit - 1).execute()

    total_pages = (total + limit - 1) // limit
    text = f"🎧 <b>Your Saved Playlist</b> (Page {page}/{total_pages})\n\n"
    keyboard = []

    for item in res.data:
        s = item["songs"]
        if not s:
            continue
        artist = s["artist"] or "Unknown"
        title = s["title"] or "Track"
        text += f"• 🎵 <b>{artist}</b> - {title} <i>({format_duration(s['duration'])})</i>\n"
        keyboard.append([
            InlineKeyboardButton(f"▶️ Play {title[:16]}", callback_data=f"pl_play_{s['id']}"),
            InlineKeyboardButton("❌ Remove", callback_data=f"pl_rem_{s['id']}_{page}")
        ])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"pl_page_{page-1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"pl_page_{page+1}"))
    if nav:
        keyboard.append(nav)

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
        limit = 5
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
            "• <code>/scanchannel &lt;start&gt; &lt;end&gt;</code> - Sync 200+ channel songs into Supabase\n"
            "• <code>/playlist</code> - View personal playlist",
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
        info = f"<b>{song.data[0]['artist']} - {song.data[0]['title']}</b>" if song.data else "the song"
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
    await asyncio.sleep(15)
    target_url = APP_URL or f"http://127.0.0.1:{PORT}/"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(target_url, timeout=10) as resp:
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
        return
    if not SUPABASE_KEY:
        logger.error("SUPABASE_KEY is missing! Please set your service_role key.")
        return

    # Initialize Supabase client
    db = await create_async_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("✅ Connected to Supabase Cloud Database!")

    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("adminmode", adminmode_command))
    app.add_handler(CommandHandler("adminmodeoff", adminmodeoff_command))
    app.add_handler(CommandHandler("record", record_command))
    app.add_handler(CommandHandler("skipall", skipall_command))
    app.add_handler(CommandHandler("scanchannel", scanchannel_command))
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

    # Lightweight Web server for health check pingers
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

    stop_signal = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_signal.set)

    await stop_signal.wait()

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
