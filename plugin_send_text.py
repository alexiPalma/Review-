# -*- coding: utf-8 -*-
"""Telegram Text for FunPay/Cardinal.

Покупка привязанного лота -> username -> подтверждение -> произвольный текст
-> отправка через Telethon. Состояние заказов сохраняется на диске.
"""
from __future__ import annotations

import asyncio
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

from FunPayAPI.types import MessageTypes

NAME = "Telegram Text"
VERSION = "2.0.0"
DESCRIPTION = "Автоматическая отправка произвольного текста в Telegram после покупки привязанного лота."
CREDITS = "@podarckov"
UUID = "2b5d7f4a-8c31-4e96-a172-53f9d0b64c28"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

API_ID = int(os.getenv("TELEGRAM_API_ID", "32493973"))
SESSION_NAME = "telegram_text"
TELETHON_PACKAGE = "telethon>=1.36,<2"
PLUGIN_DIR = os.path.join("storage", "plugins", UUID)
ORDERS_FILE = os.path.join(PLUGIN_DIR, "orders.json")
LOT_BINDINGS_FILE = os.path.join(PLUGIN_DIR, "lot_bindings.json")
SESSION_FILE = os.path.join(PLUGIN_DIR, SESSION_NAME)

logger = logging.getLogger("telegram_text")
_lock = threading.RLock()
_install_lock = threading.Lock()
_orders = {}
_bindings = {}
_cardinal = None
_worker = None

STATUS_USERNAME = "await_username"
STATUS_CONFIRM = "await_confirm"
STATUS_TEXT = "await_text"
STATUS_SENDING = "sending"
STATUS_COMPLETED = "completed"
STATUS_REFUNDED = "refunded"
STATUS_ERROR = "error"
ACTIVE = (STATUS_USERNAME, STATUS_CONFIRM, STATUS_TEXT, STATUS_SENDING, STATUS_ERROR)
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{5,32}$")
MAX_TEXT = 4096


def _api_hash():
    value = os.getenv("TELEGRAM_API_HASH")
    if value:
        return value
    # Не дублируем API hash в новом плагине. Если старый Telegram-плагин уже
    # установлен, берем тот же hash из его исходника локально.
    candidates = [
        os.path.join("storage", "plugins"),
        os.path.join("storage", "plugins", "7f8e2d91-4b36-4c2a-9f15-6a7d83e4b102"),
    ]
    rx = re.compile(r"API_HASH\s*=\s*['\"]([^'\"]+)['\"]")
    for root in candidates:
        if not os.path.isdir(root):
            continue
        paths = []
        if os.path.isfile(root):
            paths.append(root)
        else:
            for base, _, files in os.walk(root):
                for name in files:
                    if name == "plugin_delite_gift.py":
                        paths.append(os.path.join(base, name))
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    match = rx.search(f.read())
                if match:
                    return match.group(1)
            except Exception:
                pass
    raise RuntimeError("TELEGRAM_API_HASH не задан и старый Telegram-плагин не найден")


def _clean(v):
    return (unicodedata.normalize("NFKC", str(v or ""))
            .replace("\u200b", "").replace("\u200c", "")
            .replace("\u200d", "").replace("\ufeff", "").strip())


def _message_text(v):
    if v is None:
        return ""
    return (unicodedata.normalize("NFKC", str(v))
            .replace("\u200b", "").replace("\u200c", "")
            .replace("\u200d", "").replace("\ufeff", ""))


def _username(v):
    v = _clean(v)
    if not USERNAME_RE.fullmatch(v):
        return None
    return v if v.startswith("@") else "@" + v


def _refund(v):
    return _clean(v).casefold().replace(" ", "") == "!возврат"


def _plus(v):
    return _clean(v) in ("+", "＋")


def _load(path, default):
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception("Telegram Text: read error %s", path)
        return default


def _save(path, value):
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        logger.exception("Telegram Text: save error %s", path)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _save_orders():
    with _lock:
        data = dict(_orders)
    _save(ORDERS_FILE, data)


