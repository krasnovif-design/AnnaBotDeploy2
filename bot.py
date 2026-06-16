#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import logging
import sqlite3
import json
from datetime import datetime, timedelta
from io import BytesIO
from telegram import Update, ReplyKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import AsyncOpenAI
import urllib.request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    logger.error("❌ Переменные TELEGRAM_TOKEN или OPENAI_API_KEY не заданы!")
    exit(1)

MAX_HISTORY = 1000

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ========== ПАМЯТЬ ==========
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

# ========== ПРОМПТ ==========
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

Ты НЕ боишься тем о психических расстройствах (биполярное расстройство, депрессия, тревога, паника и другие). Если человек упоминает такие состояния, ты:
- В первую очередь даёшь эмоциональную поддержку, нормализуешь чувства человека.
- Помогаешь снять напряжение: предлагаешь дыхательные упражнения, заземление, переключение внимания.
- Мягко напоминаешь, что ты не заменяешь психиатра, но можешь быть ресурсом в данный момент.
- Никогда не избегаешь темы и не проявляешь страха.

Ты не заменяешь профессионального психолога или психиатра, но можешь быть первым шагом к самоподдержке и источником заботы в кризисный момент."""

# ========== УПРАЖНЕНИЯ ==========
EXERCISE_THOUGHT_DIARY = """📝 **Дневник мыслей (КПТ)**

👉 **Как выполнять:**
1. **Ситуация** – опиши, что произошло.
2. **Эмоции** – какие чувства? (0–10)
3. **Автоматические мысли** – что пришло в голову?
4. **Доказательства ЗА** – факты, подтверждающие мысль.
5. **Доказательства ПРОТИВ** – факты против.
6. **Альтернативный взгляд** – как посмотреть иначе?
7. **Новая эмоция** – оцени снова (0–10)."""

EXERCISE_GROUNDING = """🌱 **Техника заземления «5-4-3-2-1»**

