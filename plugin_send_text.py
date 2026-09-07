# -*- coding: utf-8 -*-
"""Telegram Text for FunPay Cardinal — single-file implementation."""
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

NAME = "Telegram Text"
VERSION = "4.1.0"
DESCRIPTION = "Автоматическая отправка текста после покупки привязанного лота."
CREDITS = "@podarckov"
UUID = "2b5d7f4a-8c31-4e96-a172-53f9d0b64c28"
SETTINGS_PAGE = False

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


def clean(v):
    return (unicodedata.normalize("NFKC", str(v or ""))
            .replace("\u200b", "").replace("\u200c", "")
            .replace("\u200d", "").replace("\ufeff", "").strip())


def text_of(m):
    return clean(getattr(m, "text", None) or getattr(m, "message", None))


def is_plus(v):
    return clean(v) in ("+", "＋")


def is_refund(v):
    return clean(v).replace(" ", "").casefold() in ("!возврат", "!refund")


def norm_user(v):
    v = clean(v)
    if not USERNAME_RE.fullmatch(v):
        return None
    return v if v.startswith("@") else "@" + v


def load_json(path, default):
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, type(default)) else default
    except Exception:
        log.exception("Telegram Text: read failed: %s", path)
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
        if not isinstance(_orders, dict): _orders = {}
        if not isinstance(_lots, dict): _lots = {}
        changed = False
        for o in _orders.values():
            if o.get("status") == S_SENDING:
                o["status"] = S_ERROR
                o["error"] = "Cardinal перезапустился во время отправки."
                changed = True
        if changed: persist()


def get_order(oid):
    with _lock:
        v = _orders.get(str(oid))
        return dict(v) if v else None


def update_order(oid, **fields):
    with _lock:
        v = _orders.get(str(oid))
        if not v: return None
        v.update(fields)
        v["updated_at"] = time.time()
        persist()
        return dict(v)


def send_fp(cid, message):
    if _cardinal is None or cid is None: return False
    try:
        fn = getattr(_cardinal, "send_message", None)
        if callable(fn): fn(cid, message); return True
        acc = getattr(_cardinal, "account", None)
        for name in ("send_message", "send_message_to_chat"):
            fn = getattr(acc, name, None)
            if callable(fn): fn(cid, message); return True
    except Exception:
        log.exception("Telegram Text: FunPay send failed")
    return False


def panel(message, value):
    try:
        bot = getattr(getattr(_cardinal, "telegram", None), "bot", None)
        if bot:
            bot.send_message(message.chat.id, value, parse_mode="HTML")
            return True
    except Exception:
        log.exception("Telegram Text: panel send failed")
    return False


def auth_user(message):
    try:
        return int(message.from_user.id) in _cardinal.telegram.authorized_users
    except Exception:
        return False


def event_order(event):
    return getattr(event, "order", None) or event


def event_order_id(event):
    o = event_order(event)
    v = getattr(o, "id", None)
    if v is None: v = getattr(o, "order_id", None)
    return str(v) if v is not None else ""


def full_order(c, event):
    o = event_order(event)
    oid = getattr(o, "id", None)
    if oid is not None:
        try:
            fresh = c.account.get_order(oid)
            if fresh is not None: return fresh
        except Exception:
            log.debug("get_order failed", exc_info=True)
    return o


def lot_id(obj):
    if obj is None: return None
    if isinstance(obj, dict):
        for k in ("lot_id", "lotId", "offer_id", "offerId"):
            if obj.get(k) is not None:
                try: return str(int(obj[k]))
                except Exception: return str(obj[k])
    for k in ("lot_id", "lotId", "offer_id", "offerId"):
        v = getattr(obj, k, None)
        if v is not None:
            try: return str(int(v))
            except Exception: return str(v)
    return None


def resolve_lot(c, event, order):
    objects = [order, getattr(event, "order", None), getattr(order, "lot", None),
               getattr(order, "offer", None), getattr(order, "shortcut", None),
               getattr(order, "order_shortcut", None)]
    for obj in objects:
        v = lot_id(obj)
        if v is not None: return v
    for obj in objects:
        s = clean(getattr(obj, "full_description", None) or getattr(obj, "description", None))
        m = ID_RE.search(s)
        if m: return m.group(1)
    return None


def order_chat(order, event):
    for obj in (order, getattr(event, "order", None)):
        for k in ("chat_id", "chatId"):
            v = getattr(obj, k, None)
            if v is not None: return v
    return None