def _update(oid, **kw):
    with _lock:
        if str(oid) not in _orders:
            return False
        _orders[str(oid)].update(kw)
        data = dict(_orders)
    _save(ORDERS_FILE, data)
    return True


def load_state():
    global _orders, _bindings
    with _lock:
        _orders = _load(ORDERS_FILE, {})
        _bindings = _load(LOT_BINDINGS_FILE, {})
        if not isinstance(_orders, dict): _orders = {}
        if not isinstance(_bindings, dict): _bindings = {}
        changed = False
        # Никогда не возобновляем SENDING автоматически: это единственный
        # безопасный вариант против дубля при падении после send_message().
        for order in _orders.values():
            if order.get("status") == STATUS_SENDING:
                order["status"] = STATUS_ERROR
                order["last_error"] = "Перезапуск Cardinal прервал отправку; автоматический retry отключен для защиты от дубля."
                order["needs_manual_retry_check"] = True
                changed = True
    if changed:
        _save_orders()


def _send_fp(chat_id, text):
    if _cardinal is None or chat_id is None:
        return False
    try:
        _cardinal.send_message(chat_id, text)
        return True
    except Exception:
        logger.exception("Telegram Text: FunPay send error")
        return False


def _full_order(c, e):
    try:
        return c.account.get_order(e.order.id)
    except Exception:
        return e.order


def _lot_id(c, e, order):
    for obj in (order, getattr(e, "order", None)):
        value = getattr(obj, "lot_id", None)
        if value is not None:
            return str(value)
    return None


def _bound_lot(c, e, order):
    lot = _lot_id(c, e, order)
    if lot is None:
        return None
    with _lock:
        binding = _bindings.get(lot)
    return lot if isinstance(binding, dict) and binding.get("enabled", True) else None


def _find_order(msg):
    chat = getattr(msg, "chat_id", None)
    author_id = getattr(msg, "author_id", None)
    author = _clean(getattr(msg, "author", None)).lstrip("@").casefold()
    with _lock:
        if chat is not None:
            found = [(oid, o) for oid, o in _orders.items()
                     if str(o.get("chat_id")) == str(chat) and o.get("status") in ACTIVE]
            if found:
                return max(found, key=lambda x: float(x[1].get("created_at", 0)))
        if author:
            for oid, o in sorted(_orders.items(), key=lambda x: float(x[1].get("created_at", 0)), reverse=True):
                if o.get("status") in ACTIVE and _clean(o.get("buyer", "")).lstrip("@").casefold() == author:
                    return oid, o
        if author_id is not None:
            for oid, o in _orders.items():
                if o.get("status") in ACTIVE and o.get("buyer_id") is not None and str(o.get("buyer_id")) == str(author_id):
                    return oid, o
    return None, None


def _refund_order(oid):
    with _lock:
        order = dict(_orders.get(str(oid), {}))
    if not order or order.get("status") in (STATUS_COMPLETED, STATUS_REFUNDED, STATUS_SENDING):
        return False
    try:
        _cardinal.account.refund(str(oid))
        _update(oid, status=STATUS_REFUNDED, last_error=None, refunded_at=time.time())
        _send_fp(order.get("chat_id"), "↩️ Возврат оформлен. Спасибо за покупку!")
        return True
    except Exception as exc:
        logger.exception("Telegram Text: refund error order=%s", oid)
        _update(oid, last_error=str(exc))
        _send_fp(order.get("chat_id"), "❌ Не удалось автоматически оформить возврат. Обратитесь к продавцу.")
        return False


