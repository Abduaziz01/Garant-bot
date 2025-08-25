# -*- coding: utf-8 -*-
"""
SteleBot Pro - улучшенная версия (один файл)
TeleBot (pyTelegramBotAPI) + SQLite
Требования реализованы:
 - Только кнопки (inline + кастомные reply клавиатуры где нужно).
 - Полный рабочий поток: кошелёк продавца, привязка канала (опционально), создание сделки с выбором категории,
   покупатель присоединяется по deep-link, видит адрес продавца, подтверждает оплату, продавец завершает/отменяет.
 - Хранение данных в SQLite, экспорт CSV, резервная копия БД, логирование.
 - Звукоустойчивый код с проверками и понятными ответами.
 - Место для интеграции автоматической проверки платежа (stub).
 - Всё в одном файле.
Запуск:
  pip install pyTelegramBotAPI
  export TG_BOT_TOKEN="123456789:ABC..."   # или подставьте токен в переменную TG_BOT_TOKEN
  python stelebot_pro.py
"""
from __future__ import annotations

import os
import sys
import sqlite3
import time
import datetime as dt
import logging
import csv
import threading
import signal
from typing import Optional, Dict, Any, List, Tuple

import telebot
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    Message,
    CallbackQuery,
)

# ---------------------------- Настройки ----------------------------
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "7228705061:AAGKC_Q6aKmchVgZ6XGhflhOfXjjcY0Gmdo")
DB_PATH = os.getenv("STELEBOT_DB", "stelebot_pro.db")
BACKUP_DIR = os.getenv("STELEBOT_BACKUP_DIR", "backups")
LOG_LEVEL = os.getenv("STELEBOT_LOG_LEVEL", "INFO")
POLL_INTERVAL_SECONDS = 1.0

# Опционально включить stub авто-проверки платежа (False по умолчанию)
ENABLE_AUTO_PAYMENT_CHECK = False
# Частота авто-проверки в секундах
AUTO_CHECK_INTERVAL = 30

# ---------------------------- Логирование ----------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("SteleBotPro")

# ---------------------------- Инициализация бота ----------------------------
if ":" not in TG_BOT_TOKEN:
    log.error("TG_BOT_TOKEN неверен. Задайте токен BotFather в переменной окружения TG_BOT_TOKEN")
    # Не падаем, позволим получить ошибку при bot.get_me()
bot = telebot.TeleBot(TG_BOT_TOKEN, parse_mode="HTML", threaded=True)

# ---------------------------- База данных ----------------------------
_sql_lock = threading.Lock()


def _get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _sql_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.executescript(
            """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    wallet      TEXT,
    channel_id  INTEGER,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS deals (
    deal_id     TEXT PRIMARY KEY,
    seller_id   INTEGER NOT NULL,
    buyer_id    INTEGER,
    category    TEXT,
    item_info   TEXT,
    description TEXT,
    amount      REAL NOT NULL,
    status      TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_deals_seller ON deals(seller_id);
CREATE INDEX IF NOT EXISTS idx_deals_buyer ON deals(buyer_id);
"""
        )
        conn.commit()
        conn.close()


init_db()

# ---------------------------- Утилиты DB ----------------------------


def ensure_user_row(user_id: int, username: Optional[str] = None):
    with _sql_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO users(user_id, username, created_at) VALUES (?, ?, ?)",
                    (user_id, username, int(time.time())))
        if username:
            cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
        conn.commit()
        conn.close()


def set_user_wallet(user_id: int, wallet: str):
    with _sql_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE users SET wallet=? WHERE user_id=?", (wallet, user_id))
        conn.commit()
        conn.close()


def set_user_channel(user_id: int, channel_id: int):
    with _sql_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE users SET channel_id=? WHERE user_id=?", (channel_id, user_id))
        conn.commit()
        conn.close()


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with _sql_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        conn.close()
        return row


