# -*- coding: utf-8 -*-
"""Telegram Text for FunPay Cardinal.

Поток: покупка -> username -> подтверждение -> текст -> проверка paid messages -> отправка.
Один файл: хранит заказы/привязки, авторизацию и Telegram worker.
"""
from __future__ import annotations

import asyncio
import html
import importlib
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

from FunPayAPI.types import MessageTypes

NAME = "Telegram Text"
VERSION = "3.1.0"
DESCRIPTION = "Автоматическая отправка текста после покупки привязанного лота."
CREDITS = "@podarckov"
UUID = "2b5d7f4a-8c31-4e96-a172-53f9d0b64c28"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

API_ID = 32493973
API_HASH = "e470a990253e9502835f62cc5958aed7"
TELETHON_PACKAGE = "telethon>=1.36,<2"
GIFTS_UUID = "7f8e2d91-4b36-4c2a-9f15-6a7d83e4b102"
GIFTS_PLUGIN_DIR = Path("storage") / "plugins" / GIFTS_UUID
SHARED_SESSION_FILE = GIFTS_PLUGIN_DIR / "telegram_gifts"
LEGACY_SESSION_FILE = Path("storage") / "plugins" / UUID / "telegram_text"
PLUGIN_DIR = Path("storage") / "plugins" / UUID
ORDERS_FILE = PLUGIN_DIR / "orders.json"
LOT_BINDINGS_FILE = PLUGIN_DIR / "lot_bindings.json"

STATUS_USERNAME = "await_username"
STATUS_CONFIRM = "await_confirm"
STATUS_TEXT = "await_text"
STATUS_PAID_GUARD = "await_paid_guard"
STATUS_SENDING = "sending"
STATUS_COMPLETED = "completed"
STATUS_REFUNDED = "refunded"
STATUS_ERROR = "error"
ACTIVE_STATUSES = (STATUS_USERNAME, STATUS_CONFIRM, STATUS_TEXT, STATUS_PAID_GUARD, STATUS_SENDING, STATUS_ERROR)
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{5,32}$")
ID_RE = re.compile(r"(?i)(?:^|[^A-Za-z0-9_])ID\s*:\s*(\d{10,25})(?:[^0-9]|$)")
MAX_TEXT = 4096

logger = logging.getLogger("telegram_text")
_cardinal = None
_worker = None
_orders = {}
_lot_bindings = {}
_auth_states = {}
_state_lock = threading.RLock()
_auth_lock = threading.RLock()
_install_lock = threading.Lock()


def clean_text(value):
    return (unicodedata.normalize("NFKC", str(value or ""))
            .replace("\u200b", "").replace("\u200c", "")
            .replace("\u200d", "").replace("\ufeff", "").strip())


def msg_text(msg):
    return clean_text(getattr(msg, "text", None) or getattr(msg, "message", None))


def normalize_username(value):
    value = clean_text(value)
    if not USERNAME_RE.fullmatch(value):
        return None
    return value if value.startswith("@") else "@" + value


def is_plus(value):
    return clean_text(value) in ("+", "＋")


def is_refund(value):
    return clean_text(value).casefold().replace(" ", "") in ("!возврат", "!refund")


def load_json(path, default):
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, type(default)) else default
    except Exception:
        logger.exception("Telegram Text: ошибка чтения %s", path)
        return default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=4)
    os.replace(tmp, path)


def persist_state():
    with _state_lock:
        save_json(ORDERS_FILE, _orders)
        save_json(LOT_BINDINGS_FILE, _lot_bindings)


def load_state():
    global _orders, _lot_bindings
    with _state_lock:
        _orders = load_json(ORDERS_FILE, {})
        _lot_bindings = load_json(LOT_BINDINGS_FILE, {})
        for order in _orders.values():
            if order.get("status") == STATUS_SENDING:
                order["status"] = STATUS_ERROR
                order["error"] = "Cardinal был перезапущен во время отправки."
        persist_state()


