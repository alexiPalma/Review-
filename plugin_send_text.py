# Telegram Text for FunPay/Cardinal
# Full rewritten version: stable Telethon worker, Cardinal Telegram-panel auth,
# persistent order states, bound lots, username confirmation, arbitrary text delivery.

import asyncio
import importlib
import json
import os
import re
import threading
import time
import traceback
from pathlib import Path

from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PasswordTooFreshError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

try:
    from telethon.errors.rpcerrorlist import UsernameInvalidError, UsernameNotOccupiedError
except ImportError:
    UsernameInvalidError = Exception
    UsernameNotOccupiedError = Exception

VERSION = "2.2.0"
UUID = "2b5d7f4a-8c31-4e96-a172-53f9d0b64c28"
CREDITS = "@podarckov"
API_ID = int(os.getenv("TELEGRAM_API_ID", "32493973"))
SESSION_NAME = "telegram_text"
TELETHON_PACKAGE = "telethon>=1.36,<2"

try:
    from telebot import types
except Exception:
    types = None

try:
    from funpaybot import plugin
except Exception:
    plugin = None

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

_cardinal = None
_worker = None
_state_lock = threading.RLock()
_auth_lock = threading.RLock()
_auth_states = {}
_state = {"orders": {}, "lots": {}}


def _clean(value):
    return str(value or "").strip()


def _message_text(message):
    return _clean(getattr(message, "text", None) or getattr(message, "message", None))


def _username(value):
    value = _clean(value)
    if value.startswith("@"):
        value = value[1:]
    return "@" + value


def _valid_username(value):
    return bool(USERNAME_RE.fullmatch(_clean(value)))


def _plus(value):
    return _clean(value) == "+"


def _refund(value):
    return _clean(value).lower() == "!возврат"


def _read_json(path, default):
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def _write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_state():
    global _state
    with _state_lock:
        _state = _read_json(STATE_FILE, {"orders": {}, "lots": {}})
        if not isinstance(_state.get("orders"), dict):
            _state["orders"] = {}
        if not isinstance(_state.get("lots"), dict):
            _state["lots"] = {}
        # Never auto-send after a Cardinal restart: a previous send may have
        # reached Telegram even if the process died before saving COMPLETED.
        changed = False
        for order in _state["orders"].values():
            if order.get("status") == STATUS_SENDING:
                order["status"] = STATUS_ERROR
                order["error"] = "Перезапуск Cardinal во время отправки. Результат нужно проверить перед повтором."
                changed = True
        if changed:
            _write_json(STATE_FILE, _state)
        return _state


def _save_state():
    with _state_lock:
        _write_json(STATE_FILE, _state)


def _save_orders():
    with _state_lock:
        _write_json(ORDERS_FILE, _state.get("orders", {}))
        _write_json(LOT_BINDINGS_FILE, _state.get("lots", {}))


def _lot_id(lot):
    for attr in ("id", "lot_id"):
        value = getattr(lot, attr, None)
        if value is not None:
            return str(value)
    if isinstance(lot, dict):
        for key in ("id", "lot_id"):
            if lot.get(key) is not None:
                return str(lot[key])
    return ""


def _full_order(order):
    return order


def _bound_lot(lot_id):
    with _state_lock:
        return str(lot_id) in _state["lots"]


def _find_order(order_id):
    with _state_lock:
        return _state["orders"].get(str(order_id))


def _update(order_id, **fields):
    with _state_lock:
        order = _state["orders"].get(str(order_id))
        if not order:
            return None
        order.update(fields)
        order["updated_at"] = time.time()
        _save_state()
        _save_orders()
        return dict(order)


def _send_fp(chat_id, text):
    try:
        if _cardinal is None or not getattr(_cardinal, "telegram", None):
            return False
        bot = getattr(_cardinal.telegram, "bot", None)
        if bot is None:
            return False
        bot.send_message(chat_id, text)
        return True
    except Exception:
        return False


def _authorized(message):
    try:
        users = getattr(_cardinal.telegram, "authorized_users", set())
        return int(message.from_user.id) in users
    except Exception:
        return False


