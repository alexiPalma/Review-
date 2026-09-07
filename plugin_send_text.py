# -*- coding: utf-8 -*-
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
from pathlib import Path

NAME = "Telegram Text"
VERSION = "3.3.0"
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
ORDERS = BASE / "orders.json"
LOTS = BASE / "lot_bindings.json"

USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{5,32}$")
ACTIVE = {"username", "confirm", "text", "paid"}
log = logging.getLogger("telegram_text")
_cardinal = None
_worker = None
_orders = {}
_lots = {}
_lock = threading.RLock()


def _load(path, default):
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, type(default)) else default
    except Exception:
        log.exception("Telegram Text: cannot read %s", path)
        return default


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _persist():
    with _lock:
        _save(ORDERS, _orders)
        _save(LOTS, _lots)


def _text(x):
    return str(x or "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").strip()


def _msg_text(m):
    return _text(getattr(m, "text", None) or getattr(m, "message", None))


def _plus(s):
    return _text(s) in ("+", "＋")


def _refund(s):
    return _text(s).replace(" ", "").casefold() in ("!возврат", "!refund")


def _username(s):
    s = _text(s)
    if not USERNAME_RE.fullmatch(s):
        return None
    return s if s.startswith("@") else "@" + s


def _lot_id(obj):
    if obj is None:
        return None
    if isinstance(obj, dict):
        for k in ("lot_id", "lotId", "offer_id", "offerId"):
            if obj.get(k) is not None:
                try:
                    return str(int(obj[k]))
                except Exception:
                    pass
    for k in ("lot_id", "lotId", "offer_id", "offerId"):
        v = getattr(obj, k, None)
        if v is not None:
            try:
                return str(int(v))
            except Exception:
                return str(v)
    return None


def _order_lot(event):
    order = getattr(event, "order", event)
    for obj in (order, getattr(order, "lot", None), getattr(order, "offer", None)):
        x = _lot_id(obj)
        if x is not None:
            return x
    return None


def _chat_id(order):
    for k in ("chat_id", "chatId"):
        v = getattr(order, k, None)
        if v is not None:
            return v
    return None


def _buyer(order):
    for k in ("buyer_username", "buyer", "username", "customer_username"):
        v = getattr(order, k, None)
        if v:
            return _text(v)
    return ""


def _buyer_id(order):
    for k in ("buyer_id", "buyer_user_id", "customer_id", "user_id"):
        v = getattr(order, k, None)
        if v is not None:
            return v
    return None


def _send(chat_id, text):
    if _cardinal is None or chat_id is None:
        return False
    try:
        fn = getattr(_cardinal, "send_message", None)
        if callable(fn):
            fn(chat_id, text)
            return True
        acc = getattr(_cardinal, "account", None)
        for name in ("send_message", "send_message_to_chat"):
            fn = getattr(acc, name, None)
            if callable(fn):
                fn(chat_id, text)
                return True
    except Exception:
        log.exception("Telegram Text: FunPay send failed")
    return False


def _find(msg):
    cid = getattr(msg, "chat_id", None)
    aid = getattr(msg, "author_id", None)
    author = _text(getattr(msg, "author", None)).lstrip("@").casefold()
    with _lock:
        items = [(k, v) for k, v in _orders.items() if v.get("status") in ACTIVE]
        if cid is not None:
            a = [x for x in items if str(x[1].get("chat_id")) == str(cid)]
            if a:
                return max(a, key=lambda x: x[1].get("created_at", 0))
        if aid is not None:
            a = [x for x in items if x[1].get("buyer_id") is not None and str(x[1]["buyer_id"]) == str(aid)]
            if a:
                return max(a, key=lambda x: x[1].get("created_at", 0))
        if author:
            a = [x for x in items if _text(x[1].get("buyer", "")).lstrip("@").casefold() == author]
            if a:
                return max(a, key=lambda x: x[1].get("created_at", 0))
    return None, None