def get_order(oid):
    with _state_lock:
        item = _orders.get(str(oid))
        return dict(item) if item else None


def update_order(oid, **fields):
    with _state_lock:
        item = _orders.get(str(oid))
        if not item:
            return None
        item.update(fields)
        item["updated_at"] = time.time()
        persist_state()
        return dict(item)


def send_funpay(chat_id, text):
    if _cardinal is None or chat_id is None:
        return False
    try:
        _cardinal.send_message(chat_id, text)
        return True
    except Exception:
        logger.exception("Telegram Text: ошибка отправки сообщения FunPay")
        return False


def panel_send(message, text):
    try:
        bot = getattr(getattr(_cardinal, "telegram", None), "bot", None)
        if bot:
            bot.send_message(message.chat.id, text, parse_mode="HTML")
            return True
    except Exception:
        logger.exception("Telegram Text: ошибка сообщения панели")
    return False


def authorized(message):
    try:
        return int(message.from_user.id) in _cardinal.telegram.authorized_users
    except Exception:
        return False


def extract_id(value):
    match = ID_RE.search(str(value or ""))
    return int(match.group(1)) if match else None


def lot_id_from_object(obj):
    if obj is None:
        return None
    if isinstance(obj, dict):
        for key in ("lot_id", "lotId", "offer_id", "offerId", "id"):
            if obj.get(key) is not None:
                try:
                    return str(int(obj[key]))
                except Exception:
                    pass
    for attr in ("lot_id", "lotId", "offer_id", "offerId"):
        value = getattr(obj, attr, None)
        if value is not None:
            try:
                return str(int(value))
            except Exception:
                return str(value)
    return None


def get_full_order(c, event):
    try:
        return c.account.get_order(event.order.id)
    except Exception:
        return getattr(event, "order", event)


def find_order_lot(c, event, order):
    for obj in (order, getattr(event, "order", None), getattr(order, "offer", None), getattr(order, "lot", None), getattr(order, "shortcut", None), getattr(order, "order_shortcut", None)):
        value = lot_id_from_object(obj)
        if value is not None:
            return value
    return None


def fetch_lot_description(c, lot_id):
    if lot_id is None:
        return ""
    for name in ("get_lot_page", "get_lot_fields"):
        method = getattr(c.account, name, None)
        if not callable(method):
            continue
        try:
            obj = method(int(lot_id))
        except Exception:
            continue
        if isinstance(obj, dict):
            for key in ("full_description", "description", "text"):
                if obj.get(key):
                    return clean_text(obj[key])
        for attr in ("full_description", "description_ru", "description_en", "description", "text"):
            value = getattr(obj, attr, None)
            if value:
                return clean_text(value)
    return ""


def resolve_text_lot(c, event, order):
    lot_id = find_order_lot(c, event, order)
    if lot_id is not None:
        with _state_lock:
            binding = _lot_bindings.get(str(lot_id))
        if isinstance(binding, dict) and binding.get("enabled", True):
            return str(lot_id), "binding"
    for value in (fetch_lot_description(c, lot_id), getattr(order, "full_description", None), getattr(order, "description", None), getattr(order, "short_description", None), getattr(getattr(event, "order", None), "full_description", None), getattr(getattr(event, "order", None), "description", None)):
        if extract_id(value) is not None:
            return str(lot_id) if lot_id else str(extract_id(value)), "description_id"
    return None


def find_order_by_message(msg):
    chat_id = getattr(msg, "chat_id", None)
    author_id = getattr(msg, "author_id", None)
    author = clean_text(getattr(msg, "author", None)).lstrip("@").casefold()
    with _state_lock:
        candidates = [(oid, o) for oid, o in _orders.items() if o.get("status") in ACTIVE_STATUSES and chat_id is not None and str(o.get("chat_id")) == str(chat_id)]
        if candidates:
            candidates.sort(key=lambda x: float(x[1].get("created_at", 0)), reverse=True)
            return candidates[0]
        if author_id is not None:
            for oid, o in sorted(_orders.items(), key=lambda x: float(x[1].get("created_at", 0)), reverse=True):
                if o.get("status") in ACTIVE_STATUSES and o.get("buyer_id") is not None and str(o.get("buyer_id")) == str(author_id):
                    return oid, o
        if author:
            for oid, o in sorted(_orders.items(), key=lambda x: float(x[1].get("created_at", 0)), reverse=True):
                if o.get("status") in ACTIVE_STATUSES and clean_text(o.get("buyer", "")).lstrip("@").casefold() == author:
                    return oid, o
    return None, None


