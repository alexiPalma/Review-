# -*- coding: utf-8 -*-
"""Telegram Text for FunPay Cardinal.

Автоматическая отправка произвольного текста после покупки привязанного лота.

Сценарий:
1. Покупатель покупает лот.
2. Плагин просит Telegram username.
3. Покупатель подтверждает username через + или меняет его.
4. Плагин просит произвольный текст.
5. Текст отправляется через подключенный Telegram-аккаунт.
6. После успешной отправки заказ переводится в completed, повторная обработка
   и !возврат становятся недоступны.

Состояние заказов сохраняется в JSON и переживает перезапуск Cardinal.
"""

from __future__ import annotations

import asyncio
import html
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


# ============================================================
# CARDINAL PLUGIN DATA
# ============================================================

NAME = "Telegram Text"
VERSION = "1.0.0"
DESCRIPTION = "Автоматическая отправка произвольного текста в Telegram после покупки лота."
CREDITS = "@podarckov"
UUID = "2b5d7f4a-8c31-4e96-a172-53f9d0b64c28"
SETTINGS_PAGE = False
BIND_TO_DELETE = None


# ============================================================
# TELEGRAM API
# ============================================================

API_ID = 32493973
API_HASH = "e470a990253e9502835f62cc5958aed7"
SESSION_NAME = "telegram_text"
TELETHON_PACKAGE = "telethon>=1.36,<2"


# ============================================================
# PATHS / LOGGER
# ============================================================

logger = logging.getLogger("telegram_text")

PLUGIN_DIR = os.path.join("storage", "plugins", UUID)
ORDERS_FILE = os.path.join(PLUGIN_DIR, "orders.json")
LOT_BINDINGS_FILE = os.path.join(PLUGIN_DIR, "lot_bindings.json")
SESSION_FILE = os.path.join(PLUGIN_DIR, SESSION_NAME)

_orders_lock = threading.RLock()
_telethon_install_lock = threading.Lock()
_orders: dict[str, dict] = {}
_lot_bindings: dict[str, dict] = {}
_cardinal = None
_worker = None


# ============================================================
# STATUSES
# ============================================================

STATUS_USERNAME = "await_username"
STATUS_CONFIRM = "await_confirm"
STATUS_TEXT = "await_text"
STATUS_SENDING = "sending"
STATUS_COMPLETED = "completed"
STATUS_REFUNDED = "refunded"
STATUS_ERROR = "error"

ACTIVE_STATUSES = (
    STATUS_USERNAME,
    STATUS_CONFIRM,
    STATUS_TEXT,
    STATUS_SENDING,
    STATUS_ERROR,
)


# ============================================================
# DEPENDENCY
# ============================================================

def ensure_telethon() -> bool:
    if importlib.util.find_spec("telethon") is not None:
        return True

    with _telethon_install_lock:
        if importlib.util.find_spec("telethon") is not None:
            return True
        logger.warning("Telegram Text: Telethon не найден, устанавливаю зависимость...")
        try:
            subprocess.check_call([
                sys.executable,
                "-m",
                "pip",
                "install",
                TELETHON_PACKAGE,
            ])
        except Exception:
            logger.exception("Telegram Text: не удалось установить Telethon")
            return False

    return importlib.util.find_spec("telethon") is not None


# ============================================================
# JSON STATE
# ============================================================

def _load_json(path: str, default):
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value
    except Exception:
        logger.exception("Telegram Text: ошибка чтения %s", path)
        return default


def _save_json(path: str, value) -> None:
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=4)
        os.replace(tmp, path)
    except Exception:
        logger.exception("Telegram Text: ошибка сохранения %s", path)


def load_state() -> None:
    global _orders, _lot_bindings
    with _orders_lock:
        orders = _load_json(ORDERS_FILE, {})
        bindings = _load_json(LOT_BINDINGS_FILE, {})
        _orders = orders if isinstance(orders, dict) else {}
        _lot_bindings = bindings if isinstance(bindings, dict) else {}
    logger.info(
        "Telegram Text: загружено заказов: %s, привязок лотов: %s",
        len(_orders),
        len(_lot_bindings),
    )


def save_orders() -> None:
    with _orders_lock:
        data = dict(_orders)
    _save_json(ORDERS_FILE, data)