def order_buyer(order, event):
    for obj in (order, getattr(event, "order", None)):
        for k in ("buyer_username", "buyer", "username", "customer_username"):
            v = getattr(obj, k, None)
            if v: return clean(v)
    return ""


def order_buyer_id(order, event):
    for obj in (order, getattr(event, "order", None)):
        for k in ("buyer_id", "buyer_user_id", "customer_id", "user_id"):
            v = getattr(obj, k, None)
            if v is not None: return v
    return None


def find_order(message):
    cid = getattr(message, "chat_id", None)
    aid = getattr(message, "author_id", None)
    au = clean(getattr(message, "author", None)).lstrip("@").casefold()
    with _lock:
        active = [(oid, o) for oid, o in _orders.items() if o.get("status") in ACTIVE]
        if cid is not None:
            x = [z for z in active if str(z[1].get("chat_id")) == str(cid)]
            if x: return max(x, key=lambda z: z[1].get("created_at", 0))
        if aid is not None:
            x = [z for z in active if z[1].get("buyer_id") is not None and str(z[1]["buyer_id"]) == str(aid)]
            if x: return max(x, key=lambda z: z[1].get("created_at", 0))
        if au:
            x = [z for z in active if clean(z[1].get("buyer", "")).lstrip("@").casefold() == au]
            if x: return max(x, key=lambda z: z[1].get("created_at", 0))
    return None, None


def refund(oid):
    o = get_order(oid)
    if not o: return False
    cid = o.get("chat_id")
    if o.get("status") == S_COMPLETED:
        send_fp(cid, "ℹ️ Сообщение уже отправлено. Возврат после выдачи недоступен.")
        return False
    if o.get("status") == S_REFUNDED:
        send_fp(cid, "ℹ️ Возврат по этому заказу уже выполнен.")
        return False
    if o.get("status") == S_SENDING:
        send_fp(cid, "⏳ Сообщение уже отправляется. Дождитесь результата.")
        return False
    try:
        fn = getattr(getattr(_cardinal, "account", None), "refund", None)
        if not callable(fn): raise RuntimeError("FunPay refund method unavailable")
        fn(str(oid))
        update_order(oid, status=S_REFUNDED, error=None)
        send_fp(cid, "❌ Заказ отменён.\n\nСредства возвращены.")
        return True
    except Exception:
        log.exception("Telegram Text: refund failed")
        send_fp(cid, "⚠️ Не удалось автоматически оформить возврат. Продавец обработает его вручную.")
        return False


def ensure_telethon():
    if importlib.util.find_spec("telethon") is not None: return True
    with _install_lock:
        if importlib.util.find_spec("telethon") is None:
            try: subprocess.check_call([sys.executable, "-m", "pip", "install", TELETHON_PACKAGE])
            except Exception: log.exception("Telethon install failed")
    return importlib.util.find_spec("telethon") is not None


class PaidMessagesRequired(Exception):
    def __init__(self, stars=0):
        self.stars = int(stars or 0)
        super().__init__(str(self.stars))