def create_deal(seller_id: int, amount: float, category: str, item_info: str, description: str) -> str:
    deal_id = hex(abs(hash((seller_id, amount, category, item_info, description, time.time()))))[2:14]
    now = int(time.time())
    with _sql_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO deals(deal_id, seller_id, amount, category, item_info, description, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (deal_id, seller_id, amount, category, item_info, description, "created", now, now),
        )
        conn.commit()
        conn.close()
    log.info("Created deal %s by %s", deal_id, seller_id)
    return deal_id


def get_deal(deal_id: str) -> Optional[sqlite3.Row]:
    with _sql_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM deals WHERE deal_id=?", (deal_id,))
        row = cur.fetchone()
        conn.close()
        return row


def update_deal(deal_id: str, **fields):
    if not fields:
        return
    parts = ", ".join([f"{k}=?" for k in fields.keys()])
    vals = list(fields.values())
    vals.append(int(time.time()))
    vals.append(deal_id)
    with _sql_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(f"UPDATE deals SET {parts}, updated_at=? WHERE deal_id=?", vals)
        conn.commit()
        conn.close()
    log.debug("Updated deal %s: %s", deal_id, fields)


def list_deals_by_user(uid: int) -> List[sqlite3.Row]:
    with _sql_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM deals WHERE seller_id=? OR buyer_id=? ORDER BY created_at DESC", (uid, uid))
        rows = cur.fetchall()
        conn.close()
        return list(rows)


def list_open_deals() -> List[sqlite3.Row]:
    with _sql_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM deals WHERE status IN ('created','waiting_payment') ORDER BY created_at DESC")
        rows = cur.fetchall()
        conn.close()
        return list(rows)


# ---------------------------- Backup / Export ----------------------------


def ensure_backup_dir():
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR, exist_ok=True)


def backup_db() -> str:
    ensure_backup_dir()
    ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dst = os.path.join(BACKUP_DIR, f"stelebot_backup_{ts}.db")
    with _sql_lock:
        # Simple copy
        _get_conn().close()
        try:
            # Make a complete copy using sqlite3 backup API
            src = sqlite3.connect(DB_PATH)
            dest = sqlite3.connect(dst)
            src.backup(dest)
            dest.close()
            src.close()
            log.info("Backup created: %s", dst)
            return dst
        except Exception as e:
            log.exception("Backup failed: %s", e)
            return ""


def export_deals_csv(path: str) -> None:
    with _sql_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM deals ORDER BY created_at DESC")
        rows = cur.fetchall()
        conn.close()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["deal_id", "seller_id", "buyer_id", "category", "item_info", "description", "amount", "status", "created_at", "updated_at"])
        for r in rows:
            w.writerow([r["deal_id"], r["seller_id"], r["buyer_id"], r["category"], r["item_info"], r["description"], r["amount"], r["status"], r["created_at"], r["updated_at"]])
    log.info("Exported deals to %s", path)


# ---------------------------- In-memory dialog state ----------------------------
# Формат: states[user_id] = {"step": "...", "payload": {...}}
states: Dict[int, Dict[str, Any]] = {}


def set_state(user_id: int, step: str, payload: Optional[Dict[str, Any]] = None):
    states[user_id] = {"step": step, "payload": payload or {}}
    log.debug("Set state for %s -> %s", user_id, step)


def get_state(user_id: int) -> Dict[str, Any]:
    return states.get(user_id, {"step": None, "payload": {}})


def clear_state(user_id: int):
    if user_id in states:
        del states[user_id]
        log.debug("Cleared state for %s", user_id)


# ---------------------------- Keyboards ----------------------------
def main_inline_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("➕ Добавить TON-кошелёк", callback_data="wallet"))
    kb.row(InlineKeyboardButton("💼 Создать сделку", callback_data="create_deal"))
    kb.row(InlineKeyboardButton("📂 Мои сделки", callback_data="my_deals"))
    kb.row(InlineKeyboardButton("📤 Экспорт сделок (CSV)", callback_data="export_csv"),
           InlineKeyboardButton("💾 Резервная копия", callback_data="backup_db"))
    return kb


