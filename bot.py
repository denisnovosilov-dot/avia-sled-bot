#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import logging
import sqlite3
import json
import time
import re
from datetime import datetime, timedelta
from threading import Lock
from dotenv import load_dotenv

import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from apscheduler.schedulers.background import BackgroundScheduler

# ================== КОНФИГУРАЦИЯ (из .env) ================================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
AVIASALES_TOKEN = os.getenv("AVIASALES_TOKEN")

# Если токенов нет — запрашиваем и сохраняем в .env
if not TELEGRAM_TOKEN or not AVIASALES_TOKEN:
    print("=" * 60)
    print("Токены не найдены в файле .env. Введите их сейчас:")
    tg = input("TELEGRAM_TOKEN: ").strip()
    av = input("AVIASALES_TOKEN: ").strip()
    with open(".env", "w") as f:
        f.write(f"TELEGRAM_TOKEN={tg}\nAVIASALES_TOKEN={av}\n")
    print("Токены сохранены в .env. Перезапустите бота командой: python3 bot.py")
    sys.exit(0)

CHECK_INTERVAL_MINUTES = 10
# ==========================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ------------------------- База данных -------------------------------------
DB_NAME = "subscriptions.db"
db_lock = Lock()

def init_db():
    with db_lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            origin TEXT,
            destination TEXT,
            date_from TEXT,
            date_to TEXT,
            max_price INTEGER,
            category TEXT,
            active INTEGER DEFAULT 1,
            last_check TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS found_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sub_id INTEGER,
            date TEXT,
            price INTEGER,
            link TEXT,
            notified INTEGER DEFAULT 0,
            FOREIGN KEY(sub_id) REFERENCES subscriptions(id)
        )''')
        conn.commit()
        conn.close()

def get_subscriptions(active_only=True):
    with db_lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        query = "SELECT id, user_id, type, origin, destination, date_from, date_to, max_price, category FROM subscriptions"
        if active_only:
            query += " WHERE active=1"
        c.execute(query)
        rows = c.fetchall()
        conn.close()
        return rows

def add_subscription(user_id, sub_type, origin, dest, date_from, date_to, max_price=None, category='SRC'):
    with db_lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''INSERT INTO subscriptions (user_id, type, origin, destination, date_from, date_to, max_price, category)
                     VALUES (?,?,?,?,?,?,?,?)''',
                  (user_id, sub_type, origin, dest, date_from, date_to, max_price, category))
        conn.commit()
        conn.close()

def delete_subscription(sub_id, user_id):
    with db_lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("DELETE FROM subscriptions WHERE id=? AND user_id=?", (sub_id, user_id))
        conn.commit()
        conn.close()

def toggle_subscription(sub_id, user_id, active):
    with db_lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE subscriptions SET active=? WHERE id=? AND user_id=?", (active, sub_id, user_id))
        conn.commit()
        conn.close()

