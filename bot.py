#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import logging
import sqlite3
import re
from datetime import datetime, timedelta
from threading import Lock
from dotenv import load_dotenv
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)
from apscheduler.schedulers.background import BackgroundScheduler

# === Конфигурация ===
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
AVIASALES_TOKEN = os.getenv("AVIASALES_TOKEN")
if not TELEGRAM_TOKEN or not AVIASALES_TOKEN:
    print("Введите токены:")
    tg = input("TELEGRAM_TOKEN: ").strip()
    av = input("AVIASALES_TOKEN: ").strip()
    with open(".env", "w") as f:
        f.write(f"TELEGRAM_TOKEN={tg}\nAVIASALES_TOKEN={av}\n")
    print("Сохранено. Перезапустите.")
    sys.exit(0)

CHECK_INTERVAL = 10  # минут

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === Состояния ===
TYPE, ORIGIN, DEST, DATE_FROM, DATE_TO, ROUND_TRIP, PRICE, AIRLINE, CITY_INPUT, SKIP_PRICE = range(10)

# === База данных ===
DB_NAME = "subscriptions.db"
lock = Lock()

def init_db():
    with lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS subs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            origin TEXT,
            dest TEXT,
            date_from TEXT,
            date_to TEXT,
            max_price INTEGER,
            category TEXT,
            airline TEXT,
            active INTEGER DEFAULT 1,
            round_trip INTEGER DEFAULT 1
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS found (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sub_id INTEGER,
            date TEXT,
            price INTEGER,
            link TEXT,
            notified INTEGER DEFAULT 0
        )''')
        conn.commit()
        conn.close()

def get_subs(active_only=True):
    with lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        q = "SELECT id, user_id, type, origin, dest, date_from, date_to, max_price, category, airline, active, round_trip FROM subs"
        if active_only:
            q += " WHERE active=1"
        c.execute(q)
        rows = c.fetchall()
        conn.close()
        return rows

def add_sub(user_id, typ, origin, dest, d_from, d_to, max_price=None, category='SRC', airline=None, round_trip=1):
    with lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''INSERT INTO subs 
            (user_id, type, origin, dest, date_from, date_to, max_price, category, airline, active, round_trip)
            VALUES (?,?,?,?,?,?,?,?,?,1,?)''',
            (user_id, typ, origin, dest, d_from, d_to, max_price, category, airline, round_trip))
        conn.commit()
        conn.close()

def delete_sub(sub_id, user_id):
    with lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("DELETE FROM subs WHERE id=? AND user_id=?", (sub_id, user_id))
        conn.commit()
        conn.close()

def toggle_sub(sub_id, user_id, active):
    with lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE subs SET active=? WHERE id=? AND user_id=?", (active, sub_id, user_id))
        conn.commit()
        conn.close()

def add_found(sub_id, date, price, link):
    with lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT INTO found (sub_id, date, price, link, notified) VALUES (?,?,?,?,0)",
                  (sub_id, date, price, link))
        conn.commit()
        conn.close()

def get_found(sub_id):
    with lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT date, price, link FROM found WHERE sub_id=? ORDER BY id DESC LIMIT 10", (sub_id,))
        rows = c.fetchall()
        conn.close()
        return rows

# === Города и авиакомпании ===
CITIES = {
    "Москва": "MOW", "Санкт-Петербург": "LED", "Владивосток": "VVO",
    "Сочи": "AER", "Екатеринбург": "SVX", "Новосибирск": "OVB",
    "Красноярск": "KJA", "Иркутск": "IKT", "Хабаровск": "KHV",
    "Южно-Сахалинск": "UUS", "Калининград": "KGD", "Казань": "KZN",
    "Самара": "KUF", "Ростов-на-Дону": "ROV"
}
AIRLINES = ["Аэрофлот", "S7", "Победа", "Уральские авиалинии", "Utair", "Nordwind", "Икар", "Любая"]

# === Умный поиск с запасом ±3 дня ===
def expand_dates(date_from, date_to=None, days=3):
    """Генерирует список дат для поиска: от date_from±days до date_to±days."""
    start = datetime.strptime(date_from, "%Y-%m-%d")
    if date_to:
        end = datetime.strptime(date_to, "%Y-%m-%d")
    else:
        end = start
    # Расширяем диапазон
    start_exp = start - timedelta(days=days)
    end_exp = end + timedelta(days=days)
    dates = []
    cur = start_exp
    while cur <= end_exp:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return dates

