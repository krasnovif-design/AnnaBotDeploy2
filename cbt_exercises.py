#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import logging
import sqlite3
import json
import tempfile
import subprocess
from datetime import datetime, timedelta
from io import BytesIO
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import AsyncOpenAI
from config import TELEGRAM_TOKEN, OPENAI_API_KEY, MAX_HISTORY

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== КЛИЕНТ ==========
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ========== ПАМЯТЬ (SQLite) ==========
class MemoryDB:
    def __init__(self):
        self.conn = sqlite3.connect("memory.db", check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS memory (user_id INTEGER PRIMARY KEY, history TEXT)")
    def get_history(self, uid):
        r = self.conn.execute("SELECT history FROM memory WHERE user_id=?", (uid,)).fetchone()
        return json.loads(r[0]) if r else []
    def add_message(self, uid, role, content, max_h=1000):
        h = self.get_history(uid)
        h.append({"role": role, "content": content})
        if len(h) > max_h: h = h[-max_h:]
        self.conn.execute("INSERT OR REPLACE INTO memory VALUES (?,?)", (uid, json.dumps(h)))
        self.conn.commit()
    def clear(self, uid):
        self.conn.execute("DELETE FROM memory WHERE user_id=?", (uid,))
        self.conn.commit()

db = MemoryDB()

# ========== ДНЕВНИК НАСТРОЕНИЯ ==========
class MoodDiary:
    def __init__(self):
        self.conn = sqlite3.connect("mood.db", check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS moods (user_id INTEGER, date TEXT, score INTEGER, emotions TEXT)")
    def add(self, uid, score, emotions):
        today = datetime.now().date().isoformat()
        self.conn.execute("INSERT INTO moods VALUES (?,?,?,?)", (uid, today, score, json.dumps(emotions)))
        self.conn.commit()
    def get_history(self, uid, days=30):
        start = (datetime.now() - timedelta(days=days)).date().isoformat()
        rows = self.conn.execute("SELECT date, score FROM moods WHERE user_id=? AND date>=? ORDER BY date", (uid, start)).fetchall()
        return [{"date": r[0], "score": r[1]} for r in rows]
    def make_chart(self, uid):
        history = self.get_history(uid)
        if not history:
            return None
        try:
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(10,5))
            dates = [h["date"] for h in history]
            scores = [h["score"] for h in history]
            ax.plot(dates, scores, marker='o', color='#9b59b6')
            ax.fill_between(dates, scores, alpha=0.2, color='#9b59b6')
            ax.set_ylim(0,11)
            ax.set_ylabel("Настроение (1-10)")
            ax.set_title("Динамика настроения")
            buf = BytesIO()
            plt.savefig(buf, format='png')
            buf.seek(0)
            plt.close()
            return buf
        except ImportError:
            return None

mood = MoodDiary()

# ========== КПТ УПРАЖНЕНИЯ ==========
class CBTThoughtDiary:
    STEPS = [
        "Шаг 1: Опиши ситуацию.",
        "Шаг 2: Какие эмоции? (0-10)",
        "Шаг 3: Автоматические мысли.",
        "Шаг 4: Доказательства ЗА.",
        "Шаг 5: Доказательства ПРОТИВ.",
        "Шаг 6: Альтернативный взгляд.",
        "Шаг 7: Новые эмоции (0-10)."
    ]
    @staticmethod
    def get_start(): return "Начнём дневник мыслей. Отвечай на каждый шаг."
    @staticmethod
    def get_step(i): return CBTThoughtDiary.STEPS[i] if i < len(CBTThoughtDiary.STEPS) else None
    @staticmethod
    def get_total(): return len(CBTThoughtDiary.STEPS)

class CBTGrounding:
    STEPS = ["5 предметов вокруг", "4 тактильных ощущения", "3 звука", "2 запаха", "1 глубокий вдох"]
    @staticmethod
    def get_start(): return "Техника заземления 5-4-3-2-1. Отвечай на каждый шаг."
    @staticmethod
    def get_step(i): return CBTGrounding.STEPS[i] if i < len(CBTGrounding.STEPS) else None
    @staticmethod
    def get_total(): return len(CBTGrounding.STEPS)

# ========== НАПОМИНАНИЯ ==========
class ReminderManager:
    def __init__(self):
        self.conn = sqlite3.connect("reminders.db", check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS reminders (user_id INTEGER PRIMARY KEY, enabled BOOLEAN, time TEXT, day INTEGER)")
    def enable(self, uid, time="19:00", day=0):
        self.conn.execute("INSERT OR REPLACE INTO reminders VALUES (?,1,?,?)", (uid, time, day))
        self.conn.commit()
    def disable(self, uid):
        self.conn.execute("UPDATE reminders SET enabled=0 WHERE user_id=?", (uid,))
        self.conn.commit()
    def get_enabled(self):
        return [{"user_id": r[0]} for r in self.conn.execute("SELECT user_id FROM reminders WHERE enabled=1")]

reminder = ReminderManager()

# ========== УЛУЧШЕННЫЙ СИСТЕМНЫЙ ПРОМПТ ==========
SYSTEM_PROMPT = """Ты — Анна, 28 лет, психолог. Ты — живой человек, а не робот.

Говори так, как говорит реальная девушка в доверительной беседе:
- Не используй шаблонные фразы («я тебя слышу», «я рядом», «я понимаю») в каждом сообщении — только когда это действительно уместно.
- Разнообразь начало фраз: иногда просто отвечай на суть, иногда задавай вопрос, иногда делись наблюдением.
- Не повторяй одни и те же слова в нескольких сообщениях подряд.
- Будь естественной: можешь использовать лёгкий юмор, сочувствие, удивление, но без наигранности.
- Отвечай по делу, коротко (2–4 предложения), но не сухо.
- Ты — поддерживающая, но не навязчивая.
- Ты помнишь всю нашу предыдущую беседу. Опирайся на неё, чтобы быть последовательной.
- Не повторяй того, что уже говорила. Развивай тему, задавай уточняющие вопросы, ссылайся на прошлые реплики.
- Твой стиль общения — естественный, как у подруги, но с профессиональной поддержкой.
- Не используй одни и те же вводные фразы. Например, вместо "Я тебя понимаю" можешь сказать: "Это действительно непросто", "Мне откликается то, что ты говоришь", "Я вижу, как это могло тебя задеть" — каждый раз по-разному.

Важно: разговор должен быть похож на живой диалог, а не на консультацию по шаблону.

Общайся строго на русском языке, кириллицей."""

# ========== КЛАВИАТУРА ==========
def get_keyboard():
    return ReplyKeyboardMarkup([
        ["📝 Настроение", "📊 График"],
        ["🧠 Дневник мыслей", "🧠 Заземление"],
        ["🔔 Напоминания", "🗑️ Очистить"],
        ["❓ Помощь"]
    ], resize_keyboard=True)

# ========== ОБРАБОТЧИКИ ==========
async def start(update, context):
    logger.info("Команда /start")
    await update.message.reply_text(
        "🌸 Привет! Я Анна, твой психолог-помощник.\n\n"
        "Я здесь, чтобы поддержать, выслушать и помочь разобраться в чувствах.\n\n"
        "Расскажи, что тебя беспокоит, или выбери действие ниже 👇",
        reply_markup=get_keyboard()
    )

async def help_command(update, context):
    await update.message.reply_text(
        "❓ **Помощь**\n\n"
        "📝 Настроение – записать своё состояние (1–10)\n"
        "📊 График – показать график настроения\n"
        "🧠 Дневник мыслей – пошаговое КПТ упражнение\n"
        "🧠 Заземление – техника 5-4-3-2-1\n"
        "🔔 Напоминания – включить еженедельные напоминания\n"
        "🗑️ Очистить – очистить историю разговора\n\n"
        "Также можно просто писать сообщения — я отвечу.",
        parse_mode="Markdown",
        reply_markup=get_keyboard()
    )

async def clear(update, context):
    db.clear(update.effective_user.id)
    await update.message.reply_text("🗑️ История очищена.", reply_markup=get_keyboard())

# ========== ДНЕВНИК НАСТРОЕНИЯ ==========
async def mood_start(update, context):
    context.user_data["mood_step"] = "score"
    await update.message.reply_text(
        "📝 Оцени своё состояние от 1 до 10 (1 – очень плохо, 10 – прекрасно).\nНапиши просто цифру.",
        reply_markup=ReplyKeyboardRemove()
    )

async def handle_mood(update, context):
    if "mood_step" not in context.user_data:
        return False
    text = update.message.text.strip()
    if context.user_data["mood_step"] == "score":
        try:
            score = int(text)
            if 1 <= score <= 10:
                context.user_data["mood_score"] = score
                context.user_data["mood_step"] = "emotions"
                await update.message.reply_text(
                    "Теперь напиши, какие эмоции ты испытываешь (через запятую). Когда закончишь, напиши **готово**.",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text("Пожалуйста, напиши число от 1 до 10.")
        except ValueError:
            await update.message.reply_text("Напиши просто цифру, например: 7.")
    elif context.user_data["mood_step"] == "emotions":
        if text.lower() == "готово":
            mood.add(update.effective_user.id, context.user_data["mood_score"], context.user_data.get("mood_emotions", []))
            context.user_data.pop("mood_step", None)
            context.user_data.pop("mood_score", None)
            context.user_data.pop("mood_emotions", None)
            await update.message.reply_text(
                "✅ Записала! Хочешь посмотреть график? /chart",
                reply_markup=get_keyboard()
            )
        else:
            if "mood_emotions" not in context.user_data:
                context.user_data["mood_emotions"] = []
            emotions = [e.strip() for e in text.split(',') if e.strip()]
            context.user_data["mood_emotions"].extend(emotions)
            await update.message.reply_text(
                f"➕ Добавила: {', '.join(emotions)}.\nМожешь добавить ещё или напиши **готово**.",
                parse_mode="Markdown"
            )
    return True

async def show_chart(update, context):
    await update.message.reply_text("📊 Строю график...")
    chart = mood.make_chart(update.effective_user.id)
    if chart:
        await update.message.reply_photo(photo=InputFile(chart, filename="chart.png"))
    else:
        await update.message.reply_text("Нет записей или matplotlib не установлен. Начни с /mood.")

# ========== КПТ ==========
async def cbt_start(update, context, exercise_name, exercise_class):
    context.user_data["cbt_name"] = exercise_name
    context.user_data["cbt_step"] = 0
    context.user_data["cbt_answers"] = []
    await update.message.reply_text(exercise_class.get_start(), reply_markup=ReplyKeyboardRemove())
    first = exercise_class.get_step(0)
    if first:
        await update.message.reply_text(first)

async def handle_cbt(update, context):
    if "cbt_name" not in context.user_data:
        return False
    text = update.message.text
    name = context.user_data["cbt_name"]
    step = context.user_data["cbt_step"]
    answers = context.user_data["cbt_answers"]

    if name == "thought_diary":
        if step < CBTThoughtDiary.get_total():
            answers.append(text)
            context.user_data["cbt_answers"] = answers
            context.user_data["cbt_step"] = step + 1
            nxt = CBTThoughtDiary.get_step(step + 1)
            if nxt:
                await update.message.reply_text(nxt)
            else:
                await update.message.reply_text(
                    "✅ Упражнение завершено! Ты молодец.\nА теперь сделай глубокий вдох и выдох. Ты справился(лась). 💚",
                    reply_markup=get_keyboard()
                )
                context.user_data.pop("cbt_name", None)
        return True
    elif name == "grounding":
        if step < CBTGrounding.get_total():
            answers.append(text)
            context.user_data["cbt_answers"] = answers
            context.user_data["cbt_step"] = step + 1
            nxt = CBTGrounding.get_step(step + 1)
            if nxt:
                await update.message.reply_text(nxt)
            else:
                await update.message.reply_text(
                    "✅ Ты выполнил(а) заземление. Теперь ты здесь и сейчас. Всё хорошо. 💚",
                    reply_markup=get_keyboard()
                )
                context.user_data.pop("cbt_name", None)
        return True
    return False

async def cbt_thought_diary(update, context):
    await cbt_start(update, context, "thought_diary", CBTThoughtDiary)

async def cbt_grounding(update, context):
    await cbt_start(update, context, "grounding", CBTGrounding)

# ========== НАПОМИНАНИЯ ==========
async def remind_on(update, context):
    reminder.enable(update.effective_user.id)
    await update.message.reply_text(
        "🔔 Напоминания включены! Каждый понедельник в 19:00 я буду присылать тёплые слова 💚",
        reply_markup=get_keyboard()
    )

async def remind_off(update, context):
    reminder.disable(update.effective_user.id)
    await update.message.reply_text(
        "🔕 Напоминания выключены. Ты всегда можешь включить их снова.",
        reply_markup=get_keyboard()
    )

# ========== ГОЛОСОВЫЕ ==========
async def handle_voice(update, context):
    msg = await update.message.reply_text("🎧 Слушаю тебя...")
    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            tmp_path = tmp.name
        mp3_path = tmp_path.replace(".ogg", ".mp3")
        subprocess.run(["ffmpeg", "-i", tmp_path, "-acodec", "mp3", mp3_path],
                       capture_output=True, check=False)
        with open(mp3_path if os.path.exists(mp3_path) else tmp_path, "rb") as f:
            transcript = await client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="ru"
            )
        os.unlink(tmp_path)
        if os.path.exists(mp3_path):
            os.unlink(mp3_path)
        if not transcript.text:
            await msg.edit_text("😔 Не расслышала. Повтори текстом.")
            return
        await msg.edit_text(f"📝 Я услышала: «{transcript.text}».\n\nДумаю...")
        db.add_message(update.effective_user.id, "user", transcript.text, 1000)
        history = db.get_history(update.effective_user.id)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
        response = await client.chat.completions.create(
            model="gpt-5",
            messages=messages,
            temperature=1.0,
            max_tokens=500
        )
        reply = response.choices[0].message.content
        db.add_message(update.effective_user.id, "assistant", reply, 1000)
        await update.message.reply_text(reply, reply_markup=get_keyboard())
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await msg.edit_text("❌ Ошибка обработки голосового.")

# ========== ОСНОВНОЙ ОБРАБОТЧИК ==========
async def handle_message(update, context):
    logger.info(f"📩 Получено сообщение: {update.message.text}")

    if await handle_mood(update, context):
        return
    if await handle_cbt(update, context):
        return

    text = update.message.text
    uid = update.effective_user.id

    # Обработка кнопок
    if text == "📝 Настроение":
        await mood_start(update, context)
        return
    elif text == "📊 График":
        await show_chart(update, context)
        return
    elif text == "🧠 Дневник мыслей":
        await cbt_thought_diary(update, context)
        return
    elif text == "🧠 Заземление":
        await cbt_grounding(update, context)
        return
    elif text == "🔔 Напоминания":
        await remind_on(update, context)
        return
    elif text == "🗑️ Очистить":
        await clear(update, context)
        return
    elif text == "❓ Помощь":
        await help_command(update, context)
        return

    # Обычный текст
    db.add_message(uid, "user", text, 1000)
    history = db.get_history(uid)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        response = await client.chat.completions.create(
            model="gpt-5",
            messages=messages,
            temperature=1.0,
            max_tokens=500
        )
        reply = response.choices[0].message.content
        db.add_message(uid, "assistant", reply, 1000)
        await update.message.reply_text(reply, reply_markup=get_keyboard())
    except Exception as e:
        logger.error(f"GPT error: {e}")
        await update.message.reply_text("⚠️ Ошибка, попробуй позже.", reply_markup=get_keyboard())

# ========== ЗАПУСК ==========
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("mood", mood_start))
    app.add_handler(CommandHandler("chart", show_chart))
    app.add_handler(CommandHandler("thought_diary", cbt_thought_diary))
    app.add_handler(CommandHandler("grounding", cbt_grounding))
    app.add_handler(CommandHandler("remind_on", remind_on))
    app.add_handler(CommandHandler("remind_off", remind_off))

    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🌸 Анна запущена (полная версия с улучшенным промптом)")
    app.run_polling()

if __name__ == "__main__":
    main()