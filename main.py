import os
import re
import signal
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timezone

import aiosqlite
from aiohttp import web
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", "7429996344"))
PORT = int(os.environ.get("PORT", "8080"))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

DATABASE_PATH = DATA_DIR / "bot.db"
PROCESSED_SONGS_FILE = DATA_DIR / "processed_songs.txt"

DATA_DIR.mkdir(exist_ok=True)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# WEB PAGE
# ============================================================

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Music Bot</title>

    <style>
        body {
            background: #0f172a;
            color: #4ade80;
            font-family: Arial, sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
        }

        .container {
            text-align: center;
        }

        h1 {
            margin-bottom: 10px;
        }

        p {
            color: #94a3b8;
        }
    </style>
</head>

<body>
    <div class="container">
        <h1>🤖 Music Bot is online</h1>
        <p>Telegram service is running.</p>
    </div>
</body>
</html>
"""


async def web_index(request):
    return web.Response(
        text=HTML_PAGE,
        content_type="text/html",
    )


async def web_health(request):
    return web.json_response({
        "status": "ok",
        "service": "music-bot",
    })


# ============================================================
# DATABASE
# ============================================================

async def init_database():

    async with aiosqlite.connect(DATABASE_PATH) as db:

        # ----------------------------------------------------
        # Settings
        # ----------------------------------------------------

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # ----------------------------------------------------
        # Recording batches
        # ----------------------------------------------------

        await db.execute("""
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                genre TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'recording',
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
        """)

        # ----------------------------------------------------
        # Songs
        # ----------------------------------------------------

        await db.execute("""
            CREATE TABLE IF NOT EXISTS songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                title TEXT NOT NULL,
                artist TEXT,
                album TEXT,
                genre TEXT NOT NULL,

                normalized_key TEXT NOT NULL,

                telegram_file_id TEXT,
                telegram_file_unique_id TEXT,

                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,

                duration INTEGER,

                status TEXT NOT NULL DEFAULT 'recorded',

                created_at TEXT NOT NULL
            )
        """)

        # ----------------------------------------------------
        # Songs belonging to batches
        # ----------------------------------------------------

        await db.execute("""
            CREATE TABLE IF NOT EXISTS batch_songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                batch_id INTEGER NOT NULL,
                song_id INTEGER NOT NULL,

                position INTEGER NOT NULL,

                FOREIGN KEY(batch_id)
                    REFERENCES batches(id),

                FOREIGN KEY(song_id)
                    REFERENCES songs(id)
            )
        """)

        # ----------------------------------------------------
        # Playlist
        # We'll use this later.
        # ----------------------------------------------------

        await db.execute("""
            CREATE TABLE IF NOT EXISTS playlist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                telegram_user_id INTEGER NOT NULL,
                song_id INTEGER NOT NULL,

                added_at TEXT NOT NULL,

                UNIQUE(
                    telegram_user_id,
                    song_id
                ),

                FOREIGN KEY(song_id)
                    REFERENCES songs(id)
            )
        """)

        # ----------------------------------------------------
        # Pending duplicate decisions
        # ----------------------------------------------------

        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_duplicates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                batch_id INTEGER NOT NULL,
                song_id INTEGER NOT NULL,

                created_at TEXT NOT NULL,

                FOREIGN KEY(batch_id)
                    REFERENCES batches(id),

                FOREIGN KEY(song_id)
                    REFERENCES songs(id)
            )
        """)

        await db.commit()

    logger.info("Database initialized.")


# ============================================================
# DATABASE HELPERS
# ============================================================

async def get_setting(key: str):

    async with aiosqlite.connect(DATABASE_PATH) as db:

        cursor = await db.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        )

        row = await cursor.fetchone()

        return row[0] if row else None


async def set_setting(key: str, value: str):

    async with aiosqlite.connect(DATABASE_PATH) as db:

        await db.execute(
            """
            INSERT INTO settings(key, value)
            VALUES (?, ?)

            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

        await db.commit()


async def get_active_batch():

    async with aiosqlite.connect(DATABASE_PATH) as db:

        cursor = await db.execute(
            """
            SELECT id, genre, created_at
            FROM batches
            WHERE status = 'recording'
            ORDER BY id DESC
            LIMIT 1
            """
        )

        return await cursor.fetchone()


async def get_batch_count(batch_id: int):

    async with aiosqlite.connect(DATABASE_PATH) as db:

        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM batch_songs
            WHERE batch_id = ?
            """,
            (batch_id,),
        )

        row = await cursor.fetchone()

        return row[0]