class Worker:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.loop = None
        self.queue = None
        self.thread = None
        self.client = None
        self.ready = threading.Event()
        self.stop_event = threading.Event()

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._main, name="telegram-text-worker", daemon=True)
        self.thread.start()
        self.ready.wait(10)

    def _main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.queue = asyncio.Queue()
        self.ready.set()
        try:
            self.loop.run_until_complete(self._run())
        except Exception:
            logger.exception("Telegram Text: worker crashed")
        finally:
            try: self.loop.run_until_complete(self._disconnect())
            except Exception: pass
            self.loop.close()

    async def _run(self):
        while not self.stop_event.is_set():
            try: oid = await asyncio.wait_for(self.queue.get(), 1)
            except asyncio.TimeoutError: continue
            try: await self._job(str(oid))
            except Exception: logger.exception("Telegram Text: job error %s", oid)

    def submit(self, oid):
        if not self.loop or not self.loop.is_running() or not self.queue: return False
        try:
            asyncio.run_coroutine_threadsafe(self.queue.put(str(oid)), self.loop).result(5)
            return True
        except Exception:
            logger.exception("Telegram Text: queue error")
            return False

    def call(self, coro):
        if not self.loop or not self.loop.is_running(): raise RuntimeError("worker is not running")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(60)

    async def _client(self):
        if self.client is not None:
            if not self.client.is_connected(): await self.client.connect()
            if await self.client.is_user_authorized(): return True
        if not importlib.util.find_spec("telethon"):
            with _install_lock:
                if not importlib.util.find_spec("telethon"):
                    subprocess.check_call([sys.executable, "-m", "pip", "install", TELETHON_PACKAGE])
        from telethon import TelegramClient
        self.client = TelegramClient(SESSION_FILE, API_ID, _api_hash(), device_model="FunPay Cardinal Telegram Text", app_version=VERSION)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            phone = input("Telegram Text: номер Telegram-аккаунта: ").strip()
            await self.client.send_code_request(phone)
            code = input("Telegram Text: код из Telegram: ").strip()
            try:
                await self.client.sign_in(phone=phone, code=code)
            except Exception as exc:
                if exc.__class__.__name__ != "SessionPasswordNeededError": raise
                await self.client.sign_in(password=input("Telegram Text: пароль 2FA: ").strip())
        return await self.client.is_user_authorized()

    async def _disconnect(self):
        if self.client is not None:
            try: await self.client.disconnect()
            except Exception: pass

    async def _send(self, username, text):
        if not await self._client(): raise RuntimeError("Telegram-аккаунт не авторизован")
        entity = await self.client.get_entity(username)
        for pos in range(0, len(text), MAX_TEXT):
            await self.client.send_message(entity, text[pos:pos + MAX_TEXT])

    async def _job(self, oid):
        with _lock: order = dict(_orders.get(str(oid), {}))
        if not order or order.get("status") != STATUS_SENDING: return
        try:
            await self._send(order["recipient"], order["text"])
            _update(oid, status=STATUS_COMPLETED, last_error=None, completed_at=time.time(), needs_manual_retry_check=False)
            _send_fp(order.get("chat_id"), "✅ Сообщение успешно отправлено! Спасибо за покупку ❤️ Если всё понравилось, будем благодарны за отзыв!")
        except Exception as exc:
            name, err = exc.__class__.__name__, str(exc)
            if name == "FloodWaitError":
                seconds = int(getattr(exc, "seconds", 0) or 0)
                if 0 < seconds <= 300:
                    await asyncio.sleep(seconds)
                    try:
                        await self._send(order["recipient"], order["text"])
                        _update(oid, status=STATUS_COMPLETED, last_error=None, completed_at=time.time())
                        _send_fp(order.get("chat_id"), "✅ Сообщение успешно отправлено! Спасибо за покупку ❤️ Если всё понравилось, будем благодарны за отзыв!")
                        return
                    except Exception as retry: name, err = retry.__class__.__name__, str(retry)
            if name in {"UsernameInvalidError", "UsernameNotOccupiedError", "PeerIdInvalidError"}:
                _update(oid, status=STATUS_USERNAME, recipient=None, last_error=err)
                _send_fp(order.get("chat_id"), "❌ Telegram не нашёл этот username. Отправьте корректный @username.")
            else:
                _update(oid, status=STATUS_ERROR, last_error=f"{name}: {err}")
                _send_fp(order.get("chat_id"), "❌ Не удалось отправить сообщение. Напишите + для повторной попытки или !возврат для возврата.")

    def info(self):
        async def f():
            if not await self._client(): return "❌ Telegram-аккаунт не авторизован."
            me = await self.client.get_me()
            name = " ".join(x for x in (getattr(me, "first_name", None), getattr(me, "last_name", None)) if x) or "—"
            user = "@" + me.username if getattr(me, "username", None) else "—"
            return f"👤 Telegram-аккаунт\nИмя: {name}\nUsername: {user}\nID: {getattr(me, 'id', None)}\nСтатус: авторизован"
        return self.call(f())

    def stop(self):
        self.stop_event.set()
        if self.loop and self.loop.is_running():
            try: asyncio.run_coroutine_threadsafe(self._disconnect(), self.loop).result(5)
            except Exception: pass
        if self.thread and self.thread.is_alive(): self.thread.join(5)
        self.thread = self.loop = self.queue = None