def add_found_ticket(sub_id, date, price, link):
    with db_lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''INSERT INTO found_tickets (sub_id, date, price, link, notified)
                     VALUES (?,?,?,?,0)''', (sub_id, date, price, link))
        conn.commit()
        conn.close()

def mark_notified(ticket_id):
    with db_lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE found_tickets SET notified=1 WHERE id=?", (ticket_id,))
        conn.commit()
        conn.close()

# ------------------------- Поиск субсидий (парсинг Аэрофлота) --------------
def fetch_subsidy_flights(origin, destination, date_from, date_to, category='SRC'):
    results = []
    api_url = "https://www.aeroflot.ru/api/ru-RU/search/flights"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    start = datetime.strptime(date_from, "%Y-%m-%d")
    end = datetime.strptime(date_to, "%Y-%m-%d")
    delta = timedelta(days=1)
    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        payload = {
            "origin": origin,
            "destination": destination,
            "departureDate": date_str,
            "adultCount": 1,
            "childCount": 0,
            "infantCount": 0,
            "cabin": "Y",
            "fareType": category,
        }
        try:
            resp = requests.post(api_url, json=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if "segments" in data:
                    for seg in data["segments"]:
                        if seg.get("fareType") == "SUB" or seg.get("isSubsidized") == True:
                            price = seg.get("price", {}).get("amount", 0)
                            link = f"https://www.aeroflot.ru/ru-ru/pbsa/search?origin={origin}&destination={destination}&date={date_str}"
                            results.append({
                                "date": date_str,
                                "price": int(price) if price else 0,
                                "link": link
                            })
                            break
        except Exception as e:
            logger.error(f"Ошибка при запросе к API Аэрофлота: {e}")
        current += delta
    return results

# ------------------------- Поиск через Aviasales ----------------------------
def fetch_aviasales_price(origin, destination, date_from, date_to):
    url = "https://api.travelpayouts.com/v1/prices/calendar"
    params = {
        "origin": origin,
        "destination": destination,
        "depart_date": date_from,
        "return_date": date_to,
        "token": AVIASALES_TOKEN,
        "currency": "rub",
        "show_to_affiliates": "true"
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "data" in data and data["data"]:
                prices = []
                for d in data["data"]:
                    if "price" in d:
                        prices.append(d["price"])
                if prices:
                    return {"date": date_from, "price": min(prices)}
    except Exception as e:
        logger.error(f"Ошибка Aviasales API: {e}")
    return None

# ------------------------- Периодическая проверка --------------------------
def check_all_subscriptions(context: ContextTypes.DEFAULT_TYPE):
    subs = get_subscriptions(active_only=True)
    for sub in subs:
        sub_id, user_id, sub_type, origin, dest, date_from, date_to, max_price, category = sub
        if sub_type == 'subsidy':
            tickets = fetch_subsidy_flights(origin, dest, date_from, date_to, category)
            for ticket in tickets:
                add_found_ticket(sub_id, ticket["date"], ticket["price"], ticket["link"])
                message = (
                    f"🎫 СУБСИДИРОВАННЫЙ БИЛЕТ ПОЯВИЛСЯ!\n\n"
                    f"Направление: {origin} → {dest}\n"
                    f"Дата: {ticket['date']}\n"
                    f"Цена: фиксированная (льготная)\n"
                    f"Ссылка: {ticket['link']}\n"
                    f"❗ Успейте купить, места ограничены!"
                )
                context.bot.send_message(chat_id=user_id, text=message)
        elif sub_type == 'aviasales':
            result = fetch_aviasales_price(origin, dest, date_from, date_to)
            if result and max_price and result["price"] <= max_price:
                add_found_ticket(sub_id, result["date"], result["price"], None)
                message = (
                    f"💰 НАЙДЕН БИЛЕТ ПО ВАШЕЙ ЦЕНЕ!\n\n"
                    f"Направление: {origin} → {dest}\n"
                    f"Дата: {result['date']}\n"
                    f"Цена: {result['price']} ₽ (ваш порог: {max_price} ₽)\n"
                    f"Ссылка: https://www.aviasales.ru/search/{origin}{dest}{result['date']}\n"
                    f"🔄 Цена может вырасти, поторопитесь!"
                )
                context.bot.send_message(chat_id=user_id, text=message)

# ------------------------- Обработчики команд Telegram ---------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔍 Настроить новый поиск", callback_data="add")],
        [InlineKeyboardButton("📋 Мои подписки", callback_data="list")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")],
        [InlineKeyboardButton("⏸ Приостановить все", callback_data="pause_all")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 Привет! Я бот для мониторинга авиабилетов.\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )

async def add_subscription_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите данные в формате:\n"
                                    "тип(aviasales/subsidy) откуда куда дата_от дата_до [цена(для aviasales)]\n"
                                    "Например: aviasales MOW VVO 2026-07-10 2026-07-15 15000")
    context.user_data['awaiting_input'] = True

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_input'):
        parts = update.message.text.split()
        if len(parts) < 5:
            await update.message.reply_text("Недостаточно данных. Попробуйте ещё раз.")
            return
        sub_type = parts[0].lower()
        origin = parts[1].upper()
        dest = parts[2].upper()
        date_from = parts[3]
        date_to = parts[4]
        max_price = int(parts[5]) if len(parts) > 5 and sub_type == 'aviasales' else None
        category = 'SRC' if sub_type == 'subsidy' else None
        add_subscription(update.effective_user.id, sub_type, origin, dest, date_from, date_to, max_price, category)
        await update.message.reply_text("Подписка добавлена!")
        context.user_data['awaiting_input'] = False

async def list_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs = get_subscriptions(active_only=False)
    user_subs = [s for s in subs if s[1] == update.effective_user.id]
    if not user_subs:
        await update.message.reply_text("У вас нет активных подписок.")
        return
    text = "📋 Ваши подписки:\n"
    for s in user_subs:
        sub_id, _, sub_type, origin, dest, date_from, date_to, max_price, category = s
        status = "✅ активна" if s[7] == 1 else "⏸ приостановлена"
        text += f"#{sub_id}: {origin}→{dest}, {date_from}–{date_to}, {sub_type}, {status}\n"
    await update.message.reply_text(text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот мониторит:\n"
        "1. Субсидированные билеты Аэрофлота (фиксированная цена)\n"
        "2. Дешёвые билеты через Aviasales (порог цены)\n\n"
        "Команды:\n"
        "/start - Главное меню\n"
        "/add - Добавить подписку\n"
        "/list - Список подписок\n"
        "/help - Помощь"
    )

def main():
    init_db()
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_subscription_handler))
    application.add_handler(CommandHandler("list", list_subscriptions))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    scheduler = BackgroundScheduler()
    scheduler.add_job(check_all_subscriptions, 'interval', minutes=CHECK_INTERVAL_MINUTES, args=[application])
    scheduler.start()

    application.run_polling()

if __name__ == "__main__":
    main()
