#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import logging
import sqlite3
import json
import re
from datetime import datetime, timedelta
from threading import Lock
from dotenv import load_dotenv

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from apscheduler.schedulers.background import BackgroundScheduler

# ================== КОНФИГУРАЦИЯ ================================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
AVIASALES_TOKEN = os.getenv("AVIASALES_TOKEN")

if not TELEGRAM_TOKEN or not AVIASALES_TOKEN:
    print("=" * 60)
    print("Токены не найдены. Введите их сейчас:")
    tg = input("TELEGRAM_TOKEN: ").strip()
    av = input("AVIASALES_TOKEN: ").strip()
    with open(".env", "w") as f:
        f.write(f"TELEGRAM_TOKEN={tg}\nAVIASALES_TOKEN={av}\n")
    print("Сохранено. Перезапустите бота.")
    sys.exit(0)

CHECK_INTERVAL_MINUTES = 10
# ================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------- Состояния для ConversationHandler ----------
(
    SELECT_TYPE,
    SELECT_ORIGIN,
    SELECT_DEST,
    SELECT_DATE_FROM,
    SELECT_DATE_TO,
    SELECT_PRICE,
    SELECT_AIRLINE,
    ENTER_CITY_ORIGIN,
    ENTER_CITY_DEST,
) = range(9)

# ---------- База данных ----------
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
            airline TEXT,
            active INTEGER DEFAULT 1
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS found_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sub_id INTEGER,
            date TEXT,
            price INTEGER,
            link TEXT,
            notified INTEGER DEFAULT 0
        )''')
        conn.commit()
        conn.close()

def get_subscriptions(active_only=True):
    with db_lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        query = "SELECT id, user_id, type, origin, destination, date_from, date_to, max_price, category, airline FROM subscriptions"
        if active_only:
            query += " WHERE active=1"
        c.execute(query)
        rows = c.fetchall()
        conn.close()
        return rows

def add_subscription(user_id, sub_type, origin, dest, date_from, date_to, max_price=None, category='SRC', airline=None):
    with db_lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''INSERT INTO subscriptions 
                     (user_id, type, origin, destination, date_from, date_to, max_price, category, airline)
                     VALUES (?,?,?,?,?,?,?,?,?)''',
                  (user_id, sub_type, origin, dest, date_from, date_to, max_price, category, airline))
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

# ---------- Словари городов и авиакомпаний ----------
CITIES = {
    "Москва": "MOW",
    "Санкт-Петербург": "LED",
    "Владивосток": "VVO",
    "Сочи": "AER",
    "Екатеринбург": "SVX",
    "Новосибирск": "OVB",
    "Красноярск": "KJA",
    "Иркутск": "IKT",
    "Хабаровск": "KHV",
    "Южно-Сахалинск": "UUS",
    "Калининград": "KGD",
    "Казань": "KZN",
    "Самара": "KUF",
    "Ростов-на-Дону": "ROV",
}
AIRLINES = ["Аэрофлот", "S7", "Победа", "Уральские авиалинии", "Utair", "Nordwind", "Икар", "Не важно"]

# ---------- Функции поиска (без изменений) ----------
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
                            results.append({"date": date_str, "price": int(price) if price else 0, "link": link})
                            break
        except Exception as e:
            logger.error(f"Ошибка API Аэрофлота: {e}")
        current += delta
    return results

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
                prices = [d["price"] for d in data["data"] if "price" in d]
                if prices:
                    return {"date": date_from, "price": min(prices)}
    except Exception as e:
        logger.error(f"Ошибка Aviasales API: {e}")
    return None