async def get_next_position(batch_id: int):

    async with aiosqlite.connect(DATABASE_PATH) as db:

        cursor = await db.execute(
            """
            SELECT COALESCE(MAX(position), 0)
            FROM batch_songs
            WHERE batch_id = ?
            """,
            (batch_id,),
        )

        row = await cursor.fetchone()

        return row[0] + 1


# ============================================================
# HELPERS
# ============================================================

def is_owner(update: Update) -> bool:

    user = update.effective_user

    return bool(
        user and user.id == OWNER_ID
    )


async def require_owner(update: Update) -> bool:

    if is_owner(update):
        return True

    if update.effective_message:

        await update.effective_message.reply_text(
            "❌ You are not authorized to use this command."
        )

    return False


def now_iso():

    return datetime.now(
        timezone.utc
    ).isoformat()


def clean_text(value):

    if not value:
        return ""

    value = value.strip()

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value


def normalize_song_part(value):

    if not value:
        return ""

    value = value.lower()

    value = value.replace(
        "—",
        "-"
    )

    value = value.replace(
        "–",
        "-"
    )

    # Remove punctuation.
    value = re.sub(
        r"[^\w\s]",
        " ",
        value,
        flags=re.UNICODE,
    )

    # Collapse whitespace.
    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def make_duplicate_key(
    artist: str,
    title: str,
):

    artist_part = normalize_song_part(
        artist
    )

    title_part = normalize_song_part(
        title
    )

    return f"{artist_part}|{title_part}"


def display_song(
    artist: str,
    title: str,
):

    artist = clean_text(artist)
    title = clean_text(title)

    if artist and title:
        return f"{artist} — {title}"

    if title:
        return title

    return "Unknown song"


def extract_genre(command_args):

    if not command_args:
        return None

    genre = " ".join(command_args).strip()

    # Remove one surrounding pair of quotes.
    if len(genre) >= 2:

        if (
            genre.startswith('"')
            and genre.endswith('"')
        ):
            genre = genre[1:-1]

        elif (
            genre.startswith("'")
            and genre.endswith("'")
        ):
            genre = genre[1:-1]

    genre = clean_text(genre)

    return genre if genre else None


# ============================================================
# PROCESSED SONGS FILE
# ============================================================

def append_processed_song(
    artist,
    title,
    album,
    genre,
):

    line = (
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        f" | {genre}"
        f" | {artist or 'Unknown Artist'}"
        f" | {title or 'Unknown Title'}"
        f" | {album or 'Unknown Album'}\n"
    )

    with open(
        PROCESSED_SONGS_FILE,
        "a",
        encoding="utf-8",
    ) as file:

        file.write(line)


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.effective_message.reply_text(
        "🎵 <b>Music Bot</b>\n\n"
        "Bot is online.\n\n"

        "<b>Configuration</b>\n"
        "/setgroupid\n"
        "/setchannelid -100xxxxxxxxxx\n"
        "/status\n\n"

        "<b>Recording</b>\n"
        '/record "genre"\n'
        "/over\n\n"

        "Example:\n"
        '/record "rnb"',
        parse_mode="HTML",
    )


# ============================================================
# /SETGROUPID
# ============================================================

async def set_group_id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_owner(update):
        return

    message = update.effective_message
    chat = update.effective_chat

    # --------------------------------------------------------
    # Explicit ID
    # --------------------------------------------------------

    if context.args:

        group_id = context.args[0].strip()

        try:
            int(group_id)

        except ValueError:

            await message.reply_text(
                "❌ Invalid group ID.\n\n"
                "Example:\n"
                "<code>/setgroupid -100123456789</code>",
                parse_mode="HTML",
            )

            return

        await set_setting(
            "group_id",
            group_id,
        )

        await message.reply_text(
            "✅ Recording group updated.\n\n"
            f"Group ID: <code>{group_id}</code>",
            parse_mode="HTML",
        )

        return

    # --------------------------------------------------------
    # Use current group
    # --------------------------------------------------------

    if chat.type not in (
        "group",
        "supergroup",
    ):

        await message.reply_text(
            "❌ Use this command inside the recording group.\n\n"
            "Or provide the ID manually:\n"
            "<code>/setgroupid -100123456789</code>",
            parse_mode="HTML",
        )

        return

    group_id = str(chat.id)

    await set_setting(
        "group_id",
        group_id,
    )

    await message.reply_text(
        "✅ This group is now the recording group.\n\n"
        f"Group ID: <code>{group_id}</code>",
        parse_mode="HTML",
    )


