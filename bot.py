import os
import re
import logging
import httpx
import asyncio
import threading
import base64
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler

BALE_TOKEN = os.getenv("BALE_TOKEN")
BALE_BASE_URL = "https://tapi.bale.ai/"
HF_PORT = int(os.getenv("PORT", 7860))

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bale-Gemini Bot is Running!")

    def log_message(self, format, *args):
        return

def run_health_server():
    logging.info(f"Starting HF health-check server on port {HF_PORT}...")
    server = HTTPServer(("", HF_PORT), HealthCheckHandler)
    server.serve_forever()

# ==================== توابع Gemini ====================
async def get_available_models(api_key: str):
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
                timeout=30.0
            )
            if response.status_code == 200:
                data = response.json()
                models = []
                for model in data.get("models", []):
                    if "generateContent" in model.get("supportedGenerationMethods", []):
                        name = model["name"].replace("models/", "")
                        models.append(name)
                return models
            else:
                logging.error(f"Failed to fetch models: {response.status_code} {response.text}")
                return []
    except Exception as e:
        logging.error(f"Error fetching models: {e}")
        return []

def clean_response(raw_text: str) -> str:
    """
    تلاش برای حذف فرآیند فکری و استخراج پاسخ نهایی
    """
    if not raw_text:
        return raw_text

    text = raw_text.strip()
    lines = text.split('\n')
    
    thought_indicators = ["user says:", "predicted", "thinking:", "analysis:", "language:", "meaning:", "respond appropriately"]
    
    clean_lines = []
    for line in lines:
        line_lower = line.lower().strip()
        if any(indicator in line_lower for indicator in thought_indicators):
            continue
        if "respond with only the final answer" in line_lower:
            continue
        if line.strip():
            clean_lines.append(line.strip())

    if clean_lines:
        final_candidate = clean_lines[-1]
        final_candidate = re.sub(r'^[\*\-\s"\']+|[\*\s"\']+$', '', final_candidate)
        if final_candidate:
            return final_candidate

    quoted_pattern = r'"([^"]*)"'
    matches = re.findall(quoted_pattern, text)
    if matches:
        return matches[-1].strip()

    return text

async def call_gemini(api_key: str, model: str, prompt: str, test_mode: bool = False, file_bytes: bytes = None, mime_type: str = None, max_retries: int = 3):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    system_instruction = (
        "You are a helpful assistant. "
        "Respond ONLY with the final answer. "
        "Do not include any reasoning, thinking steps, analysis, bullet points, or explanations. "
        "Output just the direct answer to the user's message."
    )

    # ساختار parts برای ارسال متن و فایل
    user_parts = []
    if prompt:
        user_parts.append({"text": prompt})
    elif file_bytes:  # اگر متنی نبود ولی فایل بود
        user_parts.append({"text": "لطفاً این فایل/تصویر را بررسی کن."})
        
    if file_bytes and mime_type:
        encoded_file = base64.b64encode(file_bytes).decode('utf-8')
        user_parts.append({
            "inlineData": {
                "mimeType": mime_type,
                "data": encoded_file
            }
        })

    payload = {
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        },
        "contents": [
            {
                "role": "user",
                "parts": user_parts
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 700
        },
        "tools": [
            {"googleSearch": {}}
        ]
    }

    # غیرفعال‌سازی تفکر در صورت پشتیبانی مدل (بازگردانی شده)
    if "gemini-3" in model or "gemini-3.5" in model:
        payload["generationConfig"]["thinkingConfig"] = {
            "thinkingLevel": "minimal"
        }
    elif "gemini-2.5" in model:
        payload["generationConfig"]["thinkingConfig"] = {
            "thinkingBudget": 0
        }

    last_exception = None
    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, timeout=60.0)
                if response.status_code == 200:
                    data = response.json()
                    
                    # خروجی خام برای حالت تست (اصلاح و کاهش به ۳۰۰۰ کاراکتر)
                    if test_mode:
                        json_data = json.dumps(data, indent=2, ensure_ascii=False)[:3000]
                        return f"🔍 **خروجی خام JSON (Test Mode)**:\n```json\n{json_data}\n```"

                    candidates = data.get("candidates", [])
                    if candidates and candidates[0].get("content", {}).get("parts"):
                        parts = candidates[0]["content"]["parts"]
                        
                        text_parts = []
                        for part in parts:
                            if part.get("thought") is True:
                                continue
                            if "text" in part:
                                text_parts.append(part["text"])
                                
                        if text_parts:
                            raw_reply = "\n".join(text_parts)
                        else:
                            raw_reply = parts[0].get("text", "")

                        return clean_response(raw_reply)
                    else:
                        return "⚠️ پاسخی از مدل دریافت نشد."
                elif response.status_code in [429, 500, 503]:
                    logging.warning(f"Attempt {attempt}: status {response.status_code}, retrying...")
                    await asyncio.sleep(2 ** attempt)
                    last_exception = Exception(f"Status {response.status_code}")
                    continue
                else:
                    error_text = response.text
                    return f"❌ خطا در ارتباط با Gemini (کد {response.status_code}):\n{error_text}"
        except Exception as e:
            logging.error(f"Attempt {attempt}: {e}")
            last_exception = e
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)
            else:
                return f"❌ خطا در اتصال: {str(e)}"

    return f"❌ درخواست پس از {max_retries} تلاش ناموفق ماند: {last_exception}"

