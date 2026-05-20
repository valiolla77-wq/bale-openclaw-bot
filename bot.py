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
# Hugging Face Spaces expects the app to listen on port 7860
HF_PORT = int(os.getenv("PORT", 7860))

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- Health Check Server for Hugging Face ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bale-OpenClaw Bot is Running!")

    def log_message(self, format, *args):
        return 

def run_health_server():
    logging.info(f"Starting HF health-check server on port {HF_PORT}...")
    server = HTTPServer(("", HF_PORT), HealthCheckHandler)
    server.serve_forever()

# --- Bot Logic ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_text = update.message.text
    chat_id = update.effective_chat.id
    logging.info(f"Message from {chat_id}: {user_text}")

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
                reply_text = f"OpenClaw Error ({response.status_code})"
    except Exception as e:
        reply_text = "Connection error with AI brain!"
        logging.error(f"Exception: {e}")

    await context.bot.send_message(chat_id=chat_id, text=reply_text)

if __name__ == '__main__':
    if not BALE_TOKEN:
        logging.error("BALE_TOKEN not found!")
        exit(1)

    threading.Thread(target=run_health_server, daemon=True).start()

    application = ApplicationBuilder().token(BALE_TOKEN).base_url(BALE_BASE_URL).build()
    message_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)
    application.add_handler(message_handler)
    
    logging.info("Bot and HF Health-server started.")
    application.run_polling()