def _tg_send(chat_id, text):
    try:
        if _cardinal is None or not getattr(_cardinal, "telegram", None):
            return False
        bot = getattr(_cardinal.telegram, "bot", None)
        if bot is None:
            return False
        bot.send_message(chat_id, text)
        return True
    except Exception:
        return False


def _api_hash():
    value = os.getenv("TELEGRAM_API_HASH")
    if value:
        return value
    root = Path("storage") / "plugins"
    target = root / "7f8e2d91-4b36-4c2a-9f15-6a7d83e4b102" / "plugin_delite_gift.py"
    if target.exists():
        text = target.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"^\s*API_HASH\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
        if match:
            return match.group(1)
    raise RuntimeError("TELEGRAM_API_HASH не задан")


class Worker:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.thread = None
        self.loop = None
        self.queue = None
        self.client = None
        self.ready = threading.Event()
        self.stopping = threading.Event()

    def is_running(self):
        return bool(self.thread and self.thread.is_alive() and self.loop and self.loop.is_running())

    def start(self):
        if self.is_running():
            return True
        self.stopping.clear()
        self.ready.clear()
        self.thread = threading.Thread(target=self._thread_main, name="telegram-text-worker", daemon=True)
        self.thread.start()
        if not self.ready.wait(15):
            return False
        return self.is_running()

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.queue = asyncio.Queue()
        self.loop.create_task(self._job_loop())
        self.ready.set()
        try:
            self.loop.run_forever()
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

    async def _job_loop(self):
        while not self.stopping.is_set():
            try:
                oid = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                await self._job(str(oid))
            except Exception:
                traceback.print_exc()
                _update(str(oid), status=STATUS_ERROR, error="Необработанная ошибка отправки")

    async def _client(self):
        if self.client is not None:
            return self.client
        try:
            telethon = importlib.import_module("telethon")
        except ImportError:
            import subprocess
            subprocess.check_call([os.sys.executable, "-m", "pip", "install", TELETHON_PACKAGE])
            telethon = importlib.import_module("telethon")
        TelegramClient = telethon.TelegramClient
        self.client = TelegramClient(
            str(SESSION_FILE), API_ID, _api_hash(),
            device_model="FunPay Cardinal Telegram Text",
            app_version=VERSION,
        )
        await self.client.connect()
        return self.client

    def call(self, factory, timeout=60):
        if not self.is_running():
            raise RuntimeError("Telegram worker не запущен")
        future = asyncio.run_coroutine_threadsafe(factory(), self.loop)
        return future.result(timeout=timeout)

    async def request_code(self, phone):
        client = await self._client()
        if await client.is_user_authorized():
            return "authorized"
        result = await client.send_code_request(phone)
        return result.phone_code_hash

    async def sign_in_code(self, phone, code, phone_code_hash):
        client = await self._client()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            return "authorized"
        except SessionPasswordNeededError:
            return "2fa"

    async def sign_in_password(self, password):
        client = await self._client()
        await client.sign_in(password=password)
        return "authorized"

    async def account_info(self):
        client = await self._client()
        if not await client.is_user_authorized():
            return None
        me = await client.get_me()
        return {
            "id": getattr(me, "id", ""),
            "username": getattr(me, "username", None),
            "phone": getattr(me, "phone", None),
            "name": " ".join(x for x in [getattr(me, "first_name", None), getattr(me, "last_name", None)] if x),
        }

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
                pass
        return True

    async def _send(self, username, text):
        client = await self._client()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram-аккаунт не авторизован")
        if len(text) > MAX_TEXT:
            raise ValueError("Текст длиннее лимита Telegram 4096 символов")
        entity = await client.get_entity(username)
        await client.send_message(entity, text)

    async def _job(self, oid):
        order = _find_order(oid)
        if not order or order.get("status") != STATUS_SENDING:
            return
        username = order.get("username")
        text = order.get("text", "")
        try:
            await self._send(username, text)
            _update(oid, status=STATUS_COMPLETED, completed_at=time.time(), error=None)
            _send_fp(order.get("chat_id"), "✅ Сообщение успешно отправлено! Спасибо за покупку ❤️ Если всё понравилось, будем благодарны за отзыв!")
        except FloodWaitError as exc:
            wait = int(getattr(exc, "seconds", 0) or 0)
            if wait > 300:
                _update(oid, status=STATUS_ERROR, error=f"Telegram просит подождать {wait} сек.")
                _send_fp(order.get("chat_id"), f"❌ Telegram временно ограничил отправку. Попробуй позже или напиши + для повтора после проверки.")
                return
            await asyncio.sleep(wait)
            try:
                await self._send(username, text)
                _update(oid, status=STATUS_COMPLETED, completed_at=time.time(), error=None)
                _send_fp(order.get("chat_id"), "✅ Сообщение успешно отправлено! Спасибо за покупку ❤️ Если всё понравилось, будем благодарны за отзыв!")
            except Exception as retry_exc:
                _update(oid, status=STATUS_ERROR, error=type(retry_exc).__name__)
                _send_fp(order.get("chat_id"), "❌ Не удалось отправить сообщение. Проверь текст/юз и напиши + для повтора.")
        except (UsernameInvalidError, UsernameNotOccupiedError, ValueError) as exc:
            _update(oid, status=STATUS_USERNAME, error=str(exc))
            _send_fp(order.get("chat_id"), "❌ Не удалось использовать этот юз. Отправь другой @username.")
        except Exception as exc:
            _update(oid, status=STATUS_ERROR, error=type(exc).__name__)
            _send_fp(order.get("chat_id"), "❌ Не удалось отправить сообщение. Если хочешь повторить попытку — напиши +.")

    def submit(self, oid):
        if not self.is_running():
            raise RuntimeError("Telegram worker не запущен")
        asyncio.run_coroutine_threadsafe(self.queue.put(str(oid)), self.loop)

    def stop(self):
        self.stopping.set()
        if self.loop and self.loop.is_running():
            def stopper():
                async def shutdown():
                    if self.client is not None:
                        try:
                            await self.client.disconnect()
                        except Exception:
                            pass
                    self.client = None
                    self.loop.stop()
                asyncio.create_task(shutdown())
            self.loop.call_soon_threadsafe(stopper)
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=10)
        self.thread = None


