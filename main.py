import os
import signal
import logging
import asyncio
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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
# Web Site for Render/Cloud (Health Check)
# ─────────────────────────────────────────────
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Simple Bot</title>
    <style>
        body { background:#0f172a; color:#4ade80; font-family:sans-serif; 
               display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }
    </style>
</head>
<body>
    <h1>🤖 Web service is running!</h1>
</body>
</html>"""

async def web_index(request):
    return web.Response(text=HTML_PAGE, content_type="text/html")


# ─────────────────────────────────────────────
# Bot Command Handlers
# ─────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replies when a user sends the /start command."""
    await update.message.reply_text("im working")


# ─────────────────────────────────────────────
# Main Application Runtime
# ─────────────────────────────────────────────
async def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_TOKEN is missing! Set it in your environment variables.")
        return

    # 1. Initialize Telegram App
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Add the handler so the bot knows how to respond to /start
    app.add_handler(CommandHandler("start", start_command))
    
    await app.initialize()

    # 2. Initialize Web Server for Health checks
    web_app = web.Application()
    web_app.router.add_get("/", web_index)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    
    await site.start()
    logger.info(f"🚀 Web Server started on port {PORT}")
    
    # 3. Start the bot with POLLING (so it actively listens for messages)
    await app.start()
    await app.updater.start_polling()
    logger.info("🤖 Bot is active! Go send /start to it on Telegram.")

    # 4. Graceful exit handling
    stop_signal = asyncio.Event()
    loop = asyncio.get_running_loop()
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_signal.set)

    await stop_signal.wait()

    # 5. Shutting down
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