def _update(oid, **kw):
    with _lock:
        if str(oid) not in _orders:
            return None
        _orders[str(oid)].update(kw)
        _orders[str(oid)]["updated_at"] = time.time()
        _persist()
        return dict(_orders[str(oid)])


def _ensure_telethon():
    if importlib.util.find_spec("telethon") is not None:
        return True
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", TELETHON_PACKAGE])
        return importlib.util.find_spec("telethon") is not None
    except Exception:
        log.exception("Telegram Text: Telethon install failed")
        return False


class Worker:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.client = None
        self.ready = threading.Event()
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return True
            self.ready.clear()
            self.thread = threading.Thread(target=self._run, daemon=True, name="telegram-text")
            self.thread.start()
        return self.ready.wait(15)

    def _run(self):
        if not _ensure_telethon():
            self.ready.set()
            return
        try:
            from telethon import TelegramClient
            self.session = SHARED_SESSION if SHARED_SESSION.exists() or Path(str(SHARED_SESSION)+".session").exists() else LEGACY_SESSION
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.client = TelegramClient(str(self.session), API_ID, API_HASH)
            self.loop.run_until_complete(self.client.connect())
            if not self.loop.run_until_complete(self.client.is_user_authorized()):
                log.error("Telegram Text: Telegram session is not authorized. Login is required in console.")
                self.loop.run_until_complete(self.client.disconnect())
                self.ready.set()
                return
            self.ready.set()
            self.loop.run_forever()
        except Exception:
            log.exception("Telegram Text: worker crashed")
            self.ready.set()

    def call(self, coro):
        if not self.start() or not self.loop or not self.client:
            raise RuntimeError("Telegram session is not ready")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=60)

    def check_paid(self, username):
        from telethon.tl.functions.users import GetFullUserRequest
        full = self.call(self.client(GetFullUserRequest(username)))
        return int(getattr(full.full_user, "send_paid_messages_stars", 0) or 0)

    def send(self, username, text):
        return self.call(self.client.send_message(username, text))


def _paid_guard(username):
    global _worker
    if _worker is None:
        _worker = Worker()
    return _worker.check_paid(username)


def _tg_send(username, text):
    global _worker
    if _worker is None:
        _worker = Worker()
    return _worker.send(username, text)


def on_new_order(c, e, *args):
    global _cardinal
    _cardinal = c
    order = getattr(e, "order", e)
    oid = getattr(order, "id", None)
    if oid is None:
        return
    lot = _order_lot(e)
    if lot is None:
        return
    with _lock:
        if str(lot) not in _lots:
            return
    chat = _chat_id(order)
    buyer = _buyer(order)
    with _lock:
        _orders[str(oid)] = {"status":"username", "lot_id":str(lot), "chat_id":chat,
                             "buyer":buyer, "buyer_id":_buyer_id(order), "created_at":time.time()}
        _persist()
    _send(chat, "🎁 Спасибо за покупку!\n\nОтправьте ваш Telegram username, куда отправить текст.\nПример: @username\n\n❗ Для отмены заказа отправьте: !возврат")