def _ensure_worker():
    global _worker
    if _cardinal is None:
        return False
    if _worker is None:
        _worker = Worker(_cardinal)
    if _worker.is_running():
        return True
    try:
        return _worker.start()
    except Exception:
        traceback.print_exc()
        return False


def _auth_start(uid, chat_id):
    with _auth_lock:
        _auth_states[uid] = {"state": "phone", "chat_id": chat_id, "started_at": time.time()}


def command_account(message):
    if not _authorized(message):
        return
    if not _ensure_worker():
        _tg_send(message.chat.id, "❌ Не удалось запустить Telegram-модуль. Проверь лог Cardinal.")
        return
    try:
        info = _worker.call(lambda: _worker.account_info(), 30)
    except Exception:
        info = None
    if info:
        username = "@" + info["username"] if info.get("username") else "нет"
        name = info.get("name") or "без имени"
        _tg_send(message.chat.id, f"📱 Telegram-аккаунт подключен\n\nИмя: {name}\nЮзер: {username}\nТелефон: {info.get('phone') or 'скрыт'}\nID: {info.get('id')}\n\nДля замены: /text_account_reset")
        return
    _auth_start(message.from_user.id, message.chat.id)
    _tg_send(message.chat.id, "📱 Введи номер телефона Telegram в международном формате, например +79991234567")


def command_account_reset(message):
    if not _authorized(message):
        return
    if not _ensure_worker():
        _tg_send(message.chat.id, "❌ Не удалось запустить Telegram-модуль. Проверь лог Cardinal.")
        return
    try:
        _worker.call(lambda: _worker.reset_session(), 30)
    except Exception as exc:
        _tg_send(message.chat.id, f"❌ Не удалось сбросить сессию: {type(exc).__name__}")
        return
    _auth_start(message.from_user.id, message.chat.id)
    _tg_send(message.chat.id, "♻️ Старый Telegram-аккаунт отключен. Введи номер нового аккаунта в международном формате.")