def save_bindings() -> None:
    with _orders_lock:
        data = dict(_lot_bindings)
    _save_json(LOT_BINDINGS_FILE, data)


def update_order(order_id, **kwargs) -> None:
    oid = str(order_id)
    with _orders_lock:
        if oid not in _orders:
            return
        _orders[oid].update(kwargs)
    save_orders()


# ============================================================
# TEXT / INPUT HELPERS
# ============================================================

def clean_text(value) -> str:
    return (
        unicodedata.normalize("NFKC", str(value or ""))
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
        .strip()
    )


def is_refund_command(value) -> bool:
    return clean_text(value).casefold().replace(" ", "") == "!возврат"


def is_plus(value) -> bool:
    return clean_text(value) in ("+", "＋")


USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{5,32}$")


def normalize_username(value):
    text = clean_text(value)
    if not USERNAME_RE.fullmatch(text):
        return None
    return text if text.startswith("@") else "@" + text


# ============================================================
# FUNPAY HELPERS
# ============================================================

def send_funpay_message(chat_id, text) -> bool:
    if _cardinal is None or chat_id is None:
        return False
    try:
        _cardinal.send_message(chat_id, text)
        return True
    except Exception:
        logger.exception("Telegram Text: не удалось отправить сообщение в FunPay chat=%s", chat_id)
        return False


def get_full_order(c, event):
    try:
        return c.account.get_order(event.order.id)
    except Exception:
        logger.debug("Telegram Text: get_order не сработал", exc_info=True)
        return event.order


def find_order_lot(c, event, order):
    """Определяет реальный LOT_ID, включая fallback для старых версий Cardinal."""
    direct = getattr(order, "lot_id", None)
    if direct is not None:
        return str(direct)

    direct = getattr(event.order, "lot_id", None)
    if direct is not None:
        return str(direct)

    subcategory = getattr(event.order, "subcategory", None) or getattr(order, "subcategory", None)
    description = clean_text(getattr(event.order, "description", None) or "")
    if subcategory is None or not description:
        return None

    try:
        grouped = c.profile.get_sorted_lots(2)
        lots = grouped.get(subcategory, {})
        candidates = []
        for lot in lots.values():
            parts = [
                clean_text(getattr(lot, "server", None)),
                clean_text(getattr(lot, "side", None)),
                clean_text(getattr(lot, "description", None)),
            ]
            signature = ", ".join(x for x in parts if x)
            if signature and signature in description:
                candidates.append((len(signature), lot))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return str(candidates[0][1].id)
    except Exception:
        logger.exception("Telegram Text: не удалось определить LOT_ID по профилю")

    return None


def resolve_bound_lot(c, event, order):
    lot_id = find_order_lot(c, event, order)
    if lot_id is None:
        return None
    with _orders_lock:
        binding = _lot_bindings.get(str(lot_id))
    if isinstance(binding, dict) and binding.get("enabled", True):
        return str(lot_id)
    return None


# ============================================================
# ORDER LOOKUP
# ============================================================

def find_order_by_message(msg):
    chat_id = getattr(msg, "chat_id", None)
    author_id = getattr(msg, "author_id", None)
    author = clean_text(getattr(msg, "author", None)).lstrip("@").casefold()

    with _orders_lock:
        if chat_id is not None:
            candidates = [
                (oid, order)
                for oid, order in _orders.items()
                if str(order.get("chat_id")) == str(chat_id)
                and order.get("status") in ACTIVE_STATUSES
            ]
            if candidates:
                candidates.sort(
                    key=lambda x: float(x[1].get("created_at", 0)),
                    reverse=True,
                )
                return candidates[0]

        if author:
            for oid, order in sorted(
                _orders.items(),
                key=lambda x: float(x[1].get("created_at", 0)),
                reverse=True,
            ):
                if order.get("status") not in ACTIVE_STATUSES:
                    continue
                buyer = clean_text(order.get("buyer", "")).lstrip("@").casefold()
                if buyer and buyer == author:
                    return oid, order

        if author_id is not None:
            for oid, order in _orders.items():
                if order.get("status") in ACTIVE_STATUSES and order.get("buyer_id") is not None:
                    if str(order.get("buyer_id")) == str(author_id):
                        return oid, order

    return None, None