class Worker:
    def __init__(self):
        self.loop = None; self.thread = None; self.queue = None; self.client = None
        self.ready = threading.Event(); self.stop_event = threading.Event(); self.lock = threading.Lock()

    def running(self):
        return bool(self.thread and self.thread.is_alive() and self.loop and self.loop.is_running())

    def start(self):
        with self.lock:
            if self.running(): return True
            self.stop_event.clear(); self.ready.clear()
            self.thread = threading.Thread(target=self.main, name="telegram-text-worker", daemon=True)
            self.thread.start()
        return self.ready.wait(15) and self.running()

    def main(self):
        self.loop = asyncio.new_event_loop(); asyncio.set_event_loop(self.loop)
        self.queue = asyncio.Queue(); self.loop.create_task(self.queue_loop()); self.ready.set()
        try: self.loop.run_forever()
        except Exception: log.exception("Telegram Text worker crashed")
        finally:
            try:
                tasks = asyncio.all_tasks(self.loop)
                for t in tasks: t.cancel()
                if tasks: self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                self.loop.close()
            except Exception: pass
            self.loop = None

    async def queue_loop(self):
        while not self.stop_event.is_set():
            try: oid = await asyncio.wait_for(self.queue.get(), .5)
            except asyncio.TimeoutError: continue
            try: await self.process(str(oid))
            except Exception:
                log.exception("Telegram Text: processing failed")
                update_order(oid, status=S_ERROR, error="worker_exception")

    def session_path(self):
        if SHARED_SESSION.exists() or Path(str(SHARED_SESSION) + ".session").exists(): return SHARED_SESSION
        if LEGACY_SESSION.exists() or Path(str(LEGACY_SESSION) + ".session").exists(): return LEGACY_SESSION
        return SHARED_SESSION

    async def client_get(self):
        if not ensure_telethon(): raise RuntimeError("Telethon не установлен")
        if self.client is None:
            t = importlib.import_module("telethon")
            self.client = t.TelegramClient(str(self.session_path()), API_ID, API_HASH,
                                            device_model="FunPay Cardinal Telegram Text",
                                            system_version="Linux", app_version=VERSION,
                                            lang_code="en", system_lang_code="en-US")
        if not self.client.is_connected(): await self.client.connect()
        return self.client

    async def account_info(self):
        c = await self.client_get()
        if not await c.is_user_authorized(): return None
        me = await c.get_me()
        return {"id": me.id, "username": me.username, "phone": me.phone,
                "name": " ".join(x for x in (me.first_name, me.last_name) if x) or "—"}

    async def send_code(self, phone):
        c = await self.client_get()
        if await c.is_user_authorized(): return "authorized"
        r = await c.send_code_request(phone); return r.phone_code_hash

    async def sign_code(self, phone, code, h):
        c = await self.client_get()
        try:
            await c.sign_in(phone=phone, code=code, phone_code_hash=h); return "authorized"
        except Exception as e:
            if type(e).__name__ == "SessionPasswordNeededError": return "2fa"
            raise

    async def sign_password(self, password):
        c = await self.client_get(); await c.sign_in(password=password); return "authorized"

    async def paid_check(self, target):
        c = await self.client_get(); entity = await c.get_entity(target)
        v = getattr(entity, "send_paid_messages_stars", None)
        if v is not None and int(v or 0) > 0: return True, int(v)
        try:
            from telethon import functions
            full = await c(functions.users.GetFullUserRequest(id=entity))
            fu = getattr(full, "full_user", None)
            v = getattr(fu, "send_paid_messages_stars", None)
            if v is None: v = getattr(full, "send_paid_messages_stars", None)
            if v is not None and int(v) > 0: return True, int(v)
        except Exception:
            log.exception("Telegram Text: paid-message check failed")
            raise RuntimeError("Не удалось безопасно проверить плату за сообщения")
        return False, 0

    async def send_text(self, target, value):
        c = await self.client_get()
        if not await c.is_user_authorized(): raise RuntimeError("Telegram-аккаунт не авторизован")
        required, stars = await self.paid_check(target)
        if required: raise PaidMessagesRequired(stars)
        try: await c.send_message(target, value)
        except Exception as e:
            u = str(e).upper()
            if "ALLOW_PAYMENT_REQUIRED" in u or ("PAID_MESSAGES" in u and "REQUIRED" in u):
                raise PaidMessagesRequired(0)
            raise

    async def process(self, oid):
        o = get_order(oid)
        if not o or o.get("status") != S_SENDING: return
        try:
            await self.send_text(o["username"], o["text"])
        except PaidMessagesRequired as e:
            update_order(oid, status=S_PAID, paid_stars=e.stars, error="paid_messages")
            suffix = f" ({e.stars} ⭐️)" if e.stars else ""
            send_fp(o["chat_id"], "⚠️ У этого пользователя включена плата за входящие сообщения" + suffix + ".\n\nПожалуйста, отключите плату за сообщения в Telegram, после отключения отправьте «+».\n\nСообщение НЕ отправлено.")
            return
        except Exception as e:
            name = type(e).__name__
            if name in {"UsernameInvalidError", "UsernameNotOccupiedError", "PeerIdInvalidError", "UserIdInvalidError"}:
                update_order(oid, status=S_USERNAME, error=name)
                send_fp(o["chat_id"], "❌ Telegram не нашёл этот username. Отправьте другой @username.")
                return
            if name == "FloodWaitError":
                seconds = int(getattr(e, "seconds", 0) or 0)
                if 0 < seconds <= 300:
                    send_fp(o["chat_id"], f"⏳ Telegram попросил подождать {seconds} сек. Повторяю автоматически.")
                    await asyncio.sleep(seconds)
                    if get_order(oid) and get_order(oid).get("status") == S_SENDING: await self.process(oid)
                    return
            log.exception("Telegram Text: send failed")
            update_order(oid, status=S_ERROR, error=name)
            send_fp(o["chat_id"], "❌ Не удалось отправить сообщение. Отправьте + для повторной попытки или !возврат.")
            return
        update_order(oid, status=S_COMPLETED, completed_at=time.time(), error=None)
        send_fp(o["chat_id"], "✅ Сообщение успешно отправлено!\n\nСпасибо за покупку ❤️")

    def submit(self, oid):
        if not self.running(): raise RuntimeError("Telegram worker не запущен")
        asyncio.run_coroutine_threadsafe(self.queue.put(str(oid)), self.loop)

    def call(self, coro_factory, timeout=90):
        if not self.running(): raise RuntimeError("Telegram worker не запущен")
        return asyncio.run_coroutine_threadsafe(coro_factory(), self.loop).result(timeout)

    def reset(self):
        async def do():
            if self.client is not None:
                try: await self.client.disconnect()
                except Exception: pass
                self.client = None
        return self.call(do, 30)

    def stop(self):
        self.stop_event.set()
        if self.loop and self.loop.is_running():
            def close():
                async def c():
                    if self.client is not None:
                        try: await self.client.disconnect()
                        except Exception: pass
                        self.client = None
                    self.loop.stop()
                asyncio.create_task(c())
            self.loop.call_soon_threadsafe(close)
        if self.thread and self.thread.is_alive(): self.thread.join(10)
        self.thread = None