# ---------- Календарь (исправленный) ----------
def create_calendar(year, month, prefix):
    """Создаёт inline-клавиатуру для выбора дня."""
    now = datetime.now()
    # Определяем первый день месяца и количество дней
    first_day = datetime(year, month, 1)
    if month == 12:
        last_day = datetime(year+1, 1, 1) - timedelta(days=1)
    else:
        last_day = datetime(year, month+1, 1) - timedelta(days=1)
    start_weekday = first_day.weekday()  # 0=пн, 6=вс

    keyboard = []
    # Заголовок с месяцем и годом
    month_names = ["Янв","Фев","Мар","Апр","Май","Июн","Июл","Авг","Сен","Окт","Ноя","Дек"]
    keyboard.append([InlineKeyboardButton(f"{month_names[month-1]} {year}", callback_data="ignore")])
    # Дни недели
    week_days = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    keyboard.append([InlineKeyboardButton(day, callback_data="ignore") for day in week_days])

    # Пустые ячейки до первого дня
    row = []
    for i in range(start_weekday):
        row.append(InlineKeyboardButton(" ", callback_data="ignore"))
    day = 1
    while day <= last_day.day:
        date_str = f"{year}-{month:02d}-{day:02d}"
        # Нельзя выбирать прошедшие дни
        if datetime(year, month, day).date() < now.date():
            btn = InlineKeyboardButton(str(day), callback_data="ignore")
        else:
            btn = InlineKeyboardButton(str(day), callback_data=f"{prefix}_{date_str}")
        row.append(btn)
        if len(row) == 7:
            keyboard.append(row)
            row = []
        day += 1
    if row:
        keyboard.append(row)
    # Кнопки навигации
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