def telegram_auth_message(message):
    if not _authorized(message) or not _ensure_worker():
        return False
    uid = int(message.from_user.id)
    with _auth_lock:
        data = _auth_states.get(uid)
    if not data:
        return False
    text = _message_text(message)
    if not text or text.startswith("/"):
        return False
    state = data.get("state")
    try:
        if state == "phone":
            phone = re.sub(r"[\s()\-]", "", text)
            if not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
                _tg_send(data["chat_id"], "❌ Неверный номер. Введи номер в формате +79991234567")
                return True
            result = _worker.call(lambda: _worker.request_code(phone), 60)
            if result == "authorized":
                with _auth_lock:
                    _auth_states.pop(uid, None)
                _tg_send(data["chat_id"], "✅ Telegram-аккаунт уже был авторизован. Подключение готово.")
                return True
            if not result:
                raise RuntimeError("Telegram не вернул phone_code_hash")
            data.update({"phone": phone, "phone_code_hash": result, "state": "code"})
            with _auth_lock:
                _auth_states[uid] = data
            _tg_send(data["chat_id"], "📨 Код Telegram отправлен. Введи код подтверждения одним сообщением.")
            return True
        if state == "code":
            code = re.sub(r"\s", "", text)
            if not code.isdigit():
                _tg_send(data["chat_id"], "❌ Код должен содержать только цифры.")
                return True
            result = _worker.call(lambda: _worker.sign_in_code(data["phone"], code, data.get("phone_code_hash")), 60)
            if result == "2fa":
                data["state"] = "2fa"
                with _auth_lock:
                    _auth_states[uid] = data
                _tg_send(data["chat_id"], "🔐 На аккаунте включена двухэтапная проверка. Введи пароль 2FA.")
            else:
                with _auth_lock:
                    _auth_states.pop(uid, None)
                _tg_send(data["chat_id"], "✅ Telegram-аккаунт успешно подключен.")
            return True
        if state == "2fa":
            _worker.call(lambda: _worker.sign_in_password(text), 60)
            with _auth_lock:
                _auth_states.pop(uid, None)
            _tg_send(data["chat_id"], "✅ Telegram-аккаунт успешно подключен.")
            return True
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        _tg_send(data["chat_id"], "❌ Код неверный или устарел. Начни заново через /text_account.")
        with _auth_lock:
            _auth_states.pop(uid, None)
        return True
    except (PasswordHashInvalidError, PasswordTooFreshError):
        _tg_send(data["chat_id"], "❌ Неверный пароль 2FA. Начни заново через /text_account.")
        with _auth_lock:
            _auth_states.pop(uid, None)
        return True
    except (PhoneNumberInvalidError, PhoneNumberBannedError):
        _tg_send(data["chat_id"], "❌ Номер недействителен или заблокирован Telegram. Начни заново через /text_account.")
        with _auth_lock:
            _auth_states.pop(uid, None)
        return True
    except RuntimeError as exc:
        _tg_send(data["chat_id"], f"❌ Telegram-модуль не готов: {exc}")
        return True
    except Exception as exc:
        traceback.print_exc()
        _tg_send(data["chat_id"], f"❌ Ошибка авторизации: {type(exc).__name__}. Начни заново через /text_account.")
        with _auth_lock:
            _auth_states.pop(uid, None)
        return True
    return False


def command_lots(message):
    if not _authorized(message):
        return
    with _state_lock:
        lots = dict(_state["lots"])
    if not lots:
        _tg_send(message.chat.id, "📦 Привязанных лотов пока нет.")
        return
    lines = ["📦 Привязанные лоты:"]
    for lid, info in lots.items():
        title = info.get("title") if isinstance(info, dict) else ""
        lines.append(f"• {lid}" + (f" — {title}" if title else ""))
    _tg_send(message.chat.id, "\n".join(lines))


