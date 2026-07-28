#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import logging
import sqlite3
import re
import time
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
( TYPE, ORIGIN, DEST, DATE_MODE, DATE_FROM, DATE_TO, ROUND_TRIP,
  PRICE, AIRLINE, CITY_INPUT, PASSENGER_TYPE, EDIT_SUB ) = range(12)

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
            airline TEXT,
            passenger_type TEXT,
            active INTEGER DEFAULT 1,
            round_trip INTEGER DEFAULT 1,
            any_date INTEGER DEFAULT 0
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
        q = "SELECT id, user_id, type, origin, dest, date_from, date_to, max_price, airline, passenger_type, active, round_trip, any_date FROM subs"
        if active_only:
            q += " WHERE active=1"
        c.execute(q)
        rows = c.fetchall()
        conn.close()
        return rows

def get_sub_by_id(sub_id, user_id):
    with lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT id, user_id, type, origin, dest, date_from, date_to, max_price, airline, passenger_type, active, round_trip, any_date FROM subs WHERE id=? AND user_id=?", (sub_id, user_id))
        row = c.fetchone()
        conn.close()
        return row

def add_sub(user_id, typ, origin, dest, date_from, date_to, max_price=None, airline=None, passenger_type=None, round_trip=1, any_date=0):
    with lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''INSERT INTO subs 
            (user_id, type, origin, dest, date_from, date_to, max_price, airline, passenger_type, active, round_trip, any_date)
            VALUES (?,?,?,?,?,?,?,?,?,1,?,?)''',
            (user_id, typ, origin, dest, date_from, date_to, max_price, airline, passenger_type, round_trip, any_date))
        conn.commit()
        conn.close()

def update_sub(sub_id, user_id, **kwargs):
    with lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        for key, value in kwargs.items():
            if value is not None:
                c.execute(f"UPDATE subs SET {key}=? WHERE id=? AND user_id=?", (value, sub_id, user_id))
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
PASSENGER_TYPES = [
    ("Молодёжь (до 23 лет)", "YTH"),
    ("Пенсионеры (женщины 55+, мужчины 60+)", "SRC"),
    ("Субсидированный (общий)", "SRC"),  # фактически тот же, но для единообразия
]

# === Функции поиска ===
def fetch_aviasales(origin, dest, date_list, max_price=None, airline=None):
    """Ищет цены на Aviasales через API."""
    results = []
    url = "https://api.travelpayouts.com/v1/prices/calendar"
    for date_str in date_list:
        params = {
            "origin": origin,
            "destination": dest,
            "depart_date": date_str,
            "token": AVIASALES_TOKEN,
            "currency": "rub",
            "show_to_affiliates": "true"
        }
        # Если указана авиакомпания, передаём её (если поддерживается)
        if airline and airline != "Любая":
            params["airline"] = airline
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and data.get("data"):
                    for d, info in data["data"].items():
                        price = info.get("price")
                        if price is not None:
                            link = f"https://www.aviasales.ru/search/{origin}/{dest}/{d}"
                            if max_price is None or price <= max_price:
                                results.append({"date": d, "price": price, "link": link})
                            break
                else:
                    logger.warning(f"Aviasales error: {data}")
        except Exception as e:
            logger.error(f"Aviasales error: {e}")
    return results

def fetch_aeroflot_subsidy(origin, dest, date_list, passenger_type='YTH'):
    """Ищет субсидированные билеты через API Аэрофлота с категорией пассажира."""
    results = []
    # passenger_type может быть YTH, SRC, CNN и др.
    # В API это передаётся как fareType
    url = "https://www.aeroflot.ru/api/ru-RU/search/flights"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.aeroflot.ru/ru-ru/pbsa/search",
        "Origin": "https://www.aeroflot.ru",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
    }
    session = requests.Session()
    # Имитация браузера
    session.get("https://www.aeroflot.ru/ru-ru/pbsa/search", headers=headers, timeout=10)
    time.sleep(2)  # задержка

    for date_str in date_list:
        payload = {
            "origin": origin,
            "destination": dest,
            "departureDate": date_str,
            "adultCount": 1,
            "childCount": 0,
            "infantCount": 0,
            "cabin": "Y",
            "fareType": passenger_type,  # YTH, SRC и т.д.
        }
        try:
            resp = session.post(url, json=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                # Ищем сегменты с субсидией (fareType == SUB или isSubsidized)
                for seg in data.get("segments", []):
                    if seg.get("fareType") == "SUB" or seg.get("isSubsidized"):
                        price = seg.get("price", {}).get("amount", 0)
                        link = f"https://www.aeroflot.ru/ru-ru/pbsa/search?origin={origin}&destination={dest}&date={date_str}"
                        results.append({"date": date_str, "price": int(price) if price else 0, "link": link})
                        break
            else:
                logger.warning(f"Аэрофлот статус {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Аэрофлот ошибка: {e}")
    return results

# === Вспомогательные функции для дат ===
def expand_dates(date_from, date_to=None, days=3):
    """Расширяет диапазон дат на ±days дней."""
    start = datetime.strptime(date_from, "%Y-%m-%d")
    if date_to:
        end = datetime.strptime(date_to, "%Y-%m-%d")
    else:
        end = start
    start_exp = start - timedelta(days=days)
    end_exp = end + timedelta(days=days)
    dates = []
    cur = start_exp
    while cur <= end_exp:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return dates

def dates_in_range(date_from, date_to=None):
    """Генерирует все даты от date_from до date_to включительно."""
    start = datetime.strptime(date_from, "%Y-%m-%d")
    if date_to:
        end = datetime.strptime(date_to, "%Y-%m-%d")
    else:
        end = start
    dates = []
    cur = start
    while cur <= end:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return dates

def generate_date_range_for_any_date(max_days=30):
    """Для режима 'любая дата' генерируем диапазон от сегодня до сегодня+max_days."""
    today = datetime.now()
    end = today + timedelta(days=max_days)
    dates = []
    cur = today
    while cur <= end:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return dates

# === Календарь ===
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
    await update.message.reply_text("✈️ Главное меню:", reply_markup=InlineKeyboardMarkup(keyboard))

async def new_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("💰 Дешёвые (Aviasales)", callback_data="type_aviasales")],
        [InlineKeyboardButton("🛫 Субсидированные (Аэрофлот)", callback_data="type_subsidy")],
        [InlineKeyboardButton("↩️ Назад", callback_data="back")]
    ]
    await query.edit_message_text("Выберите тип билетов:", reply_markup=InlineKeyboardMarkup(keyboard))
    return TYPE

async def type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    typ = query.data.split("_")[1]
    context.user_data['type'] = typ
    # Если это субсидия, спросим категорию пассажира
    if typ == 'subsidy':
        keyboard = []
        for label, code in PASSENGER_TYPES:
            keyboard.append([InlineKeyboardButton(label, callback_data=f"passenger_{code}")])
        keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="back")])
        await query.edit_message_text("Выберите категорию пассажира:", reply_markup=InlineKeyboardMarkup(keyboard))
        return PASSENGER_TYPE
    else:
        # Для Aviasales переходим к городам
        return await show_cities(update, context)

async def passenger_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    passenger_code = query.data.split("_")[1]
    context.user_data['passenger_type'] = passenger_code
    return await show_cities(update, context)

async def show_cities(update, context):
    """Показывает список городов для выбора вылета."""
    keyboard = []
    for city, code in CITIES.items():
        keyboard.append([InlineKeyboardButton(f"{city} ({code})", callback_data=f"origin_{code}")])
    keyboard.append([InlineKeyboardButton("✏️ Ввести другой", callback_data="origin_manual")])
    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="back")])
    if update.callback_query:
        await update.callback_query.edit_message_text("📍 Выберите город вылета:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text("📍 Выберите город вылета:", reply_markup=InlineKeyboardMarkup(keyboard))
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
    if text in ("SVO", "DME", "VKO"):
        text = "MOW"
    for city, code in CITIES.items():
        if text == code or text == city.upper():
            context.user_data['origin'] = code
            break
    else:
        context.user_data['origin'] = text
    return await show_dest(update, context)

async def show_dest(update, context):
    keyboard = []
    origin = context.user_data['origin']
    for city, code in CITIES.items():
        if code != origin:
            keyboard.append([InlineKeyboardButton(f"{city} ({code})", callback_data=f"dest_{code}")])
    keyboard.append([InlineKeyboardButton("✏️ Ввести другой", callback_data="dest_manual")])
    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="back")])
    if update.callback_query:
        await update.callback_query.edit_message_text(
            f"📍 Вылет из {origin}. Теперь город прилёта:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            f"📍 Вылет из {origin}. Теперь город прилёта:",
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
        # Спрашиваем, нужна ли обратная дата
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
    else:
        context.user_data['round_trip'] = 0
    # Теперь выбираем режим дат: конкретная дата или любая
    keyboard = [
        [InlineKeyboardButton("📅 Конкретная дата", callback_data="date_mode_specific")],
        [InlineKeyboardButton("🗓️ Любая дата (самая дешёвая)", callback_data="date_mode_any")],
        [InlineKeyboardButton("↩️ Назад", callback_data="back")]
    ]
    await query.edit_message_text("Выберите режим дат:", reply_markup=InlineKeyboardMarkup(keyboard))
    return DATE_MODE

async def date_mode_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split("_")[2]  # "specific" или "any"
    context.user_data['date_mode'] = mode
    if mode == "specific":
        now = datetime.now()
        await query.edit_message_text(
            "📅 Выберите дату вылета (или введите в формате ДД.ММ.ГГГГ):",
            reply_markup=build_calendar(now.year, now.month, "from")
        )
        return DATE_FROM
    else:  # any
        context.user_data['date_from'] = None
        context.user_data['date_to'] = None
        context.user_data['any_date'] = 1
        # Переходим к цене
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

async def calendar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("_")
    prefix = parts[0]

    if prefix == "ignore":
        return

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
            await query.edit_message_text(
                f"✅ Вылет: {date_str}\nТеперь выберите дату возврата (или введите в формате ДД.ММ.ГГГГ):",
                reply_markup=build_calendar(selected_date.year, selected_date.month, "to")
            )
            context.user_data['calendar_prefix'] = "to"
            return DATE_TO
        else:
            context.user_data['date_to'] = None
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
    elif prefix == "to":
        context.user_data['date_to'] = date_str
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
        await query.edit_message_text("Неизвестная команда.")
        return

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
        await update.message.reply_text(
            f"✅ Вылет: {date_str}\nТеперь введите дату возврата (ДД.ММ.ГГГГ):",
            reply_markup=build_calendar(selected.year, selected.month, "to")
        )
        context.user_data['calendar_prefix'] = "to"
        return DATE_TO
    else:
        context.user_data['date_to'] = None
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

async def price_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "price_custom":
        await query.edit_message_text("Введите цену числом (только цифры):")
        return PRICE
    elif data == "price_skip":
        context.user_data['max_price'] = None
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
    # Завершаем создание подписки
    return await finish(update, context)

async def finish(update, context):
    user_id = update.effective_user.id
    typ = context.user_data['type']
    origin = context.user_data['origin']
    dest = context.user_data['dest']
    max_price = context.user_data.get('max_price')
    airline = context.user_data.get('airline')
    passenger_type = context.user_data.get('passenger_type')
    round_trip = context.user_data.get('round_trip', 1)
    date_mode = context.user_data.get('date_mode')
    any_date = 1 if date_mode == 'any' else 0

    if any_date:
        date_from = None
        date_to = None
    else:
        date_from = context.user_data.get('date_from')
        date_to = context.user_data.get('date_to')
        if not date_from:
            await update.callback_query.edit_message_text("Ошибка: не указана дата.")
            return ConversationHandler.END
        if round_trip == 1 and not date_to:
            date_to = date_from  # если только туда, то дата возврата = дата вылета

    # Сохраняем подписку
    add_sub(user_id, typ, origin, dest, date_from, date_to, max_price, airline, passenger_type, round_trip, any_date)

    # Получаем ID новой подписки
    subs = get_subs(active_only=False)
    user_subs = [s for s in subs if s[1] == user_id]
    sub_id = user_subs[-1][0] if user_subs else None

    # Очищаем данные
    context.user_data.clear()

    if update.callback_query:
        await update.callback_query.edit_message_text(
            f"✅ Подписка добавлена!\n📍 {origin} → {dest}\n" +
            (f"📅 {date_from}" + (f" – {date_to}" if date_to else "") if not any_date else "🗓️ Любая дата")
        )
    else:
        await update.message.reply_text("✅ Подписка добавлена!")

    if sub_id:
        await immediate_check(update, context, sub_id, user_id, typ, origin, dest, date_from, date_to, max_price, airline, passenger_type, any_date)
    return ConversationHandler.END

async def immediate_check(update, context, sub_id, user_id, typ, origin, dest, date_from, date_to, max_price, airline, passenger_type, any_date):
    if any_date:
        # Ищем на любую дату в ближайшие 30 дней
        date_list = generate_date_range_for_any_date(max_days=30)
    else:
        if date_from:
            date_list = dates_in_range(date_from, date_to)
        else:
            date_list = []
    if not date_list:
        await context.bot.send_message(chat_id=user_id, text="❌ Нет дат для поиска.")
        return

    if typ == 'aviasales':
        tickets = fetch_aviasales(origin, dest, date_list, max_price, airline)
    else:
        tickets = fetch_aeroflot_subsidy(origin, dest, date_list, passenger_type)

    if tickets:
        # Сортируем по цене и показываем самый дешёвый
        tickets.sort(key=lambda x: x['price'])
        # Покажем до 5 билетов
        for t in tickets[:5]:
            keyboard = [[InlineKeyboardButton("✈️ Купить", url=t["link"])]]
            await context.bot.send_message(
                chat_id=user_id,
                text=f"{'💰 БИЛЕТ ПО ВАШЕЙ ЦЕНЕ!' if typ=='aviasales' else '🎫 СУБСИДИРОВАННЫЙ БИЛЕТ!'}\n"
                     f"📍 {origin}→{dest}\n📅 {t['date']}\n💵 {t['price']} ₽" + (f" (порог {max_price} ₽)" if max_price else ""),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            add_found(sub_id, t['date'], t['price'], t['link'])
    else:
        await context.bot.send_message(chat_id=user_id, text="🔍 Пока билетов по вашим условиям нет. Будем отслеживать.")

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
        sub_id, _, typ, origin, dest, date_from, date_to, max_price, airline, passenger, active, round_trip, any_date = s
        status = "✅ Активна" if active == 1 else "⏸ Приостановлена"
        text += f"#{sub_id}: {origin}→{dest}"
        if any_date:
            text += " (любая дата)"
        else:
            text += f", {date_from}" + (f" – {date_to}" if date_to else "")
        text += f"\n   {status}"
        if max_price:
            text += f", порог {max_price} ₽"
        if airline:
            text += f", {airline}"
        text += "\n"
        row = []
        if active == 1:
            row.append(InlineKeyboardButton("⏸", callback_data=f"pause_{sub_id}"))
        else:
            row.append(InlineKeyboardButton("▶️", callback_data=f"resume_{sub_id}"))
        row.append(InlineKeyboardButton("❌", callback_data=f"delete_{sub_id}"))
        row.append(InlineKeyboardButton("✏️", callback_data=f"edit_{sub_id}"))
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
    if len(parts) < 2:
        await query.edit_message_text("Ошибка.")
        return
    sub_id_str = parts[1]
    if not sub_id_str.isdigit():
        await query.edit_message_text("Ошибка ID.")
        return
    sub_id = int(sub_id_str)
    user_id = update.effective_user.id

    if action == "pause":
        toggle_sub(sub_id, user_id, 0)
        await list_subs(update, context)
    elif action == "resume":
        toggle_sub(sub_id, user_id, 1)
        await list_subs(update, context)
    elif action == "delete":
        delete_sub(sub_id, user_id)
        await list_subs(update, context)
    elif action == "edit":
        context.user_data['edit_sub_id'] = sub_id
        sub = get_sub_by_id(sub_id, user_id)
        if not sub:
            await query.edit_message_text("Подписка не найдена.")
            return
        keyboard = [
            [InlineKeyboardButton("📅 Изменить даты", callback_data="edit_dates")],
            [InlineKeyboardButton("💰 Изменить цену", callback_data="edit_price")],
            [InlineKeyboardButton("📍 Изменить направление", callback_data="edit_direction")],
            [InlineKeyboardButton("🔄 Изменить режим дат (любая/конкретная)", callback_data="edit_date_mode")],
            [InlineKeyboardButton("↩️ Назад", callback_data="back_to_list")]
        ]
        await query.edit_message_text("Выберите, что хотите изменить:", reply_markup=InlineKeyboardMarkup(keyboard))
        return EDIT_SUB
    elif action == "found":
        found = get_found(sub_id)
        if not found:
            await query.edit_message_text("📭 Нет найденных билетов для этой подписки.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]]))
            return
        text = "📊 Найденные билеты:\n\n"
        for f in found:
            text += f"📅 {f[0]}, 💵 {f[1]} ₽\n{f[2]}\n\n"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]]))

async def edit_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    sub_id = context.user_data.get('edit_sub_id')
    user_id = update.effective_user.id
    if not sub_id:
        await query.edit_message_text("Ошибка: не выбрана подписка.")
        return
    if data == "edit_dates":
        context.user_data['edit_field'] = 'dates'
        await query.edit_message_text("Введите новые даты в формате ДД.ММ.ГГГГ-ДД.ММ.ГГГГ (или одну дату):")
        return ConversationHandler.END
    elif data == "edit_price":
        context.user_data['edit_field'] = 'price'
        await query.edit_message_text("Введите новую цену (или 'skip' для удаления порога):")
        return ConversationHandler.END
    elif data == "edit_direction":
        context.user_data['edit_field'] = 'direction'
        await query.edit_message_text("Введите новое направление в формате 'ОТКУДА КУДА' (например, MOW LED):")
        return ConversationHandler.END
    elif data == "edit_date_mode":
        context.user_data['edit_field'] = 'date_mode'
        keyboard = [
            [InlineKeyboardButton("📅 Конкретная дата", callback_data="edit_date_specific")],
            [InlineKeyboardButton("🗓️ Любая дата", callback_data="edit_date_any")],
            [InlineKeyboardButton("↩️ Назад", callback_data="back_to_list")]
        ]
        await query.edit_message_text("Выберите новый режим дат:", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    elif data == "back_to_list":
        await list_subs(update, context)
        return ConversationHandler.END

# === Обработка текстового ввода для редактирования ===
async def handle_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    sub_id = context.user_data.get('edit_sub_id')
    field = context.user_data.get('edit_field')
    if not sub_id or not field:
        await update.message.reply_text("Ошибка: неизвестная операция.")
        return
    sub = get_sub_by_id(sub_id, user_id)
    if not sub:
        await update.message.reply_text("Подписка не найдена.")
        return

    if field == 'dates':
        if '-' in text:
            parts = text.split('-')
            if len(parts) != 2:
                await update.message.reply_text("Неверный формат. Используйте ДД.ММ.ГГГГ-ДД.ММ.ГГГГ")
                return
            d1 = parts[0].strip(); d2 = parts[1].strip()
            try:
                d1_obj = datetime.strptime(d1, "%d.%m.%Y")
                d2_obj = datetime.strptime(d2, "%d.%m.%Y")
            except:
                await update.message.reply_text("Неверный формат даты. Используйте ДД.ММ.ГГГГ.")
                return
            if d1_obj.date() < datetime.now().date() or d2_obj.date() < datetime.now().date():
                await update.message.reply_text("Даты не могут быть в прошлом.")
                return
            if d2_obj < d1_obj:
                await update.message.reply_text("Дата возврата не может быть раньше даты вылета.")
                return
            date_from = d1_obj.strftime("%Y-%m-%d")
            date_to = d2_obj.strftime("%Y-%m-%d")
            update_sub(sub_id, user_id, date_from=date_from, date_to=date_to, any_date=0)
        else:
            try:
                d1_obj = datetime.strptime(text, "%d.%m.%Y")
            except:
                await update.message.reply_text("Неверный формат даты. Используйте ДД.ММ.ГГГГ.")
                return
            if d1_obj.date() < datetime.now().date():
                await update.message.reply_text("Дата не может быть в прошлом.")
                return
            date_from = d1_obj.strftime("%Y-%m-%d")
            date_to = None
            update_sub(sub_id, user_id, date_from=date_from, date_to=date_to, any_date=0)
        await update.message.reply_text("✅ Даты обновлены.")
    elif field == 'price':
        if text.lower() == 'skip':
            new_price = None
        else:
            try:
                new_price = int(text)
            except:
                await update.message.reply_text("Введите число или 'skip'.")
                return
        update_sub(sub_id, user_id, max_price=new_price)
        await update.message.reply_text(f"✅ Цена обновлена: {new_price if new_price is not None else 'без ограничений'}.")
    elif field == 'direction':
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("Введите два IATA-кода через пробел (например, MOW LED).")
            return
        origin = parts[0].upper()
        dest = parts[1].upper()
        update_sub(sub_id, user_id, origin=origin, dest=dest)
        await update.message.reply_text(f"✅ Направление обновлено: {origin} → {dest}.")
    elif field == 'date_mode':
        # Обработка через колбэк, здесь не будет
        pass
    else:
        await update.message.reply_text("Неизвестное поле.")
        return
    context.user_data.clear()
    await list_subs(update, context)

async def edit_date_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    sub_id = context.user_data.get('edit_sub_id')
    user_id = update.effective_user.id
    if not sub_id:
        await query.edit_message_text("Ошибка.")
        return
    if data == "edit_date_specific":
        # Запросить конкретную дату
        context.user_data['edit_field'] = 'dates'  # переиспользуем обработку дат
        await query.edit_message_text("Введите конкретную дату в формате ДД.ММ.ГГГГ (или диапазон):")
        return
    elif data == "edit_date_any":
        update_sub(sub_id, user_id, any_date=1, date_from=None, date_to=None)
        await query.edit_message_text("✅ Режим изменён на 'Любая дата'.")
        await list_subs(update, context)
    else:
        await list_subs(update, context)

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
        if s[1] == user_id and s[10] == 1:
            toggle_sub(s[0], user_id, 0)
    await query.edit_message_text("⏸ Все подписки приостановлены.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]]))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🤖 Бот ищет дешёвые билеты через Aviasales и субсидированные Аэрофлота.\n"
        "➕ Можно выбрать конкретную дату или 'Любую дату' (самые дешёвые билеты).\n"
        "💰 Цену можно указать или пропустить.\n"
        "✈️ Фильтр по авиакомпаниям.\n"
        "👤 Для субсидий нужно выбрать категорию пассажира.\n"
        "📊 История найденных билетов.\n"
        "✏️ Редактирование подписок: изменить даты, цену, направление, режим дат.",
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

# === Периодическая проверка (с расширением ±3 дня) ===
def check_all(context: ContextTypes.DEFAULT_TYPE):
    subs = get_subs(active_only=True)
    for sub in subs:
        sub_id, user_id, typ, origin, dest, date_from, date_to, max_price, airline, passenger, active, round_trip, any_date = sub
        if active != 1:
            continue
        # Генерируем список дат для поиска
        if any_date:
            date_list = generate_date_range_for_any_date(max_days=30)
        else:
            if date_from:
                date_list = expand_dates(date_from, date_to, days=3)  # расширение для мониторинга
            else:
                continue
        if not date_list:
            continue
        if typ == 'aviasales':
            tickets = fetch_aviasales(origin, dest, date_list, max_price, airline)
        else:
            tickets = fetch_aeroflot_subsidy(origin, dest, date_list, passenger)
        for t in tickets:
            add_found(sub_id, t["date"], t["price"], t["link"])
            keyboard = [[InlineKeyboardButton("✈️ Купить", url=t["link"])]]
            context.bot.send_message(
                chat_id=user_id,
                text=f"{'💰 НАЙДЕН БИЛЕТ!' if typ=='aviasales' else '🎫 СУБСИДИРОВАННЫЙ БИЛЕТ!'}\n"
                     f"📍 {origin}→{dest}\n📅 {t['date']}\n💵 {t['price']} ₽" + (f" (порог {max_price} ₽)" if max_price else ""),
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
            PASSENGER_TYPE: [CallbackQueryHandler(passenger_type_chosen, pattern="^passenger_")],
            ORIGIN: [CallbackQueryHandler(origin_chosen, pattern="^origin_"), MessageHandler(filters.TEXT & ~filters.COMMAND, origin_manual)],
            DEST: [CallbackQueryHandler(dest_chosen, pattern="^dest_"), MessageHandler(filters.TEXT & ~filters.COMMAND, dest_manual)],
            ROUND_TRIP: [CallbackQueryHandler(round_trip_chosen, pattern="^round_")],
            DATE_MODE: [CallbackQueryHandler(date_mode_chosen, pattern="^date_mode_")],
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
    app.add_handler(CallbackQueryHandler(manage_sub, pattern="^(pause|resume|delete|edit|found)_\d+$"))
    app.add_handler(CallbackQueryHandler(edit_sub_callback, pattern="^edit_(dates|price|direction|date_mode|back_to_list)$"))
    app.add_handler(CallbackQueryHandler(edit_date_mode_callback, pattern="^edit_date_(specific|any)$"))
    app.add_handler(CallbackQueryHandler(pause_all, pattern="^pause_all$"))
    app.add_handler(CallbackQueryHandler(help_cmd, pattern="^help$"))
    app.add_handler(CallbackQueryHandler(back, pattern="^back$"))
    app.add_handler(CallbackQueryHandler(show_all_found, pattern="^show_found$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_text))

    scheduler = BackgroundScheduler()
    scheduler.add_job(check_all, 'interval', minutes=CHECK_INTERVAL, args=[app])
    scheduler.start()

    app.run_polling()

if __name__ == "__main__":
    main()