# === Функции поиска ===
def fetch_subsidy(origin, dest, date_list, category='SRC'):
    """Ищет субсидированные билеты на указанные даты."""
    results = []
    url = "https://www.aeroflot.ru/api/ru-RU/search/flights"
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
    for date_str in date_list:
        payload = {
            "origin": origin, "destination": dest,
            "departureDate": date_str,
            "adultCount": 1, "childCount": 0, "infantCount": 0,
            "cabin": "Y", "fareType": category
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                for seg in data.get("segments", []):
                    if seg.get("fareType") == "SUB" or seg.get("isSubsidized"):
                        price = seg.get("price", {}).get("amount", 0)
                        link = f"https://www.aeroflot.ru/ru-ru/pbsa/search?origin={origin}&destination={dest}&date={date_str}"
                        results.append({"date": date_str, "price": int(price) if price else 0, "link": link})
                        break
        except Exception as e:
            logger.error(f"Ошибка Аэрофлота: {e}")
    return results

def fetch_aviasales(origin, dest, date_list):
    """Ищет цены через Aviasales API на указанные даты."""
    results = []
    url = "https://api.travelpayouts.com/v1/prices/calendar"
    for date_str in date_list:
        params = {
            "origin": origin, "destination": dest,
            "depart_date": date_str,
            "token": AVIASALES_TOKEN,
            "currency": "rub",
            "show_to_affiliates": "true"
        }
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                prices = [d["price"] for d in data.get("data", []) if "price" in d]
                if prices:
                    min_price = min(prices)
                    link = f"https://www.aviasales.ru/search/{origin}{dest}{date_str}"
                    results.append({"date": date_str, "price": min_price, "link": link})
        except Exception as e:
            logger.error(f"Ошибка Aviasales: {e}")
    return results

# === Календарь (исправленный) ===
def build_calendar(year, month, prefix):
    now = datetime.now()
    first_day = datetime(year, month, 1)
    if month == 12:
        last_day = datetime(year+1, 1, 1) - timedelta(days=1)
    else:
        last_day = datetime(year, month+1, 1) - timedelta(days=1)
    start_weekday = first_day.weekday()
    keyboard = []
    month_names = ["Янв","Фев","Мар","Апр","Май","Июн","Июл","Авг","Сен","Окт","Ноя","Дек"]
    keyboard.append([InlineKeyboardButton(f"{month_names[month-1]} {year}", callback_data="ignore")])
    week = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    keyboard.append([InlineKeyboardButton(d, callback_data="ignore") for d in week])
    row = []
    for _ in range(start_weekday):
        row.append(InlineKeyboardButton(" ", callback_data="ignore"))
    day = 1
    while day <= last_day.day:
        date_obj = datetime(year, month, day)
        if date_obj.date() < now.date():
            btn = InlineKeyboardButton(str(day), callback_data="ignore")
        else:
            date_str = f"{year}-{month:02d}-{day:02d}"
            btn = InlineKeyboardButton(str(day), callback_data=f"{prefix}_{date_str}")
        row.append(btn)
        if len(row) == 7:
            keyboard.append(row)
            row = []
        day += 1
    if row:
        while len(row) < 7:
            row.append(InlineKeyboardButton(" ", callback_data="ignore"))
        keyboard.append(row)
    nav = []
    if month > 1:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"{prefix}_prev_{year}_{month-1}"))
    else:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"{prefix}_prev_{year-1}_{12}"))
    if month < 12:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"{prefix}_next_{year}_{month+1}"))
    else:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"{prefix}_next_{year+1}_{1}"))
    keyboard.append(nav)
    return InlineKeyboardMarkup(keyboard)

# === Обработчики ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔍 Новый поиск", callback_data="new")],
        [InlineKeyboardButton("📋 Мои подписки", callback_data="list")],
        [InlineKeyboardButton("📊 Найденные билеты", callback_data="show_found")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")],
        [InlineKeyboardButton("⏸ Приостановить все", callback_data="pause_all")]
    ]
    await update.message.reply_text("✈️ Привет! Выберите действие:", reply_markup=InlineKeyboardMarkup(keyboard))

async def new_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("🛫 Субсидированный (Аэрофлот)", callback_data="type_subsidy")],
        [InlineKeyboardButton("💰 Дешёвые (Aviasales)", callback_data="type_aviasales")],
        [InlineKeyboardButton("↩️ Назад", callback_data="back")]
    ]
    await query.edit_message_text("Выберите тип билетов:", reply_markup=InlineKeyboardMarkup(keyboard))
    return TYPE