def _authorized(message):
    try: return message.from_user.id in _cardinal.telegram.authorized_users
    except Exception: return False


def command_lots(message):
    if not _authorized(message): return
    with _lock: bindings = dict(_bindings)
    lines = ["📦 Привязанные лоты:"] if bindings else ["📭 Нет привязанных лотов."]
    for lot, data in sorted(bindings.items()): lines.append(f"• {lot} — {'включен' if data.get('enabled', True) else 'выключен'}")
    _cardinal.telegram.send_message(message.chat.id, "\n".join(lines))


def command_bind(message):
    if not _authorized(message): return
    p = _clean(getattr(message, "text", "")).split()
    if len(p) != 2 or not p[1].isdigit():
        _cardinal.telegram.send_message(message.chat.id, "Формат: /text_bind LOT_ID"); return
    lot = str(int(p[1]))
    with _lock: _bindings[lot] = {"enabled": True, "created_at": time.time()}
    _save(LOT_BINDINGS_FILE, dict(_bindings))
    _cardinal.telegram.send_message(message.chat.id, f"✅ Лот {lot} привязан.")


def command_unbind(message):
    if not _authorized(message): return
    p = _clean(getattr(message, "text", "")).split()
    if len(p) != 2 or not p[1].isdigit():
        _cardinal.telegram.send_message(message.chat.id, "Формат: /text_unbind LOT_ID"); return
    lot = str(int(p[1]))
    with _lock: existed = _bindings.pop(lot, None)
    _save(LOT_BINDINGS_FILE, dict(_bindings))
    _cardinal.telegram.send_message(message.chat.id, f"{'✅ Лот отвязан.' if existed else 'ℹ️ Лот не был привязан.'}")


def command_account(message):
    if not _authorized(message): return
    try: text = _worker.info()
    except Exception as exc: text = f"❌ Ошибка Telegram: {exc}"
    _cardinal.telegram.send_message(message.chat.id, text)


def new_order_handler(c, event):
    global _cardinal
    _cardinal = c
    obj = getattr(event, "order", None)
    if obj is None: return
    oid = str(getattr(obj, "id", ""))
    if not oid: return
    with _lock:
        if oid in _orders: return
    order = _full_order(c, event)
    lot = _bound_lot(c, event, order)
    if lot is None: return
    chat = getattr(obj, "chat_id", None) or getattr(order, "chat_id", None)
    buyer = _clean(getattr(obj, "buyer", None) or getattr(order, "buyer", None) or getattr(obj, "buyer_username", None) or "")
    buyer_id = getattr(obj, "buyer_id", None) or getattr(order, "buyer_id", None)
    if chat is None and buyer:
        try: chat = getattr(c.account.get_chat_by_name(buyer.lstrip("@")), "id", None)
        except Exception: pass
    record = {"order_id": oid, "lot_id": lot, "buyer": buyer, "buyer_id": buyer_id, "chat_id": chat,
              "recipient": None, "text": None, "status": STATUS_USERNAME, "last_error": None, "created_at": time.time()}
    with _lock:
        if oid in _orders: return
        _orders[oid] = record
        data = dict(_orders)
    _save(ORDERS_FILE, data)
    _send_fp(chat, "Привет! Спасибо что купил мой лот, отправь мне юзернейм на который должен поступить текст.")


def _system(msg):
    t = getattr(msg, "type", None)
    vals = [getattr(MessageTypes, "SYSTEM", None), getattr(MessageTypes, "SOLD", None)]
    return t is not None and t in tuple(x for x in vals if x is not None)