def refund_order(oid):
    order = get_order(oid)
    if not order:
        return False
    if order.get("status") == STATUS_COMPLETED:
        send_funpay(order["chat_id"], "ℹ️ Сообщение уже успешно отправлено. Возврат после выдачи недоступен.")
        return False
    if order.get("status") == STATUS_REFUNDED:
        send_funpay(order["chat_id"], "ℹ️ По этому заказу возврат уже выполнен.")
        return False
    if order.get("status") == STATUS_SENDING:
        send_funpay(order["chat_id"], "⏳ Сообщение уже отправляется. Дождитесь результата.")
        return False
    try:
        _cardinal.account.refund(str(oid))
        update_order(oid, status=STATUS_REFUNDED, error=None, refunded_at=time.time())
        send_funpay(order["chat_id"], "❌ Заказ отменён.\n\nСредства возвращены.")
        return True
    except Exception:
        logger.exception("Telegram Text: ошибка возврата order=%s", oid)
        send_funpay(order["chat_id"], "⚠️ Не удалось автоматически оформить возврат.\n\nПродавец обработает возврат вручную.")
        return False


def ensure_telethon():
    if importlib.util.find_spec("telethon") is not None:
        return True
    with _install_lock:
        if importlib.util.find_spec("telethon") is not None:
            return True
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", TELETHON_PACKAGE])
        except Exception:
            logger.exception("Telegram Text: не удалось установить Telethon")
            return False
    return importlib.util.find_spec("telethon") is not None