# ---------- Функция проверки и отображения билетов сразу ----------
async def show_immediate_tickets(update, context, subscription_id):
    """Проверяет билеты для подписки и отправляет их пользователю, если есть."""
    sub = get_subscription_by_id(subscription_id)
    if not sub:
        return
    user_id, sub_type, origin, dest, date_from, date_to, max_price, category, airline = sub
    if sub_type == 'subsidy':
        tickets = fetch_subsidy_flights(origin, dest, date_from, date_to, category)
        if tickets:
            for ticket in tickets:
                keyboard = [[InlineKeyboardButton("✈️ Купить", url=ticket["link"])]]
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎫 СУБСИДИРОВАННЫЙ БИЛЕТ ДОСТУПЕН ПРЯМО СЕЙЧАС!\n\n"
                         f"📍 {origin} → {dest}\n"
                         f"📅 {ticket['date']}\n"
                         f"💵 Цена фиксированная (льготная)\n"
                         f"❗ Успейте купить!",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        else:
            await context.bot.send_message(
                chat_id=user_id,
                text="🔍 Пока билетов нет. Будет отслеживать и пришлёт уведомление при появлении."
            )
    elif sub_type == 'aviasales':
        result = fetch_aviasales_price(origin, dest, date_from, date_to)
        if result and max_price and result["price"] <= max_price:
            keyboard = [[InlineKeyboardButton("✈️ Купить", url=f"https://www.aviasales.ru/search/{origin}{dest}{result['date']}")]]
            await context.bot.send_message(
                chat_id=user_id,
                text=f"💰 БИЛЕТ ПО ВАШЕЙ ЦЕНЕ УЖЕ ЕСТЬ!\n\n"
                     f"📍 {origin} → {dest}\n"
                     f"📅 {result['date']}\n"
                     f"💵 {result['price']} ₽ (ваш порог: {max_price} ₽)\n"
                     f"🔄 Цена может вырасти!",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await context.bot.send_message(
                chat_id=user_id,
                text="🔍 Пока билетов по вашей цене нет. Будет отслеживать и пришлёт уведомление."
            )

# ---------- Обработчики команд ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔍 Новый поиск", callback_data="new_search")],
        [InlineKeyboardButton("📋 Мои подписки", callback_data="my_subs")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")],
        [InlineKeyboardButton("⏸ Приостановить все", callback_data="pause_all")]
    ]
    await update.message.reply_text(
        "✈️ Привет! Я бот-помощник для поиска авиабилетов.\n"
        "Выберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def start_new_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("🛫 Субсидированный (Аэрофлот)", callback_data="type_subsidy")],
        [InlineKeyboardButton("💰 Дешёвые (Aviasales)", callback_data="type_aviasales")],
        [InlineKeyboardButton("↩️ Назад", callback_data="back_main")]
    ]
    await query.edit_message_text(
        "Выберите тип билетов:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_TYPE

async def select_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sub_type = query.data.split("_")[1]
    context.user_data['sub_type'] = sub_type
    # Показываем список городов
    keyboard = []
    for city, code in CITIES.items():
        keyboard.append([InlineKeyboardButton(f"{city} ({code})", callback_data=f"origin_{code}")])
    keyboard.append([InlineKeyboardButton("✏️ Ввести другой", callback_data="origin_manual")])
    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="new_search")])
    await query.edit_message_text(
        "📍 Выберите город вылета:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_ORIGIN

async def select_origin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "origin_manual":
        await query.edit_message_text("Введите название города вылета (на русском или IATA-код):")
        return ENTER_CITY_ORIGIN
    else:
        origin = data.split("_")[1]
        context.user_data['origin'] = origin
        return await show_dest_list(update, context)

async def enter_city_origin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = update.message.text.strip().upper()
    # Проверяем, есть ли в словаре
    if city in CITIES.values() or city in CITIES.keys():
        # Если пользователь ввёл IATA-код или название
        for name, code in CITIES.items():
            if city == code or city == name.upper():
                context.user_data['origin'] = code
                break
    else:
        # Если город не найден, используем введённый как IATA
        context.user_data['origin'] = city  # пользователь сам ввёл код
    # Переходим к выбору города назначения
    return await show_dest_list(update, context)

async def show_dest_list(update, context):
    """Показывает список городов для прилёта."""
    keyboard = []
    for city, code in CITIES.items():
        if code != context.user_data.get('origin'):
            keyboard.append([InlineKeyboardButton(f"{city} ({code})", callback_data=f"dest_{code}")])
    keyboard.append([InlineKeyboardButton("✏️ Ввести другой", callback_data="dest_manual")])
    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="new_search")])
    if isinstance(update, Update) and update.callback_query:
        await update.callback_query.edit_message_text(
            f"📍 Вылет из {context.user_data['origin']}. Теперь выберите город прилёта:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            f"📍 Вылет из {context.user_data['origin']}. Теперь выберите город прилёта:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return SELECT_DEST

async def select_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "dest_manual":
        await query.edit_message_text("Введите название города прилёта (на русском или IATA-код):")
        return ENTER_CITY_DEST
    else:
        dest = data.split("_")[1]
        context.user_data['dest'] = dest
        # Показываем календарь для даты вылета
        now = datetime.now()
        await query.edit_message_text(
            "📅 Выберите дату вылета (кликните по дню):",
            reply_markup=create_calendar(now.year, now.month, "date_from")
        )
        return SELECT_DATE_FROM

async def enter_city_dest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = update.message.text.strip().upper()
    if city in CITIES.values() or city in CITIES.keys():
        for name, code in CITIES.items():
            if city == code or city == name.upper():
                context.user_data['dest'] = code
                break
    else:
        context.user_data['dest'] = city
    # Теперь показываем календарь
    now = datetime.now()
    await update.message.reply_text(
        "📅 Выберите дату вылета (кликните по дню):",
        reply_markup=create_calendar(now.year, now.month, "date_from")
    )
    return SELECT_DATE_FROM

async def calendar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("_")
    prefix = parts[0]
    if prefix == "ignore":
        return
    if prefix in ("date_from", "date_to"):
        # Выбрана дата
        date_str = parts[1]
        # Проверяем, что дата не прошла
        if datetime.strptime(date_str, "%Y-%m-%d").date() < datetime.now().date():
            await query.edit_message_text("❌ Нельзя выбрать прошедшую дату. Выберите другую.")
            return
        if prefix == "date_from":
            context.user_data['date_from'] = date_str
            # Сохраняем текущий префикс для навигации
            context.user_data['calendar_prefix'] = "date_to"
            # Показываем календарь для даты возврата (можно выбрать ту же)
            await query.edit_message_text(
                f"✅ Дата вылета: {date_str}\nТеперь выберите дату возврата (или ту же, если нужен только туда):",
                reply_markup=create_calendar(
                    datetime.strptime(date_str, "%Y-%m-%d").year,
                    datetime.strptime(date_str, "%Y-%m-%d").month,
                    "date_to"
                )
            )
            return SELECT_DATE_TO
        else:  # date_to
            context.user_data['date_to'] = date_str
            # Переходим к следующему шагу
            if context.user_data.get('sub_type') == 'aviasales':
                # Запрашиваем цену
                keyboard = [
                    [InlineKeyboardButton("5000", callback_data="price_5000"),
                     InlineKeyboardButton("10000", callback_data="price_10000"),
                     InlineKeyboardButton("15000", callback_data="price_15000"),
                     InlineKeyboardButton("20000", callback_data="price_20000")],
                    [InlineKeyboardButton("✏️ Ввести свою", callback_data="price_custom")],
                    [InlineKeyboardButton("↩️ Назад", callback_data="new_search")]
                ]
                await query.edit_message_text(
                    "💰 Укажите максимальную цену (выберите или введите свою):",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return SELECT_PRICE
            else:
                # Для субсидий пропускаем цену и авиакомпанию
                await finish_subscription(update, context)
                return ConversationHandler.END
    else:
        # Навигация по календарю
        action = parts[1]  # prev или next
        year = int(parts[2])
        month = int(parts[3])
        if action == "prev":
            if month < 1:
                month = 12
                year -= 1
        elif action == "next":
            if month > 12:
                month = 1
                year += 1
        # Определяем, какой это календарь (date_from или date_to)
        current_prefix = context.user_data.get('calendar_prefix', 'date_from')
        await query.edit_message_reply_markup(
            reply_markup=create_calendar(year, month, current_prefix)
        )
        return

async def select_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "price_custom":
        await query.edit_message_text("Введите максимальную цену числом (только цифры):")
        return SELECT_PRICE
    else:
        price = int(data.split("_")[1])
        context.user_data['max_price'] = price
        # Переходим к выбору авиакомпании
        return await show_airlines(update, context)

async def handle_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = int(update.message.text.strip())
        context.user_data['max_price'] = price
        await update.message.reply_text(f"✅ Цена {price} ₽ сохранена.")
        return await show_airlines(update, context)
    except ValueError:
        await update.message.reply_text("❌ Пожалуйста, введите число.")
        return SELECT_PRICE

async def show_airlines(update, context):
    """Показывает список авиакомпаний для фильтрации."""
    keyboard = []
    for airline in AIRLINES:
        keyboard.append([InlineKeyboardButton(airline, callback_data=f"airline_{airline}")])
    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="new_search")])
    if isinstance(update, Update) and update.callback_query:
        await update.callback_query.edit_message_text(
            "✈️ Выберите авиакомпанию (или 'Не важно' для всех):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            "✈️ Выберите авиакомпанию (или 'Не важно' для всех):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return SELECT_AIRLINE

async def select_airline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    airline = query.data.split("_")[1]
    if airline == "Не важно":
        airline = None
    context.user_data['airline'] = airline
    # Завершаем подписку
    await finish_subscription(update, context)
    return ConversationHandler.END

async def finish_subscription(update, context):
    """Сохраняет подписку и сразу показывает билеты, если есть."""
    user_id = update.effective_user.id
    sub_type = context.user_data.get('sub_type')
    origin = context.user_data.get('origin')
    dest = context.user_data.get('dest')
    date_from = context.user_data.get('date_from')
    date_to = context.user_data.get('date_to')
    max_price = context.user_data.get('max_price')
    category = 'SRC' if sub_type == 'subsidy' else None
    airline = context.user_data.get('airline')
    # Добавляем подписку
    add_subscription(user_id, sub_type, origin, dest, date_from, date_to, max_price, category, airline)
    # Получаем id последней добавленной подписки
    subs = get_subscriptions(active_only=False)
    user_subs = [s for s in subs if s[1] == user_id]
    sub_id = user_subs[-1][0] if user_subs else None
    # Очищаем данные
    context.user_data.clear()
    # Отправляем сообщение об успехе
    if update.callback_query:
        await update.callback_query.edit_message_text(
            f"✅ Подписка добавлена!\n"
            f"📍 {origin} → {dest}\n"
            f"📅 {date_from} – {date_to}\n"
            f"Тип: {'Субсидия' if sub_type=='subsidy' else 'Aviasales'}"
        )
    else:
        await update.message.reply_text("✅ Подписка добавлена!")
    # Проверяем и показываем билеты сразу
    if sub_id:
        await show_immediate_tickets(update, context, sub_id)

# ---------- Список подписок ----------
async def my_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    subs = get_subscriptions(active_only=False)
    user_subs = [s for s in subs if s[1] == user_id]
    if not user_subs:
        await query.edit_message_text("📭 У вас нет подписок.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_main")]]))
        return
    text = "📋 Ваши подписки:\n\n"
    keyboard = []
    for s in user_subs:
        sub_id, _, sub_type, origin, dest, date_from, date_to, max_price, category, airline = s
        status = "✅ Активна" if s[7] == 1 else "⏸ Приостановлена"
        text += f"#{sub_id}: {origin}→{dest}, {date_from}–{date_to}\n   {status}\n"
        row = []
        if s[7] == 1:
            row.append(InlineKeyboardButton("⏸", callback_data=f"pause_{sub_id}"))
        else:
            row.append(InlineKeyboardButton("▶️", callback_data=f"resume_{sub_id}"))
        row.append(InlineKeyboardButton("❌", callback_data=f"delete_{sub_id}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_main")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def manage_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("_")
    action = parts[0]
    sub_id = int(parts[1])
    user_id = update.effective_user.id
    if action == "pause":
        toggle_subscription(sub_id, user_id, 0)
        await query.edit_message_text("⏸ Подписка приостановлена.")
    elif action == "resume":
        toggle_subscription(sub_id, user_id, 1)
        await query.edit_message_text("▶️ Подписка возобновлена.")
    elif action == "delete":
        delete_subscription(sub_id, user_id)
        await query.edit_message_text("❌ Подписка удалена.")
    # Обновим список подписок
    await my_subscriptions(update, context)

# ---------- Остальные кнопки ----------
async def pause_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    subs = get_subscriptions(active_only=False)
    for s in subs:
        if s[1] == user_id and s[7] == 1:
            toggle_subscription(s[0], user_id, 0)
    await query.edit_message_text("⏸ Все подписки приостановлены.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_main")]]))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🤖 Бот помогает отслеживать:\n"
        "1️⃣ Субсидированные билеты Аэрофлота – уведомление при появлении мест\n"
        "2️⃣ Дешёвые билеты через Aviasales – при падении цены ниже порога\n\n"
        "Используйте кнопки для настройки. Все данные сохраняются в базе.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_main")]])
    )

