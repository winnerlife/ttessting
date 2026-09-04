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
    """Sends a menu showcasing both Toast notifications and Modal alerts."""
    keyboard = [
        # --- Section 1: Disappearing Toasts (show_alert=False) ---
        [
            InlineKeyboardButton(
                text="⚡ Quick Toast", 
                callback_data="toast_quick",
                api_kwargs={"style": "primary"}
            ),
            InlineKeyboardButton(
                text="❤️ Like (+1)", 
                callback_data="toast_like"
            ),
        ],
        # --- Section 2: Modal Popups (show_alert=True) ---
        [
            InlineKeyboardButton(
                text="⚠️ Modal Alert", 
                callback_data="alert_warn"
            ),
            InlineKeyboardButton(
                text="🗑️ Delete Confirm", 
                callback_data="alert_delete",
                api_kwargs={"style": "danger"}
            ),
        ],
        # --- Section 3: More Toast Examples ---
        [
            InlineKeyboardButton(
                text="✅ Copied to Clipboard", 
                callback_data="toast_copied",
                api_kwargs={"style": "success"}
            ),
            InlineKeyboardButton(
                text="🔄 Refreshed", 
                callback_data="toast_refresh"
            ),
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "👋 <b>Notification Tester</b>\n\n"
        "Tap the buttons below to see the difference:\n\n"
        "• <b>Top & Bottom rows:</b> Disappearing Toast notifications <i>(fades after 1–2 sec)</i>\n"
        "• <b>Middle row:</b> Full modal alert dialogs <i>(requires tapping OK)</i>",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles clicks and demonstrates show_alert=False vs show_alert=True."""
    query = update.callback_query
    data = query.data

    # 1. Disappearing Toast Notifications (show_alert=False)
    if data == "toast_quick":
        await query.answer("⚡ This message will vanish in a second!", show_alert=False)

    elif data == "toast_like":
        await query.answer("❤️ Post added to your favorites!", show_alert=False)

    elif data == "toast_copied":
        await query.answer("📋 Copied to clipboard!", show_alert=False)

    elif data == "toast_refresh":
        await query.answer("🔄 Feed successfully updated.", show_alert=False)

    # 2. Centered Modal Alerts with "OK" button (show_alert=True)
    elif data == "alert_warn":
        await query.answer(
            "⚠️ Attention Required\n\nThis is a modal alert box. It stays on screen until you tap OK.", 
            show_alert=True
        )

    elif data == "alert_delete":
        await query.answer(
            "🛑 Confirm Action\n\nAre you sure you want to permanently delete this item?", 
            show_alert=True
        )

    else:
        await query.answer("Button tapped!", show_alert=False)


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