class TelegramWorker:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.queue = None
        self.client = None
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.session_path = None

    def choose_session(self):
        for path in (SHARED_SESSION_FILE, Path(str(SHARED_SESSION_FILE) + ".session")):
            if path.exists():
                return SHARED_SESSION_FILE
        for path in (LEGACY_SESSION_FILE, Path(str(LEGACY_SESSION_FILE) + ".session")):
            if path.exists():
                return LEGACY_SESSION_FILE
        return SHARED_SESSION_FILE

    def running(self):
        return bool(self.thread and self.thread.is_alive() and self.loop and self.loop.is_running())

    def start(self):
        with self.lock:
            if self.running():
                return True
            self.stop_event.clear()
            self.ready.clear()
            self.thread = threading.Thread(target=self._thread_main, name="telegram-text-worker", daemon=True)
            self.thread.start()
        return self.ready.wait(15) and self.running()

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.queue = asyncio.Queue()
        self.loop.create_task(self._queue_loop())
        self.ready.set()
        try:
            self.loop.run_forever()
        except Exception:
            logger.exception("Telegram Text: worker loop crashed")
        finally:
            try:
                pending = asyncio.all_tasks(self.loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                self.loop.close()
            except Exception:
                pass
            self.loop = None

    async def _queue_loop(self):
        while not self.stop_event.is_set():
            try:
                oid = await asyncio.wait_for(self.queue.get(), timeout=.5)
            except asyncio.TimeoutError:
                continue
            try:
                await self._process(str(oid))
            except Exception:
                logger.exception("Telegram Text: ошибка обработки order=%s", oid)
                update_order(oid, status=STATUS_ERROR, error="worker_exception")

    async def _get_client(self):
        if not ensure_telethon():
            raise RuntimeError("Telethon не установлен")
        if self.client is None:
            telethon = importlib.import_module("telethon")
            self.session_path = self.choose_session()
            self.client = telethon.TelegramClient(str(self.session_path), API_ID, API_HASH, device_model="FunPay Cardinal Telegram Text", system_version="Linux", app_version=VERSION, lang_code="en", system_lang_code="en-US")
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def account_info(self):
        client = await self._get_client()
        if not await client.is_user_authorized():
            return None
        me = await client.get_me()
        return {"id": me.id, "username": me.username, "phone": me.phone, "name": " ".join(x for x in (me.first_name, me.last_name) if x) or "—"}

    async def send_code(self, phone):
        client = await self._get_client()
        if await client.is_user_authorized():
            return "authorized"
        result = await client.send_code_request(phone)
        return result.phone_code_hash

    async def sign_code(self, phone, code, phone_code_hash):
        client = await self._get_client()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            return "authorized"
        except Exception as exc:
            if type(exc).__name__ == "SessionPasswordNeededError":
                return "2fa"
            raise

    async def sign_password(self, password):
        client = await self._get_client()
        await client.sign_in(password=password)
        return "authorized"

    async def paid_message_required(self, username):
        """True only when Telegram says this exact recipient currently requires Stars.

        user.send_paid_messages_stars > 0 only tells us that paid messages are enabled.
        userFull.send_paid_messages_stars is the authoritative check for whether our
        current account actually has to pay. A value of 0 means exempt; absent means
        no paid-message requirement.
        """
        client = await self._get_client()
        entity = await client.get_entity(username)
        user_flag = getattr(entity, "send_paid_messages_stars", None)
        if not user_flag:
            return False, 0
        try:
            from telethon import functions
            full = await client(functions.users.GetFullUserRequest(id=entity))
            required = getattr(getattr(full, "full_user", None), "send_paid_messages_stars", None)
            if required is None:
                required = getattr(full, "send_paid_messages_stars", None)
            if required is None:
                return False, 0
            required = int(required)
            return required > 0, required
        except Exception:
            logger.exception("Telegram Text: не удалось проверить paid messages для %s", username)
            # Без достоверной проверки не отправляем, чтобы не рисковать Stars.
            raise RuntimeError("Не удалось безопасно проверить платные сообщения Telegram")

    async def send_text(self, username, text):
        client = await self._get_client()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram-аккаунт не авторизован")
        required, stars = await self.paid_message_required(username)
        if required:
            raise PaidMessagesRequired(stars)
        await client.send_message(username, text)

    async def _process(self, oid):
        order = get_order(oid)
        if not order or order.get("status") not in (STATUS_SENDING,):
            return
        try:
            await self.send_text(order["username"], order["text"])
        except PaidMessagesRequired as exc:
            update_order(oid, status=STATUS_PAID_GUARD, error=f"paid_messages:{exc.stars}", paid_stars=exc.stars)
            amount = f" ({exc.stars} ⭐️)" if exc.stars else ""
            send_funpay(order["chat_id"], "⚠️ У этого пользователя включена плата за входящие сообщения" + amount + ".\n\nПожалуйста, отключите плату за сообщения в Telegram, после отключения отправьте «+».\n\nПодарок/сообщение не отправлено.")
            return
        except Exception as exc:
            name = type(exc).__name__
            upper = str(exc).upper()
            if name in {"UsernameInvalidError", "UsernameNotOccupiedError", "PeerIdInvalidError", "UserIdInvalidError"}:
                update_order(oid, status=STATUS_USERNAME, error=name)
                send_funpay(order["chat_id"], "❌ Telegram не нашёл этот username. Отправьте другой @username.")
                return
            if "ALLOW_PAYMENT_REQUIRED" in upper or "PAID_MESSAGES" in upper and "REQUIRED" in upper:
                update_order(oid, status=STATUS_PAID_GUARD, error=name)
                send_funpay(order["chat_id"], "⚠️ Telegram всё ещё требует оплату за сообщение.\n\nПожалуйста, отключите плату за сообщения и отправьте «+». Сообщение не отправлено.")
                return
            if name == "FloodWaitError":
                seconds = int(getattr(exc, "seconds", 0))
                if seconds <= 300:
                    send_funpay(order["chat_id"], f"⏳ Telegram попросил подождать {seconds} сек. Повторяю автоматически.")
                    await asyncio.sleep(seconds)
                    current = get_order(oid)
                    if current and current.get("status") == STATUS_SENDING:
                        await self._process(oid)
                    return
                update_order(oid, status=STATUS_ERROR, error=f"FloodWait:{seconds}")
                send_funpay(order["chat_id"], "❌ Telegram временно ограничил отправку. Напишите + позже или !возврат.")
                return
            update_order(oid, status=STATUS_ERROR, error=name)
            send_funpay(order["chat_id"], "❌ Не удалось отправить сообщение. Напишите + для повторной попытки или !возврат.")
            return
        update_order(oid, status=STATUS_COMPLETED, completed_at=time.time(), error=None)
        send_funpay(order["chat_id"], "✅ Сообщение успешно отправлено! Спасибо за покупку ❤️ Пожалуйста, подтвердите заказ и оставьте отзыв!")

    def submit(self, oid):
        if not self.running():
            raise RuntimeError("Telegram worker не запущен")
        asyncio.run_coroutine_threadsafe(self.queue.put(str(oid)), self.loop)

    def call(self, factory, timeout=90):
        if not self.running():
            raise RuntimeError("Telegram worker не запущен")
        return asyncio.run_coroutine_threadsafe(factory(), self.loop).result(timeout=timeout)

    async def reset_session(self):
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    def stop(self):
        self.stop_event.set()
        if self.loop and self.loop.is_running():
            async def close():
                if self.client is not None:
                    try:
                        await self.client.disconnect()
                    except Exception:
                        pass
                    self.client = None
                self.loop.stop()
            self.loop.call_soon_threadsafe(lambda: asyncio.create_task(close()))
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=10)
        self.thread = None


