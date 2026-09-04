import os
import signal
import logging
import asyncio
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", "8080"))

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Web Site for EthioDeploy / Health Check
# ─────────────────────────────────────────────
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Bot Status</title>
    <style>
        body { background:#0f172a; color:#4ade80; font-family:sans-serif; 
               display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }
    </style>
</head>
<body>
    <h1>🤖 Web service is online and running!</h1>
</body>
</html>"""

async def web_index(request):
    return web.Response(text=HTML_PAGE, content_type="text/html")


# ─────────────────────────────────────────────
# Bot Handlers
# ─────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a menu showcasing all button color styles."""
    keyboard = [
        # --- Row 1: Native Bot API Colors ---
        [
            InlineKeyboardButton(
                text="Confirm ✅",
                callback_data="btn_green",
                api_kwargs={"style": "success"}  # 🟢 Native Green
            ),
            InlineKeyboardButton(
                text="Delete 🗑️",
                callback_data="btn_red",
                api_kwargs={"style": "danger"}   # 🔴 Native Red
            ),
        ],
        # --- Row 2: Native Primary & Default ---
        [
            InlineKeyboardButton(
                text="Primary Action 🚀",
                callback_data="btn_blue",
                api_kwargs={"style": "primary"}  # 🔵 Native Blue
            ),
            InlineKeyboardButton(
                text="Standard Button ⚪",
                callback_data="btn_default"       # Default gray style
            ),
        ],
        # --- Row 3: Emoji-accented buttons (for other colors) ---
        [
            InlineKeyboardButton(text="🟡 Warning", callback_data="btn_yellow"),
            InlineKeyboardButton(text="🟣 VIP / Special", callback_data="btn_purple"),
            InlineKeyboardButton(text="🟠 Alert", callback_data="btn_orange"),
        ],
        # --- Row 4: URL Button ---
        [
            InlineKeyboardButton(text="🌐 Open GitHub", url="https://github.com")
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🎨 <b>Colored Buttons Test Menu</b>\n\n"
        "• 🟢 <b>Green (Success):</b> Confirmations / positive actions\n"
        "• 🔴 <b>Red (Danger):</b> Deletions / warnings / cancels\n"
        "• 🔵 <b>Blue (Primary):</b> Main recommended action\n"
        "• ⚪ <b>Default:</b> Standard Telegram theme\n"
        "• 🟡 🟣 🟠 <b>Emoji Accents:</b> Custom themes\n\n"
        "<i>Tap any button to test its response:</i>",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answers button clicks and displays a notification."""
    query = update.callback_query
    await query.answer()  # Acknowledge the click

    responses = {
        "btn_green": "✅ You clicked the GREEN (success) button!",
        "btn_red": "🔴 You clicked the RED (danger) button!",
        "btn_blue": "🔵 You clicked the BLUE (primary) button!",
        "btn_default": "⚪ You clicked the DEFAULT button.",
        "btn_yellow": "🟡 You clicked the YELLOW emoji button.",
        "btn_purple": "🟣 You clicked the PURPLE emoji button.",
        "btn_orange": "🟠 You clicked the ORANGE emoji button.",
    }

    selected_text = responses.get(query.data, f"Clicked: {query.data}")

    # Show a popup alert on Telegram
    await query.answer(text=selected_text, show_alert=True)


# ─────────────────────────────────────────────
# Main Application Runtime
# ─────────────────────────────────────────────
async def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_TOKEN is missing! Set it in your environment variables.")
        return

    # 1. Initialize Telegram App
    app = Application.builder().token(BOT_TOKEN).build()

    # 2. Register Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(button_click_handler))

    await app.initialize()

    # 3. Initialize Web Server for EthioDeploy health checks
    web_app = web.Application()
    web_app.router.add_get("/", web_index)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)

    await site.start()
    logger.info(f"🚀 Web Server started on port {PORT}")

    # 4. Start polling
    await app.start()
    await app.updater.start_polling()
    logger.info("🤖 Bot is active! Send /start on Telegram to test.")

    # 5. Graceful exit handling
    stop_signal = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_signal.set)

    await stop_signal.wait()

    # 6. Shutting down
    logger.info("Shutting down...")
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
