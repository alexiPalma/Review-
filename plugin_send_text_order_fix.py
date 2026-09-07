# -*- coding: utf-8 -*-
"""Compatibility adapter for Telegram Text order detection.

Keeps plugin_send_text.py order/message state machine untouched and supplies it
with a reliable NewOrderEvent -> order record conversion.
"""
from __future__ import annotations

import re
import time

import plugin_send_text as target

NAME = "Telegram Text Order Fix"
VERSION = "1.0.0"
DESCRIPTION = "Совместимость определения заказов Telegram Text с разными версиями FunPayAPI."
CREDITS = "@podarckov"
UUID = "c4e5d8f1-6a22-4db1-ae2b-91f5e77f9c31"
SETTINGS_PAGE = False
BIND_TO_DELETE = None


def _clean(value):
    return target.clean(value)


def _lot_title(order, event):
    return str(
        getattr(order, "title", None)
        or getattr(order, "short_description", None)
        or getattr(order, "description", None)
        or getattr(getattr(event, "order", None), "description", None)
        or ""
    ).strip()


def _bound_lot(c, order, event):
    title = _lot_title(order, event)
    lot = target.resolve_lot(c, event, order)

    with target._lock:
        if lot is not None:
            value = target._lots.get(str(lot))
            if isinstance(value, dict) and value.get("enabled", True):
                return str(lot), value, title

        wanted = _clean(title).casefold()
        if wanted:
            for saved_lot, value in target._lots.items():
                if not isinstance(value, dict) or not value.get("enabled", True):
                    continue
                saved_title = _clean(value.get("title", "")).casefold()
                if saved_title and saved_title == wanted:
                    return str(saved_lot), value, title

        saved = list(target._lots.items())

    # Old bindings may have only the numeric lot id. Resolve their current title.
    for saved_lot, value in saved:
        if not isinstance(value, dict) or not value.get("enabled", True):
            continue
        if _clean(value.get("title", "")) or not title:
            continue
        try:
            fields = c.account.get_lot_fields(str(saved_lot))
            fetched = _clean(
                getattr(fields, "title_ru", None)
                or getattr(fields, "title_en", None)
                or getattr(fields, "title", None)
            )
        except Exception:
            target.log.exception("Telegram Text Order Fix: get_lot_fields failed for lot %s", saved_lot)
            continue
        if not fetched:
            continue
        with target._lock:
            current = target._lots.get(str(saved_lot))
            if isinstance(current, dict):
                current["title"] = fetched
                current["updated_at"] = time.time()
                target.persist()
        if fetched.casefold() == _clean(title).casefold():
            return str(saved_lot), value, title

    return None, None, title


def on_new_order(c, event):
    try:
        order = target.get_full_order(c, event)
        oid = target.order_id_from_event(event)
        if not oid:
            target.log.warning("Telegram Text Order Fix: NewOrderEvent without order id")
            return

        with target._lock:
            if oid in target._orders:
                return

        bound_lot, binding, lot_title = _bound_lot(c, order, event)
        if binding is None:
            target.log.info(
                "Telegram Text Order Fix: order %s skipped: no bound lot (title=%r)",
                oid,
                lot_title,
            )
            return

        chat_id = target.order_chat_id(order, event)
        buyer = target.order_buyer(order, event)
        buyer_id = target.order_buyer_id(order, event)

        if chat_id is None and buyer:
            try:
                chat = c.account.get_chat_by_name(buyer)
                chat_id = getattr(chat, "id", None) if chat else None
            except Exception:
                target.log.exception("Telegram Text Order Fix: get_chat_by_name failed for order %s", oid)

        if chat_id is None:
            target.log.error("Telegram Text Order Fix: order %s has no chat id", oid)
            return

        target._orders[oid] = {
            "order_id": oid,
            "lot_id": bound_lot,
            "lot_title": lot_title,
            "chat_id": chat_id,
            "buyer": buyer,
            "buyer_id": buyer_id,
            "username": None,
            "text": None,
            "status": target.S_USERNAME,
            "created_at": time.time(),
            "error": None,
        }
        target.persist()

        target.send_funpay(
            chat_id,
            "👋 Спасибо за покупку!\n\n"
            "Отправьте Telegram username, куда нужно отправить текст.\n\n"
            "Пример: @username\n\n"
            "❗ Для отмены заказа отправьте: !возврат",
        )
        target.log.info(
            "Telegram Text Order Fix: created order=%s lot=%s title=%r buyer=%r chat=%s",
            oid,
            bound_lot,
            lot_title,
            buyer,
            chat_id,
        )
    except Exception:
        target.log.exception("Telegram Text Order Fix: new order handler failed")


def post_init(c):
    target._cardinal = c
    target.BASE.mkdir(parents=True, exist_ok=True)
    target.load_state()
    target.log.info("Telegram Text Order Fix v%s loaded", VERSION)


def post_stop(c):
    pass


BIND_TO_POST_INIT = [post_init]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_POST_STOP = [post_stop]
BIND_TO_DELETE = None