def command_bind(message):
    if not _authorized(message):
        return
    args = _message_text(message).split(maxsplit=2)
    if len(args) < 2:
        _tg_send(message.chat.id, "Использование: /text_bind <ID_лота> [название]")
        return
    lid = args[1].strip()
    title = args[2].strip() if len(args) > 2 else ""
    with _state_lock:
        _state["lots"][lid] = {"title": title, "bound_at": time.time()}
        _save_state(); _save_orders()
    _tg_send(message.chat.id, f"✅ Лот {lid} привязан к Telegram Text.")


def command_unbind(message):
    if not _authorized(message):
        return
    args = _message_text(message).split(maxsplit=1)
    if len(args) < 2:
        _tg_send(message.chat.id, "Использование: /text_unbind <ID_лота>")
        return
    lid = args[1].strip()
    with _state_lock:
        existed = _state["lots"].pop(lid, None)
        _save_state(); _save_orders()
    _tg_send(message.chat.id, "✅ Лот отвязан." if existed else "ℹ️ Такой лот не был привязан.")


def new_order_handler(order):
    try:
        lid = _lot_id(getattr(order, "lot", None)) or _lot_id(order)
        if not lid or not _bound_lot(lid):
            return
        oid = str(getattr(order, "id", None) or getattr(order, "order_id", None) or "")
        if not oid:
            return
        with _state_lock:
            if oid in _state["orders"]:
                return
            buyer = getattr(order, "buyer", None)
            chat_id = getattr(buyer, "id", None) or getattr(order, "chat_id", None) or getattr(order, "buyer_id", None)
            _state["orders"][oid] = {
                "id": oid,
                "lot_id": lid,
                "chat_id": chat_id,
                "status": STATUS_USERNAME,
                "created_at": time.time(),
                "username": None,
                "text": None,
            }
            _save_state(); _save_orders()
        _send_fp(chat_id, "Привет! Спасибо что купил мой лот, отправь мне юзернейм на который должен поступить текст.")
    except Exception:
        traceback.print_exc()


def _buyer_id(message):
    for attr in ("author_id", "user_id", "from_id"):
        value = getattr(message, attr, None)
        if value is not None:
            return str(value)
    author = getattr(message, "author", None)
    if author is not None:
        value = getattr(author, "id", None)
        if value is not None:
            return str(value)
    return None