# ============================================================
# /SETCHANNELID
# ============================================================

async def set_channel_id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_owner(update):
        return

    message = update.effective_message

    if not context.args:

        await message.reply_text(
            "❌ Please provide the channel ID.\n\n"
            "Example:\n"
            "<code>/setchannelid -100123456789</code>",
            parse_mode="HTML",
        )

        return

    channel_id = context.args[0].strip()

    try:
        int(channel_id)

    except ValueError:

        await message.reply_text(
            "❌ Invalid channel ID.",
        )

        return

    await set_setting(
        "channel_id",
        channel_id,
    )

    await message.reply_text(
        "✅ Music channel updated.\n\n"
        f"Channel ID: <code>{channel_id}</code>",
        parse_mode="HTML",
    )


# ============================================================
# /STATUS
# ============================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_owner(update):
        return

    group_id = await get_setting(
        "group_id"
    )

    channel_id = await get_setting(
        "channel_id"
    )

    active_batch = await get_active_batch()

    if active_batch:

        batch_id = active_batch[0]
        genre = active_batch[1]

        song_count = await get_batch_count(
            batch_id
        )

        recording_status = (
            f"🟢 Recording\n"
            f"Batch: #{batch_id}\n"
            f"Genre: {genre}\n"
            f"Songs: {song_count}"
        )

    else:

        recording_status = "⚪ Not recording"

    await update.effective_message.reply_text(
        "⚙️ <b>Bot Status</b>\n\n"

        f"📥 <b>Recording Group</b>\n"
        f"<code>{group_id or 'Not configured'}</code>\n\n"

        f"📢 <b>Music Channel</b>\n"
        f"<code>{channel_id or 'Not configured'}</code>\n\n"

        f"🎙️ <b>Recorder</b>\n"
        f"{recording_status}",
        parse_mode="HTML",
    )


# ============================================================
# /RECORD
# ============================================================

async def record_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_owner(update):
        return

    message = update.effective_message

    genre = extract_genre(
        context.args
    )

    if not genre:

        await message.reply_text(
            '❌ Please specify a genre.\n\n'
            'Example:\n'
            '/record "rnb"',
        )

        return

    group_id = await get_setting(
        "group_id"
    )

    if not group_id:

        await message.reply_text(
            "❌ No recording group has been configured.\n\n"
            "Use /setgroupid first.",
        )

        return

    active_batch = await get_active_batch()

    if active_batch:

        await message.reply_text(
            "⚠️ A recording session is already active.\n\n"
            f"Batch: #{active_batch[0]}\n"
            f"Genre: {active_batch[1]}\n\n"
            "Use /over before starting another one.",
        )

        return

    created_at = now_iso()

    async with aiosqlite.connect(
        DATABASE_PATH
    ) as db:

        cursor = await db.execute(
            """
            INSERT INTO batches(
                genre,
                status,
                created_at
            )
            VALUES (?, 'recording', ?)
            """,
            (
                genre,
                created_at,
            ),
        )

        batch_id = cursor.lastrowid

        await db.commit()

    await message.reply_text(
        "🔴 <b>Recording started</b>\n\n"
        f"Genre: <b>{genre}</b>\n"
        f"Batch: <code>#{batch_id}</code>\n\n"

        "Send/forward the songs into the recording group.\n"
        "Captions will be ignored.\n\n"

        "When you're finished:\n"
        "/over",
        parse_mode="HTML",
    )

    logger.info(
        "Recording started: batch=%s genre=%s",
        batch_id,
        genre,
    )


# ============================================================
# /OVER
# ============================================================