# ==================== هندلرها ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["api_key"] = None
    context.user_data["model"] = None
    context.user_data["test_mode"] = False
    await update.message.reply_text(
        "🤖 ربات هوش مصنوعی Gemini\n\n"
        "لطفاً کلید API جمینای خود را ارسال کنید.\n"
        "برای فعال‌سازی حالت نمایش JSON از دستور /testmode استفاده کنید."
    )

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def testmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_mode = context.user_data.get("test_mode", False)
    context.user_data["test_mode"] = not current_mode
    status = "روشن 🟢" if context.user_data["test_mode"] else "خاموش 🔴"
    await update.message.reply_text(f"حالت تست (Test Mode) {status} شد.\nاگر روشن باشد، پاسخ‌ها به صورت JSON خام ارسال می‌شوند.")

async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_key = context.user_data.get("api_key")
    if not api_key:
        await update.message.reply_text("❗ ابتدا باید کلید API خود را ارسال کنید.")
        return

    await update.message.reply_text("⏳ در حال دریافت لیست مدل‌ها...")
    models = await get_available_models(api_key)
    if models:
        model_list = "\n".join([f"• `{m}`" for m in models[:15]])  # بازگردانی بک‌تیک‌ها
        await update.message.reply_text(
            f"✅ مدل‌های در دسترس:\n\n{model_list}\n\n"
            "لطفاً یکی از مدل‌ها را کپی کرده و ارسال کنید.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ نتوانستم مدل‌ها را دریافت کنم. لطفاً بعداً تلاش کنید.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip() if update.message.text else update.message.caption or ""

    if "api_key" not in context.user_data:
        context.user_data["api_key"] = None
        context.user_data["model"] = None
        context.user_data["test_mode"] = False

    # مرحله ۱: دریافت API Key
    if not context.user_data["api_key"]:
        if text and ((text.startswith("AIza") and len(text) > 30) or (not text.startswith("/") and len(text) > 20)):
            await update.message.reply_text("⏳ در حال بررسی کلید API...")
            models = await get_available_models(text)
            if models is False or not models:
                await update.message.reply_text("❌ کلید API معتبر نیست یا خطایی رخ داد.")
                return
            context.user_data["api_key"] = text
            model_list = "\n".join([f"• `{m}`" for m in models[:15]])
            await update.message.reply_text(
                f"✅ کلید API ذخیره شد.\n\nمدل‌های در دسترس:\n{model_list}\n\nلطفاً یکی از مدل‌ها را کپی کرده و ارسال کنید.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("🔑 لطفاً اول یک کلید API معتبر جمینای ارسال کنید.")
        return

    # مرحله ۲: انتخاب مدل
    if not context.user_data["model"]:
        if update.message.photo or update.message.document:
            await update.message.reply_text("❗ لطفاً اول اسم مدل را به صورت متنی بفرست.")
            return
        
        api_key = context.user_data["api_key"]
        models = await get_available_models(api_key)
        if text in models:
            context.user_data["model"] = text
            await update.message.reply_text(f"✅ مدل انتخاب شد: `{text}`\nحالا می‌تونی چت کنی یا فایل بفرستی.", parse_mode="Markdown")
        else:
            await update.message.reply_text("❗ لطفاً یک مدل معتبر ارسال کنید (مثل gemini-2.0-flash).")
        return

    # مرحله ۳: دریافت فایل (در صورت وجود)
    file_bytes = None
    mime_type = None
    
    if update.message.photo:
        file_obj = await update.message.photo[-1].get_file()
        file_bytes = await file_obj.download_as_bytearray()
        mime_type = "image/jpeg"
    elif update.message.document:
        file_obj = await update.message.document.get_file()
        file_bytes = await file_obj.download_as_bytearray()
        mime_type = update.message.document.mime_type or "application/pdf"

    # مرحله ۴: چت و ارسال به Gemini
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    thinking_msg = await update.message.reply_text("🧠 در حال پردازش...")

    reply = await call_gemini(
        api_key=context.user_data["api_key"],
        model=context.user_data["model"],
        prompt=text,
        test_mode=context.user_data.get("test_mode", False),
        file_bytes=file_bytes,
        mime_type=mime_type
    )

    # انتخاب parse_mode: در حالت تست Markdown، در غیر این صورت بدون فرمت
    parse_mode = "Markdown" if context.user_data.get("test_mode") else None
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=thinking_msg.message_id,
            text=reply,
            parse_mode=parse_mode
        )
    except Exception:
        await update.message.reply_text(reply, parse_mode=parse_mode)

# ==================== اجرای اصلی ====================
if __name__ == '__main__':
    if not BALE_TOKEN:
        logging.error("BALE_TOKEN not found!")
        exit(1)

    threading.Thread(target=run_health_server, daemon=True).start()

    application = ApplicationBuilder().token(BALE_TOKEN).base_url(BALE_BASE_URL).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("models", models_command))
    application.add_handler(CommandHandler("testmode", testmode_command))
    
    application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.ALL & ~filters.COMMAND, handle_message))

    logging.info("Gemini Bot started successfully.")
    application.run_polling()
