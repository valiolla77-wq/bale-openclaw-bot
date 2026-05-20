import os
import logging
import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

# Settings from environment variables
BALE_TOKEN = os.getenv("BALE_TOKEN")
# OpenClaw API address as requested
OPENCLAW_API_URL = "http://127.0.0.1:18789/api/chat"
# Base URL for Bale servers
BALE_BASE_URL = "https://tapi.bale.ai/"

# Logging configuration
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle received messages and forward to OpenClaw"""
    if not update.message or not update.message.text:
        return

    user_text = update.message.text
    chat_id = update.effective_chat.id
    
    logging.info(f"New message from {chat_id}: {user_text}")

    try:
        # Send async request to OpenClaw
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

    # Send final response back to user on Bale
    await context.bot.send_message(chat_id=chat_id, text=reply_text)

if __name__ == '__main__':
    if not BALE_TOKEN:
        logging.error("BALE_TOKEN environment variable not found!")
        exit(1)

    # Create bot application with Bale base URL
    application = ApplicationBuilder().token(BALE_TOKEN).base_url(BALE_BASE_URL).build()
    
    # Register handler for text messages (excluding commands)
    message_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)
    application.add_handler(message_handler)
    
    logging.info("Bale bot started successfully.")
    application.run_polling()