def message_handler(c, event):
    global _cardinal
    _cardinal = c
    msg = getattr(event, "message", None)
    if msg is None or _system(msg): return
    aid = getattr(msg, "author_id", None)
    try:
        if aid is not None and str(aid) == str(c.account.id): return
    except Exception: pass
    raw = getattr(msg, "text", None)
    if raw is None: raw = getattr(msg, "html", None)
    text = _message_text(raw)
    if not _clean(text): return
    oid, order = _find_order(msg)
    if not order: return
    if aid is not None and order.get("buyer_id") is not None and str(aid) != str(order["buyer_id"]): return
    status = order.get("status")
    if _refund(text):
        if status in ACTIVE: _refund_order(oid)
        return
    if status == STATUS_USERNAME:
        user = _username(text)
        if not user:
            _send_fp(order.get("chat_id"), "❌ Некорректный username. Отправь @username длиной от 5 до 32 символов."); return
        _update(oid, recipient=user, status=STATUS_CONFIRM, last_error=None)
        _send_fp(order.get("chat_id"), "Хорошо, теперь напиши + чтобы подтвердить юз, если ты хочешь изменить юз — напиши новый, для возврата пропиши !возврат."); return
    if status == STATUS_CONFIRM:
        if _plus(text):
            _update(oid, status=STATUS_TEXT, last_error=None)
            _send_fp(order.get("chat_id"), "Теперь отправь текст, который нужно отправить на этот Telegram username."); return
        user = _username(text)
        if user:
            _update(oid, recipient=user, last_error=None)
            _send_fp(order.get("chat_id"), "Username изменён. Напиши + чтобы подтвердить его, или отправь новый username."); return
        _send_fp(order.get("chat_id"), "Напиши + для подтверждения username или отправь новый @username. Для возврата — !возврат."); return
    if status == STATUS_TEXT:
        if not _clean(text):
            _send_fp(order.get("chat_id"), "❌ Текст пустой. Отправь текст ещё раз."); return
        _update(oid, text=text, status=STATUS_SENDING, last_error=None, sending_at=time.time())
        _send_fp(order.get("chat_id"), "⏳ Отправляю сообщение...")
        if not _worker or not _worker.submit(oid):
            _update(oid, status=STATUS_ERROR, last_error="worker unavailable")
            _send_fp(order.get("chat_id"), "❌ Сервис отправки недоступен. Напишите + для повторной попытки или !возврат для возврата.")
        return
    if status == STATUS_SENDING:
        _send_fp(order.get("chat_id"), "⏳ Сообщение уже отправляется, дождись результата."); return
    if status == STATUS_ERROR:
        if _plus(text):
            _update(oid, status=STATUS_SENDING, last_error=None, retry_at=time.time())
            _send_fp(order.get("chat_id"), "⏳ Повторяю отправку...")
            if not _worker or not _worker.submit(oid):
                _update(oid, status=STATUS_ERROR, last_error="worker unavailable")
            return
        user = _username(text)
        if user:
            _update(oid, recipient=user, status=STATUS_CONFIRM, last_error=None)
            _send_fp(order.get("chat_id"), "Username изменён. Напиши + для подтверждения."); return
        _send_fp(order.get("chat_id"), "Напиши + для повторной отправки или !возврат для возврата.")


def post_init(c):
    global _cardinal, _worker
    _cardinal = c
    load_state()
    _worker = Worker(c)
    _worker.start()
    try:
        c.add_telegram_commands(UUID, {
            "text_account": command_account,
            "text_lots": command_lots,
            "text_bind": command_bind,
            "text_unbind": command_unbind,
        })
    except Exception:
        logger.exception("Telegram Text: command registration error")


def post_stop(c):
    global _worker
    if _worker: _worker.stop()
    _worker = None


BIND_TO_POST_INIT = [post_init]
BIND_TO_NEW_ORDER = [new_order_handler]
BIND_TO_NEW_MESSAGE = [message_handler]
BIND_TO_POST_STOP = [post_stop]
BIND_TO_DELETE = None