async def over_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await require_owner(update):
        return

    message = update.effective_message

    active_batch = await get_active_batch()

    if not active_batch:

        await message.reply_text(
            "⚪ There is no active recording session.",
        )

        return

    batch_id = active_batch[0]
    genre = active_batch[1]

    song_count = await get_batch_count(
        batch_id
    )

    completed_at = now_iso()

    async with aiosqlite.connect(
        DATABASE_PATH
    ) as db:

        await db.execute(
            """
            UPDATE batches
            SET
                status = 'queued',
                completed_at = ?
            WHERE id = ?
            """,
            (
                completed_at,
                batch_id,
            ),
        )

        await db.commit()

    await message.reply_text(
        "✅ <b>Recording complete</b>\n\n"

        f"Batch: <code>#{batch_id}</code>\n"
        f"Genre: <b>{genre}</b>\n"
        f"Songs recorded: <b>{song_count}</b>\n\n"

        "The batch is now queued for publishing.",
        parse_mode="HTML",
    )

    logger.info(
        "Recording finished: batch=%s songs=%s",
        batch_id,
        song_count,
    )


# ============================================================
# AUDIO MESSAGE HANDLER
# ============================================================

async def audio_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.effective_message

    if not message:
        return

    # --------------------------------------------------------
    # Only process messages from configured group.
    # --------------------------------------------------------

    group_id = await get_setting(
        "group_id"
    )

    if not group_id:
        return

    try:
        configured_group_id = int(
            group_id
        )

    except ValueError:
        return

    if message.chat_id != configured_group_id:
        return

    # --------------------------------------------------------
    # Check whether recording is active.
    # --------------------------------------------------------

    active_batch = await get_active_batch()

    if not active_batch:
        return

    batch_id = active_batch[0]
    genre = active_batch[1]

    # --------------------------------------------------------
    # Make sure this is actually audio.
    # --------------------------------------------------------

    audio = message.audio

    if not audio:
        return

    # --------------------------------------------------------
    # Extract metadata.
    # --------------------------------------------------------

    title = clean_text(
        audio.title
    )

    artist = clean_text(
        audio.performer
    )

    album = clean_text(
        audio.album
    )

    duration = audio.duration

    file_id = audio.file_id
    file_unique_id = audio.file_unique_id

    # Some Telegram audio messages may have no title.
    if not title:

        if audio.file_name:
            title = clean_text(
                Path(audio.file_name).stem
            )

        else:
            title = "Unknown Title"

    duplicate_key = make_duplicate_key(
        artist,
        title,
    )

    # --------------------------------------------------------
    # Check for duplicate.
    # --------------------------------------------------------

    async with aiosqlite.connect(
        DATABASE_PATH
    ) as db:

        cursor = await db.execute(
            """
            SELECT
                id,
                artist,
                title,
                album,
                genre,
                created_at
            FROM songs
            WHERE normalized_key = ?
            AND status IN ('recorded', 'duplicate_recorded')
            ORDER BY id ASC
            LIMIT 1
            """,
            (
                duplicate_key,
            ),
        )

        existing_song = await cursor.fetchone()

    # --------------------------------------------------------
    # Duplicate detected.
    # --------------------------------------------------------

    if existing_song:

        async with aiosqlite.connect(
            DATABASE_PATH
        ) as db:

            cursor = await db.execute(
                """
                INSERT INTO songs(
                    title,
                    artist,
                    album,
                    genre,
                    normalized_key,
                    telegram_file_id,
                    telegram_file_unique_id,
                    source_chat_id,
                    source_message_id,
                    duration,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    artist or None,
                    album or None,
                    genre,
                    duplicate_key,
                    file_id,
                    file_unique_id,
                    message.chat_id,
                    message.message_id,
                    duration,
                    "duplicate_pending",
                    now_iso(),
                ),
            )

            pending_song_id = cursor.lastrowid

            await db.execute(
                """
                INSERT INTO pending_duplicates(
                    batch_id,
                    song_id,
                    created_at
                )
                VALUES (?, ?, ?)
                """,
                (
                    batch_id,
                    pending_song_id,
                    now_iso(),
                ),
            )

            await db.commit()

        existing_display = display_song(
            existing_song[1],
            existing_song[2],
        )

        current_display = display_song(
            artist,
            title,
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    text="⏭️ Skip",
                    callback_data=(
                        f"dup_skip:{pending_song_id}"
                    ),
                ),
                InlineKeyboardButton(
                    text="✅ Record Anyway",
                    callback_data=(
                        f"dup_record:{pending_song_id}"
                    ),
                ),
            ]
        ]

        await message.reply_text(
            "⚠️ <b>Duplicate detected</b>\n\n"

            f"Current:\n"
            f"🎵 <b>{current_display}</b>\n\n"

            f"Already recorded:\n"
            f"♻️ <b>{existing_display}</b>\n\n"

            "What would you like to do?",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            ),
            parse_mode="HTML",
        )

        return

    # --------------------------------------------------------
    # New song.
    # --------------------------------------------------------

    position = await get_next_position(
        batch_id
    )

    async with aiosqlite.connect(
        DATABASE_PATH
    ) as db:

        cursor = await db.execute(
            """
            INSERT INTO songs(
                title,
                artist,
                album,
                genre,
                normalized_key,
                telegram_file_id,
                telegram_file_unique_id,
                source_chat_id,
                source_message_id,
                duration,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                artist or None,
                album or None,
                genre,
                duplicate_key,
                file_id,
                file_unique_id,
                message.chat_id,
                message.message_id,
                duration,
                "recorded",
                now_iso(),
            ),
        )

        song_id = cursor.lastrowid

        await db.execute(
            """
            INSERT INTO batch_songs(
                batch_id,
                song_id,
                position
            )
            VALUES (?, ?, ?)
            """,
            (
                batch_id,
                song_id,
                position,
            ),
        )

        await db.commit()

    append_processed_song(
        artist,
        title,
        album,
        genre,
    )

    display_name = display_song(
        artist,
        title,
    )

    await message.reply_text(
        "✅ <b>Recorded</b>\n\n"
        f"#{position} — <b>{display_name}</b>",
        parse_mode="HTML",
    )

    logger.info(
        "Recorded song: batch=%s position=%s song=%s",
        batch_id,
        position,
        display_name,
    )