class PaidMessagesRequired(Exception):
    def __init__(self, stars=0):
        self.stars = int(stars or 0)
        super().__init__(f"paid_messages:{self.stars}")


def ensure_worker():
    global _worker
    if _worker is None:
        _worker = TelegramWorker()
    return _worker.start()


def start_auth(message):
    uid = int(message.from_user.id)
    with _auth_lock:
        _auth_states[uid] = {"state": "phone", "chat_id": message.chat.id, "created_at": time.time()}
    panel_send(message, "📱 Введите номер Telegram в международном формате, например <code>+79991234567</code>")


def command_account(message):
    if not authorized(message): return
    if not ensure_worker():
        panel_send(message, "❌ Telegram worker не удалось запустить.")
        return
    try:
        info = _worker.call(lambda: _worker.account_info(), 30)
    except Exception as exc:
        logger.exception("Telegram Text: account info")
        panel_send(message, f"⚠️ Не удалось проверить текущую session: {type(exc).__name__}.\n\nПопробуйте /text_account_reset.")
        return
    if info:
        username = "@" + info["username"] if info.get("username") else "нет username"
        panel_send(message, f"📱 <b>Telegram-аккаунт подключен</b>\n\n👤 {html.escape(info.get('name') or '—')}\n🔗 {html.escape(username)}\n🆔 <code>{info.get('id')}</code>\n📞 <code>{html.escape(str(info.get('phone') or '—'))}</code>\n\nИспользуется общая session Telegram Gifts.")
    else:
        start_auth(message)


def command_account_reset(message):
    if not authorized(message): return
    if not ensure_worker():
        panel_send(message, "❌ Telegram worker не удалось запустить.")
        return
    try:
        _worker.call(lambda: _worker.reset_session(), 30)
    except Exception:
        logger.exception("Telegram Text: reset session")
    start_auth(message)