def ensure_worker():
    global _worker
    if _worker is None: _worker = Worker()
    return _worker.start()


def start_auth(message):
    with _auth_lock: _auth[int(message.from_user.id)] = {"state": "phone"}
    panel(message, "📱 Введите номер Telegram в международном формате, например <code>+79991234567</code>")


def cmd_account(message):
    if not auth_user(message): return
    if not ensure_worker(): panel(message, "❌ Telegram worker не удалось запустить."); return
    try: info = _worker.call(lambda: _worker.account_info(), 30)
    except Exception as e: panel(message, f"❌ Ошибка проверки аккаунта: {type(e).__name__}"); return
    if not info: start_auth(message); return
    u = "@" + info["username"] if info.get("username") else "нет username"
    panel(message, f"📱 <b>Telegram-аккаунт подключен</b>\n\n👤 {html.escape(info.get('name') or '—')}\n🔗 {html.escape(u)}\n🆔 <code>{info.get('id')}</code>\n📞 <code>{html.escape(str(info.get('phone') or '—'))}</code>\n\nИспользуется session из Gift Delete.")


def cmd_reset(message):
    if not auth_user(message): return
    if ensure_worker():
        try: _worker.reset()
        except Exception: log.exception("reset failed")
    start_auth(message)


def auth_message(message):
    if not auth_user(message): return False
    uid = int(message.from_user.id)
    with _auth_lock: state = dict(_auth.get(uid) or {})
    if not state: return False
    value = text_of(message)
    if not value or value.startswith("/"): return False
    if not ensure_worker(): panel(message, "❌ Telegram worker не запущен."); return True
    try:
        if state["state"] == "phone":
            phone = re.sub(r"[\s()\-]", "", value)
            if not re.fullmatch(r"\+[1-9]\d{6,14}", phone): panel(message, "❌ Неверный номер. Формат: +79991234567"); return True
            r = _worker.call(lambda: _worker.send_code(phone), 90)
            if r == "authorized":
                with _auth_lock: _auth.pop(uid, None)
                panel(message, "✅ Telegram-аккаунт уже авторизован."); return True
            state.update(phone=phone, code_hash=r, state="code")
            with _auth_lock: _auth[uid] = state
            panel(message, "📨 Код отправлен Telegram. Введите код одним сообщением."); return True
        if state["state"] == "code":
            code = re.sub(r"\s", "", value)
            if not code.isdigit(): panel(message, "❌ Код должен состоять из цифр."); return True
            r = _worker.call(lambda: _worker.sign_code(state["phone"], code, state["code_hash"]), 90)
            if r == "2fa":
                state["state"] = "2fa"
                with _auth_lock: _auth[uid] = state
                panel(message, "🔐 Введите пароль Telegram 2FA.")
            else:
                with _auth_lock: _auth.pop(uid, None)
                panel(message, "✅ Telegram-аккаунт успешно подключен.")
            return True
        if state["state"] == "2fa":
            _worker.call(lambda: _worker.sign_password(value), 90)
            with _auth_lock: _auth.pop(uid, None)
            panel(message, "✅ Telegram-аккаунт успешно подключен."); return True
    except Exception as e:
        name = type(e).__name__
        errors = {"PhoneCodeInvalidError":"❌ Неверный код.","PhoneCodeExpiredError":"❌ Код устарел. Запустите /text_account заново.","PhoneNumberInvalidError":"❌ Неверный номер Telegram.","PhoneNumberBannedError":"❌ Этот номер заблокирован Telegram.","PasswordHashInvalidError":"❌ Неверный пароль 2FA.","ApiIdInvalidError":"❌ Telegram отклонил API ID/API HASH."}
        log.exception("auth error")
        panel(message, errors.get(name, f"❌ Ошибка авторизации: {name}"))
        with _auth_lock: _auth.pop(uid, None)
        return True
    return False