def category_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("📢 Канал", callback_data="cat:channel"),
           InlineKeyboardButton("🎁 Gift", callback_data="cat:gift"))
    kb.row(InlineKeyboardButton("🎨 NFT", callback_data="cat:nft"),
           InlineKeyboardButton("⬅️ Отмена", callback_data="cancel"))
    return kb


def buyer_entry_keyboard(deal_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("✅ Присоединиться", callback_data=f"join:{deal_id}"),
           InlineKeyboardButton("❌ Отмена", callback_data="cancel"))
    return kb


def buyer_after_join_keyboard(deal_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("💰 Я оплатил", callback_data=f"paid:{deal_id}"),
           InlineKeyboardButton("🚪 Выйти", callback_data=f"leave:{deal_id}"))
    return kb


def seller_controls_keyboard(deal_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("🔑 Завершить сделку", callback_data=f"finish:{deal_id}"),
           InlineKeyboardButton("❌ Отменить сделку", callback_data=f"canceldeal:{deal_id}"))
    return kb


# ---------------------------- Формат карточки сделки ----------------------------
def deal_card_text(row: sqlite3.Row) -> str:
    return (
        f"💼 Сделка <code>#{row['deal_id']}</code>\n"
        f"🏷 Категория: <b>{(row['category'] or '-').upper()}</b>\n"
        f"📦 Инфо: {row['item_info'] or '-'}\n"
        f"🪙 Сумма: <b>{row['amount']}</b> TON\n"
        f"📚 Описание: {row['description'] or '-'}\n"
        f"📊 Статус: <b>{row['status']}</b>\n"
    )


# ---------------------------- Auto payment check (stub) ----------------------------
def auto_payment_checker_loop(stop_event: threading.Event):
    """Если ENABLE_AUTO_PAYMENT_CHECK=True, в этой функции реализуется цикл проверки.
    Сейчас stub: проверяет любые сделки со статусом waiting_payment и не меняет статус.
    Здесь можно интегрировать TonCenter/TonScan API для проверки реального поступления."""
    log.info("Auto payment checker started: %s", ENABLE_AUTO_PAYMENT_CHECK)
    while not stop_event.is_set():
        try:
            if ENABLE_AUTO_PAYMENT_CHECK:
                rows = list_open_deals()
                for r in rows:
                    # stub: логируем и не делаем изменений
                    log.debug("Auto-check stub for deal %s status %s", r["deal_id"], r["status"])
                    # Здесь вы бы проверяли по адресу продавца: если на адрес пришли средства в размере r['amount'],
                    # то update_deal(r['deal_id'], status='paid')
                    # И уведомляли продавца/покупателя.
            stop_event.wait(AUTO_CHECK_INTERVAL)
        except Exception as e:
            log.exception("Auto-check loop exception: %s", e)
            time.sleep(5)


# ---------------------------- Handlers ----------------------------
@bot.message_handler(commands=["start"])
def cmd_start(msg: Message):
    uid = msg.from_user.id
    username = msg.from_user.username
    ensure_user_row(uid, username)
    # deep link payload
    parts = msg.text.split(maxsplit=1)
    if len(parts) > 1:
        payload = parts[1].strip()
        if payload.startswith("deal_"):
            deal_id = payload.split("deal_", 1)[1]
        else:
            deal_id = payload
        row = get_deal(deal_id)
        if not row:
            bot.send_message(uid, "❌ Сделка не найдена.")
            return
        # Prevent seller joining own deal
        if row["seller_id"] == uid:
            bot.send_message(uid, "‼️ Вы продавец этой сделки. Нельзя присоединиться к своей сделке.")
            return
        if row["buyer_id"]:
            bot.send_message(uid, "❌ У сделки уже есть покупатель.")
            return
        bot.send_message(uid, deal_card_text(row) + "\nПрисоединиться к сделке?", reply_markup=buyer_entry_keyboard(deal_id))
        return
    # No payload: show main menu inline
    bot.send_message(uid, "👋 Добро пожаловать в SteleBot Pro. Главное меню:", reply_markup=main_inline_keyboard())


