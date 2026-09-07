# -*- coding: utf-8 -*-
"""Telegram Text for FunPay Cardinal.

Single-file plugin. Order flow:
FunPay purchase -> username -> confirmation '+' -> text -> paid-message check -> send.
The Telegram session is taken from the Gift Delete plugin when available, otherwise
from this plugin's own session path. FunPay replies are matched by chat, buyer id,
and buyer username so normal customer messages are not silently ignored.
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
from types import SimpleNamespace

from FunPayAPI.types import MessageTypes

NAME = "Telegram Text"
VERSION = "5.0.0"
DESCRIPTION = "Автоматическая отправка текста после покупки привязанного лота."
CREDITS = "@podarckov"
UUID = "2b5d7f4a-8c31-4e96-a172-53f9d0b64c28"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

API_ID = 32493973
API_HASH = "e470a990253e9502835f62cc5958aed7"
TELETHON_PACKAGE = "telethon>=1.36,<2"
GIFTS_UUID = "7f8e2d91-4b36-4c2a-9f15-6a7d83e4b102"
BASE = Path("storage") / "plugins" / UUID
SHARED_SESSION = Path("storage") / "plugins" / GIFTS_UUID / "telegram_gifts"
LEGACY_SESSION = BASE / "telegram_text"
ORDERS_FILE = BASE / "orders.json"
LOTS_FILE = BASE / "lot_bindings.json"
MAX_TEXT = 4096
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{5,32}$")
ID_RE = re.compile(r"(?i)(?:^|[^A-Za-z0-9_])ID\s*:\s*(\d{10,25})(?:[^0-9]|$)")

S_USERNAME = "username"
S_CONFIRM = "confirm"
S_TEXT = "text"
S_PAID = "paid"
S_SENDING = "sending"
S_COMPLETED = "completed"
S_REFUNDED = "refunded"
S_ERROR = "error"
ACTIVE = {S_USERNAME, S_CONFIRM, S_TEXT, S_PAID, S_SENDING, S_ERROR}

log = logging.getLogger("telegram_text")
_cardinal = None
_worker = None
_orders = {}
_lots = {}
_auth = {}
_lock = threading.RLock()
_auth_lock = threading.RLock()
_install_lock = threading.Lock()


def clean(value):
    return (unicodedata.normalize("NFKC", str(value or ""))
            .replace("\u200b", "").replace("\u200c", "")
            .replace("\u200d", "").replace("\ufeff", "").strip())


def message_text(message):
    return clean(getattr(message, "text", None) or getattr(message, "content", None)
                 or getattr(message, "message", None))


def is_plus(value):
    return clean(value) in ("+", "＋")


def is_refund(value):
    return clean(value).replace(" ", "").casefold() in ("!возврат", "!refund")


def normalize_username(value):
    value = clean(value)
    if not USERNAME_RE.fullmatch(value):
        return None
    return value if value.startswith("@") else "@" + value


def load_json(path, default):
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, type(default)) else default
    except Exception:
        log.exception("Telegram Text: cannot read %s", path)
        return default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    with tmp.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def persist():
    with _lock:
        save_json(ORDERS_FILE, _orders)
        save_json(LOTS_FILE, _lots)


def load_state():
    global _orders, _lots
    with _lock:
        _orders = load_json(ORDERS_FILE, {})
        _lots = load_json(LOTS_FILE, {})
        if not isinstance(_orders, dict):
            _orders = {}
        if not isinstance(_lots, dict):
            _lots = {}
        changed = False
        for order in _orders.values():
            if order.get("status") == S_SENDING:
                order["status"] = S_ERROR
                order["error"] = "Cardinal был перезапущен во время отправки."
                changed = True
        if changed:
            persist()


def get_order(order_id):
    with _lock:
        value = _orders.get(str(order_id))
        return dict(value) if value else None


def update_order(order_id, **fields):
    with _lock:
        value = _orders.get(str(order_id))
        if value is None:
            return None
        value.update(fields)
        value["updated_at"] = time.time()
        persist()
        return dict(value)


def send_funpay(chat_id, text):
    if _cardinal is None or chat_id is None:
        return False
    try:
        for owner in (_cardinal, getattr(_cardinal, "account", None)):
            if owner is None:
                continue
            for name in ("send_message", "send_message_to_chat"):
                fn = getattr(owner, name, None)
                if not callable(fn):
                    continue
                try:
                    fn(chat_id, text)
                    return True
                except TypeError:
                    try:
                        fn(str(chat_id), text)
                        return True
                    except Exception:
                        pass
    except Exception:
        log.exception("Telegram Text: FunPay message send failed")
    return False


def panel(message, text):
    try:
        telegram = getattr(_cardinal, "telegram", None)
        bot = getattr(telegram, "bot", None)
        if bot is not None:
            bot.send_message(message.chat.id, text, parse_mode="HTML")
            return True
    except Exception:
        log.exception("Telegram Text: panel message failed")
    return False


def authorized(message):
    try:
        return int(message.from_user.id) in _cardinal.telegram.authorized_users
    except Exception:
        return False


def event_order(event):
    return getattr(event, "order", None) or event


def order_id_from_event(event):
    order = event_order(event)
    value = getattr(order, "id", None)
    if value is None:
        value = getattr(order, "order_id", None)
    return str(value) if value is not None else ""


def get_full_order(cardinal, event):
    order = event_order(event)
    oid = getattr(order, "id", None)
    if oid is not None:
        try:
            fresh = cardinal.account.get_order(oid)
            if fresh is not None:
                return fresh
        except Exception:
            log.debug("Telegram Text: account.get_order failed", exc_info=True)
    return order


def lot_id_from_object(obj):
    if obj is None:
        return None
    if isinstance(obj, dict):
        for key in ("lot_id", "lotId", "offer_id", "offerId"):
            value = obj.get(key)
            if value is not None:
                try:
                    return str(int(value))
                except Exception:
                    return str(value)
    for key in ("lot_id", "lotId", "offer_id", "offerId"):
        value = getattr(obj, key, None)
        if value is not None:
            try:
                return str(int(value))
            except Exception:
                return str(value)
    return None


def resolve_lot(cardinal, event, order):
    objects = [order, getattr(event, "order", None), getattr(order, "lot", None),
               getattr(order, "offer", None), getattr(order, "shortcut", None),
               getattr(order, "order_shortcut", None)]
    for obj in objects:
        value = lot_id_from_object(obj)
        if value is not None:
            return value
    for obj in objects:
        for attr in ("full_description", "description", "short_description"):
            value = clean(getattr(obj, attr, None))
            match = ID_RE.search(value)
            if match:
                return match.group(1)
    return None


def order_chat_id(order, event):
    for obj in (order, getattr(event, "order", None)):
        for key in ("chat_id", "chatId"):
            value = getattr(obj, key, None)
            if value is not None:
                return value
    return None


def order_buyer(order, event):
    for obj in (order, getattr(event, "order", None)):
        for key in ("buyer_username", "buyer", "username", "customer_username"):
            value = getattr(obj, key, None)
            if value:
                return clean(value)
    return ""


def order_buyer_id(order, event):
    for obj in (order, getattr(event, "order", None)):
        for key in ("buyer_id", "buyer_user_id", "customer_id", "user_id"):
            value = getattr(obj, key, None)
            if value is not None:
                return value
    return None


def find_order(message):
    """Return (order_id, order) or (None, None). Never return a truthy empty tuple."""
    chat_id = getattr(message, "chat_id", None)
    author_id = getattr(message, "author_id", None)
    author = clean(getattr(message, "author", None)).lstrip("@").casefold()
    with _lock:
        active = [(oid, value) for oid, value in _orders.items()
                  if value.get("status") in ACTIVE]
        if chat_id is not None:
            same_chat = [item for item in active
                         if str(item[1].get("chat_id")) == str(chat_id)]
            if same_chat:
                return max(same_chat, key=lambda item: float(item[1].get("created_at", 0)))
        if author_id is not None:
            same_id = [item for item in active
                       if item[1].get("buyer_id") is not None
                       and str(item[1].get("buyer_id")) == str(author_id)]
            if same_id:
                return max(same_id, key=lambda item: float(item[1].get("created_at", 0)))
        if author:
            same_user = [item for item in active
                         if clean(item[1].get("buyer", "")).lstrip("@").casefold() == author]
            if same_user:
                return max(same_user, key=lambda item: float(item[1].get("created_at", 0)))
    return None, None


def refund_order(order_id):
    order = get_order(order_id)
    if not order:
        return False
    chat_id = order.get("chat_id")
    status = order.get("status")
    if status == S_COMPLETED:
        send_funpay(chat_id, "ℹ️ Сообщение уже успешно отправлено. Возврат после выдачи недоступен.")
        return False
    if status == S_REFUNDED:
        send_funpay(chat_id, "ℹ️ По этому заказу возврат уже выполнен.")
        return False
    if status == S_SENDING:
        send_funpay(chat_id, "⏳ Сообщение уже отправляется. Дождитесь результата.")
        return False
    try:
        fn = getattr(getattr(_cardinal, "account", None), "refund", None)
        if not callable(fn):
            raise RuntimeError("FunPay refund method unavailable")
        fn(str(order_id))
        update_order(order_id, status=S_REFUNDED, error=None, refunded_at=time.time())
        send_funpay(chat_id, "❌ Заказ отменён.\n\nСредства возвращены.")
        return True
    except Exception:
        log.exception("Telegram Text: refund failed for %s", order_id)
        send_funpay(chat_id, "⚠️ Не удалось автоматически оформить возврат.\n\nПродавец обработает возврат вручную.")
        return False


def ensure_telethon():
    if importlib.util.find_spec("telethon") is not None:
        return True
    with _install_lock:
        if importlib.util.find_spec("telethon") is None:
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", TELETHON_PACKAGE])
            except Exception:
                log.exception("Telegram Text: Telethon installation failed")
    return importlib.util.find_spec("telethon") is not None


class PaidMessagesRequired(Exception):
    def __init__(self, stars=0):
        self.stars = int(stars or 0)
        super().__init__(f"paid_messages:{self.stars}")


class TelegramWorker:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.queue = None
        self.client = None
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

    def running(self):
        return bool(self.thread and self.thread.is_alive() and
                    self.loop and self.loop.is_running())

    def start(self):
        with self.lock:
            if self.running():
                return True
            self.stop_event.clear()
            self.ready.clear()
            self.thread = threading.Thread(target=self._main, name="telegram-text-worker", daemon=True)
            self.thread.start()
        return self.ready.wait(15) and self.running()

    def _main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.queue = asyncio.Queue()
        self.loop.create_task(self._queue_loop())
        self.ready.set()
        try:
            self.loop.run_forever()
        except Exception:
            log.exception("Telegram Text worker crashed")
        finally:
            try:
                tasks = asyncio.all_tasks(self.loop)
                for task in tasks:
                    task.cancel()
                if tasks:
                    self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                self.loop.close()
            except Exception:
                pass
            self.loop = None

    async def _queue_loop(self):
        while not self.stop_event.is_set():
            try:
                order_id = await asyncio.wait_for(self.queue.get(), 0.5)
            except asyncio.TimeoutError:
                continue
            try:
                await self.process(str(order_id))
            except Exception:
                log.exception("Telegram Text: order processing crashed")
                update_order(order_id, status=S_ERROR, error="worker_exception")
                order = get_order(order_id)
                if order:
                    send_funpay(order.get("chat_id"), "❌ Внутренняя ошибка обработки заказа. Отправьте + для повторной попытки или !возврат.")

    def session_path(self):
        shared_candidates = (SHARED_SESSION, Path(str(SHARED_SESSION) + ".session"))
        for path in shared_candidates:
            if path.exists():
                return SHARED_SESSION
        legacy_candidates = (LEGACY_SESSION, Path(str(LEGACY_SESSION) + ".session"))
        for path in legacy_candidates:
            if path.exists():
                return LEGACY_SESSION
        return SHARED_SESSION

    async def client_get(self):
        if not ensure_telethon():
            raise RuntimeError("Telethon не установлен")
        if self.client is None:
            telethon = importlib.import_module("telethon")
            self.client = telethon.TelegramClient(
                str(self.session_path()), API_ID, API_HASH,
                device_model="FunPay Cardinal Telegram Text",
                system_version="Linux", app_version=VERSION,
                lang_code="en", system_lang_code="en-US"
            )
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def account_info(self):
        client = await self.client_get()
        if not await client.is_user_authorized():
            return None
        me = await client.get_me()
        return {
            "id": me.id,
            "username": me.username,
            "phone": me.phone,
            "name": " ".join(x for x in (me.first_name, me.last_name) if x) or "—",
            "session": str(self.session_path()),
        }

    async def send_code(self, phone):
        client = await self.client_get()
        if await client.is_user_authorized():
            return "authorized"
        result = await client.send_code_request(phone)
        return result.phone_code_hash

    async def sign_code(self, phone, code, phone_code_hash):
        client = await self.client_get()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            return "authorized"
        except Exception as exc:
            if type(exc).__name__ == "SessionPasswordNeededError":
                return "2fa"
            raise

    async def sign_password(self, password):
        client = await self.client_get()
        await client.sign_in(password=password)
        return "authorized"

    async def paid_check(self, target):
        client = await self.client_get()
        entity = await client.get_entity(target)
        value = getattr(entity, "send_paid_messages_stars", None)
        if value is not None and int(value or 0) > 0:
            return True, int(value)
        try:
            from telethon import functions
            full = await client(functions.users.GetFullUserRequest(id=entity))
            full_user = getattr(full, "full_user", None)
            value = getattr(full_user, "send_paid_messages_stars", None)
            if value is None:
                value = getattr(full, "send_paid_messages_stars", None)
            if value is not None and int(value or 0) > 0:
                return True, int(value)
        except Exception:
            log.exception("Telegram Text: paid-message check failed")
            raise RuntimeError("Не удалось безопасно проверить плату за сообщения")
        return False, 0

    async def send_text(self, target, text):
        client = await self.client_get()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram-аккаунт не авторизован")
        required, stars = await self.paid_check(target)
        if required:
            raise PaidMessagesRequired(stars)
        try:
            await client.send_message(target, text)
        except Exception as exc:
            upper = str(exc).upper()
            if "ALLOW_PAYMENT_REQUIRED" in upper or ("PAID_MESSAGES" in upper and "REQUIRED" in upper):
                raise PaidMessagesRequired(0) from exc
            raise

    async def process(self, order_id):
        order = get_order(order_id)
        if not order or order.get("status") != S_SENDING:
            return
        try:
            await self.send_text(order["username"], order["text"])
        except PaidMessagesRequired as exc:
            update_order(order_id, status=S_PAID, paid_stars=exc.stars, error="paid_messages")
            suffix = f" ({exc.stars} ⭐️)" if exc.stars else ""
            send_funpay(order.get("chat_id"),
                        "⚠️ У этого пользователя включена плата за входящие сообщения" + suffix + ".\n\n"
                        "Пожалуйста, отключите плату за сообщения в Telegram.\n"
                        "После отключения отправьте «+».\n\n"
                        "Сообщение НЕ отправлено.")
            return
        except Exception as exc:
            name = type(exc).__name__
            if name in {"UsernameInvalidError", "UsernameNotOccupiedError", "PeerIdInvalidError", "UserIdInvalidError"}:
                update_order(order_id, status=S_USERNAME, error=name)
                send_funpay(order.get("chat_id"), "❌ Telegram не нашёл этот username. Отправьте другой @username.")
                return
            if name == "FloodWaitError":
                seconds = int(getattr(exc, "seconds", 0) or 0)
                if 0 < seconds <= 300:
                    send_funpay(order.get("chat_id"), f"⏳ Telegram попросил подождать {seconds} сек. Повторяю автоматически.")
                    await asyncio.sleep(seconds)
                    if get_order(order_id) and get_order(order_id).get("status") == S_SENDING:
                        await self.process(order_id)
                    return
            log.exception("Telegram Text: Telegram send failed")
            update_order(order_id, status=S_ERROR, error=name)
            send_funpay(order.get("chat_id"), "❌ Не удалось отправить сообщение. Отправьте + для повторной попытки или !возврат.")
            return
        update_order(order_id, status=S_COMPLETED, completed_at=time.time(), error=None)
        send_funpay(order.get("chat_id"), "✅ Сообщение успешно отправлено!\n\nСпасибо за покупку ❤️")

    def submit(self, order_id):
        if not self.running():
            raise RuntimeError("Telegram worker не запущен")
        asyncio.run_coroutine_threadsafe(self.queue.put(str(order_id)), self.loop)

    def call(self, coroutine_factory, timeout=90):
        if not self.running():
            raise RuntimeError("Telegram worker не запущен")
        future = asyncio.run_coroutine_threadsafe(coroutine_factory(), self.loop)
        return future.result(timeout=timeout)

    def reset(self):
        async def disconnect():
            if self.client is not None:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                self.client = None
        return self.call(disconnect, 30)

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
            try:
                self.loop.call_soon_threadsafe(lambda: asyncio.create_task(close()))
            except Exception:
                pass
        if self.thread and self.thread.is_alive():
            self.thread.join(10)
        self.thread = None


def ensure_worker():
    global _worker
    if _worker is None:
        _worker = TelegramWorker()
    return _worker.start()


def begin_auth(message):
    with _auth_lock:
        _auth[int(message.from_user.id)] = {"state": "phone"}
    panel(message, "📱 Введите номер Telegram в международном формате, например <code>+79991234567</code>")


def cmd_account(message):
    if not authorized(message):
        return
    if not ensure_worker():
        panel(message, "❌ Telegram-модуль не удалось запустить.")
        return
    try:
        info = _worker.call(lambda: _worker.account_info(), 30)
    except Exception as exc:
        log.exception("Telegram Text: account check failed")
        panel(message, f"❌ Ошибка проверки аккаунта: {type(exc).__name__}")
        return
    if not info:
        begin_auth(message)
        return
    username = "@" + info["username"] if info.get("username") else "нет username"
    panel(message,
          "📱 <b>Telegram-аккаунт подключен</b>\n\n"
          f"👤 {html.escape(info.get('name') or '—')}\n"
          f"🔗 {html.escape(username)}\n"
          f"🆔 <code>{info.get('id')}</code>\n"
          f"📞 <code>{html.escape(str(info.get('phone') or '—'))}</code>\n\n"
          "Используется общая session из Gift Delete, если она найдена.")


def cmd_account_reset(message):
    if not authorized(message):
        return
    if ensure_worker():
        try:
            _worker.reset()
        except Exception:
            log.exception("Telegram Text: worker reset failed")
    begin_auth(message)


def auth_message(message):
    if not authorized(message):
        return False
    uid = int(message.from_user.id)
    with _auth_lock:
        state = dict(_auth.get(uid) or {})
    if not state:
        return False
    value = message_text(message)
    if not value or value.startswith("/"):
        return False
    if not ensure_worker():
        panel(message, "❌ Telegram-модуль не удалось запустить.")
        return True
    try:
        if state.get("state") == "phone":
            phone = re.sub(r"[\s()\-]", "", value)
            if not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
                panel(message, "❌ Неверный номер. Формат: +79991234567")
                return True
            result = _worker.call(lambda: _worker.send_code(phone), 90)
            if result == "authorized":
                with _auth_lock:
                    _auth.pop(uid, None)
                panel(message, "✅ Telegram-аккаунт уже авторизован.")
                return True
            state.update(phone=phone, code_hash=result, state="code")
            with _auth_lock:
                _auth[uid] = state
            panel(message, "📨 Код отправлен Telegram. Введите код одним сообщением.")
            return True
        if state.get("state") == "code":
            code = re.sub(r"\s", "", value)
            if not code.isdigit():
                panel(message, "❌ Код должен состоять из цифр.")
                return True
            result = _worker.call(lambda: _worker.sign_code(state["phone"], code, state["code_hash"]), 90)
            if result == "2fa":
                state["state"] = "2fa"
                with _auth_lock:
                    _auth[uid] = state
                panel(message, "🔐 Введите пароль Telegram 2FA.")
            else:
                with _auth_lock:
                    _auth.pop(uid, None)
                panel(message, "✅ Telegram-аккаунт успешно подключен.")
            return True
        if state.get("state") == "2fa":
            _worker.call(lambda: _worker.sign_password(value), 90)
            with _auth_lock:
                _auth.pop(uid, None)
            panel(message, "✅ Telegram-аккаунт успешно подключен.")
            return True
    except Exception as exc:
        name = type(exc).__name__
        errors = {
            "PhoneCodeInvalidError": "❌ Неверный код.",
            "PhoneCodeExpiredError": "❌ Код устарел. Запустите /text_account заново.",
            "PhoneNumberInvalidError": "❌ Неверный номер Telegram.",
            "PhoneNumberBannedError": "❌ Этот номер заблокирован Telegram.",
            "PasswordHashInvalidError": "❌ Неверный пароль 2FA.",
            "ApiIdInvalidError": "❌ Telegram отклонил API ID/API HASH.",
        }
        log.exception("Telegram Text: authorization error")
        panel(message, errors.get(name, f"❌ Ошибка авторизации: {name}"))
        with _auth_lock:
            _auth.pop(uid, None)
        return True
    return False


def cmd_lots(message):
    if not authorized(message):
        return
    with _lock:
        items = dict(_lots)
    if not items:
        panel(message, "📦 Привязанных лотов нет.")
        return
    lines = ["📦 <b>Привязанные лоты Telegram Text</b>", ""]
    for lot, data in sorted(items.items(), key=lambda item: str(item[0])):
        title = data.get("title", "") if isinstance(data, dict) else ""
        suffix = f" — {html.escape(title)}" if title else ""
        lines.append(f"• <code>{html.escape(str(lot))}</code>{suffix}")
    panel(message, "\n".join(lines))


def cmd_bind(message):
    if not authorized(message):
        return
    parts = message_text(message).split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        panel(message, "Использование: /text_bind LOT_ID [название]")
        return
    lot = str(int(parts[1]))
    title = parts[2] if len(parts) > 2 else ""
    with _lock:
        _lots[lot] = {"title": title, "enabled": True, "updated_at": time.time()}
        persist()
    panel(message, f"✅ Лот <code>{lot}</code> привязан к Telegram Text.")


def cmd_unbind(message):
    if not authorized(message):
        return
    parts = message_text(message).split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        panel(message, "Использование: /text_unbind LOT_ID")
        return
    lot = str(int(parts[1]))
    with _lock:
        existed = _lots.pop(lot, None)
        persist()
    panel(message, "✅ Лот отвязан." if existed else "ℹ️ Такой лот не был привязан.")


def new_order(c, event):
    try:
        order = get_full_order(c, event)
        oid = order_id_from_event(event)
        if not oid:
            log.warning("Telegram Text: new order without id")
            return
        with _lock:
            if oid in _orders:
                return
        lot = resolve_lot(c, event, order)
        if lot is None:
            return
        with _lock:
            binding = _lots.get(str(lot))
            if not isinstance(binding, dict) or not binding.get("enabled", True):
                return
        chat_id = order_chat_id(order, event)
        buyer = order_buyer(order, event)
        buyer_id = order_buyer_id(order, event)
        if chat_id is None and buyer:
            try:
                chat = c.account.get_chat_by_name(buyer, True)
                chat_id = getattr(chat, "id", None) if chat else None
            except Exception:
                log.debug("Telegram Text: get_chat_by_name failed", exc_info=True)
        if chat_id is None:
            log.error("Telegram Text: order %s has no FunPay chat id", oid)
            return
        record = {
            "order_id": oid,
            "lot_id": str(lot),
            "chat_id": chat_id,
            "buyer": buyer,
            "buyer_id": buyer_id,
            "username": None,
            "text": None,
            "status": S_USERNAME,
            "created_at": time.time(),
            "error": None,
        }
        with _lock:
            _orders[oid] = record
            persist()
        send_funpay(chat_id,
                    "👋 Спасибо за покупку!\n\n"
                    "Отправьте Telegram username, куда нужно отправить текст.\n\n"
                    "Пример: @username\n\n"
                    "❗ Для отмены заказа отправьте: !возврат")
    except Exception:
        log.exception("Telegram Text: new order handler failed")


def new_message(c, event):
    """Process every FunPay message; do not rely on MessageTypes.NON_SYSTEM."""
    try:
        message = getattr(event, "message", None) or event
        author_id = getattr(message, "author_id", None)
        own_id = getattr(getattr(c, "account", None), "id", None)
        if author_id is not None and own_id is not None and str(author_id) == str(own_id):
            return
        if getattr(message, "by_bot", False):
            return
        value = message_text(message)
        if not value:
            return

        found = find_order(message)
        if not found or found[0] is None or found[1] is None:
            return
        oid, order = found
        chat_id = getattr(message, "chat_id", None) or order.get("chat_id")
        buyer_id = order.get("buyer_id")
        if buyer_id is not None and author_id is not None and str(buyer_id) != str(author_id):
            return

        status = order.get("status")
        if is_refund(value):
            refund_order(oid)
            return
        if status == S_SENDING:
            send_funpay(chat_id, "⏳ Сообщение уже отправляется. Пожалуйста, подождите.")
            return

        if status == S_USERNAME:
            username = normalize_username(value)
            if not username:
                send_funpay(chat_id, "❌ Некорректный Telegram username. Отправьте @username или !возврат.")
                return
            update_order(oid, username=username, status=S_CONFIRM, error=None)
            send_funpay(chat_id,
                        f"📋 Получатель: {username}\n\n"
                        "Если всё верно — отправьте «+».\n"
                        "Если хотите изменить — отправьте новый @username.\n\n"
                        "❗ Для возврата: !возврат")
            return

        if status == S_CONFIRM:
            if is_plus(value):
                update_order(oid, status=S_TEXT, error=None)
                send_funpay(chat_id,
                            "💬 Отлично. Теперь отправьте текст, который нужно отправить этому пользователю.\n\n"
                            "Следующее сообщение будет отправлено как текст.\n\n"
                            "❗ Для отмены: !возврат")
                return
            username = normalize_username(value)
            if username:
                update_order(oid, username=username, error=None)
                send_funpay(chat_id, f"📋 Получатель изменён: {username}\n\nЕсли всё верно — отправьте «+».")
                return
            send_funpay(chat_id, "❓ Отправьте «+» для подтверждения, новый @username или !возврат.")
            return

        if status == S_TEXT:
            if len(value) > MAX_TEXT:
                send_funpay(chat_id, "❌ Максимальная длина текста — 4096 символов.")
                return
            update_order(oid, text=value, status=S_SENDING, error=None)
            if not ensure_worker():
                update_order(oid, status=S_ERROR, error="worker_unavailable")
                send_funpay(chat_id, "❌ Telegram-модуль не удалось запустить. Отправьте + для повторной попытки или !возврат.")
                return
            try:
                _worker.submit(oid)
                send_funpay(chat_id, "⏳ Проверяю возможность отправки и отправляю сообщение...")
            except Exception:
                log.exception("Telegram Text: submit failed")
                update_order(oid, status=S_ERROR, error="submit_failed")
                send_funpay(chat_id, "❌ Не удалось запустить отправку. Отправьте + для повторной попытки или !возврат.")
            return

        if status == S_PAID:
            if not is_plus(value):
                send_funpay(chat_id, "⚠️ Сначала отключите плату за сообщения в Telegram, затем отправьте «+».")
                return
            if not ensure_worker():
                send_funpay(chat_id, "❌ Telegram-модуль не удалось запустить.")
                return
            update_order(oid, status=S_SENDING, error=None)
            try:
                _worker.submit(oid)
                send_funpay(chat_id, "⏳ Повторно проверяю плату за сообщения...")
            except Exception:
                log.exception("Telegram Text: paid retry submit failed")
                update_order(oid, status=S_PAID, error="submit_failed")
                send_funpay(chat_id, "❌ Не удалось повторить отправку. Отправьте «+» ещё раз.")
            return

        if status == S_ERROR:
            if is_plus(value) and order.get("username") and order.get("text"):
                if not ensure_worker():
                    send_funpay(chat_id, "❌ Telegram-модуль не удалось запустить.")
                    return
                update_order(oid, status=S_SENDING, error=None)
                try:
                    _worker.submit(oid)
                    send_funpay(chat_id, "⏳ Повторная попытка отправки запущена.")
                except Exception:
                    log.exception("Telegram Text: error-state submit failed")
                    update_order(oid, status=S_ERROR, error="submit_failed")
                    send_funpay(chat_id, "❌ Не удалось запустить повторную отправку.")
                return
            username = normalize_username(value)
            if username:
                update_order(oid, username=username, status=S_CONFIRM, error=None)
                send_funpay(chat_id, f"📋 Получатель: {username}\n\nОтправьте + для подтверждения.")
                return
            send_funpay(chat_id, "❓ Отправьте + для повторной попытки, новый @username или !возврат.")
    except Exception:
        log.exception("Telegram Text: message handler failed")


def old_message(c, event):
    """Convert Cardinal old-mode chat changes into the same full Message flow."""
    if not getattr(c, "old_mode_enabled", False):
        return
    chat = getattr(event, "chat", None)
    if chat is None:
        return

    def recover_and_process():
        try:
            account_id = getattr(c.account, "id", None)
            message = None
            for attempt in range(5):
                messages = c.account.get_chat_history(
                    chat.id,
                    last_message_id=None,
                    interlocutor_username=getattr(chat, "name", None),
                )
                for candidate in reversed(messages or []):
                    if getattr(candidate, "chat_id", None) is not None and str(candidate.chat_id) != str(chat.id):
                        continue
                    if getattr(candidate, "by_bot", False):
                        continue
                    author_id = getattr(candidate, "author_id", None)
                    if author_id is not None and account_id is not None and str(author_id) == str(account_id):
                        continue
                    if message_text(candidate):
                        message = candidate
                        break
                if message is not None:
                    break
                time.sleep(0.2)

            if message is None:
                log.warning("Telegram Text: old mode could not recover incoming Message for chat %s", chat.id)
                return

            new_message(c, SimpleNamespace(message=message))
        except Exception:
            log.exception("Telegram Text: old mode full message recovery failed")

    threading.Thread(
        target=recover_and_process,
        name="telegram-text-old-mode",
        daemon=True,
    ).start()


def post_init(c):
    global _cardinal
    _cardinal = c
    BASE.mkdir(parents=True, exist_ok=True)
    load_state()
    try:
        telegram = getattr(c, "telegram", None)
        if telegram is None:
            log.error("Telegram Text: Cardinal telegram manager is unavailable")
            return
        ensure_worker()
        commands = [
            ("text_account", "Показать/настроить Telegram аккаунт", True),
            ("text_account_reset", "Повторно авторизовать Telegram аккаунт", False),
            ("text_lots", "Показать привязанные лоты", True),
            ("text_bind", "Привязать лот", False),
            ("text_unbind", "Отвязать лот", False),
        ]
        try:
            c.add_telegram_commands(UUID, commands)
        except Exception:
            log.exception("Telegram Text: command menu registration failed")
        telegram.msg_handler(cmd_account, commands=["text_account"])
        telegram.msg_handler(cmd_account_reset, commands=["text_account_reset"])
        telegram.msg_handler(cmd_lots, commands=["text_lots"])
        telegram.msg_handler(cmd_bind, commands=["text_bind"])
        telegram.msg_handler(cmd_unbind, commands=["text_unbind"])
        telegram.msg_handler(auth_message, func=lambda m: bool(_auth.get(int(m.from_user.id))))
        log.info("Telegram Text v%s loaded; single-file message flow enabled", VERSION)
    except Exception:
        log.exception("Telegram Text: registration failed")


def post_stop(c):
    global _worker, _cardinal
    if _worker is not None:
        try:
            _worker.stop()
        except Exception:
            log.exception("Telegram Text: worker stop failed")
    _worker = None
    _cardinal = None


BIND_TO_POST_INIT = [post_init]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_NEW_MESSAGE = [new_message]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [old_message]
BIND_TO_POST_STOP = [post_stop]
BIND_TO_DELETE = None
