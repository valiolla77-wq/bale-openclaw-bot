import os
import re
import logging
import asyncio
import base64
import json
import httpx
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler
from huggingface_hub import hf_hub_download, upload_file

BALE_TOKEN = os.getenv("BALE_TOKEN")
BALE_BASE_URL = "https://tapi.bale.ai/"
HF_TOKEN = os.getenv("HF_TOKEN")
MEMORY_REPO = "valiolla/bale-bot-memory"  # نام دیتاست خود را جایگزین کنید
MEMORY_FILE = "memory.json"
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# دیکشنری درون‌حافظه‌ای برای نگهداری تاریخچه‌ها
user_histories = {}  # ساختار: {user_id: [{"role": "user", "text": "..."}, {"role": "model", "text": "..."}]}
history_dirty = False  # آیا تغییری کرده که نیاز به ذخیره داشته باشد؟

# ==================== توابع حافظه ====================
def load_memory():
    global user_histories
    try:
        if not HF_TOKEN:
            logging.warning("HF_TOKEN not set, cannot load memory")
            return
        path = hf_hub_download(repo_id=MEMORY_REPO, filename=MEMORY_FILE, token=HF_TOKEN, repo_type="dataset")
        with open(path, "r", encoding="utf-8") as f:
            user_histories = json.load(f)
        logging.info(f"Memory loaded from Hub: {len(user_histories)} users")
    except Exception as e:
        logging.warning(f"Could not load memory: {e}. Starting fresh.")
        user_histories = {}

def save_memory():
    global history_dirty
    try:
        if not HF_TOKEN:
            logging.warning("HF_TOKEN not set, cannot save memory")
            return
        # ذخیره در یک فایل موقت و سپس آپلود
        tmp_file = "/tmp/memory.json"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(user_histories, f, ensure_ascii=False)
        upload_file(
            path_or_fileobj=tmp_file,
            path_in_repo=MEMORY_FILE,
            repo_id=MEMORY_REPO,
            token=HF_TOKEN,
            repo_type="dataset"
        )
        logging.info("Memory saved to Hub")
        history_dirty = False
    except Exception as e:
        logging.error(f"Failed to save memory: {e}")

def add_message(user_id: str, role: str, text: str):
    global history_dirty
    if user_id not in user_histories:
        user_histories[user_id] = []
    user_histories[user_id].append({"role": role, "text": text})
    # فقط ۲۰ پیام آخر را نگه دار
    if len(user_histories[user_id]) > 20:
        user_histories[user_id] = user_histories[user_id][-20:]
    history_dirty = True

async def periodic_save(interval: int = 10):
    """هر interval ثانیه یک‌بار اگر تغییری بود، ذخیره کند"""
    global history_dirty
    while True:
        await asyncio.sleep(interval)
        if history_dirty:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, save_memory)

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
                return [m["name"].replace("models/", "") for m in data.get("models", [])
                        if "generateContent" in m.get("supportedGenerationMethods", [])]
            return []
    except Exception as e:
        logging.error(f"Error fetching models: {e}")
        return []

def clean_response(raw_text: str) -> str:
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
        final = re.sub(r'^[\*\-\s"\']+|[\*\s"\']+$', '', clean_lines[-1])
        if final:
            return final
    quoted = re.findall(r'"([^"]*)"', text)
    if quoted:
        return quoted[-1].strip()
    return text

async def call_gemini(api_key: str, model: str, prompt: str, user_id: str,
                      test_mode: bool = False, file_bytes: bytes = None, mime_type: str = None,
                      max_retries: int = 3):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    system_instruction = (
        "You are a helpful assistant. "
        "Respond ONLY with the final answer. "
        "Do not include any reasoning, thinking steps, analysis, bullet points, or explanations. "
        "Output just the direct answer."
    )

    # ساخت conversation از تاریخچهٔ کاربر (۱۰ پیام آخر)
    history = user_histories.get(user_id, [])[-10:]
    contents = []
    for msg in history:
        contents.append({
            "role": msg["role"],
            "parts": [{"text": msg["text"]}]
        })
    # اضافه کردن پیام جدید کاربر
    user_parts = []
    if prompt:
        user_parts.append({"text": prompt})
    elif file_bytes:
        user_parts.append({"text": "لطفاً این فایل/تصویر را بررسی کن."})
    if file_bytes and mime_type:
        encoded_file = base64.b64encode(file_bytes).decode('utf-8')
        user_parts.append({"inlineData": {"mimeType": mime_type, "data": encoded_file}})

    contents.append({"role": "user", "parts": user_parts})

    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 700},
        "tools": [{"googleSearch": {}}]
    }
    if "gemini-3" in model or "gemini-3.5" in model:
        payload["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "minimal"}
    elif "gemini-2.5" in model:
        payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}

    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, timeout=60.0)
                if response.status_code == 200:
                    data = response.json()
                    if test_mode:
                        json_data = json.dumps(data, indent=2, ensure_ascii=False)[:3000]
                        return f"🔍 **خروجی خام JSON (Test Mode)**:\n```json\n{json_data}\n```"
                    candidates = data.get("candidates", [])
                    if candidates and candidates[0].get("content", {}).get("parts"):
                        parts = candidates[0]["content"]["parts"]
                        text_parts = [p["text"] for p in parts if not p.get("thought") and "text" in p]
                        raw_reply = "\n".join(text_parts) if text_parts else parts[0].get("text", "")
                        return clean_response(raw_reply)
                    return "⚠️ پاسخی از مدل دریافت نشد."
                elif response.status_code in [429, 500, 503]:
                    await asyncio.sleep(2 ** attempt)
                    continue
                else:
                    return f"❌ خطا در ارتباط با Gemini (کد {response.status_code})"
        except Exception as e:
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)
            else:
                return f"❌ خطا در اتصال: {str(e)}"
    return "❌ تلاش‌ها بی‌نتیجه ماند."