@bot.callback_query_handler(func=lambda c: True)
def callback_router(cq: CallbackQuery):
    uid = cq.from_user.id
    data = cq.data or ""
    log.debug("Callback from %s: %s", uid, data)
    try:
        bot.answer_callback_query(cq.id)
    except Exception:
        pass

    # Cancel / back
    if data == "cancel":
        clear_state(uid)
        bot.send_message(uid, "Отмена. Главное меню:", reply_markup=main_inline_keyboard())
        return

    # Add wallet flow
    if data == "wallet":
        set_state(uid, "wallet_wait")
        bot.send_message(uid, "⛓ Пришлите адрес вашего TON-кошелька (только текст).")
        return

    # Create deal entry point
    if data == "create_deal":
        user = get_user(uid)
        if not user or not user["wallet"]:
            bot.send_message(uid, "❗ Сначала укажите TON-кошелёк. Нажмите кнопку \"Добавить TON-кошелёк\".", reply_markup=main_inline_keyboard())
            return
        set_state(uid, "choose_category")
        bot.send_message(uid, "Выберите категорию товара:", reply_markup=category_keyboard())
        return

    # Category chosen
    if data.startswith("cat:"):
        cat = data.split(":", 1)[1]
        set_state(uid, "enter_item", {"category": cat})
        bot.send_message(uid, "Перешлите сообщение из канала или опишите товар/ссылку/уникальную информацию.")
        return

    # My deals
    if data == "my_deals":
        rows = list_deals_by_user(uid)
        if not rows:
            bot.send_message(uid, "У вас пока нет сделок.", reply_markup=main_inline_keyboard())
            return
        for r in rows:
            bot.send_message(uid, deal_card_text(r), reply_markup=seller_controls_keyboard(r["deal_id"]))
        return

    # Export CSV
    if data == "export_csv":
        # Create CSV in temp and send as file
        fn = f"deals_export_{int(time.time())}.csv"
        export_deals_csv(fn)
        try:
            with open(fn, "rb") as f:
                bot.send_document(uid, f)
        except Exception as e:
            bot.send_message(uid, f"Ошибка при отправке CSV: {e}")
        finally:
            try:
                os.remove(fn)
            except Exception:
                pass
        return

    # Backup DB
    if data == "backup_db":
        dst = backup_db()
        if dst:
            try:
                with open(dst, "rb") as f:
                    bot.send_document(uid, f)
            except Exception as e:
                bot.send_message(uid, f"Резервная копия создана: {dst} (ошибка при отправке: {e})")
        else:
            bot.send_message(uid, "Не удалось создать резервную копию.")
        return

    # Buyer joins
    if data.startswith("join:"):
        deal_id = data.split(":", 1)[1]
        row = get_deal(deal_id)
        if not row:
            bot.send_message(uid, "❌ Сделка не найдена.")
            return
        if row["seller_id"] == uid:
            bot.send_message(uid, "‼️ Вы продавец этой сделки.")
            return
        if row["buyer_id"]:
            bot.send_message(uid, "❌ Сделка уже занята.")
            return
        update_deal(deal_id, buyer_id=uid, status="waiting_payment")
        # Show payment instructions with seller wallet
        seller = get_user(row["seller_id"])
        wallet = seller["wallet"] if seller and seller["wallet"] else None
        text = deal_card_text(row)
        if wallet:
            text += f"\nОтправьте <b>{row['amount']} TON</b> на адрес продавца:\n<code>{wallet}</code>\n\nПосле перевода нажмите «Я оплатил»."
        else:
            text += "\nПродавец не указал TON-кошелёк. Свяжитесь с продавцом."
        bot.send_message(uid, text, reply_markup=buyer_after_join_keyboard(deal_id))
        bot.send_message(row["seller_id"], f"👤 Покупатель @{cq.from_user.username or uid} присоединился к сделке #{deal_id}.")
        return

    # Buyer "I paid"
    if data.startswith("paid:"):
        deal_id = data.split(":", 1)[1]
        row = get_deal(deal_id)
        if not row:
            bot.send_message(uid, "❌ Сделка не найдена.")
            return
        if row["buyer_id"] != uid:
            bot.send_message(uid, "❌ Только покупатель может подтвердить оплату.")
            return
        if row["status"] not in ("waiting_payment", "created"):
            bot.send_message(uid, "ℹ️ Нельзя подтвердить оплату в текущем статусе.")
            return
        update_deal(deal_id, status="paid")
        bot.send_message(uid, f"💰 Вы отметили оплату для сделки #{deal_id}. Ожидайте подтверждения продавца.")
        bot.send_message(row["seller_id"], f"💰 Покупатель подтвердил оплату по сделке #{deal_id}. Проверьте перевод и завершите сделку.")
        return

    # Buyer leave
    if data.startswith("leave:"):
        deal_id = data.split(":", 1)[1]
        row = get_deal(deal_id)
        if not row:
            bot.send_message(uid, "❌ Сделка не найдена.")
            return
        if row["buyer_id"] != uid:
            bot.send_message(uid, "❌ Вы не покупатель.")
            return
        if row["status"] in ("finished", "canceled"):
            bot.send_message(uid, "ℹ️ Сделка уже завершена или отменена.")
            return
        update_deal(deal_id, buyer_id=None, status="created")
        bot.send_message(uid, f"🚪 Вы вышли из сделки #{deal_id}.")
        bot.send_message(row["seller_id"], f"🚪 Покупатель вышел из сделки #{deal_id}.")
        return

    # Seller finish
    if data.startswith("finish:"):
        deal_id = data.split(":", 1)[1]
        row = get_deal(deal_id)
        if not row:
            bot.send_message(uid, "❌ Сделка не найдена.")
            return
        if row["seller_id"] != uid:
            bot.send_message(uid, "❌ Только продавец может завершить сделку.")
            return
        if row["status"] != "paid":
            bot.send_message(uid, "ℹ️ Сделку можно завершить только после оплаты.")
            return
        update_deal(deal_id, status="finished")
        bot.send_message(uid, f"✅ Сделка #{deal_id} завершена. Передайте доступ/товар покупателю.")
        if row["buyer_id"]:
            bot.send_message(row["buyer_id"], f"✅ Сделка #{deal_id} завершена продавцом.")
        return

    # Cancel deal by participant
    if data.startswith("canceldeal:"):
        deal_id = data.split(":", 1)[1]
        row = get_deal(deal_id)
        if not row:
            bot.send_message(uid, "❌ Сделка не найдена.")
            return
        if uid not in (row["seller_id"], row["buyer_id"] or -1):
            bot.send_message(uid, "❌ Вы не участник сделки.")
            return
        if row["status"] in ("finished", "canceled"):
            bot.send_message(uid, "ℹ️ Уже завершена или отменена.")
            return
        update_deal(deal_id, status="canceled")
        bot.send_message(uid, f"❌ Сделка #{deal_id} отменена.")
        if row["seller_id"] != uid:
            bot.send_message(row["seller_id"], f"❌ Сделка #{deal_id} отменена.")
        if row["buyer_id"] and row["buyer_id"] != uid:
            bot.send_message(row["buyer_id"], f"❌ Сделка #{deal_id} отменена.")
        return

    # Unknown callback
    bot.send_message(uid, "Неизвестное действие. Главное меню:", reply_markup=main_inline_keyboard())