# ============================================================
# REFUND
# ============================================================

def refund_order(order_id) -> bool:
    oid = str(order_id)
    with _orders_lock:
        order = _orders.get(oid)
    if not order:
        return False

    status = order.get("status")
    if status == STATUS_COMPLETED:
        send_funpay_message(
            order.get("chat_id"),
            "ℹ️ Сообщение уже было успешно отправлено. Возврат через !возврат после выполнения недоступен.",
        )
        return False
    if status == STATUS_SENDING:
        send_funpay_message(
            order.get("chat_id"),
            "⏳ Сообщение уже отправляется. Дождитесь результата текущей попытки.",
        )
        return False
    if status == STATUS_REFUNDED:
        return False

    try:
        _cardinal.account.refund(oid)
        update_order(
            oid,
            status=STATUS_REFUNDED,
            last_error=None,
            refunded_at=time.time(),
        )
        send_funpay_message(
            order.get("chat_id"),
            "↩️ Возврат оформлен. Спасибо за покупку!",
        )
        return True
    except Exception as exc:
        logger.exception("Telegram Text: ошибка возврата order=%s", oid)
        update_order(oid, last_error=str(exc))
        send_funpay_message(
            order.get("chat_id"),
            "❌ Не удалось автоматически оформить возврат. Обратитесь к продавцу для ручного возврата.",
        )
        return False


# ============================================================
# TELEGRAM WORKER
# ============================================================