async def type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    typ = query.data.split("_")[1]
    context.user_data['type'] = typ
    keyboard = []
    for city, code in CITIES.items():
        keyboard.append([InlineKeyboardButton(f"{city} ({code})", callback_data=f"origin_{code}")])
    keyboard.append([InlineKeyboardButton("✏️ Ввести другой", callback_data="origin_manual")])
    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="back")])
    await query.edit_message_text("📍 Выберите город вылета:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ORIGIN

async def origin_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "origin_manual":
        await query.edit_message_text("Введите название города (на русском или IATA-код):")
        return CITY_INPUT
    else:
        context.user_data['origin'] = data.split("_")[1]
        return await show_dest(update, context)

async def origin_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    for city, code in CITIES.items():
        if text == code or text == city.upper():
            context.user_data['origin'] = code
            break
    else:
        context.user_data['origin'] = text
    return await show_dest(update, context)

async def show_dest(update, context):
    keyboard = []
    for city, code in CITIES.items():
        if code != context.user_data['origin']:
            keyboard.append([InlineKeyboardButton(f"{city} ({code})", callback_data=f"dest_{code}")])
    keyboard.append([InlineKeyboardButton("✏️ Ввести другой", callback_data="dest_manual")])
    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="back")])
    if update.callback_query:
        await update.callback_query.edit_message_text(
            f"📍 Вылет из {context.user_data['origin']}. Теперь город прилёта:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            f"📍 Вылет из {context.user_data['origin']}. Теперь город прилёта:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return DEST

async def dest_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "dest_manual":
        await query.edit_message_text("Введите город прилёта:")
        return CITY_INPUT
    else:
        context.user_data['dest'] = data.split("_")[1]
        # Спросить, нужна ли обратная дата
        keyboard = [
            [InlineKeyboardButton("✅ Да, туда и обратно", callback_data="round_yes")],
            [InlineKeyboardButton("➡️ Только туда", callback_data="round_no")],
            [InlineKeyboardButton("↩️ Назад", callback_data="back")]
        ]
        await query.edit_message_text("Вам нужен билет туда и обратно?", reply_markup=InlineKeyboardMarkup(keyboard))
        return ROUND_TRIP

async def round_trip_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "round_yes":
        context.user_data['round_trip'] = 1
        now = datetime.now()
        await query.edit_message_text(
            "📅 Выберите дату вылета (или введите в формате ДД.ММ.ГГГГ):",
            reply_markup=build_calendar(now.year, now.month, "from")
        )
        return DATE_FROM
    else:
        context.user_data['round_trip'] = 0
        # Сразу переходим к выбору даты вылета (date_to не нужна)
        now = datetime.now()
        await query.edit_message_text(
            "📅 Выберите дату вылета (или введите в формате ДД.ММ.ГГГГ):",
            reply_markup=build_calendar(now.year, now.month, "from")
        )
        return DATE_FROM

async def dest_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    for city, code in CITIES.items():
        if text == code or text == city.upper():
            context.user_data['dest'] = code
            break
    else:
        context.user_data['dest'] = text
    # Спросить про обратную дату
    keyboard = [
        [InlineKeyboardButton("✅ Да, туда и обратно", callback_data="round_yes")],
        [InlineKeyboardButton("➡️ Только туда", callback_data="round_no")],
        [InlineKeyboardButton("↩️ Назад", callback_data="back")]
    ]
    await update.message.reply_text("Вам нужен билет туда и обратно?", reply_markup=InlineKeyboardMarkup(keyboard))
    return ROUND_TRIP

async def calendar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("_")
    prefix = parts[0]

    if prefix == "ignore":
        return

    # Обработка навигации
    if len(parts) >= 4 and parts[1] in ("prev", "next"):
        action = parts[1]
        year = int(parts[2])
        month = int(parts[3])
        if action == "prev":
            if month == 1:
                year -= 1
                month = 12
            else:
                month -= 1
        else:
            if month == 12:
                year += 1
                month = 1
            else:
                month += 1
        current_prefix = context.user_data.get('calendar_prefix', 'from')
        await query.edit_message_reply_markup(
            reply_markup=build_calendar(year, month, current_prefix)
        )
        return

    if len(parts) != 2:
        await query.edit_message_text("Ошибка формата даты.")
        return
    date_str = parts[1]
    try:
        selected_date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        await query.edit_message_text("Ошибка формата даты.")
        return

    if selected_date.date() < datetime.now().date():
        await query.edit_message_text("❌ Нельзя выбрать прошедшую дату.")
        return

    if prefix == "from":
        context.user_data['date_from'] = date_str
        if context.user_data.get('round_trip') == 1:
            # Если нужен обратный билет, просим дату возврата
            await query.edit_message_text(
                f"✅ Вылет: {date_str}\nТеперь выберите дату возврата (или введите в формате ДД.ММ.ГГГГ):",
                reply_markup=build_calendar(selected_date.year, selected_date.month, "to")
            )
            context.user_data['calendar_prefix'] = "to"
            return DATE_TO
        else:
            # Только туда
            context.user_data['date_to'] = None  # нет обратной даты
            # Переходим к цене или завершению
            if context.user_data.get('type') == 'aviasales':
                keyboard = [
                    [InlineKeyboardButton("5000", callback_data="price_5000"),
                     InlineKeyboardButton("10000", callback_data="price_10000"),
                     InlineKeyboardButton("15000", callback_data="price_15000"),
                     InlineKeyboardButton("20000", callback_data="price_20000")],
                    [InlineKeyboardButton("✏️ Своя", callback_data="price_custom")],
                    [InlineKeyboardButton("⏩ Пропустить цену", callback_data="price_skip")],
                    [InlineKeyboardButton("↩️ Назад", callback_data="back")]
                ]
                await query.edit_message_text("💰 Укажите максимальную цену (или пропустите):", reply_markup=InlineKeyboardMarkup(keyboard))
                return PRICE
            else:
                return await finish(update, context)
    elif prefix == "to":
        context.user_data['date_to'] = date_str
        # Переход к цене или завершению
        if context.user_data.get('type') == 'aviasales':
            keyboard = [
                [InlineKeyboardButton("5000", callback_data="price_5000"),
                 InlineKeyboardButton("10000", callback_data="price_10000"),
                 InlineKeyboardButton("15000", callback_data="price_15000"),
                 InlineKeyboardButton("20000", callback_data="price_20000")],
                [InlineKeyboardButton("✏️ Своя", callback_data="price_custom")],
                [InlineKeyboardButton("⏩ Пропустить цену", callback_data="price_skip")],
                [InlineKeyboardButton("↩️ Назад", callback_data="back")]
            ]
            await query.edit_message_text("💰 Укажите максимальную цену (или пропустите):", reply_markup=InlineKeyboardMarkup(keyboard))
            return PRICE
        else:
            return await finish(update, context)
    else:
        await query.edit_message_text("Неизвестная команда.")
        return

# Обработка текстового ввода дат
async def handle_date_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    pattern = r'^(\d{2})\.(\d{2})\.(\d{4})$'
    match = re.match(pattern, text)
    if not match:
        await update.message.reply_text("❌ Используйте формат ДД.ММ.ГГГГ.")
        return DATE_FROM
    day = int(match.group(1)); month = int(match.group(2)); year = int(match.group(3))
    selected = datetime(year, month, day)
    if selected.date() < datetime.now().date():
        await update.message.reply_text("❌ Нельзя выбрать прошлое.")
        return DATE_FROM
    date_str = selected.strftime("%Y-%m-%d")
    context.user_data['date_from'] = date_str
    if context.user_data.get('round_trip') == 1:
        # Требуем дату возврата
        await update.message.reply_text(
            f"✅ Вылет: {date_str}\nТеперь введите дату возврата (ДД.ММ.ГГГГ):",
            reply_markup=build_calendar(selected.year, selected.month, "to")
        )
        context.user_data['calendar_prefix'] = "to"
        return DATE_TO
    else:
        context.user_data['date_to'] = None
        if context.user_data.get('type') == 'aviasales':
            keyboard = [
                [InlineKeyboardButton("5000", callback_data="price_5000"),
                 InlineKeyboardButton("10000", callback_data="price_10000"),
                 InlineKeyboardButton("15000", callback_data="price_15000"),
                 InlineKeyboardButton("20000", callback_data="price_20000")],
                [InlineKeyboardButton("✏️ Своя", callback_data="price_custom")],
                [InlineKeyboardButton("⏩ Пропустить цену", callback_data="price_skip")],
                [InlineKeyboardButton("↩️ Назад", callback_data="back")]
            ]
            await update.message.reply_text("💰 Укажите максимальную цену (или пропустите):", reply_markup=InlineKeyboardMarkup(keyboard))
            return PRICE
        else:
            return await finish(update, context)

async def handle_date_to_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    pattern = r'^(\d{2})\.(\d{2})\.(\d{4})$'
    match = re.match(pattern, text)
    if not match:
        await update.message.reply_text("❌ Используйте формат ДД.ММ.ГГГГ.")
        return DATE_TO
    day = int(match.group(1)); month = int(match.group(2)); year = int(match.group(3))
    selected = datetime(year, month, day)
    if selected.date() < datetime.now().date():
        await update.message.reply_text("❌ Нельзя выбрать прошлое.")
        return DATE_TO
    date_str = selected.strftime("%Y-%m-%d")
    context.user_data['date_to'] = date_str
    if context.user_data.get('type') == 'aviasales':
        keyboard = [
            [InlineKeyboardButton("5000", callback_data="price_5000"),
             InlineKeyboardButton("10000", callback_data="price_10000"),
             InlineKeyboardButton("15000", callback_data="price_15000"),
             InlineKeyboardButton("20000", callback_data="price_20000")],
            [InlineKeyboardButton("✏️ Своя", callback_data="price_custom")],
            [InlineKeyboardButton("⏩ Пропустить цену", callback_data="price_skip")],
            [InlineKeyboardButton("↩️ Назад", callback_data="back")]
        ]
        await update.message.reply_text("💰 Укажите максимальную цену (или пропустите):", reply_markup=InlineKeyboardMarkup(keyboard))
        return PRICE
    else:
        return await finish(update, context)

async def price_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "price_custom":
        await query.edit_message_text("Введите цену числом (только цифры):")
        return PRICE
    elif data == "price_skip":
        context.user_data['max_price'] = None  # без ограничения
        return await show_airlines(update, context)
    else:
        context.user_data['max_price'] = int(data.split("_")[1])
        return await show_airlines(update, context)

async def price_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = int(update.message.text.strip())
        context.user_data['max_price'] = price
        await update.message.reply_text(f"✅ Цена {price} ₽ сохранена.")
        return await show_airlines(update, context)
    except:
        await update.message.reply_text("❌ Введите число.")
        return PRICE

async def show_airlines(update, context):
    keyboard = []
    for airline in AIRLINES:
        keyboard.append([InlineKeyboardButton(airline, callback_data=f"airline_{airline}")])
    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="back")])
    if update.callback_query:
        await update.callback_query.edit_message_text(
            "✈️ Выберите авиакомпанию (или 'Любая'):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            "✈️ Выберите авиакомпанию:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return AIRLINE

async def airline_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    airline = query.data.split("_")[1]
    if airline == "Любая":
        airline = None
    context.user_data['airline'] = airline
    return await finish(update, context)

async def finish(update, context):
    user_id = update.effective_user.id
    typ = context.user_data['type']
    origin = context.user_data['origin']
    dest = context.user_data['dest']
    d_from = context.user_data['date_from']
    d_to = context.user_data.get('date_to')
    max_price = context.user_data.get('max_price')
    category = 'SRC' if typ == 'subsidy' else None
    airline = context.user_data.get('airline')
    round_trip = 1 if context.user_data.get('round_trip') == 1 else 0
    if not d_to and round_trip == 1:
        d_to = d_from  # если туда-обратно, но не ввели обратную, ставим ту же
    add_sub(user_id, typ, origin, dest, d_from, d_to, max_price, category, airline, round_trip)
    # Получаем id новой подписки
    subs = get_subs(active_only=False)
    user_subs = [s for s in subs if s[1] == user_id]
    sub_id = user_subs[-1][0] if user_subs else None
    context.user_data.clear()

    if update.callback_query:
        await update.callback_query.edit_message_text(
            f"✅ Подписка добавлена!\n📍 {origin} → {dest}\n📅 {d_from}" + (f" – {d_to}" if d_to else "")
        )
    else:
        await update.message.reply_text("✅ Подписка добавлена!")

    if sub_id:
        await immediate_check(update, context, sub_id, user_id, typ, origin, dest, d_from, d_to, max_price, category)
    return ConversationHandler.END

async def immediate_check(update, context, sub_id, user_id, typ, origin, dest, d_from, d_to, max_price, category):
    # Генерируем список дат для поиска (с запасом ±3 дня)
    date_list = expand_dates(d_from, d_to, days=3)
    if typ == 'subsidy':
        tickets = fetch_subsidy(origin, dest, date_list, category)
        if tickets:
            for t in tickets:
                keyboard = [[InlineKeyboardButton("✈️ Купить", url=t["link"])]]
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎫 СУБСИДИРОВАННЫЙ БИЛЕТ ДОСТУПЕН!\n📍 {origin}→{dest}\n📅 {t['date']}\n💵 {t['price']} ₽",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                add_found(sub_id, t['date'], t['price'], t['link'])
        else:
            await context.bot.send_message(chat_id=user_id, text="🔍 Пока билетов нет. Будем отслеживать с запасом ±3 дня.")
    else:  # aviasales
        tickets = fetch_aviasales(origin, dest, date_list)
        found = False
        for t in tickets:
            if max_price is None or t['price'] <= max_price:
                keyboard = [[InlineKeyboardButton("✈️ Купить", url=t["link"])]]
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"💰 БИЛЕТ ПО ВАШЕЙ ЦЕНЕ!\n📍 {origin}→{dest}\n📅 {t['date']}\n💵 {t['price']} ₽" + (f" (порог {max_price} ₽)" if max_price else ""),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                add_found(sub_id, t['date'], t['price'], t['link'])
                found = True
        if not found:
            await context.bot.send_message(chat_id=user_id, text="🔍 Пока билетов по вашей цене нет. Будем отслеживать с запасом ±3 дня.")

# === Список подписок ===
async def list_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    subs = get_subs(active_only=False)
    user_subs = [s for s in subs if s[1] == user_id]
    if not user_subs:
        await query.edit_message_text("📭 У вас нет подписок.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]]))
        return
    text = "📋 Ваши подписки:\n\n"
    keyboard = []
    for s in user_subs:
        sub_id, _, typ, origin, dest, d_from, d_to, max_price, category, airline, active, round_trip = s
        status = "✅ Активна" if active == 1 else "⏸ Приостановлена"
        text += f"#{sub_id}: {origin}→{dest}, {d_from}" + (f" – {d_to}" if d_to else "") + f"\n   {status}"
        if max_price:
            text += f", порог {max_price} ₽"
        text += "\n"
        row = []
        if active == 1:
            row.append(InlineKeyboardButton("⏸", callback_data=f"pause_{sub_id}"))
        else:
            row.append(InlineKeyboardButton("▶️", callback_data=f"resume_{sub_id}"))
        row.append(InlineKeyboardButton("❌", callback_data=f"delete_{sub_id}"))
        row.append(InlineKeyboardButton("📊", callback_data=f"found_{sub_id}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def manage_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("_")
    action = parts[0]
    sub_id = int(parts[1])
    user_id = update.effective_user.id
    if action == "pause":
        toggle_sub(sub_id, user_id, 0)
    elif action == "resume":
        toggle_sub(sub_id, user_id, 1)
    elif action == "delete":
        delete_sub(sub_id, user_id)
    elif action == "found":
        # Показать найденные билеты
        found = get_found(sub_id)
        if not found:
            await query.edit_message_text("📭 Нет найденных билетов для этой подписки.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]]))
            return
        text = "📊 Найденные билеты:\n\n"
        for f in found:
            text += f"📅 {f[0]}, 💵 {f[1]} ₽\n{f[2]}\n\n"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]]))
        return
    await list_subs(update, context)

# === Показать найденные билеты для всех подписок ===
async def show_all_found(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    subs = get_subs(active_only=False)
    user_subs = [s for s in subs if s[1] == user_id]
    if not user_subs:
        await query.edit_message_text("У вас нет подписок.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]]))
        return
    text = "📊 Все найденные билеты:\n\n"
    any_found = False
    for s in user_subs:
        sub_id = s[0]
        found = get_found(sub_id)
        if found:
            any_found = True
            text += f"Подписка #{sub_id}:\n"
            for f in found:
                text += f"  📅 {f[0]}, 💵 {f[1]} ₽\n  {f[2]}\n"
            text += "\n"
    if not any_found:
        text = "📭 Нет найденных билетов ни для одной подписки."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]]))