def auth_message(message):
    if not authorized(message): return False
    uid = int(message.from_user.id)
    with _auth_lock:
        data = dict(_auth_states.get(uid) or {})
    if not data: return False
    text = msg_text(message)
    if not text or text.startswith("/"): return False
    if not ensure_worker():
        panel_send(message, "❌ Telegram worker не запущен.")
        return True
    try:
        state = data.get("state")
        if state == "phone":
            phone = re.sub(r"[\s()\-]", "", text)
            if not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
                panel_send(message, "❌ Неверный номер. Формат: +79991234567")
                return True
            result = _worker.call(lambda: _worker.send_code(phone), 90)
            if result == "authorized":
                with _auth_lock: _auth_states.pop(uid, None)
                panel_send(message, "✅ Telegram-аккаунт уже авторизован.")
                return True
            data.update(phone=phone, phone_code_hash=result, state="code")
            with _auth_lock: _auth_states[uid] = data
            panel_send(message, "📨 Код отправлен Telegram. Введите код одним сообщением.")
            return True
        if state == "code":
            code = re.sub(r"\s", "", text)
            if not code.isdigit():
                panel_send(message, "❌ Код должен состоять из цифр.")
                return True
            result = _worker.call(lambda: _worker.sign_code(data["phone"], code, data["phone_code_hash"]), 90)
            if result == "2fa":
                data["state"] = "2fa"
                with _auth_lock: _auth_states[uid] = data
                panel_send(message, "🔐 Введите облачный пароль Telegram 2FA.")
            else:
                with _auth_lock: _auth_states.pop(uid, None)
                panel_send(message, "✅ Telegram-аккаунт успешно подключен.")
            return True
        if state == "2fa":
            _worker.call(lambda: _worker.sign_password(text), 90)
            with _auth_lock: _auth_states.pop(uid, None)
            panel_send(message, "✅ Telegram-аккаунт успешно подключен.")
            return True
    except Exception as exc:
        name = type(exc).__name__
        logger.exception("Telegram Text: auth error")
        messages = {"PhoneCodeInvalidError":"❌ Неверный код. Запустите /text_account заново.","PhoneCodeExpiredError":"❌ Код устарел. Запустите /text_account заново.","PhoneNumberInvalidError":"❌ Неверный номер Telegram.","PhoneNumberBannedError":"❌ Этот номер заблокирован Telegram.","PasswordHashInvalidError":"❌ Неверный пароль 2FA.","ApiIdInvalidError":"❌ Telegram отклонил API ID/API HASH.","ApiIdPublishedFloodError":"❌ Telegram ограничил этот API ID. Попробуйте позже."}
        panel_send(message, messages.get(name, f"❌ Ошибка авторизации: {name}"))
        with _auth_lock: _auth_states.pop(uid, None)
        return True
    return False


def command_lots(message):
    if not authorized(message): return
    with _state_lock: bindings = dict(_lot_bindings)
    if not bindings:
        panel_send(message, "📦 Привязанных лотов Telegram Text нет.")
        return
    lines = ["📦 <b>Привязанные лоты Telegram Text</b>", ""]
    for lid, data in sorted(bindings.items(), key=lambda x: str(x[0])):
        title = data.get("title", "") if isinstance(data, dict) else ""
        lines.append(f"• <code>{html.escape(str(lid))}</code>" + (f" — {html.escape(title)}" if title else ""))
    panel_send(message, "\n".join(lines))


def command_bind(message):
    if not authorized(message): return
    parts = msg_text(message).split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        panel_send(message, "Использование: /text_bind LOT_ID [название]")
        return
    lid = str(int(parts[1])); title = parts[2] if len(parts) > 2 else ""
    with _state_lock:
        _lot_bindings[lid] = {"title": title, "enabled": True, "updated_at": time.time()}
        persist_state()
    panel_send(message, f"✅ Лот <code>{lid}</code> привязан к Telegram Text.")