# ============================================================
# DUPLICATE BUTTON HANDLER
# ============================================================

async def duplicate_button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    # Acknowledge the callback immediately.
    await query.answer()

    # Only the owner can make these decisions.
    if (
        not query.from_user
        or query.from_user.id != OWNER_ID
    ):

        await query.answer(
            "❌ You are not authorized.",
            show_alert=True,
        )

        return

    data = query.data or ""

    if ":" not in data:
        return

    action, value = data.split(
        ":",
        1,
    )

    try:
        pending_song_id = int(value)

    except ValueError:
        return

    # --------------------------------------------------------
    # Look up pending duplicate.
    # --------------------------------------------------------

    async with aiosqlite.connect(
        DATABASE_PATH
    ) as db:

        cursor = await db.execute(
            """
            SELECT
                pd.batch_id,
                pd.song_id,
                s.artist,
                s.title,
                s.album,
                s.genre,
                s.status
            FROM pending_duplicates pd

            JOIN songs s
                ON s.id = pd.song_id

            WHERE pd.song_id = ?

            LIMIT 1
            """,
            (
                pending_song_id,
            ),
        )

        pending = await cursor.fetchone()

    if not pending:

        await query.answer(
            "This decision has already been handled.",
            show_alert=True,
        )

        return

    batch_id = pending[0]
    song_id = pending[1]

    artist = pending[2]
    title = pending[3]
    album = pending[4]
    genre = pending[5]
    status = pending[6]

    if status != "duplicate_pending":

        await query.answer(
            "This song has already been handled.",
            show_alert=True,
        )

        return

    display_name = display_song(
        artist,
        title,
    )

    # --------------------------------------------------------
    # SKIP
    # --------------------------------------------------------

    if action == "dup_skip":

        async with aiosqlite.connect(
            DATABASE_PATH
        ) as db:

            await db.execute(
                """
                UPDATE songs
                SET status = 'skipped'
                WHERE id = ?
                """,
                (song_id,),
            )

            await db.execute(
                """
                DELETE FROM pending_duplicates
                WHERE song_id = ?
                """,
                (song_id,),
            )

            await db.commit()

        await query.edit_message_text(
            "⏭️ <b>Skipped</b>\n\n"
            f"{display_name}\n\n"
            "Duplicate was not recorded.",
            parse_mode="HTML",
        )

        return

    # --------------------------------------------------------
    # RECORD ANYWAY
    # --------------------------------------------------------

    if action == "dup_record":

        position = await get_next_position(
            batch_id
        )

        async with aiosqlite.connect(
            DATABASE_PATH
        ) as db:

            await db.execute(
                """
                UPDATE songs
                SET status = 'duplicate_recorded'
                WHERE id = ?
                """,
                (song_id,),
            )

            await db.execute(
                """
                INSERT INTO batch_songs(
                    batch_id,
                    song_id,
                    position
                )
                VALUES (?, ?, ?)
                """,
                (
                    batch_id,
                    song_id,
                    position,
                ),
            )

            await db.execute(
                """
                DELETE FROM pending_duplicates
                WHERE song_id = ?
                """,
                (song_id,),
            )

            await db.commit()

        append_processed_song(
            artist,
            title,
            album,
            genre,
        )

        await query.edit_message_text(
            "✅ <b>Recorded anyway</b>\n\n"
            f"#{position} — {display_name}",
            parse_mode="HTML",
        )

        return