class TelegramTextWorker:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.loop = None
        self.thread = None
        self.queue = None
        self.client = None
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.telethon_available = False

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(
            target=self._thread_main,
            name="telegram-text-worker",
            daemon=True,
        )
        self.thread.start()
        self.ready.wait(timeout=10)

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.queue = asyncio.Queue()
        self.ready.set()
        try:
            self.loop.run_until_complete(self._worker_loop())
        except Exception:
            logger.exception("Telegram Text: worker завершился с ошибкой")
        finally:
            try:
                self.loop.run_until_complete(self._disconnect())
            except Exception:
                logger.debug("Telegram Text: disconnect error", exc_info=True)
            self.loop.close()

    async def _worker_loop(self):
        while not self.stop_event.is_set():
            try:
                job = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._process_job(str(job))
            except Exception:
                logger.exception("Telegram Text: ошибка обработки job=%s", job)

    def submit(self, order_id):
        if not self.loop or not self.queue:
            return False
        try:
            asyncio.run_coroutine_threadsafe(
                self.queue.put(str(order_id)),
                self.loop,
            )
            return True
        except Exception:
            logger.exception("Telegram Text: не удалось поставить заказ в очередь")
            return False

    def run_coro(self, coro):
        if not self.loop:
            raise RuntimeError("Telegram worker не запущен")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=30)

    async def _ensure_client(self):
        if self.client is not None:
            if self.client.is_connected():
                return True
            await self.client.connect()
            return await self.client.is_user_authorized()

        if not ensure_telethon():
            self.telethon_available = False
            return False

        from telethon import TelegramClient

        self.telethon_available = True
        self.client = TelegramClient(
            SESSION_FILE,
            API_ID,
            API_HASH,
            device_model="FunPay Cardinal Telegram Text",
            system_version="Linux",
            app_version=VERSION,
            lang_code="en",
            system_lang_code="en-US",
        )
        await self.client.connect()

        if not await self.client.is_user_authorized():
            await self._authorize()

        return await self.client.is_user_authorized()

    async def _authorize(self):
        phone = input("Telegram Text: номер Telegram-аккаунта: ").strip()
        await self.client.send_code_request(phone)
        code = input("Telegram Text: код из Telegram: ").strip()
        try:
            await self.client.sign_in(phone=phone, code=code)
        except Exception as exc:
            if exc.__class__.__name__ == "SessionPasswordNeededError":
                password = input("Telegram Text: пароль 2FA: ")
                await self.client.sign_in(password=password)
            else:
                raise

    async def _disconnect(self):
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                logger.debug("Telegram Text: ошибка отключения", exc_info=True)

    async def _send_text(self, username, text):
        if not await self._ensure_client():
            raise RuntimeError("Telegram-аккаунт не авторизован")

        entity = await self.client.get_entity(username)
        await self.client.send_message(entity, text)

    async def _process_job(self, order_id):
        with _orders_lock:
            order = dict(_orders.get(str(order_id), {}))

        if not order or order.get("status") != STATUS_SENDING:
            return

        username = order.get("recipient")
        text = order.get("text")
        if not username or text is None:
            update_order(
                order_id,
                status=STATUS_ERROR,
                last_error="missing recipient or text",
            )
            send_funpay_message(
                order.get("chat_id"),
                "❌ Не хватает данных для отправки. Напишите + для повторной попытки или !возврат для возврата.",
            )
            return

        try:
            await self._send_text(username, text)
            update_order(
                order_id,
                status=STATUS_COMPLETED,
                last_error=None,
                completed_at=time.time(),
            )
            send_funpay_message(
                order.get("chat_id"),
                "✅ Сообщение успешно отправлено! Спасибо за покупку ❤️ Если всё понравилось, будем благодарны за отзыв!",
            )
        except Exception as exc:
            logger.exception(
                "Telegram Text: ошибка отправки order=%s username=%s",
                order_id,
                username,
            )
            error_name = exc.__class__.__name__
            error_text = str(exc)

            if error_name == "FloodWaitError":
                seconds = int(getattr(exc, "seconds", 0) or 0)
                if 0 < seconds <= 300:
                    await asyncio.sleep(seconds)
                    try:
                        await self._send_text(username, text)
                        update_order(
                            order_id,
                            status=STATUS_COMPLETED,
                            last_error=None,
                            completed_at=time.time(),
                        )
                        send_funpay_message(
                            order.get("chat_id"),
                            "✅ Сообщение успешно отправлено! Спасибо за покупку ❤️ Если всё понравилось, будем благодарны за отзыв!",
                        )
                        return
                    except Exception as retry_exc:
                        error_text = str(retry_exc)
                        error_name = retry_exc.__class__.__name__

            invalid_names = {
                "UsernameInvalidError",
                "UsernameNotOccupiedError",
                "PeerIdInvalidError",
                "ValueError",
            }
            if error_name in invalid_names and "username" in error_text.casefold():
                update_order(
                    order_id,
                    status=STATUS_USERNAME,
                    recipient=None,
                    last_error=error_text,
                )
                send_funpay_message(
                    order.get("chat_id"),
                    "❌ Этот Telegram username не найден. Отправьте корректный username ещё раз.",
                )
                return

            update_order(
                order_id,
                status=STATUS_ERROR,
                last_error=error_text,
            )
            send_funpay_message(
                order.get("chat_id"),
                "❌ Не удалось отправить сообщение. Проверьте username и напишите + для повторной попытки или !возврат для возврата.",
            )

    def account_info(self):
        async def _info():
            if not await self._ensure_client():
                return "❌ Telegram-аккаунт не авторизован."
            me = await self.client.get_me()
            first_name = clean_text(getattr(me, "first_name", None))
            last_name = clean_text(getattr(me, "last_name", None))
            username = getattr(me, "username", None)
            user_id = getattr(me, "id", None)
            name = " ".join(x for x in (first_name, last_name) if x) or "—"
            username_text = "@" + username if username else "—"
            return (
                "📱 Telegram-аккаунт\n"
                f"Имя: {name}\n"
                f"Username: {username_text}\n"
                f"ID: {user_id}\n"
                f"Telethon: {'OK' if self.telethon_available else 'не загружен'}"
            )

        return self.run_coro(_info())

    def stop(self):
        self.stop_event.set()
        if self.loop and self.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._disconnect(), self.loop)
            except Exception:
                pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def authorized(message) -> bool:
    try:
        return message.from_user.id in _cardinal.telegram.authorized_users
    except Exception:
        return False


def command_lots(message):
    if not authorized(message):
        return
    with _orders_lock:
        bindings = dict(_lot_bindings)
    if not bindings:
        _cardinal.telegram.send_message(message.chat.id, "📋 Привязанных лотов нет.")
        return

    lines = ["📋 Привязанные лоты:"]
    for lot_id, binding in sorted(bindings.items(), key=lambda x: x[0]):
        enabled = binding.get("enabled", True) if isinstance(binding, dict) else True
        lines.append(f"• LOT {lot_id} — {'включён' if enabled else 'выключен'}")
    _cardinal.telegram.send_message(message.chat.id, "\n".join(lines))