def command_unbind(message):
    if not authorized(message): return
    parts = msg_text(message).split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        panel_send(message, "Использование: /text_unbind LOT_ID")
        return
    lid = str(int(parts[1]))
    with _state_lock:
        existed = _lot_bindings.pop(lid, None)
        persist_state()
    panel_send(message, "✅ Лот отвязан." if existed else "ℹ️ Такой лот не был привязан.")


def new_order_handler(c, e):
    try:
        order = get_full_order(c, e)
        oid = str(getattr(order, "id", None) or getattr(e.order, "id", None) or "")
        if not oid: return
        with _state_lock:
            if oid in _orders: return
        resolved = resolve_text_lot(c, e, order)
        if not resolved: return
        lot_id, source = resolved
        chat_id = getattr(e.order, "chat_id", None) or getattr(order, "chat_id", None)
        buyer = getattr(order, "buyer_username", None) or getattr(e.order, "buyer_username", None) or getattr(order, "buyer", None) or ""
        buyer_id = getattr(order, "buyer_id", None) or getattr(e.order, "buyer_id", None) or getattr(order, "buyer_user_id", None)
        if chat_id is None and buyer:
            try:
                chat = c.account.get_chat_by_name(buyer, True)
                chat_id = chat.id if chat else None
            except Exception:
                logger.debug("Telegram Text: не удалось получить чат", exc_info=True)
        if chat_id is None:
            logger.error("Telegram Text: у заказа %s отсутствует chat_id", oid)
            return
        record = {"order_id":oid,"lot_id":lot_id,"lot_source":source,"chat_id":chat_id,"buyer":buyer,"buyer_id":buyer_id,"username":None,"text":None,"status":STATUS_USERNAME,"error":None,"created_at":time.time()}
        with _state_lock:
            _orders[oid] = record
            persist_state()
        send_funpay(chat_id, "👋 Спасибо за покупку!\n\nОтправьте Telegram username, на который нужно отправить текст.\n\nПример: @username\n\n❗ Для отмены заказа отправьте: !возврат")
    except Exception:
        logger.exception("Telegram Text: ошибка обработки нового заказа")


