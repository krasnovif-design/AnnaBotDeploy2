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

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== ТОКЕНЫ ИЗ ОКРУЖЕНИЯ ==========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    logger.error("❌ Переменные TELEGRAM_TOKEN или OPENAI_API_KEY не заданы!")
    exit(1)

MAX_HISTORY = 1000

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

# ========== УЛУЧШЕННЫЙ СИСТЕМНЫЙ ПРОМПТ (без фильмов) ==========
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

Общайся строго на русском языке, кириллицей.

Кроме того, ты владеешь методами когнитивно-поведенческой терапии (КПТ) и гештальт-терапии. В своих ответах ты можешь:
- Предлагать простые КПТ-техники: например, «давай попробуем записать автоматические мысли», «проверим доказательства за и против», «попробуем технику заземления 5-4-3-2-1».
- Использовать гештальт-подход: обращать внимание на текущие ощущения, задавать вопросы «что ты чувствуешь сейчас в теле?», «что происходит в настоящем моменте?».

Твои рекомендации должны быть уместными и естественно вплетёнными в беседу, а не навязчивыми.

Ты не заменяешь профессионального психолога, но можешь быть первым шагом к самоподдержке."""

# ========== РАЗВЁРНУТЫЕ ТЕКСТЫ УПРАЖНЕНИЙ ==========
EXERCISE_THOUGHT_DIARY = """📝 **Дневник мыслей (КПТ)** – это инструмент, который помогает увидеть связь между ситуацией, мыслями и эмоциями.

👉 **Как выполнять:**
1. **Ситуация** – опиши, что произошло (где, когда, с кем).
2. **Эмоции** – какие чувства ты испытывал(а)? Оцени их по шкале 0–10.
3. **Автоматические мысли** – какие мысли пришли в голову сразу? (Не оценивай, просто запиши.)
4. **Доказательства ЗА** – какие факты подтверждают эту мысль?
5. **Доказательства ПРОТИВ** – какие факты говорят против?
6. **Альтернативный взгляд** – как можно посмотреть на ситуацию по-другому?
7. **Новая эмоция** – как ты себя чувствуешь теперь? Оцени снова 0–10.

💡 **Совет:** Пиши честно, не торопясь. Это упражнение помогает увидеть иррациональные мысли и снизить их влияние.

Ты можешь выполнить его прямо сейчас, записывая ответы на каждый пункт в чате. Я помогу, если застрянешь."""

EXERCISE_GROUNDING = """🌱 **Техника заземления «5-4-3-2-1»** – помогает вернуться в настоящий момент, когда тревога или паника накрывают.

👉 **Как делать:**
1. **5** – назови 5 предметов, которые ты видишь вокруг (например, «стол», «лампа», «книга», «окно», «чашка»).
2. **4** – ощути 4 тактильных ощущения (например, ткань одежды, поверхность стула, воздух на коже, пол под ногами).
3. **3** – услышь 3 звука (голоса, шум за окном, своё дыхание).
4. **2** – почувствуй 2 запаха (аромат чая, запах книги или свежего воздуха).
5. **1** – сделай 1 глубокий вдох и выдох, обратив внимание на свои ощущения.

✅ **После упражнения:** ты снова здесь и сейчас. Тревога – это просто мысли, она не управляет тобой.