def cmd_lots(message):
    if not auth_user(message): return
    with _lock: items = dict(_lots)
    if not items: panel(message, "📦 Привязанных лотов нет."); return
    out = ["📦 <b>Привязанные лоты Telegram Text</b>", ""]
    for lid, data in sorted(items.items(), key=lambda x: str(x[0])):
        title = data.get("title", "") if isinstance(data, dict) else ""
        out.append(f"• <code>{html.escape(str(lid))}</code>" + (f" — {html.escape(title)}" if title else ""))
    panel(message, "\n".join(out))


def cmd_bind(message):
    if not auth_user(message): return
    p = text_of(message).split(maxsplit=2)
    if len(p) < 2 or not p[1].isdigit(): panel(message, "Использование: /text_bind LOT_ID [название]"); return
    lid = str(int(p[1])); title = p[2] if len(p) > 2 else ""
    with _lock: _lots[lid] = {"title": title, "enabled": True, "updated_at": time.time()}; persist()
    panel(message, f"✅ Лот <code>{lid}</code> привязан к Telegram Text.")


def cmd_unbind(message):
    if not auth_user(message): return
    p = text_of(message).split(maxsplit=1)
    if len(p) != 2 or not p[1].isdigit(): panel(message, "Использование: /text_unbind LOT_ID"); return
    lid = str(int(p[1]))
    with _lock: existed = _lots.pop(lid, None); persist()
    panel(message, "✅ Лот отвязан." if existed else "ℹ️ Такой лот не был привязан.")


def new_order(c, event):
    try:
        o = full_order(c, event); oid = event_order_id(event)
        if not oid: return
        with _lock:
            if oid in _orders: return
        lid = resolve_lot(c, event, o)
        if lid is None: return
        with _lock:
            if not _lots.get(str(lid), {}).get("enabled", True): return
        cid = order_chat_id(o, event); buy = order_buyer(o, event); bid = order_buyer_id(o, event)
        if cid is None and buy:
            try:
                ch = c.account.get_chat_by_name(buy, True); cid = ch.id if ch else None
            except Exception: pass
        if cid is None: log.error("Telegram Text: order %s has no chat id", oid); return
        rec = {"order_id":oid,"lot_id":str(lid),"chat_id":cid,"buyer":buy,"buyer_id":bid,"username":None,"text":None,"status":S_USERNAME,"created_at":time.time(),"error":None}
        with _lock: _orders[oid] = rec; persist()
        send_fp(cid, "👋 Спасибо за покупку!\n\nОтправьте Telegram username, куда нужно отправить текст.\n\nПример: @username\n\n❗ Для отмены заказа отправьте: !возврат")
    except Exception: log.exception("Telegram Text: new order failed")