# ==================== هندلرهای ربات ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data.update({"api_key": None, "model": None, "test_mode": False})
    await update.message.reply_text("🤖 ربات هوش مصنوعی Gemini (با حافظه)\n\nلطفاً کلید API جمینای خود را ارسال کنید.\nبرای حالت تست: /testmode")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_chat.id)
    if user_id in user_histories:
        del user_histories[user_id]
        global history_dirty
        history_dirty = True
    await start(update, context)

async def testmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = context.user_data.get("test_mode", False)
    context.user_data["test_mode"] = not current
    status = "روشن 🟢" if context.user_data["test_mode"] else "خاموش 🔴"
    await update.message.reply_text(f"حالت تست (JSON خام) {status} شد.")

async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_key = context.user_data.get("api_key")
    if not api_key:
        await update.message.reply_text("❗ ابتدا باید کلید API خود را ارسال کنید.")
        return
    await update.message.reply_text("⏳ در حال دریافت لیست مدل‌ها...")
    models = await get_available_models(api_key)
    if models:
        model_list = "\n".join([f"• `{m}`" for m in models[:15]])
        await update.message.reply_text(f"✅ مدل‌های در دسترس:\n\n{model_list}\n\nیکی را کپی کنید و ارسال کنید.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ نتوانستم مدل‌ها را دریافت کنم.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = str(chat_id)
    text = update.message.text.strip() if update.message.text else update.message.caption or ""

    if "api_key" not in context.user_data:
        context.user_data.update({"api_key": None, "model": None, "test_mode": False})

    if not context.user_data["api_key"]:
        if text and ((text.startswith("AIza") and len(text) > 30) or (not text.startswith("/") and len(text) > 20)):
            await update.message.reply_text("⏳ در حال بررسی کلید API...")
            models = await get_available_models(text)
            if not models:
                await update.message.reply_text("❌ کلید API معتبر نیست یا خطایی رخ داد.")
                return
            context.user_data["api_key"] = text
            model_list = "\n".join([f"• `{m}`" for m in models[:15]])
            await update.message.reply_text(f"✅ کلید API ذخیره شد.\n\nمدل‌های در دسترس:\n{model_list}\n\nیک مدل را کپی و ارسال کنید.", parse_mode="Markdown")
        else:
            await update.message.reply_text("🔑 لطفاً اول یک کلید API معتبر جمینای ارسال کنید.")
        return

    if not context.user_data["model"]:
        if update.message.photo or update.message.document:
            await update.message.reply_text("❗ لطفاً اول اسم مدل را به صورت متنی بفرستید.")
            return
        models = await get_available_models(context.user_data["api_key"])
        if text in models:
            context.user_data["model"] = text
            await update.message.reply_text(f"✅ مدل انتخاب شد: `{text}`\nحالا می‌تونی چت کنی یا فایل بفرستی.", parse_mode="Markdown")
        else:
            await update.message.reply_text("❗ لطفاً یک مدل معتبر از لیست ارسال کنید (مثل gemini-2.0-flash).")
        return

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

    # ذخیرهٔ پیام کاربر در حافظه (فقط اگر متن داشته باشد)
    if text:
        add_message(user_id, "user", text)

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception as e:
        logging.warning(f"send_chat_action failed: {e}")

    thinking_msg = await update.message.reply_text("🧠 در حال پردازش...")

    reply = await call_gemini(
        api_key=context.user_data["api_key"],
        model=context.user_data["model"],
        prompt=text,
        user_id=user_id,
        test_mode=context.user_data.get("test_mode", False),
        file_bytes=file_bytes,
        mime_type=mime_type
    )

    # ذخیرهٔ پاسخ مدل در حافظه
    if not reply.startswith("❌") and not reply.startswith("⚠️") and not reply.startswith("🔍"):
        add_message(user_id, "model", reply)

    parse_mode = "Markdown" if context.user_data.get("test_mode") else None
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=thinking_msg.message_id, text=reply, parse_mode=parse_mode)
    except Exception:
        await update.message.reply_text(reply, parse_mode=parse_mode)

# ==================== سرور سلامت ====================
async def health_check(request):
    return web.Response(text="OK")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Health server running on port {PORT}")

# ==================== اجرای اصلی ====================
async def main():
    if not BALE_TOKEN:
        logging.error("BALE_TOKEN not found!")
        return

    # بارگذاری حافظه از Hub
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, load_memory)

    # تسک ذخیرهٔ دوره‌ای
    asyncio.create_task(periodic_save(interval=10))

    ptb_app = ApplicationBuilder().token(BALE_TOKEN).base_url(BALE_BASE_URL).build()
    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(CommandHandler("reset", reset_command))
    ptb_app.add_handler(CommandHandler("models", models_command))
    ptb_app.add_handler(CommandHandler("testmode", testmode_command))
    ptb_app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.ALL & ~filters.COMMAND, handle_message))

    await ptb_app.initialize()
    await ptb_app.start()
    await ptb_app.updater.start_polling()

    await run_web_server()

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
