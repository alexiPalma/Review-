# -*- coding: utf-8 -*-
"""Telegram Text for FunPay Cardinal.

Покупка привязанного лота -> username -> подтверждение -> произвольный текст
-> отправка текста через подключенный Telegram-аккаунт.

Важно: Telethon импортируется только при первом обращении к Telegram, поэтому
отсутствие Telethon НЕ мешает Cardinal загрузить этот плагин.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from FunPayAPI.types import MessageTypes
except Exception:
    MessageTypes = None

NAME = "Telegram Text"
VERSION = "2.3.0"
DESCRIPTION = "Автоматическая отправка произвольного текста в Telegram после покупки привязанного лота."
CREDITS = "@podarckov"
UUID = "2b5d7f4a-8c31-4e96-a172-53f9d0b64c28"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

API_ID = int(os.getenv("TELEGRAM_API_ID", "32493973"))
SESSION_NAME = "telegram_text"
TELETHON_PACKAGE = "telethon>=1.36,<2"

PLUGIN_DIR = Path("storage") / "plugins" / UUID
PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = PLUGIN_DIR / "state.json"
ORDERS_FILE = PLUGIN_DIR / "orders.json"
LOT_BINDINGS_FILE = PLUGIN_DIR / "lot_bindings.json"
SESSION_FILE = PLUGIN_DIR / SESSION_NAME

STATUS_USERNAME = "await_username"
STATUS_CONFIRM = "await_confirm"
STATUS_TEXT = "await_text"
STATUS_SENDING = "sending"
STATUS_COMPLETED = "completed"
STATUS_REFUNDED = "refunded"
STATUS_ERROR = "error"
ACTIVE = {STATUS_USERNAME, STATUS_CONFIRM, STATUS_TEXT, STATUS_SENDING, STATUS_ERROR}
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{5,32}$")
MAX_TEXT = 4096

logger = logging.getLogger("telegram_text")
_cardinal = None
_worker = None
_state_lock = threading.RLock()
_auth_lock = threading.RLock()
_orders = {}
_lots = {}
_auth_states = {}


def _clean(value):
    return str(value or "").strip()


def _msg_text(msg):
    return _clean(getattr(msg, "text", None) or getattr(msg, "message", None))


def _normalize_username(value):
    value = _clean(value)
    if value.startswith("@"):
        value = value[1:]
    return "@" + value


def _valid_username(value):
    return bool(USERNAME_RE.fullmatch(_clean(value)))


def _is_plus(value):
    return _clean(value) == "+"


def _is_refund(value):
    return _clean(value).lower() == "!возврат"


def _load_json(path, default):
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        logger.exception("Не удалось прочитать %s", path)
        return default


def _save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_state():
    global _orders, _lots
    with _state_lock:
        combined = _load_json(STATE_FILE, {"orders": {}, "lots": {}})
        _orders = combined.get("orders", {}) if isinstance(combined.get("orders"), dict) else {}
        _lots = combined.get("lots", {}) if isinstance(combined.get("lots"), dict) else {}
        legacy_orders = _load_json(ORDERS_FILE, {})
        legacy_lots = _load_json(LOT_BINDINGS_FILE, {})
        if not _orders and legacy_orders:
            _orders = legacy_orders
        if not _lots and legacy_lots:
            _lots = legacy_lots
        changed = False
        for order in _orders.values():
            if order.get("status") == STATUS_SENDING:
                order["status"] = STATUS_ERROR
                order["error"] = "Cardinal был перезапущен во время отправки. Повторять автоматически нельзя."
                changed = True
        if changed or not STATE_FILE.exists():
            _persist()


def _persist():
    with _state_lock:
        data = {"orders": _orders, "lots": _lots}
        _save_json(STATE_FILE, data)
        _save_json(ORDERS_FILE, _orders)
        _save_json(LOT_BINDINGS_FILE, _lots)


def _update_order(oid, **fields):
    with _state_lock:
        order = _orders.get(str(oid))
        if not order:
            return None
        order.update(fields)
        order["updated_at"] = time.time()
        _persist()
        return dict(order)


def _get_order(oid):
    with _state_lock:
        order = _orders.get(str(oid))
        return dict(order) if order else None


def _send_funpay(chat_id, text):
    try:
        if _cardinal is None:
            return False
        bot = getattr(getattr(_cardinal, "telegram", None), "bot", None)
        # FunPay message sending is done through Cardinal account/chat API.
        if chat_id is None:
            return False
        account = getattr(_cardinal, "account", None)
        if account is not None:
            for name in ("send_message", "send_message_to_chat"):
                fn = getattr(account, name, None)
                if callable(fn):
                    try:
                        fn(chat_id, text)
                        return True
                    except TypeError:
                        pass
            get_chat = getattr(account, "get_chat_by_id", None)
            if callable(get_chat):
                chat = get_chat(chat_id)
                if chat:
                    send = getattr(chat, "send_message", None)
                    if callable(send):
                        send(text)
                        return True
        # Some Cardinal builds expose the order chat sender differently.
        sender = getattr(_cardinal, "send_message", None)
        if callable(sender):
            sender(chat_id, text)
            return True
    except Exception:
        logger.exception("Ошибка отправки сообщения в FunPay")
    return False


def _tg_panel_send(chat_id, text):
    try:
        bot = getattr(getattr(_cardinal, "telegram", None), "bot", None)
        if bot is not None:
            bot.send_message(chat_id, text)
            return True
    except Exception:
        logger.exception("Ошибка отправки сообщения в Telegram-панель")
    return False


def _authorized(message):
    try:
        tg = _cardinal.telegram
        return int(message.from_user.id) in tg.authorized_users
    except Exception:
        return False


def _api_hash():
    value = os.getenv("TELEGRAM_API_HASH")
    if value:
        return value
    # Reuse the API hash already configured by the original Telegram Gifts
    # plugin, without hardcoding the secret into this plugin.
    target = Path("storage/plugins/7f8e2d91-4b36-4c2a-9f15-6a7d83e4b102/plugin_delite_gift.py")
    if target.exists():
        text = target.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"^\s*API_HASH\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
        if match:
            return match.group(1)
    raise RuntimeError("TELEGRAM_API_HASH не задан")


class TelegramWorker:
    def __init__(self):
        self.thread = None
        self.loop = None
        self.queue = None
        self.client = None
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.start_lock = threading.Lock()

    def running(self):
        return bool(self.thread and self.thread.is_alive() and self.loop and self.loop.is_running())

    def start(self):
        with self.start_lock:
            if self.running():
                return True
            self.stop_event.clear()
            self.ready.clear()
            self.thread = threading.Thread(target=self._thread_main, name="telegram-text-worker", daemon=True)
            self.thread.start()
            if not self.ready.wait(15):
                logger.error("Telegram Text: worker не поднялся")
                return False
            return self.running()

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.queue = asyncio.Queue()
        self.loop.create_task(self._queue_loop())
        self.ready.set()
        try:
            self.loop.run_forever()
        except Exception:
            logger.exception("Telegram Text: event loop завершился с ошибкой")
        finally:
            try:
                pending = asyncio.all_tasks(self.loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            self.loop.close()
            self.loop = None

    async def _queue_loop(self):
        while not self.stop_event.is_set():
            try:
                oid = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                await self._process(str(oid))
            except Exception:
                logger.exception("Telegram Text: ошибка обработки заказа %s", oid)
                _update_order(str(oid), status=STATUS_ERROR, error="worker_exception")

    async def _get_client(self):
        if self.client is not None:
            if not self.client.is_connected():
                await self.client.connect()
            return self.client
        try:
            telethon = importlib.import_module("telethon")
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install", TELETHON_PACKAGE])
            telethon = importlib.import_module("telethon")
        self.client = telethon.TelegramClient(
            str(SESSION_FILE), API_ID, _api_hash(),
            device_model="FunPay Cardinal Telegram Text",
            app_version=VERSION,
        )
        await self.client.connect()
        return self.client

    def call(self, coroutine_factory, timeout=60):
        if not self.running():
            raise RuntimeError("Telegram worker не запущен")
        future = asyncio.run_coroutine_threadsafe(coroutine_factory(), self.loop)
        return future.result(timeout=timeout)

    async def account_info(self):
        client = await self._get_client()
        if not await client.is_user_authorized():
            return None
        me = await client.get_me()
        return {
            "id": me.id,
            "username": getattr(me, "username", None),
            "phone": getattr(me, "phone", None),
            "name": " ".join(x for x in (getattr(me, "first_name", None), getattr(me, "last_name", None)) if x),
        }

    async def request_code(self, phone):
        client = await self._get_client()
        if await client.is_user_authorized():
            return "authorized"
        result = await client.send_code_request(phone)
        return result.phone_code_hash

    async def sign_in_code(self, phone, code, phone_code_hash):
        client = await self._get_client()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            return "authorized"
        except Exception as exc:
            if type(exc).__name__ == "SessionPasswordNeededError":
                return "2fa"
            raise

    async def sign_in_password(self, password):
        client = await self._get_client()
        await client.sign_in(password=password)
        return "authorized"

    async def reset_session(self):
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = Path(str(SESSION_FILE) + suffix)
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                logger.warning("Не удалось удалить %s", path)
        return True

    async def _send(self, username, text):
        client = await self._get_client()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram-аккаунт не авторизован")
        if len(text) > MAX_TEXT:
            raise ValueError("Текст длиннее 4096 символов")
        await client.send_message(username, text)

    async def _process(self, oid):
        order = _get_order(oid)
        if not order or order.get("status") != STATUS_SENDING:
            return
        try:
            await self._send(order["username"], order["text"])
        except Exception as exc:
            if type(exc).__name__ in {"UsernameInvalidError", "UsernameNotOccupiedError", "PeerIdInvalidError"}:
                _update_order(oid, status=STATUS_USERNAME, error=type(exc).__name__)
                _send_funpay(order.get("chat_id"), "❌ Telegram не нашёл этот юз. Отправь другой @username.")
                return
            if type(exc).__name__ == "FloodWaitError":
                seconds = int(getattr(exc, "seconds", 0))
                if seconds <= 300:
                    _send_funpay(order.get("chat_id"), f"⏳ Telegram попросил подождать {seconds} сек. Повторяю отправку автоматически.")
                    await asyncio.sleep(seconds)
                    current = _get_order(oid)
                    if current and current.get("status") == STATUS_SENDING:
                        try:
                            await self._send(current["username"], current["text"])
                        except Exception as retry_exc:
                            _update_order(oid, status=STATUS_ERROR, error=type(retry_exc).__name__)
                            _send_funpay(order.get("chat_id"), "❌ Не удалось отправить сообщение. Напиши + для повторной попытки.")
                            return
                    else:
                        return
                else:
                    _update_order(oid, status=STATUS_ERROR, error=f"FloodWait:{seconds}")
                    _send_funpay(order.get("chat_id"), "❌ Telegram временно ограничил отправку. Напиши + позже для повторной попытки.")
                    return
            else:
                _update_order(oid, status=STATUS_ERROR, error=type(exc).__name__)
                _send_funpay(order.get("chat_id"), "❌ Не удалось отправить сообщение. Напиши + для повторной попытки.")
                return
        _update_order(oid, status=STATUS_COMPLETED, completed_at=time.time(), error=None)
        _send_funpay(order.get("chat_id"), "✅ Сообщение успешно отправлено! Спасибо за покупку ❤️ Если всё понравилось, будем благодарны за отзыв!")

    def submit(self, oid):
        if not self.running():
            raise RuntimeError("Telegram worker не запущен")
        asyncio.run_coroutine_threadsafe(self.queue.put(str(oid)), self.loop)

    def stop(self):
        self.stop_event.set()
        if self.loop and self.loop.is_running():
            def stop_loop():
                async def close():
                    if self.client is not None:
                        try:
                            await self.client.disconnect()
                        except Exception:
                            pass
                    self.client = None
                    self.loop.stop()
                asyncio.create_task(close())
            self.loop.call_soon_threadsafe(stop_loop)
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=10)
        self.thread = None


def _ensure_worker():
    global _worker
    if _worker is None:
        _worker = TelegramWorker()
    return _worker.start()


def _start_auth(message):
    uid = int(message.from_user.id)
    with _auth_lock:
        _auth_states[uid] = {"state": "phone", "chat_id": message.chat.id, "created_at": time.time()}
    _tg_panel_send(message.chat.id, "📱 Введи номер Telegram в международном формате, например +79991234567")


def command_account(message):
    if not _authorized(message):
        return
    if not _ensure_worker():
        _tg_panel_send(message.chat.id, "❌ Telegram worker не удалось запустить. Проверь лог Cardinal.")
        return
    try:
        info = _worker.call(lambda: _worker.account_info(), 30)
    except Exception as exc:
        logger.exception("/text_account")
        info = None
    if info:
        username = "@" + info["username"] if info.get("username") else "нет username"
        _tg_panel_send(message.chat.id, f"📱 <b>Telegram-аккаунт подключен</b>\n\n👤 {info.get('name') or '—'}\n🔗 {username}\n🆔 {info.get('id')}\n📞 {info.get('phone') or '—'}\n\nДля замены: /text_account_reset")
    else:
        _start_auth(message)


def command_account_reset(message):
    if not _authorized(message):
        return
    if not _ensure_worker():
        _tg_panel_send(message.chat.id, "❌ Telegram worker не удалось запустить. Проверь лог Cardinal.")
        return
    try:
        _worker.call(lambda: _worker.reset_session(), 30)
    except Exception as exc:
        _tg_panel_send(message.chat.id, f"❌ Не удалось сбросить аккаунт: {type(exc).__name__}")
        return
    _start_auth(message)


def auth_message(message):
    if not _authorized(message):
        return False
    uid = int(message.from_user.id)
    with _auth_lock:
        data = _auth_states.get(uid)
    if not data:
        return False
    text = _msg_text(message)
    if not text or text.startswith("/"):
        return False
    if not _ensure_worker():
        _tg_panel_send(data["chat_id"], "❌ Telegram worker не запущен.")
        return True
    try:
        state = data["state"]
        if state == "phone":
            phone = re.sub(r"[\s()\-]", "", text)
            if not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
                _tg_panel_send(data["chat_id"], "❌ Неверный номер. Формат: +79991234567")
                return True
            result = _worker.call(lambda: _worker.request_code(phone), 60)
            if result == "authorized":
                with _auth_lock:
                    _auth_states.pop(uid, None)
                _tg_panel_send(data["chat_id"], "✅ Этот Telegram-аккаунт уже авторизован. Готово.")
                return True
            data.update(phone=phone, phone_code_hash=result, state="code")
            with _auth_lock:
                _auth_states[uid] = data
            _tg_panel_send(data["chat_id"], "📨 Код отправлен Telegram. Введи код одним сообщением.")
            return True
        if state == "code":
            code = re.sub(r"\s", "", text)
            if not code.isdigit():
                _tg_panel_send(data["chat_id"], "❌ Код должен состоять из цифр.")
                return True
            result = _worker.call(lambda: _worker.sign_in_code(data["phone"], code, data["phone_code_hash"]), 60)
            if result == "2fa":
                data["state"] = "2fa"
                with _auth_lock:
                    _auth_states[uid] = data
                _tg_panel_send(data["chat_id"], "🔐 Введи облачный пароль 2FA.")
            else:
                with _auth_lock:
                    _auth_states.pop(uid, None)
                _tg_panel_send(data["chat_id"], "✅ Telegram-аккаунт успешно подключен.")
            return True
        if state == "2fa":
            _worker.call(lambda: _worker.sign_in_password(text), 60)
            with _auth_lock:
                _auth_states.pop(uid, None)
            _tg_panel_send(data["chat_id"], "✅ Telegram-аккаунт успешно подключен.")
            return True
    except Exception as exc:
        name = type(exc).__name__
        logger.exception("Ошибка авторизации Telegram")
        messages = {
            "PhoneCodeInvalidError": "❌ Неверный код. Начни заново через /text_account.",
            "PhoneCodeExpiredError": "❌ Код устарел. Начни заново через /text_account.",
            "PhoneNumberInvalidError": "❌ Неверный номер. Начни заново через /text_account.",
            "PhoneNumberBannedError": "❌ Этот номер заблокирован Telegram.",
            "PasswordHashInvalidError": "❌ Неверный пароль 2FA.",
        }
        _tg_panel_send(data["chat_id"], messages.get(name, f"❌ Ошибка авторизации: {name}. Начни заново через /text_account."))
        with _auth_lock:
            _auth_states.pop(uid, None)
        return True
    return False


def command_lots(message):
    if not _authorized(message):
        return
    with _state_lock:
        lots = dict(_lots)
    if not lots:
        _tg_panel_send(message.chat.id, "📦 Привязанных лотов нет.")
        return
    lines = ["📦 <b>Привязанные лоты</b>", ""]
    for lid, value in sorted(lots.items(), key=lambda x: str(x[0])):
        title = value.get("title", "") if isinstance(value, dict) else ""
        lines.append(f"• <code>{lid}</code>" + (f" — {title}" if title else ""))
    _tg_panel_send(message.chat.id, "\n".join(lines))


def command_bind(message):
    if not _authorized(message):
        return
    parts = _msg_text(message).split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        _tg_panel_send(message.chat.id, "Использование: /text_bind LOT_ID [название]")
        return
    lid = str(int(parts[1]))
    title = parts[2] if len(parts) > 2 else ""
    with _state_lock:
        _lots[lid] = {"title": title, "updated_at": time.time()}
        _persist()
    _tg_panel_send(message.chat.id, f"✅ Лот <code>{lid}</code> привязан.")


def command_unbind(message):
    if not _authorized(message):
        return
    parts = _msg_text(message).split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        _tg_panel_send(message.chat.id, "Использование: /text_unbind LOT_ID")
        return
    lid = str(int(parts[1]))
    with _state_lock:
        existed = _lots.pop(lid, None)
        _persist()
    _tg_panel_send(message.chat.id, "✅ Лот отвязан." if existed else "ℹ️ Такой лот не был привязан.")


def _extract_lot_id(event):
    order = getattr(event, "order", event)
    for obj in (order, getattr(order, "offer", None), getattr(order, "lot", None)):
        if obj is None:
            continue
        for attr in ("offer_id", "lot_id", "id"):
            value = getattr(obj, attr, None)
            if value is not None:
                return str(value)
        if isinstance(obj, dict):
            for key in ("offer_id", "lot_id", "id"):
                if obj.get(key) is not None:
                    return str(obj[key])
    return ""


def new_order_handler(c, e):
    try:
        order = getattr(e, "order", e)
        oid = str(getattr(order, "id", None) or getattr(order, "order_id", None) or "")
        lid = _extract_lot_id(e)
        if not oid or not lid:
            return
        with _state_lock:
            if lid not in _lots or oid in _orders:
                return
        chat_id = getattr(order, "chat_id", None)
        buyer_id = getattr(order, "buyer_id", None)
        buyer = getattr(order, "buyer", None)
        if chat_id is None and buyer is not None:
            chat_id = getattr(buyer, "id", None)
        record = {
            "order_id": oid,
            "lot_id": lid,
            "chat_id": chat_id,
            "buyer_id": buyer_id,
            "username": None,
            "text": None,
            "status": STATUS_USERNAME,
            "created_at": time.time(),
            "error": None,
        }
        with _state_lock:
            _orders[oid] = record
            _persist()
        _send_funpay(chat_id, "Привет! Спасибо что купил мой лот, отправь мне юзернейм на который должен поступить текст.")
    except Exception:
        logger.exception("Telegram Text: ошибка нового заказа")


def _message_chat_id(message):
    value = getattr(message, "chat_id", None)
    if value is not None:
        return value
    chat = getattr(message, "chat", None)
    return getattr(chat, "id", chat)


def new_message_handler(c, e):
    try:
        msg = getattr(e, "message", e)
        if MessageTypes is not None and getattr(msg, "type", None) not in (None, MessageTypes.NON_SYSTEM):
            return
        author_id = getattr(msg, "author_id", None)
        try:
            if author_id is not None and str(author_id) == str(c.account.id):
                return
        except Exception:
            pass
        text = _msg_text(msg)
        chat_id = _message_chat_id(msg)
        if not text or chat_id is None:
            return
        with _state_lock:
            candidates = [dict(o) for o in _orders.values() if str(o.get("chat_id")) == str(chat_id) and o.get("status") in ACTIVE]
        if not candidates:
            return
        candidates.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        order = candidates[0]
        oid = str(order["order_id"])
        status = order.get("status")

        if _is_refund(text):
            if status in {STATUS_USERNAME, STATUS_CONFIRM, STATUS_TEXT, STATUS_ERROR}:
                try:
                    c.account.refund(oid)
                    _update_order(oid, status=STATUS_REFUNDED, refunded_at=time.time())
                    _send_funpay(chat_id, "✅ Возврат оформлен.")
                except Exception as exc:
                    _send_funpay(chat_id, f"❌ Не удалось оформить возврат: {type(exc).__name__}")
            else:
                _send_funpay(chat_id, "❌ Возврат на этом этапе недоступен.")
            return

        if status == STATUS_SENDING:
            _send_funpay(chat_id, "⏳ Сообщение уже отправляется, подожди немного.")
            return

        if status == STATUS_ERROR and _is_plus(text):
            if not _ensure_worker():
                _send_funpay(chat_id, "❌ Telegram-модуль не запущен.")
                return
            _update_order(oid, status=STATUS_SENDING, error=None)
            _worker.submit(oid)
            _send_funpay(chat_id, "⏳ Повторная попытка отправки запущена.")
            return

        if status == STATUS_ERROR and _valid_username(text):
            _update_order(oid, username=_normalize_username(text), status=STATUS_CONFIRM, error=None)
            _send_funpay(chat_id, "Хорошо, теперь напиши + чтобы подтвердить юз, если ты хочешь изменить юз — напиши новый, для возврата пропиши !возврат.")
            return

        if status == STATUS_USERNAME:
            if not _valid_username(text):
                _send_funpay(chat_id, "❌ Пришли корректный @username Telegram.")
                return
            _update_order(oid, username=_normalize_username(text), status=STATUS_CONFIRM, error=None)
            _send_funpay(chat_id, "Хорошо, теперь напиши + чтобы подтвердить юз, если ты хочешь изменить юз — напиши новый, для возврата пропиши !возврат.")
            return

        if status == STATUS_CONFIRM:
            if _is_plus(text):
                _update_order(oid, status=STATUS_TEXT)
                _send_funpay(chat_id, "Отлично. Теперь отправь текст, который нужно отправить на этот Telegram-юз.")
                return
            if _valid_username(text):
                _update_order(oid, username=_normalize_username(text), status=STATUS_CONFIRM, error=None)
                _send_funpay(chat_id, "Юз обновил. Напиши + чтобы подтвердить его, или отправь новый юз. Для возврата пропиши !возврат.")
                return
            _send_funpay(chat_id, "❌ Напиши + для подтверждения или отправь новый @username. Для возврата пропиши !возврат.")
            return

        if status == STATUS_TEXT:
            if len(text) > MAX_TEXT:
                _send_funpay(chat_id, "❌ Максимум 4096 символов в одном сообщении Telegram. Сократи текст и отправь его снова.")
                return
            _update_order(oid, text=text, status=STATUS_SENDING, error=None)
            if not _ensure_worker():
                _update_order(oid, status=STATUS_ERROR, error="worker_unavailable")
                _send_funpay(chat_id, "❌ Telegram-модуль не удалось запустить. Напиши + после исправления.")
                return
            _worker.submit(oid)
            _send_funpay(chat_id, "⏳ Отправляю сообщение...")
    except Exception:
        logger.exception("Telegram Text: ошибка обработки сообщения")


def post_init(c):
    global _cardinal
    _cardinal = c
    load_state()
    if not getattr(c, "telegram", None):
        logger.warning("Telegram Text: Telegram-панель Cardinal отключена")
        return
    try:
        _ensure_worker()
        c.add_telegram_commands(UUID, [
            ("text_account", "Добавить/показать Telegram-аккаунт", True),
            ("text_account_reset", "Заменить Telegram-аккаунт", False),
            ("text_lots", "Привязанные лоты Telegram Text", True),
            ("text_bind", "Привязать лот Telegram Text", False),
            ("text_unbind", "Отвязать лот Telegram Text", False),
        ])
        tg = c.telegram
        tg.msg_handler(command_account, commands=["text_account"])
        tg.msg_handler(command_account_reset, commands=["text_account_reset"])
        tg.msg_handler(command_lots, commands=["text_lots"])
        tg.msg_handler(command_bind, commands=["text_bind"])
        tg.msg_handler(command_unbind, commands=["text_unbind"])
        tg.msg_handler(auth_message, func=lambda m: bool(_auth_states.get(int(m.from_user.id))))
        logger.info("Telegram Text: плагин загружен v%s", VERSION)
    except Exception:
        logger.exception("Telegram Text: ошибка регистрации Telegram-команд")


def post_stop(c):
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker = None
    logger.info("Telegram Text: worker остановлен")


BIND_TO_POST_INIT = [post_init]
BIND_TO_NEW_ORDER = [new_order_handler]
BIND_TO_NEW_MESSAGE = [new_message_handler]
BIND_TO_POST_STOP = [post_stop]
BIND_TO_DELETE = None