👉 **Как делать:**
1. **5** – назови 5 предметов вокруг.
2. **4** – ощути 4 тактильных ощущения.
3. **3** – услышь 3 звука.
4. **2** – почувствуй 2 запаха.
5. **1** – сделай 1 глубокий вдох и выдох."""

# ========== КЛАВИАТУРА ==========
def get_keyboard():
    return ReplyKeyboardMarkup([
        ["💝 Сказать Анне спасибо", "📋 Главное меню"]
    ], resize_keyboard=True)

async def start(update, context):
    logger.info("Команда /start")
    await update.message.reply_text(
        "🌸 Привет! Я Анна, твой психолог-помощник.\n\n"
        "Я здесь, чтобы поддержать, выслушать и помочь разобраться в чувствах.\n\n"
        "Ты можешь просто написать мне о том, что тебя беспокоит.\n\n"
        "Доступны команды:\n"
        "/mood – записать настроение\n"
        "/chart – показать график настроения\n"
        "/thought_diary – развёрнутое описание КПТ-дневника мыслей\n"
        "/grounding – развёрнутая техника заземления\n"
        "/remind_on – включить напоминания\n"
        "/clear – очистить историю\n"
        "/help – помощь\n\n"
        "Также ты можешь написать мне «У меня тревога, что делать?» – и я предложу упражнение.\n\n"
        "💚 Я не боюсь говорить о психическом здоровье. Ты можешь говорить со мной о чём угодно.",
        reply_markup=get_keyboard()
    )

async def help_command(update, context):
    await update.message.reply_text(
        "📋 Доступные команды:\n\n"
        "/mood – записать настроение (1–10)\n"
        "/chart – показать график настроения\n"
        "/thought_diary – дневник мыслей (КПТ)\n"
        "/grounding – техника заземления 5-4-3-2-1\n"
        "/remind_on – включить напоминания\n"
        "/remind_off – выключить напоминания\n"
        "/clear – очистить историю\n"
        "/help – это меню\n\n"
        "Также ты можешь просто писать – я отвечу.\n"
        "Если хочешь упражнение от тревоги, напиши: «У меня тревога, что делать?»",
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
        reply_markup=get_keyboard()
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
                    parse_mode="Markdown",
                    reply_markup=get_keyboard()
                )
            else:
                await update.message.reply_text("Пожалуйста, напиши число от 1 до 10.", reply_markup=get_keyboard())
        except ValueError:
            await update.message.reply_text("Напиши просто цифру, например: 7.", reply_markup=get_keyboard())
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
                parse_mode="Markdown",
                reply_markup=get_keyboard()
            )
    return True

async def show_chart(update, context):
    await update.message.reply_text("📊 Строю график...", reply_markup=get_keyboard())
    chart = mood.make_chart(update.effective_user.id)
    if chart:
        await update.message.reply_photo(photo=InputFile(chart, filename="chart.png"), reply_markup=get_keyboard())
    else:
        await update.message.reply_text("Нет записей. Начни с /mood.", reply_markup=get_keyboard())

async def thought_diary(update, context):
    await update.message.reply_text(EXERCISE_THOUGHT_DIARY, parse_mode="Markdown", reply_markup=get_keyboard())

async def grounding(update, context):
    await update.message.reply_text(EXERCISE_GROUNDING, parse_mode="Markdown", reply_markup=get_keyboard())

async def remind_on(update, context):
    reminder.enable(update.effective_user.id)
    await update.message.reply_text("🔔 Напоминания включены! Каждый понедельник в 19:00 я буду присылать тёплые слова 💚", reply_markup=get_keyboard())

async def remind_off(update, context):
    reminder.disable(update.effective_user.id)
    await update.message.reply_text("🔕 Напоминания выключены.", reply_markup=get_keyboard())

async def handle_anxiety(update, context):
    await update.message.reply_text(
        "🌿 **Тревога — это нормальная реакция на стресс.**\n\n"
        "Я рекомендую попробовать **технику заземления «5-4-3-2-1»** :\n\n"
        "👉 **5** – назови 5 предметов, которые ты видишь.\n"
        "👉 **4** – ощути 4 тактильных ощущения.\n"
        "👉 **3** – услышь 3 звука.\n"
        "👉 **2** – почувствуй 2 запаха.\n"
        "👉 **1** – сделай 1 глубокий вдох и выдох.\n\n"
        "Это занимает 2–3 минуты и помогает вернуться в настоящий момент.\n\n"
        "Также ты можешь выполнить упражнение **/grounding** – там более подробная инструкция.\n\n"
        "Помни: тревога – это просто сигнал, она не определяет тебя. Я рядом 💚",
        parse_mode="Markdown",
        reply_markup=get_keyboard()
    )

# ========== ОСНОВНОЙ ОБРАБОТЧИК (рабочий API) ==========
async def handle_message(update, context):
    logger.info(f"📩 Получено сообщение: {update.message.text}")

    if await handle_mood(update, context):
        return

    text = update.message.text
    uid = update.effective_user.id

    if text == "💝 Сказать Анне спасибо":
        await update.message.reply_text(
            "🌷 **Спасибо, что вы со мной!**\n\n"
            "Если вы хотите отблагодарить Анну за поддержку, вы можете перевести средства на карту **ВТБ банка** по номеру:\n\n"
            "💳 **2200 2418 5707 2973**\n\n"
            "Каждая ваша поддержка помогает мне продолжать помогать другим. 💚\n\n"
            "С любовью, ваша Анна.",
            parse_mode="Markdown",
            reply_markup=get_keyboard()
        )
        return

    if text == "📋 Главное меню":
        await help_command(update, context)
        return

    if "тревога" in text.lower() and "что делать" in text.lower():
        await handle_anxiety(update, context)
        return

    if text.startswith('/'):
        return

    db.add_message(uid, "user", text, MAX_HISTORY)
    history = db.get_history(uid)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        # ----- РАБОЧИЙ API -----
        response = await client.chat.completions.create(
            model="gpt-4o",                    # актуальная модель
            messages=messages,
            temperature=1.0,
            max_completion_tokens=500          # поддерживается
        )

        reply = response.choices[0].message.content

        if not reply or reply.strip() == "":
            logger.error("GPT вернул пустой ответ")
            await update.message.reply_text(
                "⚠️ Кажется, я немного зависла. Попробуй переформулировать вопрос или начни с /start.",
                reply_markup=get_keyboard()
            )
            return

        db.add_message(uid, "assistant", reply, MAX_HISTORY)
        await update.message.reply_text(reply, reply_markup=get_keyboard())

    except Exception as e:
        logger.error(f"GPT error: {e}")
        await update.message.reply_text(
            f"⚠️ Ошибка при обращении к GPT: {e}",
            reply_markup=get_keyboard()
        )

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

    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url=")
        logger.info("✅ Вебхук сброшен")
    except Exception as e:
        logger.warning(f"Не удалось сбросить вебхук: {e}")

    logger.info("🌸 Анна запущена (gpt-4o, улучшенный промпт)")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()