def message_handler(c, e):
    try:
        msg = e.message
        if getattr(msg, "type", None) != MessageTypes.NON_SYSTEM: return
        author_id = getattr(msg, "author_id", None)
        if author_id is not None and getattr(c.account, "id", None) is not None and str(author_id) == str(c.account.id): return
        text = msg_text(msg)
        if not text: return
        oid, order = find_order_by_message(msg)
        if not order: return
        stored_buyer_id = order.get("buyer_id")
        if stored_buyer_id is not None and author_id is not None and str(stored_buyer_id) != str(author_id): return
        chat_id = getattr(msg, "chat_id", None) or order.get("chat_id")
        status = order.get("status")

        if is_refund(text):
            refund_order(oid); return
        if status == STATUS_SENDING:
            send_funpay(chat_id, "⏳ Сообщение уже отправляется. Пожалуйста, подождите."); return
        if status == STATUS_USERNAME:
            username = normalize_username(text)
            if not username:
                send_funpay(chat_id, "❌ Некорректный Telegram username. Отправьте @username или !возврат."); return
            update_order(oid, username=username, status=STATUS_CONFIRM, error=None)
            send_funpay(chat_id, f"📋 Получатель: {username}\n\nЕсли всё верно — отправьте «+».\nЕсли хотите изменить — отправьте новый username.\n\n❌ Для возврата: !возврат"); return
        if status == STATUS_CONFIRM:
            if is_plus(text):
                update_order(oid, status=STATUS_TEXT, error=None)
                send_funpay(chat_id, "💬 Отлично. Теперь отправьте текст, который нужно отправить этому пользователю.\n\nСледующее сообщение будет отправлено как текст.\n❗ Для отмены: !возврат"); return
            username = normalize_username(text)
            if username:
                update_order(oid, username=username, status=STATUS_CONFIRM, error=None)
                send_funpay(chat_id, f"📋 Получатель изменён: {username}\n\nЕсли всё верно — отправьте «+».\nДля возврата: !возврат"); return
            send_funpay(chat_id, "❓ Отправьте «+» для подтверждения, новый @username или !возврат."); return
        if status == STATUS_TEXT:
            if len(text) > MAX_TEXT:
                send_funpay(chat_id, "❌ Максимальная длина текста — 4096 символов. Сократите текст и отправьте его снова."); return
            update_order(oid, text=text, status=STATUS_SENDING, error=None)
            if not ensure_worker():
                update_order(oid, status=STATUS_ERROR, error="worker_unavailable")
                send_funpay(chat_id, "❌ Telegram-модуль не удалось запустить. Напишите + для повторной попытки или !возврат."); return
            try: _worker.submit(oid)
            except Exception:
                update_order(oid, status=STATUS_ERROR, error="submit_failed")
                send_funpay(chat_id, "❌ Не удалось запустить отправку. Напишите + для повторной попытки или !возврат."); return
            send_funpay(chat_id, "⏳ Проверяю возможность отправки и отправляю сообщение..."); return
        if status == STATUS_PAID_GUARD:
            if is_plus(text):
                if not ensure_worker():
                    send_funpay(chat_id, "❌ Telegram-модуль не удалось запустить."); return
                update_order(oid, status=STATUS_SENDING, error=None)
                _worker.submit(oid)
                send_funpay(chat_id, "⏳ Повторно проверяю оплату за сообщения..."); return
            send_funpay(chat_id, "⚠️ Сначала отключите плату за сообщения в Telegram, затем отправьте «+»."); return
        if status == STATUS_ERROR:
            if is_plus(text) and order.get("username") and order.get("text"):
                if not ensure_worker():
                    send_funpay(chat_id, "❌ Telegram-модуль не удалось запустить."); return
                update_order(oid, status=STATUS_SENDING, error=None); _worker.submit(oid)
                send_funpay(chat_id, "⏳ Повторная попытка отправки запущена."); return
            username = normalize_username(text)
            if username:
                update_order(oid, username=username, status=STATUS_CONFIRM, error=None)
                send_funpay(chat_id, f"📋 Получатель изменён: {username}\n\nНапишите + для подтверждения."); return
            send_funpay(chat_id, "❓ Напишите + для повторной отправки, новый @username или !возврат.")
    except Exception:
        logger.exception("Telegram Text: ошибка обработки сообщения")


def post_init(c):
    global _cardinal, _worker
    _cardinal = c
    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    load_state()
    try:
        if not getattr(c, "telegram", None):
            logger.warning("Telegram Text: Telegram-панель Cardinal отключена")
            return
        ensure_worker()
        c.add_telegram_commands(UUID, [("text_account","Показать/настроить Telegram аккаунт",True),("text_account_reset","Повторно авторизовать Telegram аккаунт",False),("text_lots","Показать привязанные лоты",True),("text_bind","Привязать лот",False),("text_unbind","Отвязать лот",False)])
        tg = c.telegram
        tg.msg_handler(command_account, commands=["text_account"])
        tg.msg_handler(command_account_reset, commands=["text_account_reset"])
        tg.msg_handler(command_lots, commands=["text_lots"])
        tg.msg_handler(command_bind, commands=["text_bind"])
        tg.msg_handler(command_unbind, commands=["text_unbind"])
        tg.msg_handler(auth_message, func=lambda m: bool(_auth_states.get(int(m.from_user.id))))
        logger.info("Telegram Text: плагин успешно загружен v%s", VERSION)
    except Exception:
        logger.exception("Telegram Text: ошибка регистрации Telegram-команд")


def post_stop(c):
    global _worker
    if _worker is not None:
        _worker.stop(); _worker = None
    logger.info("Telegram Text: worker остановлен")


BIND_TO_POST_INIT = [post_init]
BIND_TO_NEW_ORDER = [new_order_handler]
BIND_TO_NEW_MESSAGE = [message_handler]
BIND_TO_POST_STOP = [post_stop]
BIND_TO_DELETE = None