async def back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # Просто показываем главное меню через start
    await start(update, context)

# ---------- Вспомогательная функция для получения подписки по id ----------
def get_subscription_by_id(sub_id):
    with db_lock:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT user_id, type, origin, destination, date_from, date_to, max_price, category, airline FROM subscriptions WHERE id=?", (sub_id,))
        row = c.fetchone()
        conn.close()
        return row

# ---------- Периодическая проверка ----------
def check_all_subscriptions(context: ContextTypes.DEFAULT_TYPE):
    subs = get_subscriptions(active_only=True)
    for sub in subs:
        sub_id, user_id, sub_type, origin, dest, date_from, date_to, max_price, category, airline = sub
        if sub_type == 'subsidy':
            tickets = fetch_subsidy_flights(origin, dest, date_from, date_to, category)
            for ticket in tickets:
                add_found_ticket(sub_id, ticket["date"], ticket["price"], ticket["link"])
                keyboard = [[InlineKeyboardButton("✈️ Купить", url=ticket["link"])]]
                context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎫 СУБСИДИРОВАННЫЙ БИЛЕТ ПОЯВИЛСЯ!\n\n"
                         f"📍 {origin} → {dest}\n"
                         f"📅 {ticket['date']}\n"
                         f"💵 Цена фиксированная (льготная)\n"
                         f"❗ Успейте купить, места ограничены!",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        elif sub_type == 'aviasales':
            result = fetch_aviasales_price(origin, dest, date_from, date_to)
            if result and max_price and result["price"] <= max_price:
                add_found_ticket(sub_id, result["date"], result["price"], None)
                keyboard = [[InlineKeyboardButton("✈️ Купить", url=f"https://www.aviasales.ru/search/{origin}{dest}{result['date']}")]]
                context.bot.send_message(
                    chat_id=user_id,
                    text=f"💰 НАЙДЕН БИЛЕТ ПО ВАШЕЙ ЦЕНЕ!\n\n"
                         f"📍 {origin} → {dest}\n"
                         f"📅 {result['date']}\n"
                         f"💵 {result['price']} ₽ (ваш порог: {max_price} ₽)\n"
                         f"🔄 Цена может вырасти!",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

# ---------- Главная функция ----------
def main():
    init_db()
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # ConversationHandler для нового поиска
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_new_search, pattern="^new_search$")],
        states={
            SELECT_TYPE: [CallbackQueryHandler(select_type, pattern="^type_")],
            SELECT_ORIGIN: [
                CallbackQueryHandler(select_origin, pattern="^origin_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_city_origin),
            ],
            SELECT_DEST: [
                CallbackQueryHandler(select_destination, pattern="^dest_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_city_dest),
            ],
            SELECT_DATE_FROM: [CallbackQueryHandler(calendar_callback, pattern="^(date_from|ignore|date_from_prev|date_from_next)")],
            SELECT_DATE_TO: [CallbackQueryHandler(calendar_callback, pattern="^(date_to|ignore|date_to_prev|date_to_next)")],
            SELECT_PRICE: [
                CallbackQueryHandler(select_price, pattern="^price_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_price_input),
            ],
            SELECT_AIRLINE: [CallbackQueryHandler(select_airline, pattern="^airline_")],
            ENTER_CITY_ORIGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_city_origin)],
            ENTER_CITY_DEST: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_city_dest)],
        },
        fallbacks=[CommandHandler("start", start), CallbackQueryHandler(back_main, pattern="^back_main$")],
        per_user=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(start_new_search, pattern="^new_search$"))
    application.add_handler(CallbackQueryHandler(select_type, pattern="^type_"))
    application.add_handler(CallbackQueryHandler(select_origin, pattern="^origin_"))
    application.add_handler(CallbackQueryHandler(select_destination, pattern="^dest_"))
    application.add_handler(CallbackQueryHandler(calendar_callback, pattern="^(date_from|date_to|ignore|date_from_prev|date_from_next|date_to_prev|date_to_next)"))
    application.add_handler(CallbackQueryHandler(select_price, pattern="^price_"))
    application.add_handler(CallbackQueryHandler(select_airline, pattern="^airline_"))
    application.add_handler(CallbackQueryHandler(my_subscriptions, pattern="^my_subs$"))
    application.add_handler(CallbackQueryHandler(manage_subscription, pattern="^(pause|resume|delete)_"))
    application.add_handler(CallbackQueryHandler(pause_all, pattern="^pause_all$"))
    application.add_handler(CallbackQueryHandler(help_command, pattern="^help$"))
    application.add_handler(CallbackQueryHandler(back_main, pattern="^back_main$"))
    application.add_handler(conv_handler)

    # Планировщик
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_all_subscriptions, 'interval', minutes=CHECK_INTERVAL_MINUTES, args=[application])
    scheduler.start()

    application.run_polling()

if __name__ == "__main__":
    main()
