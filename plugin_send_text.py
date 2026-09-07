# -*- coding: utf-8 -*-
"""Telegram Text for FunPay Cardinal.

Flow:
FunPay purchase -> bound lot -> username -> '+' -> text -> Telegram send.
The lot is selected from the administrator's saved /text_bind bindings.
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

from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent

NAME = "Telegram Text"
VERSION = "7.0.0"
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
    return unicodedata.normalize("NFKC", str(value or "")).replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "").strip()


def text_of(message):
    return clean(getattr(message, "text", None) or getattr(message, "content", None) or getattr(message, "message", None))


def norm_title(value):
    return re.sub(r"\s+", " ", clean(value)).casefold()


def is_plus(value):
    return clean(value) in {"+", "＋"}


def is_refund(value):
    return clean(value).replace(" ", "").casefold() in {"!возврат", "!refund"}


def normalize_username(value):
    value = clean(value)
    if not USERNAME_RE.fullmatch(value):
        return None
    return value if value.startswith("@") else "@" + value


def load_json(path, default):
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, type(default)) else default
    except Exception:
        log.exception("Telegram Text: cannot read %s", path)
        return default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
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
                order["error"] = "Cardinal перезапущен во время отправки."
                changed = True
        if changed:
            save_json(ORDERS_FILE, _orders)


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
        save_json(ORDERS_FILE, _orders)
        return dict(value)


def send_funpay(chat_id, text):
    if _cardinal is None or chat_id is None:
        return False
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
                log.exception("Telegram Text: FunPay send failed")
    log.error("Telegram Text: no available FunPay send method for chat %s", chat_id)
    return False


def panel(message, text):
    try:
        bot = getattr(getattr(_cardinal, "telegram", None), "bot", None)
        if bot is None:
            return False
        bot.send_message(message.chat.id, text, parse_mode="HTML")
        return True
    except Exception:
        log.exception("Telegram Text: panel send failed")
        return False


def authorized(message):
    try:
        return int(message.from_user.id) in _cardinal.telegram.authorized_users
    except Exception:
        return False


def event_order(event):
    return getattr(event, "order", None) or event


def order_id_from_event(event):
    obj = event_order(event)
    value = getattr(obj, "id", None)
    if value is None:
        value = getattr(obj, "order_id", None)
    return str(value) if value is not None else ""


def full_order(c, event):
    obj = event_order(event)
    oid = getattr(obj, "id", None)
    if oid is not None:
        try:
            fresh = c.account.get_order(oid)
            if fresh is not None:
                return fresh
        except Exception:
            log.debug("Telegram Text: get_order failed", exc_info=True)
    return obj


def attr(obj, *names):
    for name in names:
        value = getattr(obj, name, None)
        if value not in (None, ""):
            return value
    return None


def order_title(order, event):
    for obj in (order, event_order(event)):
        for name in ("title", "short_description", "description"):
            value = clean(getattr(obj, name, None))
            if value:
                return value
    return ""


def order_chat_id(order, event):
    return attr(order, "chat_id", "chatId") or attr(event_order(event), "chat_id", "chatId")


def order_buyer(order, event):
    value = attr(order, "buyer_username", "buyer", "username", "customer_username")
    if value is None:
        value = attr(event_order(event), "buyer_username", "buyer", "username", "customer_username")
    return clean(value or "")


def order_buyer_id(order, event):
    value = attr(order, "buyer_id", "buyer_user_id", "customer_id", "user_id")
    if value is None:
        value = attr(event_order(event), "buyer_id", "buyer_user_id", "customer_id", "user_id")
    return value


def lot_fields(c, lot_id):
    fn = getattr(getattr(c, "account", None), "get_lot_fields", None)
    if not callable(fn):
        return None
    try:
        return fn(int(lot_id))
    except Exception:
        try:
            return fn(str(lot_id))
        except Exception:
            log.debug("Telegram Text: get_lot_fields failed for %s", lot_id, exc_info=True)
            return None


def lot_field_title(fields):
    if fields is None:
        return ""
    if isinstance(fields, dict):
        for name in ("title", "name", "short_description", "description", "title_ru", "title_en"):
            value = clean(fields.get(name))
            if value:
                return value
    for name in ("title", "name", "short_description", "description", "title_ru", "title_en"):
        value = clean(getattr(fields, name, None))
        if value:
            return value
    return ""


def bound_lot_for_order(c, event, order):
    """Return the configured lot id for this purchase.

    Prefer an explicit lot id supplied by the event/order. Otherwise compare the
    purchase title with the title stored by /text_bind. A binding with no stored
    title is refreshed through get_lot_fields. This is intentionally based on
    the saved admin bindings, never on an unconfigured lot.
    """
    direct = None
    for obj in (event_order(event), order):
        for name in ("lot_id", "lotId", "offer_id", "offerId"):
            value = getattr(obj, name, None)
            if value is not None:
                direct = str(value)
                break
        if direct is not None:
            break

    with _lock:
        bindings = [(str(k), dict(v) if isinstance(v, dict) else {}) for k, v in _lots.items()]

    enabled = [(lot_id, data) for lot_id, data in bindings if data.get("enabled", True)]
    if not enabled:
        log.warning("Telegram Text: purchase ignored because no lots are bound")
        return None

    if direct is not None:
        for lot_id, _ in enabled:
            if str(lot_id) == str(direct):
                return lot_id

    wanted = norm_title(order_title(order, event))
    if not wanted:
        log.warning("Telegram Text: purchase %s has no title for bound-lot matching", order_id_from_event(event))
        return None

    for lot_id, data in enabled:
        saved_title = norm_title(data.get("title"))
        if saved_title and saved_title == wanted:
            return lot_id

    for lot_id, data in enabled:
        if norm_title(data.get("title")):
            continue
        fields = lot_fields(c, lot_id)
        fetched_title = lot_field_title(fields)
        if not fetched_title:
            continue
        with _lock:
            current = _lots.get(str(lot_id))
            if isinstance(current, dict):
                current["title"] = fetched_title
                current["updated_at"] = time.time()
                save_json(LOTS_FILE, _lots)
        if norm_title(fetched_title) == wanted:
            return lot_id

    log.info("Telegram Text: purchase %s title %r did not match any bound lot", order_id_from_event(event), order_title(order, event))
    return None


def find_order(message):
    chat_id = getattr(message, "chat_id", None)
    author_id = getattr(message, "author_id", None)
    author = clean(getattr(message, "author", None)).lstrip("@").casefold()
    with _lock:
        active = [(oid, value) for oid, value in _orders.items() if value.get("status") in ACTIVE]
    if chat_id is not None:
        same = [item for item in active if str(item[1].get("chat_id")) == str(chat_id)]
        if same:
            return max(same, key=lambda x: float(x[1].get("created_at", 0)))
    if author_id is not None:
        same = [item for item in active if item[1].get("buyer_id") is not None and str(item[1].get("buyer_id")) == str(author_id)]
        if same:
            return max(same, key=lambda x: float(x[1].get("created_at", 0)))
    if author:
        same = [item for item in active if clean(item[1].get("buyer", "")).lstrip("@").casefold() == author]
        if same:
            return max(same, key=lambda x: float(x[1].get("created_at", 0)))
    return None, None


def refund_order(oid):
    order = get_order(oid)
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
    try:
        fn = getattr(getattr(_cardinal, "account", None), "refund", None)
        if not callable(fn):
            raise RuntimeError("FunPay refund method unavailable")
        fn(str(oid))
        update_order(oid, status=S_REFUNDED, error=None, refunded_at=time.time())
        send_funpay(chat_id, "❌ Заказ отменён.\n\nСредства возвращены.")
        return True
    except Exception:
        log.exception("Telegram Text: refund failed for %s", oid)
        send_funpay(chat_id, "⚠️ Не удалось автоматически оформить возврат. Обработайте возврат вручную.")
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
                return False
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
        return bool(self.thread and self.thread.is_alive() and self.loop and self.loop.is_running())

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
                oid = await asyncio.wait_for(self.queue.get(), 0.5)
            except asyncio.TimeoutError:
                continue
            try:
                await self.process(str(oid))
            except Exception:
                log.exception("Telegram Text: processing crashed")
                update_order(oid, status=S_ERROR, error="worker_exception")

    def session_path(self):
        if SHARED_SESSION.exists() or Path(str(SHARED_SESSION) + ".session").exists():
            return SHARED_SESSION
        if LEGACY_SESSION.exists() or Path(str(LEGACY_SESSION) + ".session").exists():
            return LEGACY_SESSION
        return SHARED_SESSION

    async def client_get(self):
        if not ensure_telethon():
            raise RuntimeError("Telethon не установлен")
        if self.client is None:
            telethon = importlib.import_module("telethon")
            self.client = telethon.TelegramClient(str(self.session_path()), API_ID, API_HASH, device_model="FunPay Cardinal Telegram Text", system_version="Linux", app_version=VERSION, lang_code="en", system_lang_code="en-US")
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def account_info(self):
        client = await self.client_get()
        if not await client.is_user_authorized():
            return None
        me = await client.get_me()
        return {"id": me.id, "username": me.username, "phone": me.phone, "name": " ".join(x for x in (me.first_name, me.last_name) if x) or "—"}

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

    async def process(self, oid):
        order = get_order(oid)
        if not order or order.get("status") != S_SENDING:
            return
        try:
            await self.send_text(order["username"], order["text"])
        except PaidMessagesRequired as exc:
            update_order(oid, status=S_PAID, paid_stars=exc.stars, error="paid_messages")
            suffix = f" ({exc.stars} ⭐️)" if exc.stars else ""
            send_funpay(order.get("chat_id"), "⚠️ У пользователя включена плата за входящие сообщения" + suffix + ".\n\nОтключите плату за сообщения Telegram и отправьте «+».\n\nСообщение НЕ отправлено.")
            return
        except Exception as exc:
            name = type(exc).__name__
            if name in {"UsernameInvalidError", "UsernameNotOccupiedError", "PeerIdInvalidError", "UserIdInvalidError"}:
                update_order(oid, status=S_USERNAME, error=name)
                send_funpay(order.get("chat_id"), "❌ Telegram не нашёл этот username. Отправьте другой @username.")
                return
            log.exception("Telegram Text: Telegram send failed")
            update_order(oid, status=S_ERROR, error=name)
            send_funpay(order.get("chat_id"), "❌ Не удалось отправить сообщение. Отправьте + для повторной попытки или !возврат.")
            return
        update_order(oid, status=S_COMPLETED, completed_at=time.time(), error=None)
        send_funpay(order.get("chat_id"), "✅ Сообщение успешно отправлено!\n\nСпасибо за покупку ❤️")

    def submit(self, oid):
        if not self.running():
            raise RuntimeError("Telegram worker не запущен")
        asyncio.run_coroutine_threadsafe(self.queue.put(str(oid)), self.loop)

    def call(self, factory, timeout=90):
        if not self.running():
            raise RuntimeError("Telegram worker не запущен")
        future = asyncio.run_coroutine_threadsafe(factory(), self.loop)
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
    panel(message, "📱 <b>Telegram-аккаунт подключен</b>\n\n" + f"👤 {html.escape(info.get('name') or '—')}\n" + f"🔗 {html.escape(username)}\n" + f"🆔 <code>{info.get('id')}</code>\n" + f"📞 <code>{html.escape(str(info.get('phone') or '—'))}</code>")


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
    value = text_of(message)
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
        errors = {"PhoneCodeInvalidError": "❌ Неверный код.", "PhoneCodeExpiredError": "❌ Код устарел. Запустите /text_account заново.", "PhoneNumberInvalidError": "❌ Неверный номер Telegram.", "PhoneNumberBannedError": "❌ Этот номер заблокирован Telegram.", "PasswordHashInvalidError": "❌ Неверный пароль 2FA.", "ApiIdInvalidError": "❌ Telegram отклонил API ID/API HASH."}
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
    parts = text_of(message).split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        panel(message, "Использование: /text_bind LOT_ID [название]")
        return
    lot_id = str(int(parts[1]))
    manual_title = parts[2].strip() if len(parts) > 2 else ""
    title = manual_title
    if not title:
        try:
            title = lot_field_title(lot_fields(_cardinal, lot_id))
        except Exception:
            log.exception("Telegram Text: failed to read title for bound lot %s", lot_id)
    with _lock:
        _lots[lot_id] = {"title": title, "enabled": True, "updated_at": time.time()}
        save_json(LOTS_FILE, _lots)
    if title:
        panel(message, f"✅ Лот <code>{lot_id}</code> привязан.\n\nНазвание: {html.escape(title)}")
    else:
        panel(message, f"✅ Лот <code>{lot_id}</code> привязан.\n\nНазвание не удалось прочитать автоматически; при покупке плагин попробует обновить его.")


def cmd_unbind(message):
    if not authorized(message):
        return
    parts = text_of(message).split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        panel(message, "Использование: /text_unbind LOT_ID")
        return
    lot_id = str(int(parts[1]))
    with _lock:
        existed = _lots.pop(lot_id, None)
        save_json(LOTS_FILE, _lots)
    panel(message, "✅ Лот отвязан." if existed else "ℹ️ Такой лот не был привязан.")


def bind_order(c, event):
    """NewOrderEvent handler following the working Gifts plugin architecture."""
    try:
        oid = order_id_from_event(event)
        if not oid:
            log.warning("Telegram Text: NewOrderEvent without order id")
            return
        with _lock:
            if oid in _orders:
                return

        order = full_order(c, event)
        title = order_title(order, event)
        lot_id = bound_lot_for_order(c, event, order)
        log.info("Telegram Text: new order=%s title=%r matched_bound_lot=%r", oid, title, lot_id)
        if lot_id is None:
            return

        chat_id = order_chat_id(order, event)
        buyer = order_buyer(order, event)
        buyer_id = order_buyer_id(order, event)
        if chat_id is None and buyer:
            try:
                chat = c.account.get_chat_by_name(buyer, True)
            except TypeError:
                chat = c.account.get_chat_by_name(buyer)
            except Exception:
                log.debug("Telegram Text: get_chat_by_name failed", exc_info=True)
                chat = None
            chat_id = getattr(chat, "id", None) if chat else None
        if chat_id is None:
            log.error("Telegram Text: order %s has no FunPay chat id", oid)
            return

        with _lock:
            _orders[oid] = {
                "order_id": oid,
                "lot_id": str(lot_id),
                "lot_title": title,
                "chat_id": chat_id,
                "buyer": buyer,
                "buyer_id": buyer_id,
                "username": None,
                "text": None,
                "status": S_USERNAME,
                "created_at": time.time(),
                "error": None,
            }
            save_json(ORDERS_FILE, _orders)

        send_funpay(chat_id, "👋 Спасибо за покупку!\n\nОтправьте Telegram username, куда нужно отправить текст.\n\nПример: @username\n\n❗ Для отмены заказа отправьте: !возврат")
    except Exception:
        log.exception("Telegram Text: new order handler failed")


def handle_message(c, event):
    """Process a real FunPay Message; the old-mode adapter also feeds Messages here."""
    try:
        message = getattr(event, "message", None) or event
        author_id = getattr(message, "author_id", None)
        own_id = getattr(getattr(c, "account", None), "id", None)
        if author_id is not None and own_id is not None and str(author_id) == str(own_id):
            return
        if getattr(message, "by_bot", False):
            return
        value = text_of(message)
        if not value:
            return

        found = find_order(message)
        if not found or found[0] is None:
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
            send_funpay(chat_id, f"📋 Получатель: {username}\n\nЕсли всё верно — отправьте «+».\nЕсли хотите изменить — отправьте новый @username.\n\n❗ Для возврата: !возврат")
            return
        if status == S_CONFIRM:
            if is_plus(value):
                update_order(oid, status=S_TEXT, error=None)
                send_funpay(chat_id, "💬 Отлично. Теперь отправьте текст, который нужно отправить этому пользователю.\n\nСледующее сообщение будет отправлено как текст.\n\n❗ Для отмены: !возврат")
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
                send_funpay(chat_id, "⚠️ Отключите плату за сообщения в Telegram, затем отправьте «+».")
                return
            if not ensure_worker():
                send_funpay(chat_id, "❌ Telegram-модуль не удалось запустить.")
                return
            update_order(oid, status=S_SENDING, error=None)
            _worker.submit(oid)
            return
        if status == S_ERROR:
            if is_plus(value) and order.get("username") and order.get("text"):
                if not ensure_worker():
                    send_funpay(chat_id, "❌ Telegram-модуль не удалось запустить.")
                    return
                update_order(oid, status=S_SENDING, error=None)
                _worker.submit(oid)
                send_funpay(chat_id, "⏳ Повторная попытка отправки запущена.")
                return
            username = normalize_username(value)
            if username:
                update_order(oid, username=username, status=S_CONFIRM, error=None)
                send_funpay(chat_id, f"📋 Получатель: {username}\n\nОтправьте + для подтверждения.")
                return
            send_funpay(chat_id, "❓ Отправьте + для повторной попытки, новый @username или !возврат.")
    except Exception:
        log.exception("Telegram Text: message handler failed")


def old_mode_message(c, event):
    if not getattr(c, "old_mode_enabled", False):
        return
    chat = getattr(event, "chat", None)
    if chat is None:
        return

    def runner():
        try:
            history = c.account.get_chat_history(chat.id, last_message_id=None, interlocutor_username=getattr(chat, "name", None))
            if not history:
                return
            account_id = getattr(c.account, "id", None)
            for message in reversed(history):
                if getattr(message, "chat_id", chat.id) is not None and str(getattr(message, "chat_id", chat.id)) != str(chat.id):
                    continue
                if account_id is not None and getattr(message, "author_id", None) is not None and str(getattr(message, "author_id", None)) == str(account_id):
                    continue
                if getattr(message, "by_bot", False):
                    continue
                if not text_of(message):
                    continue
                handle_message(c, SimpleNamespace(message=message))
                return
        except Exception:
            log.exception("Telegram Text: old mode recovery failed")

    threading.Thread(target=runner, name="telegram-text-old-mode", daemon=True).start()


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
        log.info("Telegram Text v%s loaded", VERSION)
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
BIND_TO_NEW_ORDER = [bind_order]
BIND_TO_NEW_MESSAGE = [handle_message]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [old_mode_message]
BIND_TO_POST_STOP = [post_stop]
BIND_TO_DELETE = None