def command_bind(message):
    if not authorized(message):
        return
    parts = clean_text(getattr(message, "text", "")).split()
    if len(parts) != 2 or not parts[1].isdigit():
        _cardinal.telegram.send_message(message.chat.id, "Использование: /text_bind LOT_ID")
        return

    lot_id = str(int(parts[1]))
    with _orders_lock:
        _lot_bindings[lot_id] = {
            "enabled": True,
            "created_at": time.time(),
        }
    save_bindings()
    _cardinal.telegram.send_message(
        message.chat.id,
        f"✅ Лот {lot_id} привязан к Telegram Text.",
    )


def command_unbind(message):
    if not authorized(message):
        return
    parts = clean_text(getattr(message, "text", "")).split()
    if len(parts) != 2 or not parts[1].isdigit():
        _cardinal.telegram.send_message(message.chat.id, "Использование: /text_unbind LOT_ID")
        return

    lot_id = str(int(parts[1]))
    with _orders_lock:
        existed = _lot_bindings.pop(lot_id, None)
    save_bindings()
    _cardinal.telegram.send_message(
        message.chat.id,
        f"{'✅ Лот отвязан.' if existed else 'ℹ️ Такой лот не был привязан.'} LOT {lot_id}",
    )


def command_account(message):
    if not authorized(message):
        return
    try:
        text = _worker.account_info()
    except Exception as exc:
        logger.exception("Telegram Text: account_info error")
        text = f"❌ Не удалось получить данные Telegram-аккаунта: {html.escape(str(exc))}"
    _cardinal.telegram.send_message(message.chat.id, text)


# ============================================================
# ORDER HANDLER
# ============================================================

def new_order_handler(c, e):
    global _cardinal
    _cardinal = c

    oid = str(getattr(e.order, "id", ""))
    if not oid:
        return

    with _orders_lock:
        if oid in _orders:
            return

    order = get_full_order(c, e)
    lot_id = resolve_bound_lot(c, e, order)
    if lot_id is None:
        return

    chat_id = getattr(e.order, "chat_id", None) or getattr(order, "chat_id", None)
    buyer = clean_text(
        getattr(e.order, "buyer", None)
        or getattr(order, "buyer", None)
        or getattr(e.order, "buyer_username", None)
        or ""
    )
    buyer_id = getattr(e.order, "buyer_id", None) or getattr(order, "buyer_id", None)

    if chat_id is None:
        try:
            chat = c.account.get_chat_by_name(buyer.lstrip("@"))
            chat_id = getattr(chat, "id", None)
        except Exception:
            logger.debug("Telegram Text: не удалось найти chat по buyer", exc_info=True)

    amount = getattr(order, "amount", None)

    record = {
        "order_id": oid,
        "lot_id": lot_id,
        "buyer": buyer,
        "buyer_id": buyer_id,
        "chat_id": chat_id,
        "recipient": None,
        "text": None,
        "status": STATUS_USERNAME,
        "last_error": None,
        "amount": amount,
        "created_at": time.time(),
    }

    with _orders_lock:
        _orders[oid] = record
    save_orders()

    send_funpay_message(
        chat_id,
        "Привет! Спасибо что купил мой лот, отправь мне юзернейм на который должен поступить текст.",
    )


# ============================================================
# MESSAGE HANDLER
# ============================================================

