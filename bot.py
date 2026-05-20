import os
import logging
import httpx
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

# Settings from environment variables
BALE_TOKEN = os.getenv("BALE_TOKEN")
OPENCLAW_API_URL = "http://127.0.0.1:18789/api/chat"
BALE_BASE_URL = "https://tapi.bale.ai/"
# Render requires a port to be listening. We'll use a dummy port for health checks.
RENDER_PORT = int(os.getenv("PORT", 8080))

# Logging configuration
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- Tiny Web Server for Render Health Check ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return # Suppress logs for health checks

def run_health_server():
    logging.info(f"Starting dummy health-check server on port {RENDER_PORT}...")
    server = HTTPServer(("", RENDER_PORT), HealthCheckHandler)
    server.serve_forever()

# --- Bot Logic ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle received messages and forward to OpenClaw"""
    if not update.message or not update.message.text:
        return

    user_text = update.message.text
    chat_id = update.effective_chat.id
    
    logging.info(f"New message from {chat_id}: {user_text}")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                OPENCLAW_API_URL,
                json={"message": user_text, "chat_id": str(chat_id)},
                timeout=60.0
            )
            
            if response.status_code == 200:
                data = response.json()
                reply_text = data.get("response") or data.get("reply") or "No response from AI."
            else:
                reply_text = f"OpenClaw Error (code {response.status_code})"
                logging.error(f"OpenClaw Error: {response.text}")

    except Exception as e:
        reply_text = "Error connecting to the AI brain!"
        logging.error(f"Connection Exception: {e}")

    await context.bot.send_message(chat_id=chat_id, text=reply_text)

if __name__ == '__main__':
    if not BALE_TOKEN:
        logging.error("BALE_TOKEN environment variable not found!")
        exit(1)

    # 1. Start the dummy health-check server in a background thread
    threading.Thread(target=run_health_server, daemon=True).start()

    # 2. Create bot application
    application = ApplicationBuilder().token(BALE_TOKEN).base_url(BALE_BASE_URL).build()
    
    message_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)
    application.add_handler(message_handler)
    
    logging.info("Bale bot with health-check server started successfully.")
    application.run_polling()