@bot.message_handler(func=lambda m: True, content_types=["text", "photo", "video", "audio", "document", "sticker", "voice", "video_note", "location", "contact"])
def message_router(msg: Message):
    uid = msg.from_user.id
    u_name = msg.from_user.username or msg.from_user.first_name
    ensure_user_row(uid, u_name)
    state = get_state(uid)
    step = state.get("step")
    payload = state.get("payload", {})

    # Wallet input step
    if step == "wallet_wait" and msg.content_type == "text":
        wallet = (msg.text or "").strip()
        if not wallet:
            bot.send_message(uid, "Ошибка: пришлите текст с адресом TON-кошелька.")
            return
        set_user_wallet(uid, wallet)
        clear_state(uid)
        bot.send_message(uid, f"✅ TON-кошелёк сохранён: <code>{wallet}</code>", reply_markup=main_inline_keyboard())
        return

    # Category -> enter item info
    if step == "enter_item" or step == "enter_item_forward" or step == "enter_item_manual":
        # Accept forwarded message or plain text
        item_info = ""
        if getattr(msg, "forward_from_chat", None):
            fchat = msg.forward_from_chat
            # For channels we can store chat id and title
            item_info = f"forwarded_from_channel:{getattr(fchat,'id', '')}:{getattr(fchat,'title', '')}"
        else:
            item_info = (msg.text or "").strip()
        payload["item_info"] = item_info
        set_state(uid, "enter_amount", payload)
        bot.send_message(uid, "🪙 Введите сумму сделки в TON (пример: 100 или 99.5):")
        return

    # Amount step
    if step == "enter_amount":
        txt = (msg.text or "").strip()
        try:
            amount = float(txt.replace(",", "."))
            if amount <= 0:
                raise ValueError("negative")
        except Exception:
            bot.send_message(uid, "Введите корректную положительную сумму, например: 100 или 99.5")
            return
        payload["amount"] = amount
        set_state(uid, "enter_description", payload)
        bot.send_message(uid, "📋 Введите описание товара / условия сделки:")
        return

    # Description step -> create deal
    if step == "enter_description":
        description = (msg.text or "").strip()
        category = payload.get("category") or "other"
        item_info = payload.get("item_info") or ""
        amount = payload.get("amount") or 0.0
        deal_id = create_deal(uid, amount, category, item_info, description)
        clear_state(uid)
        link = f"https://t.me/{bot.get_me().username}?start=deal_{deal_id}"
        row = get_deal(deal_id)
        bot.send_message(uid, f"💥 Сделка создана.\n\n{deal_card_text(row)}\n\n⛓ Ссылка для покупателя: {link}", reply_markup=seller_controls_keyboard(deal_id))
        return

    # Normal text with no state -> show menu
    bot.send_message(uid, "Выберите действие:", reply_markup=main_inline_keyboard())