def new_message(c, event):
    try:
        m = getattr(event, "message", None) or event
        aid = getattr(m, "author_id", None); own = getattr(c.account, "id", None)
        if aid is not None and own is not None and str(aid) == str(own): return
        if getattr(m, "by_bot", False): return
        value = text_of(m)
        if not value: return
        found = find_order(m)
        if not found: return
        oid, o = found; cid = getattr(m, "chat_id", None) or o.get("chat_id")
        if o.get("buyer_id") is not None and aid is not None and str(o["buyer_id"]) != str(aid): return
        status = o.get("status")
        if is_refund(value): refund(oid); return
        if status == S_SENDING: send_fp(cid, "⏳ Сообщение уже отправляется. Пожалуйста, подождите."); return
        if status == S_USERNAME:
            u = norm_user(value)
            if not u: send_fp(cid, "❌ Некорректный Telegram username. Отправьте @username или !возврат."); return
            update_order(oid, username=u, status=S_CONFIRM)
            send_fp(cid, f"📋 Получатель: {u}\n\nЕсли всё верно — отправьте «+».\nЕсли хотите изменить — отправьте новый @username.\n\n❗ Для возврата: !возврат"); return
        if status == S_CONFIRM:
            if is_plus(value):
                update_order(oid, status=S_TEXT)
                send_fp(cid, "💬 Отлично. Теперь отправьте текст, который нужно отправить этому пользователю.\n\nСледующее сообщение будет отправлено как текст.\n\n❗ Для отмены: !возврат"); return
            u = norm_user(value)
            if u:
                update_order(oid, username=u)
                send_fp(cid, f"📋 Получатель изменён: {u}\n\nЕсли всё верно — отправьте «+»."); return
            send_fp(cid, "❓ Отправьте «+» для подтверждения, новый @username или !возврат."); return
        if status == S_TEXT:
            if len(value) > MAX_TEXT: send_fp(cid, "❌ Максимальная длина текста — 4096 символов."); return
            update_order(oid, text=value, status=S_SENDING)
            if not ensure_worker():
                update_order(oid, status=S_ERROR, error="worker_unavailable")
                send_fp(cid, "❌ Telegram-модуль не удалось запустить. Отправьте + для повторной попытки или !возврат."); return
            try:
                _worker.submit(oid); send_fp(cid, "⏳ Проверяю возможность отправки и отправляю сообщение...")
            except Exception:
                update_order(oid, status=S_ERROR, error="submit_failed")
                send_fp(cid, "❌ Не удалось запустить отправку. Отправьте + для повторной попытки или !возврат.")
            return
        if status == S_PAID:
            if not is_plus(value): send_fp(cid, "⚠️ Сначала отключите плату за сообщения в Telegram, затем отправьте «+»."); return
            if not ensure_worker(): send_fp(cid, "❌ Telegram-модуль не удалось запустить."); return
            update_order(oid, status=S_SENDING)
            try: _worker.submit(oid); send_fp(cid, "⏳ Повторно проверяю плату за сообщения...")
            except Exception: update_order(oid, status=S_PAID, error="submit_failed"); send_fp(cid, "❌ Не удалось повторить отправку. Отправьте «+» ещё раз.")
            return
        if status == S_ERROR:
            if is_plus(value) and o.get("username") and o.get("text"):
                if not ensure_worker(): send_fp(cid, "❌ Telegram-модуль не удалось запустить."); return
                update_order(oid, status=S_SENDING)
                try: _worker.submit(oid); send_fp(cid, "⏳ Повторная попытка отправки запущена.")
                except Exception: update_order(oid, status=S_ERROR); send_fp(cid, "❌ Не удалось запустить повторную отправку.")
                return
            u = norm_user(value)
            if u: update_order(oid, username=u, status=S_CONFIRM); send_fp(cid, f"📋 Получатель: {u}\n\nОтправьте + для подтверждения."); return
            send_fp(cid, "❓ Отправьте + для повторной попытки, новый @username или !возврат.")
    except Exception: log.exception("Telegram Text: message handler failed")


def post_init(c):
    global _cardinal
    _cardinal = c; BASE.mkdir(parents=True, exist_ok=True); load_state()
    try:
        if not getattr(c, "telegram", None): return
        ensure_worker()
        c.add_telegram_commands(UUID, [("text_account","Показать/настроить Telegram аккаунт",True),("text_account_reset","Повторно авторизовать Telegram аккаунт",False),("text_lots","Показать привязанные лоты",True),("text_bind","Привязать лот",False),("text_unbind","Отвязать лот",False)])
        tg = c.telegram
        tg.msg_handler(cmd_account, commands=["text_account"])
        tg.msg_handler(cmd_reset, commands=["text_account_reset"])
        tg.msg_handler(cmd_lots, commands=["text_lots"])
        tg.msg_handler(cmd_bind, commands=["text_bind"])
        tg.msg_handler(cmd_unbind, commands=["text_unbind"])
        tg.msg_handler(auth_message, func=lambda m: bool(_auth.get(int(m.from_user.id))))
        log.info("Telegram Text v%s loaded", VERSION)
    except Exception: log.exception("Telegram Text: registration failed")


def post_stop(c):
    global _worker
    if _worker is not None: _worker.stop(); _worker = None


BIND_TO_POST_INIT = [post_init]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_NEW_MESSAGE = [new_message]
BIND_TO_POST_STOP = [post_stop]
BIND_TO_DELETE = None