# === Прочие команды ===
async def pause_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    subs = get_subs(active_only=False)
    for s in subs:
        if s[1] == user_id and s[10] == 1:  # active
            toggle_sub(s[0], user_id, 0)
    await query.edit_message_text("⏸ Все подписки приостановлены.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]]))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🤖 Бот ищет:\n1) Субсидии Аэрофлота\n2) Дешёвые билеты Aviasales\n\n"
        "➕ Умный поиск ±3 дня, если на точные даты нет билетов.\n"
        "💰 Цену можно пропустить.\n"
        "📊 Смотрите историю найденных билетов.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]])
    )

async def back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("🔍 Новый поиск", callback_data="new")],
        [InlineKeyboardButton("📋 Мои подписки", callback_data="list")],
        [InlineKeyboardButton("📊 Найденные билеты", callback_data="show_found")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")],
        [InlineKeyboardButton("⏸ Приостановить все", callback_data="pause_all")]
    ]
    await query.edit_message_text("✈️ Главное меню:", reply_markup=InlineKeyboardMarkup(keyboard))

# === Периодическая проверка ===
def check_all(context: ContextTypes.DEFAULT_TYPE):
    subs = get_subs(active_only=True)
    for sub in subs:
        sub_id, user_id, typ, origin, dest, d_from, d_to, max_price, category, airline, active, round_trip = sub
        if active != 1:
            continue
        date_list = expand_dates(d_from, d_to, days=3)
        if typ == 'subsidy':
            tickets = fetch_subsidy(origin, dest, date_list, category)
            for t in tickets:
                add_found(sub_id, t["date"], t["price"], t["link"])
                keyboard = [[InlineKeyboardButton("✈️ Купить", url=t["link"])]]
                context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎫 СУБСИДИРОВАННЫЙ БИЛЕТ ПОЯВИЛСЯ!\n📍 {origin}→{dest}\n📅 {t['date']}\n💵 {t['price']} ₽",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        else:
            tickets = fetch_aviasales(origin, dest, date_list)
            for t in tickets:
                if max_price is None or t['price'] <= max_price:
                    add_found(sub_id, t["date"], t["price"], t["link"])
                    keyboard = [[InlineKeyboardButton("✈️ Купить", url=t["link"])]]
                    context.bot.send_message(
                        chat_id=user_id,
                        text=f"💰 НАЙДЕН БИЛЕТ!\n📍 {origin}→{dest}\n📅 {t['date']}\n💵 {t['price']} ₽" + (f" (порог {max_price} ₽)" if max_price else ""),
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )

# === Main ===
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(new_search, pattern="^new$")],
        states={
            TYPE: [CallbackQueryHandler(type_chosen, pattern="^type_")],
            ORIGIN: [CallbackQueryHandler(origin_chosen, pattern="^origin_"), MessageHandler(filters.TEXT & ~filters.COMMAND, origin_manual)],
            DEST: [CallbackQueryHandler(dest_chosen, pattern="^dest_"), MessageHandler(filters.TEXT & ~filters.COMMAND, dest_manual)],
            ROUND_TRIP: [CallbackQueryHandler(round_trip_chosen, pattern="^round_")],
            DATE_FROM: [
                CallbackQueryHandler(calendar_handler, pattern="^(from|from_prev|from_next|ignore)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date_text)
            ],
            DATE_TO: [
                CallbackQueryHandler(calendar_handler, pattern="^(to|to_prev|to_next|ignore)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date_to_text)
            ],
            PRICE: [CallbackQueryHandler(price_chosen, pattern="^price_"), MessageHandler(filters.TEXT & ~filters.COMMAND, price_manual)],
            AIRLINE: [CallbackQueryHandler(airline_chosen, pattern="^airline_")],
            CITY_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, origin_manual)],
        },
        fallbacks=[CallbackQueryHandler(back, pattern="^back$")],
        per_user=True
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(list_subs, pattern="^list$"))
    app.add_handler(CallbackQueryHandler(manage_sub, pattern="^(pause|resume|delete|found)_"))
    app.add_handler(CallbackQueryHandler(pause_all, pattern="^pause_all$"))
    app.add_handler(CallbackQueryHandler(help_cmd, pattern="^help$"))
    app.add_handler(CallbackQueryHandler(back, pattern="^back$"))
    app.add_handler(CallbackQueryHandler(show_all_found, pattern="^show_found$"))

    scheduler = BackgroundScheduler()
    scheduler.add_job(check_all, 'interval', minutes=CHECK_INTERVAL, args=[app])
    scheduler.start()

    app.run_polling()

if __name__ == "__main__":
    main()