def on_new_message(c, e):
    global _cardinal
    _cardinal = c
    msg = getattr(e, "message", e)
    text = _msg_text(msg)
    if not text:
        return
    pair = _find(msg)
    if not pair:
        return
    oid, order = pair
    chat = order.get("chat_id") or getattr(msg, "chat_id", None)
    status = order.get("status")

    if _refund(text):
        if status == "completed":
            _send(chat, "ℹ️ Сообщение уже отправлено. Возврат после выдачи недоступен.")
        else:
            try:
                fn = getattr(getattr(c, "account", None), "refund", None)
                if callable(fn):
                    fn(str(oid))
                _update(oid, status="refunded")
                _send(chat, "❌ Заказ отменён.\n\nСредства возвращены.")
            except Exception:
                log.exception("Telegram Text: refund failed")
                _send(chat, "⚠️ Не удалось автоматически оформить возврат. Обратитесь к продавцу.")
        return

    if status == "username":
        u = _username(text)
        if not u:
            _send(chat, "❌ Некорректный username.\n\nОтправьте username в формате: @username")
            return
        _update(oid, status="confirm", target=u)
        _send(chat, f"📱 Получатель: {u}\n\nЕсли всё верно, отправьте + для продолжения.")
        return

    if status == "confirm":
        if not _plus(text):
            _send(chat, "❗ Для подтверждения отправьте +")
            return
        _update(oid, status="text")
        _send(chat, "✍️ Напишите текст, который нужно отправить.")
        return

    if status == "text":
        if len(text) > 4096:
            _send(chat, "❌ Текст слишком длинный. Максимум — 4096 символов.")
            return
        _update(oid, status="paid", text=text)
        try:
            stars = _paid_guard(order.get("target"))
            if stars > 0:
                _send(chat, "⚠️ Пожалуйста, отключите плату за сообщения в Telegram.\n\nПосле отключения отправьте +")
                return
            _update(oid, status="sending")
            _tg_send(order.get("target"), text)
            _update(oid, status="completed")
            _send(chat, "✅ Текст успешно отправлен!\n\nСпасибо за покупку! ❤️")
        except Exception:
            log.exception("Telegram Text: send failed")
            _update(oid, status="error")
            _send(chat, "❌ Не удалось отправить сообщение. Проверьте username получателя и состояние Telegram-сессии.")
        return

    if status == "paid" and _plus(text):
        try:
            stars = _paid_guard(order.get("target"))
            if stars > 0:
                _send(chat, "❌ Плата за сообщения всё ещё включена. Отключите её и снова отправьте +")
                return
            _update(oid, status="sending")
            _tg_send(order.get("target"), order.get("text", ""))
            _update(oid, status="completed")
            _send(chat, "✅ Текст успешно отправлен!\n\nСпасибо за покупку! ❤️")
        except Exception:
            log.exception("Telegram Text: retry send failed")
            _update(oid, status="error")
            _send(chat, "❌ Не удалось отправить сообщение. Попробуйте ещё раз или обратитесь к продавцу.")


def _tg_admin(message):
    try:
        return int(message.from_user.id) in _cardinal.telegram.authorized_users
    except Exception:
        return False


def _admin_handler(message):
    if not _tg_admin(message):
        return
    raw = _text(getattr(message, "text", ""))
    p = raw.split()
    cmd = p[0].split("@", 1)[0].casefold() if p else ""
    bot = _cardinal.telegram.bot
    if cmd in ("/lotstext", "/textlot", "/textlots"):
        if len(p) == 1 or p[1].casefold() == "list":
            with _lock:
                rows = list(_lots.keys())
            bot.send_message(message.chat.id, "📋 Привязанные лоты:\n" + ("\n".join("• "+x for x in rows) if rows else "Нет привязанных лотов."))
            return
        try:
            lot = str(int(p[1]))
        except Exception:
            bot.send_message(message.chat.id, "❌ Использование: /lotstext [id_лота]")
            return
        with _lock:
            _lots[lot] = {"enabled":True}
            _persist()
        bot.send_message(message.chat.id, f"✅ Лот {lot} привязан к Telegram Text.")
    elif cmd in ("/unlotstext", "/textlotoff") and len(p) > 1:
        try:
            lot = str(int(p[1]))
        except Exception:
            bot.send_message(message.chat.id, "❌ Неверный ID лота.")
            return
        with _lock:
            _lots.pop(lot, None)
            _persist()
        bot.send_message(message.chat.id, f"✅ Лот {lot} отвязан.")


def post_init(c):
    global _cardinal, _orders, _lots
    _cardinal = c
    with _lock:
        _orders = _load(ORDERS, {})
        _lots = _load(LOTS, {})
    try:
        c.add_telegram_commands(UUID, [
            ("lotstext", "Привязать лот к Telegram Text", True),
            ("unlotstext", "Отвязать лот от Telegram Text", True),
        ])
        c.telegram.bot.message_handler(content_types=["text"])(_admin_handler)
    except Exception:
        log.exception("Telegram Text: Telegram command registration failed")


BIND_TO_POST_INIT = [post_init]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