# ---------------------------- Graceful shutdown ----------------------------
_stop_event = threading.Event()
_auto_check_thread: Optional[threading.Thread] = None


def start_background():
    global _auto_check_thread
    if ENABLE_AUTO_PAYMENT_CHECK:
        _auto_check_thread = threading.Thread(target=auto_payment_checker_loop, args=(_stop_event,), daemon=True)
        _auto_check_thread.start()
        log.info("Auto payment checker thread started.")


def stop_background():
    _stop_event.set()
    if _auto_check_thread and _auto_check_thread.is_alive():
        _auto_check_thread.join(timeout=2)
    log.info("Background threads stopped.")


def _signal_handler(sig, frame):
    log.info("Signal %s received. Shutting down.", sig)
    stop_background()
    try:
        bot.stop_polling()
    except Exception:
        pass
    sys.exit(0)


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ---------------------------- Run ----------------------------
if __name__ == "__main__":
    log.info("SteleBot Pro starting...")
    try:
        me = bot.get_me()
        log.info("Bot ready: @%s (%s)", me.username, me.id)
    except Exception as e:
        log.exception("Unable to start bot. Check token and network. %s", e)
        raise

    start_background()

    # Long-polling loop encapsulated by library
    try:
        bot.infinity_polling(skip_pending=True, timeout=60)
    except Exception as e:
        log.exception("Polling exited with error: %s", e)
    finally:
        stop_background()
        log.info("SteleBot Pro stopped.")