def new_message_handler(message):
    try:
        chat_id = getattr(message, "chat_id", None) or getattr(message, "chat", None)
        if hasattr(chat_id, "id"):
            chat_id = chat_id.id
        text = _message_text(message)
        if not text or chat_id is None:
            return
        order = None
        with _state_lock:
            candidates = [o for o in _state["orders"].values() if str(o.get("chat_id")) == str(chat_id) and o.get("status") in ACTIVE]
            if not candidates:
                return
            candidates.sort(key=lambda x: x.get("created_at", 0), reverse=True)
            order = dict(candidates[0])
        oid = str(order["id"])
        sender_id = _buyer_id(message)
        seller_id = None
        try:
            seller_id = str(_cardinal.account.id)
        except Exception:
            pass
        if sender_id and seller_id and sender_id == seller_id:
            return

        status = order.get("status")
        if status == STATUS_SENDING:
            _send_fp(chat_id, "⏳ Сообщение отправляется, подожди немного.")
            return
        if status == STATUS_ERROR and _plus(text):
            _update(oid, status=STATUS_SENDING, error=None)
            if not _ensure_worker():
                _update(oid, status=STATUS_ERROR, error="worker")
                _send_fp(chat_id, "❌ Telegram-модуль не запущен.")
                return
            _worker.submit(oid)
            _send_fp(chat_id, "⏳ Повторная попытка отправки запущена.")
            return
        if status == STATUS_ERROR and _valid_username(text):
            _update(oid, username=_username(text), status=STATUS_CONFIRM, error=None)
            _send_fp(chat_id, "Хорошо, теперь напиши + чтобы подтвердить юз, если ты хочешь изменить юз — напиши новый, для возврата пропиши !возврат.")
            return
        if _refund(text):
            if status in {STATUS_COMPLETED, STATUS_REFUNDED, STATUS_SENDING}:
                _send_fp(chat_id, "❌ Возврат на этом этапе недоступен.")
                return
            try:
                account = _cardinal.account
                account.refund(oid)
                _update(oid, status=STATUS_REFUNDED, refunded_at=time.time())
                _send_fp(chat_id, "✅ Возврат оформлен.")
            except Exception as exc:
                _send_fp(chat_id, f"❌ Не удалось оформить возврат: {type(exc).__name__}")
            return
        if status == STATUS_USERNAME:
            if not _valid_username(text):
                _send_fp(chat_id, "❌ Пришли корректный @username Telegram (5–32 символа).")
                return
            _update(oid, username=_username(text), status=STATUS_CONFIRM, error=None)
            _send_fp(chat_id, "Хорошо, теперь напиши + чтобы подтвердить юз, если ты хочешь изменить юз — напиши новый, для возврата пропиши !возврат.")
            return
        if status == STATUS_CONFIRM:
            if _plus(text):
                _update(oid, status=STATUS_TEXT)
                _send_fp(chat_id, "Отлично. Теперь отправь текст, который нужно отправить на этот Telegram-юз.")
                return
            if _valid_username(text):
                _update(oid, username=_username(text), status=STATUS_CONFIRM, error=None)
                _send_fp(chat_id, "Юз обновил. Напиши + чтобы подтвердить его, или отправь новый юз. Для возврата пропиши !возврат.")
                return
            _send_fp(chat_id, "❌ Напиши + для подтверждения или отправь новый @username. Для возврата пропиши !возврат.")
            return
        if status == STATUS_TEXT:
            if len(text) > MAX_TEXT:
                _send_fp(chat_id, "❌ Telegram позволяет отправить одним сообщением максимум 4096 символов. Сократи текст и отправь его ещё раз.")
                return
            _update(oid, text=text, status=STATUS_SENDING, error=None)
            if not _ensure_worker():
                _update(oid, status=STATUS_ERROR, error="worker")
                _send_fp(chat_id, "❌ Telegram-модуль не удалось запустить. Напиши + после исправления.")
                return
            _worker.submit(oid)
            _send_fp(chat_id, "⏳ Отправляю сообщение...")
    except Exception:
        traceback.print_exc()


def pre_init(c):
    global _cardinal
    _cardinal = c
    if not getattr(c, "telegram", None):
        return
    _ensure_worker()
    tg = c.telegram
    try:
        tg.msg_handler(command_account, commands=["text_account"])
        tg.msg_handler(command_account_reset, commands=["text_account_reset"])
        tg.msg_handler(command_lots, commands=["text_lots"])
        tg.msg_handler(command_bind, commands=["text_bind"])
        tg.msg_handler(command_unbind, commands=["text_unbind"])
        tg.msg_handler(telegram_auth_message, func=lambda m: bool(_auth_states.get(int(m.from_user.id))))
    except Exception:
        traceback.print_exc()
    try:
        c.add_telegram_commands(UUID, [
            ("text_account", "Добавить/показать Telegram-аккаунт", True),
            ("text_account_reset", "Заменить Telegram-аккаунт", False),
            ("text_lots", "Привязанные лоты Telegram Text", True),
            ("text_bind", "Привязать лот Telegram Text", False),
            ("text_unbind", "Отвязать лот Telegram Text", False),
        ])
    except Exception:
        traceback.print_exc()


def post_init(c):
    global _cardinal
    _cardinal = c
    load_state()
    _ensure_worker()


def post_stop(c):
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker = None


# Cardinal hook names. Keep compatibility with both decorator-style and
# direct-discovery loaders used by different Cardinal versions.
BIND_TO_PRE_INIT = pre_init
BIND_TO_POST_INIT = post_init
BIND_TO_NEW_ORDER = new_order_handler
BIND_TO_NEW_MESSAGE = new_message_handler
BIND_TO_POST_STOP = post_stop