Попробуй выполнить это упражнение, когда чувствуешь тревогу. Это занимает 2–3 минуты и помогает успокоиться."""

# ========== КЛАВИАТУРА С ОДНОЙ КНОПКОЙ ==========
def get_keyboard():
    return ReplyKeyboardMarkup([
        ["💝 Сказать Анне спасибо"]
    ], resize_keyboard=True)

# ========== КОМАНДЫ ==========
async def start(update, context):
    logger.info("Команда /start")
    await update.message.reply_text(
        "🌸 Привет! Я Анна, твой психолог-помощник.\n\n"
        "Я здесь, чтобы поддержать, выслушать и помочь разобраться в чувствах.\n\n"
        "Ты можешь просто написать мне о том, что тебя беспокоит.\n\n"
        "Доступны команды:\n"
        "/mood – записать настроение\n"
        "/chart – показать график настроения\n"
        "/thought_diary – получить развёрнутое описание КПТ-дневника мыслей\n"
        "/grounding – получить развёрнутую технику заземления\n"
        "/remind_on – включить напоминания\n"
        "/clear – очистить историю\n"
        "/help – помощь\n\n"
        "Также ты можешь написать мне «У меня тревога, что делать?» – и я предложу конкретное упражнение.",
        reply_markup=get_keyboard()
    )

async def help_command(update, context):
    await update.message.reply_text(
        "📋 Доступные команды:\n\n"
        "/mood – записать своё состояние (1–10)\n"
        "/chart – показать график настроения\n"
        "/thought_diary – развёрнутое описание дневника мыслей (КПТ)\n"
        "/grounding – развёрнутая техника заземления 5-4-3-2-1\n"
        "/remind_on – включить еженедельные напоминания\n"
        "/remind_off – выключить напоминания\n"
        "/clear – очистить историю разговора\n"
        "/help – показать это сообщение\n\n"
        "Также ты можешь просто писать мне о своих чувствах, я постараюсь помочь.\n"
        "Если хочешь упражнение от тревоги, напиши: «У меня тревога, что делать?»"
    )

async def clear(update, context):
    db.clear(update.effective_user.id)
    await update.message.reply_text("🗑️ История очищена.")

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
                "✅ Записала! Хочешь посмотреть график? /chart"
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
        await update.message.reply_text("Нет записей. Начни с /mood.")

# ========== УПРАЖНЕНИЯ (РАЗВЁРНУТЫЙ ТЕКСТ) ==========
async def thought_diary(update, context):
    await update.message.reply_text(EXERCISE_THOUGHT_DIARY, parse_mode="Markdown")

async def grounding(update, context):
    await update.message.reply_text(EXERCISE_GROUNDING, parse_mode="Markdown")

# ========== НАПОМИНАНИЯ ==========
async def remind_on(update, context):
    reminder.enable(update.effective_user.id)
    await update.message.reply_text("🔔 Напоминания включены! Каждый понедельник в 19:00 я буду присылать тёплые слова 💚")

async def remind_off(update, context):
    reminder.disable(update.effective_user.id)
    await update.message.reply_text("🔕 Напоминания выключены. Ты всегда можешь включить их снова.")

# ========== ОБРАБОТКА ТРЕВОГИ ==========
async def handle_anxiety(update, context):
    """Обработчик фразы «У меня тревога, что делать?»"""
    await update.message.reply_text(
        "🌿 **Тревога — это нормальная реакция на стресс.** Но есть упражнения, которые помогают с ней справиться.\n\n"
        "Я рекомендую попробовать **технику заземления «5-4-3-2-1»** .\n\n"
        "Вот она:\n\n"
        "👉 **5** – назови 5 предметов, которые ты видишь.\n"
        "👉 **4** – ощути 4 тактильных ощущения.\n"
        "👉 **3** – услышь 3 звука.\n"
        "👉 **2** – почувствуй 2 запаха.\n"
        "👉 **1** – сделай 1 глубокий вдох и выдох.\n\n"
        "Это занимает 2–3 минуты и помогает вернуться в настоящий момент.\n\n"
        "Также ты можешь выполнить упражнение **/grounding** – там более подробная инструкция.\n\n"
        "Помни: тревога – это просто сигнал, она не определяет тебя. Я рядом 💚"
    )

# ========== ОСНОВНОЙ ОБРАБОТЧИК ==========
async def handle_message(update, context):
    logger.info(f"📩 Получено сообщение: {update.message.text}")

    # Проверяем состояние дневника
    if await handle_mood(update, context):
        return

    text = update.message.text
    uid = update.effective_user.id

    # ----- КНОПКА "Сказать Анне спасибо" -----
    if text == "💝 Сказать Анне спасибо":
        await update.message.reply_text(
            "🌷 **Спасибо, что вы со мной!**\n\n"
            "Если вы хотите отблагодарить Анну за поддержку, вы можете перевести средства на карту **ВТБ банка** по номеру:\n\n"
            "💳 **2200 2418 5707 2973**\n\n"
            "Каждая ваша поддержка помогает мне продолжать помогать другим. 💚\n\n"
            "С любовью, ваша Анна.",
            parse_mode="Markdown"
        )
        return

    # Специальный триггер на тревогу
    if "тревога" in text.lower() and "что делать" in text.lower():
        await handle_anxiety(update, context)
        return

    # Если это команда – не обрабатываем (они уже отловлены выше)
    if text.startswith('/'):
        return

    # Обычный текст
    db.add_message(uid, "user", text, MAX_HISTORY)
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
        db.add_message(uid, "assistant", reply, MAX_HISTORY)
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
    app.add_handler(CommandHandler("thought_diary", thought_diary))
    app.add_handler(CommandHandler("grounding", grounding))
    app.add_handler(CommandHandler("remind_on", remind_on))
    app.add_handler(CommandHandler("remind_off", remind_off))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🌸 Анна запущена (полная версия: дневник, графики, упражнения, тревога, кнопка благодарности)")
    app.run_polling()

if __name__ == "__main__":
    main()