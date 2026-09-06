# -*- coding: utf-8 -*-
"""Guard for Telegram Text: never spend Stars on paid messages.

The main Telegram Text plugin calls TelegramWorker.submit() after the buyer
confirms with '+'.  This companion plugin wraps that method and performs a
read-only MTProto check of userFull.send_paid_messages_stars before the real
send is queued.  If the recipient charges Stars, the order is returned to the
confirmation state and the buyer is asked to disable paid messages and press
'+ ' again.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import threading

logger = logging.getLogger("telegram_text_paid_guard")

NAME = "Telegram Text — Stars Guard"
VERSION = "1.0.0"
DESCRIPTION = "Не допускает отправку сообщений с оплатой Stars."
CREDITS = "@podarckov"
UUID = "8c0a4f1e-0f8d-4d4d-9d2f-7b1f6b6c2a41"
SETTINGS_PAGE = False
BIND_TO_DELETE = None

_PATCHED = False
_ORIGINAL_SUBMIT = None
_LOCK = threading.RLock()


def _find_main_module():
    mod = None
    try:
        mod = importlib.import_module("plugin_send_text")
    except Exception:
        pass
    if mod is not None and hasattr(mod, "TelegramWorker"):
        return mod

    import sys
    for candidate in list(sys.modules.values()):
        if candidate is None:
            continue
        if hasattr(candidate, "TelegramWorker") and hasattr(candidate, "get_order") and hasattr(candidate, "update_order"):
            return candidate
    return None


def _paid_check_sync(worker, username):
    """Run the read-only Telegram check in the worker's asyncio loop."""
    if not worker.loop or not worker.loop.is_running():
        raise RuntimeError("Telegram worker не запущен")

    async def check():
        client = await worker._get_client()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram-аккаунт не авторизован")

        # Telegram's official paid-messages API exposes the requirement on
        # userFull.send_paid_messages_stars.  A value > 0 means this session
        # would have to spend that many Stars to send a message.  A value of
        # 0 means the user is exempt; absent/None means no paid requirement.
        from telethon.tl.functions.users import GetFullUserRequest
        entity = await client.get_input_entity(username)
        full = await client(GetFullUserRequest(id=entity))
        value = getattr(full.full_user, "send_paid_messages_stars", None)
        return int(value or 0)

    return asyncio.run_coroutine_threadsafe(check(), worker.loop).result(timeout=30)


def _guarded_submit(self, oid):
    main = _find_main_module()
    if main is None:
        return _ORIGINAL_SUBMIT(self, oid)

    order = main.get_order(oid)
    if not order or not order.get("username"):
        return _ORIGINAL_SUBMIT(self, oid)

    try:
        required_stars = _paid_check_sync(self, order["username"])
    except Exception as exc:
        logger.exception("Paid Stars guard: не удалось проверить получателя %s", order.get("username"))
        # Fail closed: do NOT call the real submit when the safety check
        # cannot determine whether Telegram will charge Stars.
        main.update_order(oid, status=main.STATUS_CONFIRM, error=f"paid_check:{type(exc).__name__}")
        main.send_funpay(
            order["chat_id"],
            "⚠️ Не удалось проверить оплату за сообщение в Telegram.\n\n"
            "Сообщение не отправлено, Stars не списаны.\n"
            "Попробуйте ещё раз через несколько секунд."
        )
        return None

    if required_stars > 0:
        main.update_order(oid, status=main.STATUS_CONFIRM, error=f"paid_messages:{required_stars}")
        main.send_funpay(
            order["chat_id"],
            "⚠️ У получателя включена плата за входящие сообщения.\n\n"
            f"Telegram требует {required_stars} ⭐ за отправку.\n\n"
            "Пожалуйста, отключите плату за сообщения у получателя.\n"
            "После отключения отправьте «+»."
        )
        return None

    # Safe: Telegram reports no Stars requirement for this recipient.
    return _ORIGINAL_SUBMIT(self, oid)


def post_init(c):
    global _PATCHED, _ORIGINAL_SUBMIT
    with _LOCK:
        if _PATCHED:
            return
        main = _find_main_module()
        if main is None or not hasattr(main, "TelegramWorker"):
            logger.error("Paid Stars guard: plugin_send_text не найден")
            return
        original = getattr(main.TelegramWorker, "submit", None)
        if original is None:
            logger.error("Paid Stars guard: TelegramWorker.submit не найден")
            return
        _ORIGINAL_SUBMIT = original
        main.TelegramWorker.submit = _guarded_submit
        _PATCHED = True
        logger.info("Paid Stars guard: проверка оплаты Stars включена")


def post_stop(c):
    global _PATCHED, _ORIGINAL_SUBMIT
    with _LOCK:
        main = _find_main_module()
        if _PATCHED and main is not None and _ORIGINAL_SUBMIT is not None:
            try:
                main.TelegramWorker.submit = _ORIGINAL_SUBMIT
            except Exception:
                logger.exception("Paid Stars guard: не удалось снять patch")
        _PATCHED = False
        _ORIGINAL_SUBMIT = None


BIND_TO_POST_INIT = [post_init]
BIND_TO_POST_STOP = [post_stop]
BIND_TO_DELETE = None
