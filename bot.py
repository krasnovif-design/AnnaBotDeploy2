#!/usr/bin/env python3
import os
import sqlite3
import json
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import AsyncOpenAI
from config import TELEGRAM_TOKEN, OPENAI_API_KEY
from database import MemoryDB

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
db = MemoryDB()

SYSTEM_PROMPT = """Ты - Анна, 28 лет, психолог. Говори тепло, по-человечески.
Спрашивай о чувствах. Не ставь диагнозы. Отвечай 2-4 предложениями.
Используй фразы: "Я тебя слышу", "Ммм...", "Расскажи подробнее".
Общайся ТОЛЬКО НА РУССКОМ ЯЗЫКЕ КИРИЛЛИЦЕЙ."""

def get_keyboard():
    return ReplyKeyboardMarkup([
        ["/mood - Настроение", "/chart - График"],
        ["/cbt - КПТ", "/clear - Очистить"]
    ], resize_keyboard=True)

async def start(update, context):
    await update.message.reply_text(
        "Привет! Я Анна, твой психолог-помощник.\n\n"
        "Расскажи, что тебя беспокоит? Я слушаю 💚",
        reply_markup=get_keyboard()
    )

async def clear(update, context):
    db.clear(update.effective_user.id)
    await update.message.reply_text("История очищена.", reply_markup=get_keyboard())

async def handle_message(update, context):
    uid = update.effective_user.id
    text = update.message.text
    db.add_message(uid, "user", text)
    history = db.get_history(uid)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    try:
        response = await client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.85
        )
        reply = response.choices[0].message.content
        db.add_message(uid, "assistant", reply)
        await update.message.reply_text(reply, reply_markup=get_keyboard())
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🌸 Анна запущена!")
    app.run_polling()

if __name__ == "__main__":
    main()