# ============================================================
# TEXT MESSAGE HANDLER
# ============================================================

async def text_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    # Intentionally do nothing for text messages.
    #
    # We don't want captions or random text in the group
    # to affect the recording system.
    #
    return


# ============================================================
# /HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.effective_message.reply_text(
        "🎵 <b>Music Bot</b>\n\n"

        "<b>Configuration</b>\n"
        "/setgroupid\n"
        "/setchannelid -100xxxxxxxxxx\n"
        "/status\n\n"

        "<b>Recording</b>\n"
        '/record "genre"\n'
        "/over\n\n"

        "<b>Example</b>\n"
        '/record "rnb"\n'
        "→ Forward songs\n"
        "→ /over",
        parse_mode="HTML",
    )


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

def create_bot_application():

    if not BOT_TOKEN:

        raise RuntimeError(
            "TELEGRAM_TOKEN is missing from "
            "environment variables."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "setgroupid",
            set_group_id_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "setchannelid",
            set_channel_id_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "record",
            record_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "over",
            over_command,
        )
    )

    # --------------------------------------------------------
    # Callback buttons
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            duplicate_button_handler,
            pattern=r"^dup_(skip|record):\d+$",
        )
    )

    # --------------------------------------------------------
    # Audio messages
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.AUDIO,
            audio_message_handler,
        )
    )

    return application


# ============================================================
# WEB SERVER
# ============================================================

async def create_web_server():

    web_app = web.Application()

    web_app.router.add_get(
        "/",
        web_index,
    )

    web_app.router.add_get(
        "/health",
        web_health,
    )

    runner = web.AppRunner(
        web_app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    await site.start()

    logger.info(
        "🌐 Web server started on port %s",
        PORT,
    )

    return web_app, runner, site


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "Starting Music Bot..."
    )

    # --------------------------------------------------------
    # Initialize database
    # --------------------------------------------------------

    await init_database()

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    application = create_bot_application()

    await application.initialize()

    bot_info = await application.bot.get_me()

    logger.info(
        "🤖 Connected as @%s (%s)",
        bot_info.username,
        bot_info.id,
    )

    # --------------------------------------------------------
    # Web service
    # --------------------------------------------------------

    web_app, runner, site = (
        await create_web_server()
    )

    # --------------------------------------------------------
    # Start Telegram
    # --------------------------------------------------------

    await application.start()

    await application.updater.start_polling()

    logger.info(
        "🤖 Telegram polling started."
    )

    # --------------------------------------------------------
    # Shutdown event
    # --------------------------------------------------------

    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    for sig in (
        signal.SIGINT,
        signal.SIGTERM,
    ):

        try:

            loop.add_signal_handler(
                sig,
                stop_event.set,
            )

        except NotImplementedError:

            # Windows doesn't support this
            # event-loop signal handler.
            pass

    try:

        await stop_event.wait()

    finally:

        logger.info(
            "Shutting down..."
        )

        # Stop Telegram polling.
        if application.updater.running:

            await application.updater.stop()

        # Stop Telegram.
        await application.stop()
        await application.shutdown()

        # Stop web server.
        await site.stop()
        await runner.cleanup()

        logger.info(
            "Shutdown complete."
        )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        pass
