import os
import signal
import logging
import asyncio
from pathlib import Path

import aiosqlite
from aiohttp import web
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
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
# HTML HEALTH CHECK
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

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        await db.commit()

    logger.info("Database initialized: %s", DATABASE_PATH)


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
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

        await db.commit()


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


def format_id(value):
    if value is None:
        return "Not configured"

    return f"<code>{value}</code>"


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "🎵 <b>Music Bot</b>\n\n"
        "Bot is online and ready.\n\n"
        "<b>Configuration</b>\n"
        "/setgroupid\n"
        "/setchannelid -100xxxxxxxxxx\n"
        "/status\n\n"
        "🎶 Music recording system will be added next.",
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
    # Option 1:
    # /setgroupid -100123456789
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

        await set_setting("group_id", group_id)

        await message.reply_text(
            "✅ Recording group updated.\n\n"
            f"Group ID: <code>{group_id}</code>",
            parse_mode="HTML",
        )

        return

    # --------------------------------------------------------
    # Option 2:
    # /setgroupid
    #
    # Uses the current chat.
    # --------------------------------------------------------

    if chat.type not in ("group", "supergroup"):
        await message.reply_text(
            "❌ This command must be used inside the recording group.\n\n"
            "Or provide the ID manually:\n"
            "<code>/setgroupid -100123456789</code>",
            parse_mode="HTML",
        )
        return

    group_id = str(chat.id)

    await set_setting("group_id", group_id)

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

    # --------------------------------------------------------
    # /setchannelid -100123456789
    # --------------------------------------------------------

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
            "❌ Invalid channel ID.\n\n"
            "Example:\n"
            "<code>/setchannelid -100123456789</code>",
            parse_mode="HTML",
        )
        return

    await set_setting("channel_id", channel_id)

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

    group_id = await get_setting("group_id")
    channel_id = await get_setting("channel_id")

    await update.effective_message.reply_text(
        "⚙️ <b>Bot Configuration</b>\n\n"
        f"📥 <b>Recording Group</b>\n"
        f"{format_id(group_id)}\n\n"
        f"📢 <b>Music Channel</b>\n"
        f"{format_id(channel_id)}",
        parse_mode="HTML",
    )


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
        "/setgroupid -100xxxxxxxxxx\n"
        "/setchannelid -100xxxxxxxxxx\n"
        "/status\n\n"
        "<b>Coming next</b>\n"
        "/record \"genre\"\n"
        "/over\n"
        "Playlist system",
        parse_mode="HTML",
    )


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

def create_bot_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_TOKEN is missing from environment variables."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("setgroupid", set_group_id_command)
    )

    application.add_handler(
        CommandHandler("setchannelid", set_channel_id_command)
    )

    application.add_handler(
        CommandHandler("status", status_command)
    )

    return application


# ============================================================
# WEB SERVER
# ============================================================

async def create_web_server():
    web_app = web.Application()

    web_app.router.add_get("/", web_index)
    web_app.router.add_get("/health", web_health)

    runner = web.AppRunner(web_app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT,
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

    logger.info("Starting Music Bot...")

    # --------------------------------------------------------
    # Database
    # --------------------------------------------------------

    await init_database()

    # --------------------------------------------------------
    # Telegram application
    # --------------------------------------------------------

    application = create_bot_application()

    await application.initialize()

    bot_info = await application.bot.get_me()

    logger.info(
        "🤖 Telegram bot connected: @%s (%s)",
        bot_info.username,
        bot_info.id,
    )

    # --------------------------------------------------------
    # Web server
    # --------------------------------------------------------

    web_app, runner, site = await create_web_server()

    # --------------------------------------------------------
    # Start Telegram
    # --------------------------------------------------------

    await application.start()
    await application.updater.start_polling()

    logger.info("🤖 Bot polling started.")

    # --------------------------------------------------------
    # Keep running until SIGINT / SIGTERM
    # --------------------------------------------------------

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig,
                stop_event.set,
            )
        except NotImplementedError:
            # Some environments (notably Windows)
            # don't support loop.add_signal_handler().
            pass

    try:
        await stop_event.wait()

    finally:
        logger.info("Shutting down...")

        # Stop polling
        if application.updater.running:
            await application.updater.stop()

        # Stop Telegram application
        await application.stop()
        await application.shutdown()

        # Stop web server
        await site.stop()
        await runner.cleanup()

        logger.info("Shutdown complete.")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        pass