def message_handler(c, e):
    global _cardinal
    _cardinal = c

    msg = getattr(e, "message", None)
    if msg is None:
        return

    msg_type = getattr(msg, "type", None)
    if msg_type in (MessageTypes.SYSTEM, MessageTypes.SOLD):
        return

    author_id = getattr(msg, "author_id", None)
    try:
        if author_id is not None and str(author_id) == str(c.account.id):
            return
    except Exception:
        pass

    text = clean_text(getattr(msg, "text", None) or getattr(msg, "html", None) or "")
    if not text:
        return

    order_id, order = find_order_by_message(msg)
    if not order:
        return

    # Проверка владельца заказа: чужой пользователь не должен менять состояние.
    if author_id is not None and order.get("buyer_id") is not None:
        if str(author_id) != str(order.get("buyer_id")):
            return

    status = order.get("status")

    # Возврат доступен только пока заказ активен и услуга не выполнена.
    if is_refund_command(text):
        if status in ACTIVE_STATUSES:
            refund_order(order_id)
        return

    if status == STATUS_USERNAME:
        username = normalize_username(text)
        if not username:
            send_funpay_message(
                order.get("chat_id"),
                "❌ Не понял username. Отправь Telegram username в формате @username.",
            )
            return

        update_order(
            order_id,
            recipient=username,
            status=STATUS_CONFIRM,
            last_error=None,
        )
        send_funpay_message(
            order.get("chat_id"),
            "Хорошо, теперь напиши + чтобы подтвердить юз, если ты хочешь изменить юз — напиши новый, для возврата пропиши !возврат.",
        )
        return

    if status == STATUS_CONFIRM:
        if is_plus(text):
            update_order(
                order_id,
                status=STATUS_TEXT,
                last_error=None,
            )
            send_funpay_message(
                order.get("chat_id"),
                "Отлично, теперь напиши текст, который нужно отправить.",
            )
            return

        username = normalize_username(text)
        if username:
            update_order(
                order_id,
                recipient=username,
                last_error=None,
            )
            send_funpay_message(
                order.get("chat_id"),
                "Username изменён. Если всё верно — отправь +. Для возврата пропиши !возврат.",
            )
            return

        send_funpay_message(
            order.get("chat_id"),
            "❌ Отправь + для подтверждения username или новый username для изменения.",
        )
        return

    if status == STATUS_TEXT:
        if not text:
            send_funpay_message(
                order.get("chat_id"),
                "❌ Текст не может быть пустым. Отправь сообщение, которое нужно переслать.",
            )
            return

        update_order(
            order_id,
            text=text,
            status=STATUS_SENDING,
            last_error=None,
            sending_at=time.time(),
        )
        send_funpay_message(
            order.get("chat_id"),
            "⏳ Отправляю сообщение, подожди немного...",
        )
        if not _worker.submit(order_id):
            update_order(
                order_id,
                status=STATUS_ERROR,
                last_error="worker unavailable",
            )
            send_funpay_message(
                order.get("chat_id"),
                "❌ Не удалось запустить отправку. Напиши + для повторной попытки или !возврат для возврата.",
            )
        return

    if status == STATUS_SENDING:
        send_funpay_message(
            order.get("chat_id"),
            "⏳ Сообщение ещё отправляется, дождись результата.",
        )
        return

    if status == STATUS_ERROR:
        if is_plus(text):
            update_order(
                order_id,
                status=STATUS_SENDING,
                last_error=None,
            )
            send_funpay_message(
                order.get("chat_id"),
                "⏳ Повторяю отправку...",
            )
            if not _worker.submit(order_id):
                update_order(order_id, status=STATUS_ERROR, last_error="worker unavailable")
            return

        username = normalize_username(text)
        if username:
            update_order(
                order_id,
                recipient=username,
                status=STATUS_CONFIRM,
                last_error=None,
            )
            send_funpay_message(
                order.get("chat_id"),
                "Username обновлён. Отправь + для подтверждения.",
            )
            return

        send_funpay_message(
            order.get("chat_id"),
            "❌ Для повторной отправки напиши +, либо укажи новый username.",
        )


# ============================================================
# LIFECYCLE
# ============================================================

def post_init(c):
    global _cardinal, _worker
    _cardinal = c
    load_state()

    _worker = TelegramTextWorker(c)
    _worker.start()

    commands = {
        "text_account": command_account,
        "text_lots": command_lots,
        "text_bind": command_bind,
        "text_unbind": command_unbind,
    }
    try:
        c.add_telegram_commands(UUID, commands)
    except Exception:
        logger.exception("Telegram Text: не удалось зарегистрировать Telegram-команды")

    logger.info("Telegram Text: плагин запущен")


def post_stop(c):
    global _worker
    if _worker is not None:
        _worker.stop()
    _worker = None
    logger.info("Telegram Text: плагин остановлен")


BIND_TO_POST_INIT = [post_init]
BIND_TO_NEW_ORDER = [new_order_handler]
BIND_TO_NEW_MESSAGE = [message_handler]
BIND_TO_POST_STOP = [post_stop]
BIND_TO_DELETE = None
