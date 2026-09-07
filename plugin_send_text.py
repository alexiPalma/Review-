# -*- coding: utf-8 -*-
"""Telegram Text for FunPay Cardinal.

Полный сценарий: покупка -> username -> + -> текст -> проверка paid messages -> отправка.
Поддерживает обычный и old_mode Cardinal, отдельное состояние заказа, возврат,
авторизацию Telegram и общую session Telegram Gifts.
"""
from __future__ import annotations
import asyncio, html, importlib, importlib.util, json, logging, os, re, subprocess, sys, threading, time, unicodedata
from pathlib import Path
from types import SimpleNamespace

NAME="Telegram Text"; VERSION="8.0.0"; DESCRIPTION="Автоматическая отправка текста после покупки привязанного лота."; CREDITS="@podarckov"
UUID="2b5d7f4a-8c31-4e96-a172-53f9d0b64c28"; SETTINGS_PAGE=False; BIND_TO_DELETE=None
API_ID=32493973; API_HASH="e470a990253e9502835f62cc5958aed7"; TELETHON_PACKAGE="telethon>=1.36,<2"
GIFTS_UUID="7f8e2d91-4b36-4c2a-9f15-6a7d83e4b102"
BASE=Path("storage")/"plugins"/UUID; SHARED_SESSION=Path("storage")/"plugins"/GIFTS_UUID/"telegram_gifts"; LEGACY_SESSION=BASE/"telegram_text"
ORDERS_FILE=BASE/"orders.json"; LOTS_FILE=BASE/"lot_bindings.json"; MAX_TEXT=4096
USERNAME_RE=re.compile(r"^@?[A-Za-z0-9_]{5,32}$")
S_USERNAME="username"; S_CONFIRM="confirm"; S_TEXT="text"; S_PAID="paid"; S_SENDING="sending"; S_COMPLETED="completed"; S_REFUNDED="refunded"; S_ERROR="error"
ACTIVE={S_USERNAME,S_CONFIRM,S_TEXT,S_PAID,S_SENDING,S_ERROR}
log=logging.getLogger("telegram_text"); _cardinal=None; _worker=None; _orders={}; _lots={}; _auth={}; _lock=threading.RLock(); _auth_lock=threading.RLock(); _install_lock=threading.Lock()

def clean(v): return unicodedata.normalize("NFKC",str(v or "")).replace("\u200b","").replace("\u200c","").replace("\u200d","").replace("\ufeff","").strip()
def text_of(m): return clean(getattr(m,"text",None) or getattr(m,"content",None) or getattr(m,"message",None))
def is_plus(v): return clean(v) in {"+","＋"}
def is_refund(v): return clean(v).replace(" ","").casefold() in {"!возврат","!refund"}
def norm_title(v): return re.sub(r"\s+"," ",clean(v)).casefold()
def normalize_username(v):
    v=clean(v)
    if not USERNAME_RE.fullmatch(v): return None
    return v if v.startswith("@") else "@"+v

def load_json(p,d):
    try:
        if not p.exists(): return d
        with p.open("r",encoding="utf-8") as f: x=json.load(f)
        return x if isinstance(x,type(d)) else d
    except Exception: log.exception("Telegram Text: read failed %s",p); return d

def save_json(p,v):
    p.parent.mkdir(parents=True,exist_ok=True); t=Path(str(p)+".tmp")
    with t.open("w",encoding="utf-8") as f: json.dump(v,f,ensure_ascii=False,indent=2)
    os.replace(t,p)

def load_state():
    global _orders,_lots
    with _lock:
        _orders=load_json(ORDERS_FILE,{}); _lots=load_json(LOTS_FILE,{})
        for o in _orders.values():
            if o.get("status")==S_SENDING: o["status"]=S_ERROR; o["error"]="Cardinal перезапущен во время отправки."
        save_json(ORDERS_FILE,_orders)

def get_order(oid):
    with _lock:
        x=_orders.get(str(oid)); return dict(x) if x else None

def update_order(oid,**fields):
    with _lock:
        x=_orders.get(str(oid))
        if x is None: return None
        x.update(fields); x["updated_at"]=time.time(); save_json(ORDERS_FILE,_orders); return dict(x)

def send_funpay(chat_id,text):
    if _cardinal is None or chat_id is None: return False
    for owner in (_cardinal,getattr(_cardinal,"account",None)):
        for name in ("send_message","send_message_to_chat"):
            fn=getattr(owner,name,None) if owner else None
            if callable(fn):
                try: fn(chat_id,text); return True
                except TypeError:
                    try: fn(str(chat_id),text); return True
                    except Exception: pass
                except Exception: log.exception("Telegram Text: FunPay send failed")
    log.error("Telegram Text: no FunPay send method chat=%s",chat_id); return False

def panel(m,t):
    try:
        b=getattr(getattr(_cardinal,"telegram",None),"bot",None)
        if b is None: return False
        b.send_message(m.chat.id,t,parse_mode="HTML"); return True
    except Exception: log.exception("Telegram Text: panel send failed"); return False

def authorized(m):
    try: return int(m.from_user.id) in _cardinal.telegram.authorized_users
    except Exception: return False

def event_order(e): return getattr(e,"order",None) or e
def order_id(e): return str(getattr(event_order(e),"id",None) or getattr(event_order(e),"order_id",None) or "")
def full_order(c,e):
    o=event_order(e)
    try:
        x=c.account.get_order(getattr(o,"id",None)); return x or o
    except Exception: return o
def order_title(o,e):
    for x in (o,event_order(e)):
        for n in ("title","short_description","description"):
            v=clean(getattr(x,n,None))
            if v:return v
    return ""
def order_chat(o,e): return getattr(o,"chat_id",None) or getattr(event_order(e),"chat_id",None)
def order_buyer(o,e): return clean(getattr(o,"buyer_username",None) or getattr(event_order(e),"buyer_username",None) or "")
def order_buyer_id(o,e): return getattr(o,"buyer_id",None) or getattr(event_order(e),"buyer_id",None)

def lot_fields(c,lot):
    fn=getattr(getattr(c,"account",None),"get_lot_fields",None)
    if not callable(fn): return None
    try:return fn(int(lot))
    except Exception:
        try:return fn(str(lot))
        except Exception:return None

def lot_title(fields):
    for n in ("title","name","short_description","description","title_ru","title_en"):
        v=clean(fields.get(n) if isinstance(fields,dict) else getattr(fields,n,None))
        if v:return v
    return ""

def bound_lot(c,e,o):
    direct=None
    for x in (event_order(e),o):
        for n in ("lot_id","lotId","offer_id","offerId"):
            v=getattr(x,n,None)
            if v is not None: direct=str(v); break
        if direct:break
    with _lock: items=[(str(k),dict(v) if isinstance(v,dict) else {}) for k,v in _lots.items() if not isinstance(v,dict) or v.get("enabled",True)]
    if direct and any(k==direct for k,_ in items): return direct
    wanted=norm_title(order_title(o,e))
    if not wanted:return None
    for k,d in items:
        t=norm_title(d.get("title"))
        if t and t==wanted:return k
    for k,d in items:
        if d.get("title"):continue
        t=lot_title(lot_fields(c,k))
        if t:
            with _lock:
                if isinstance(_lots.get(k),dict): _lots[k]["title"]=t; save_json(LOTS_FILE,_lots)
            if norm_title(t)==wanted:return k
    return None

def find_order(m):
    cid=getattr(m,"chat_id",None); aid=getattr(m,"author_id",None); au=clean(getattr(m,"author",None)).lstrip("@").casefold()
    with _lock: active=[(k,v) for k,v in _orders.items() if v.get("status") in ACTIVE]
    if cid is not None:
        x=[z for z in active if str(z[1].get("chat_id"))==str(cid)]
        if x:return max(x,key=lambda z:float(z[1].get("created_at",0)))
    if aid is not None:
        x=[z for z in active if z[1].get("buyer_id") is not None and str(z[1].get("buyer_id"))==str(aid)]
        if x:return max(x,key=lambda z:float(z[1].get("created_at",0)))
    if au:
        x=[z for z in active if clean(z[1].get("buyer","")).lstrip("@").casefold()==au]
        if x:return max(x,key=lambda z:float(z[1].get("created_at",0)))
    return None,None

def refund(oid):
    o=get_order(oid)
    if not o:return False
    if o.get("status")==S_COMPLETED: send_funpay(o.get("chat_id"),"ℹ️ Сообщение уже отправлено. Возврат после выдачи недоступен."); return False
    if o.get("status")==S_REFUNDED: send_funpay(o.get("chat_id"),"ℹ️ Возврат уже выполнен."); return False
    try:
        fn=getattr(getattr(_cardinal,"account",None),"refund",None)
        if not callable(fn):raise RuntimeError("refund unavailable")
        fn(str(oid)); update_order(oid,status=S_REFUNDED,error=None); send_funpay(o.get("chat_id"),"❌ Заказ отменён.\n\nСредства возвращены."); return True
    except Exception: log.exception("Telegram Text: refund failed"); send_funpay(o.get("chat_id"),"⚠️ Не удалось автоматически оформить возврат. Обработайте вручную."); return False

def ensure_telethon():
    if importlib.util.find_spec("telethon") is not None:return True
    with _install_lock:
        if importlib.util.find_spec("telethon") is None:
            try: subprocess.check_call([sys.executable,"-m","pip","install",TELETHON_PACKAGE])
            except Exception: log.exception("Telegram Text: Telethon install failed"); return False
    return importlib.util.find_spec("telethon") is not None

class PaidMessagesRequired(Exception):
    def __init__(self,stars=0):self.stars=int(stars or 0);super().__init__(f"paid_messages:{self.stars}")

class TelegramWorker:
    def __init__(self): self.loop=None;self.thread=None;self.queue=None;self.client=None;self.ready=threading.Event();self.stop_event=threading.Event();self.lock=threading.Lock()
    def running(self):return bool(self.thread and self.thread.is_alive() and self.loop and self.loop.is_running())
    def start(self):
        with self.lock:
            if self.running():return True
            self.stop_event.clear();self.ready.clear();self.thread=threading.Thread(target=self._main,name="telegram-text-worker",daemon=True);self.thread.start()
        return self.ready.wait(15) and self.running()
    def _main(self):
        self.loop=asyncio.new_event_loop();asyncio.set_event_loop(self.loop);self.queue=asyncio.Queue();self.loop.create_task(self._queue_loop());self.ready.set()
        try:self.loop.run_forever()
        except Exception:log.exception("Telegram Text worker crashed")
        finally:
            try:
                for t in asyncio.all_tasks(self.loop):t.cancel()
                self.loop.run_until_complete(asyncio.sleep(0))
                self.loop.close()
            except Exception:pass
            self.loop=None
    async def _queue_loop(self):
        while not self.stop_event.is_set():
            try:oid=await asyncio.wait_for(self.queue.get(),.5)
            except asyncio.TimeoutError:continue
            try:await self.process(str(oid))
            except Exception:log.exception("Telegram Text: processing crashed");update_order(oid,status=S_ERROR,error="worker_exception")
    def session(self):
        if SHARED_SESSION.exists() or Path(str(SHARED_SESSION)+".session").exists():return SHARED_SESSION
        return LEGACY_SESSION
    async def client_get(self):
        if not ensure_telethon():raise RuntimeError("Telethon не установлен")
        if self.client is None:
            T=importlib.import_module("telethon");self.client=T.TelegramClient(str(self.session()),API_ID,API_HASH,device_model="FunPay Cardinal Telegram Text",system_version="Linux",app_version=VERSION,lang_code="en",system_lang_code="en-US")
        if not self.client.is_connected():await self.client.connect()
        if not await self.client.is_user_authorized():raise RuntimeError("Telegram-аккаунт не авторизован")
        return self.client
    async def account_info(self):
        c=await self.client_get();m=await c.get_me();return {"id":m.id,"username":m.username,"phone":m.phone,"name":" ".join(x for x in (m.first_name,m.last_name) if x) or "—"}
    async def send_code(self,phone):
        c=await self.client_get()
        if await c.is_user_authorized():return "authorized"
        return (await c.send_code_request(phone)).phone_code_hash
    async def sign_code(self,phone,code,h):
        c=await self.client_get()
        try:await c.sign_in(phone=phone,code=code,phone_code_hash=h);return "authorized"
        except Exception as e:
            if type(e).__name__=="SessionPasswordNeededError":return "2fa"
            raise
    async def sign_password(self,p):await (await self.client_get()).sign_in(password=p);return "authorized"
    async def paid_check(self,target):
        c=await self.client_get();entity=await c.get_entity(target)
        v=getattr(entity,"send_paid_messages_stars",None)
        if v is not None and int(v or 0)>0:return True,int(v)
        try:
            from telethon import functions
            full=await c(functions.users.GetFullUserRequest(id=entity));fu=getattr(full,"full_user",None);v=getattr(fu,"send_paid_messages_stars",None)
            if v is None:v=getattr(full,"send_paid_messages_stars",None)
            return (True,int(v)) if v is not None and int(v or 0)>0 else (False,0)
        except Exception:log.exception("Telegram Text: paid check failed");raise RuntimeError("Не удалось проверить плату за сообщения")
    async def send_text(self,target,text):
        c=await self.client_get();required,stars=await self.paid_check(target)
        if required:raise PaidMessagesRequired(stars)
        try:await c.send_message(target,text)
        except Exception as e:
            u=str(e).upper()
            if "ALLOW_PAYMENT_REQUIRED" in u or ("PAID_MESSAGES" in u and "REQUIRED" in u):raise PaidMessagesRequired(0) from e
            raise
    async def process(self,oid):
        o=get_order(oid)
        if not o or o.get("status")!=S_SENDING:return
        try:await self.send_text(o["username"],o["text"])
        except PaidMessagesRequired as e:
            update_order(oid,status=S_PAID,paid_stars=e.stars,error="paid_messages");suffix=f" ({e.stars} ⭐️)" if e.stars else "";send_funpay(o.get("chat_id"),f"⚠️ У пользователя включена плата за входящие сообщения{suffix}.\n\nОтключите плату за сообщения в Telegram и отправьте «+».\n\nСообщение НЕ отправлено.");return
        except Exception as e:
            n=type(e).__name__
            if n in {"UsernameInvalidError","UsernameNotOccupiedError","PeerIdInvalidError","UserIdInvalidError"}:
                update_order(oid,status=S_USERNAME,error=n);send_funpay(o.get("chat_id"),"❌ Telegram не нашёл этот username. Отправьте другой @username.");return
            log.exception("Telegram Text: send failed");update_order(oid,status=S_ERROR,error=n);send_funpay(o.get("chat_id"),"❌ Не удалось отправить сообщение. Отправьте + для повторной попытки или !возврат.");return
        update_order(oid,status=S_COMPLETED,completed_at=time.time(),error=None);send_funpay(o.get("chat_id"),"✅ Сообщение успешно отправлено!\n\nСпасибо за покупку ❤️")
    def submit(self,oid):
        if not self.running():raise RuntimeError("Telegram worker не запущен")
        asyncio.run_coroutine_threadsafe(self.queue.put(str(oid)),self.loop)
    def call(self,fn,timeout=90):return asyncio.run_coroutine_threadsafe(fn(),self.loop).result(timeout)
    def reset(self):
        async def x():
            if self.client:
                try:await self.client.disconnect()
                except Exception:pass
                self.client=None
        return self.call(x,30)
    def stop(self):
        self.stop_event.set()
        if self.loop and self.loop.is_running():
            try:self.loop.call_soon_threadsafe(self.loop.stop)
            except Exception:pass
        if self.thread and self.thread.is_alive():self.thread.join(10)
        self.thread=None

def ensure_worker():
    global _worker
    if _worker is None:_worker=TelegramWorker()
    return _worker.start()

def begin_auth(m):
    with _auth_lock:_auth[int(m.from_user.id)]={"state":"phone"}
    panel(m,"📱 Введите номер Telegram в международном формате, например <code>+79991234567</code>")

def cmd_account(m):
    if not authorized(m):return
    if not ensure_worker():panel(m,"❌ Telegram-модуль не удалось запустить.");return
    try:info=_worker.call(lambda:_worker.account_info(),30)
    except Exception as e:log.exception("Telegram Text: account check");panel(m,f"❌ Ошибка проверки аккаунта: {type(e).__name__}");return
    if not info:return begin_auth(m)
    panel(m,"📱 <b>Telegram-аккаунт подключен</b>\n\n"+f"👤 {html.escape(info.get('name') or '—')}\n🔗 {html.escape('@'+info['username'] if info.get('username') else 'нет username')}\n🆔 <code>{info.get('id')}</code>")

def cmd_reset(m):
    if not authorized(m):return
    if ensure_worker():
        try:_worker.reset()
        except Exception:log.exception("Telegram Text: reset")
    begin_auth(m)

def auth_message(m):
    if not authorized(m):return False
    uid=int(m.from_user.id)
    with _auth_lock:st=dict(_auth.get(uid) or {})
    if not st:return False
    v=text_of(m)
    if not v or v.startswith("/"):return False
    if not ensure_worker():panel(m,"❌ Telegram-модуль не запущен.");return True
    try:
        if st["state"]=="phone":
            phone=re.sub(r"[\s()\-]","",v)
            if not re.fullmatch(r"\+[1-9]\d{6,14}",phone):panel(m,"❌ Неверный номер. Формат: +79991234567");return True
            h=_worker.call(lambda:_worker.send_code(phone),90)
            if h=="authorized":_auth.pop(uid,None);panel(m,"✅ Telegram-аккаунт уже авторизован.");return True
            st.update(state="code",phone=phone,code_hash=h);_auth[uid]=st;panel(m,"📨 Код отправлен Telegram. Введите его одним сообщением.");return True
        if st["state"]=="code":
            r=_worker.call(lambda:_worker.sign_code(st["phone"],v.replace(" ",""),st["code_hash"]),90)
            if r=="2fa":st["state"]="2fa";_auth[uid]=st;panel(m,"🔐 Введите пароль Telegram 2FA.")
            else:_auth.pop(uid,None);panel(m,"✅ Telegram-аккаунт успешно подключен.")
            return True
        if st["state"]=="2fa":_worker.call(lambda:_worker.sign_password(v),90);_auth.pop(uid,None);panel(m,"✅ Telegram-аккаунт успешно подключен.");return True
    except Exception as e:
        log.exception("Telegram Text: auth error");panel(m,f"❌ Ошибка авторизации: <code>{html.escape(type(e).__name__)}</code>");_auth.pop(uid,None);return True
    return False

def cmd_lots(m):
    if not authorized(m):return
    with _lock:items=dict(_lots)
    if not items:return panel(m,"📦 Привязанных лотов нет.")
    panel(m,"📦 <b>Привязанные лоты</b>\n\n"+"\n".join(f"• <code>{html.escape(str(k))}</code>"+(f" — {html.escape(str(v.get('title','')))}" if isinstance(v,dict) and v.get('title') else "") for k,v in sorted(items.items())))

def cmd_bind(m):
    if not authorized(m):return
    p=text_of(m).split(maxsplit=2)
    if len(p)<2 or not p[1].isdigit():return panel(m,"Использование: /text_bind LOT_ID [название]")
    lot=str(int(p[1]));title=p[2].strip() if len(p)>2 else lot_title(lot_fields(_cardinal,lot))
    with _lock:_lots[lot]={"title":title,"enabled":True,"updated_at":time.time()};save_json(LOTS_FILE,_lots)
    panel(m,f"✅ Лот <code>{lot}</code> привязан."+(f"\n\nНазвание: {html.escape(title)}" if title else ""))

def cmd_unbind(m):
    if not authorized(m):return
    p=text_of(m).split()
    if len(p)!=2 or not p[1].isdigit():return panel(m,"Использование: /text_unbind LOT_ID")
    with _lock:ex=_lots.pop(str(int(p[1])),None);save_json(LOTS_FILE,_lots)
    panel(m,"✅ Лот отвязан." if ex else "ℹ️ Такой лот не был привязан.")

def bind_order(c,e):
    try:
        oid=order_id(e)
        if not oid:return
        with _lock:
            if oid in _orders:return
        o=full_order(c,e);lot=bound_lot(c,e,o)
        if lot is None:return
        chat=order_chat(o,e)
        buyer=order_buyer(o,e);bid=order_buyer_id(o,e)
        if chat is None and buyer:
            try:ch=c.account.get_chat_by_name(buyer,True)
            except TypeError:ch=c.account.get_chat_by_name(buyer)
            except Exception:ch=None
            chat=getattr(ch,"id",None) if ch else None
        if chat is None:log.error("Telegram Text: order %s has no chat",oid);return
        with _lock:_orders[oid]={"order_id":oid,"lot_id":lot,"lot_title":order_title(o,e),"chat_id":chat,"buyer":buyer,"buyer_id":bid,"username":None,"text":None,"status":S_USERNAME,"created_at":time.time(),"error":None};save_json(ORDERS_FILE,_orders)
        send_funpay(chat,"👋 Спасибо за покупку!\n\nОтправьте Telegram username, куда нужно отправить текст.\n\nПример: @username\n\n❗ Для отмены заказа отправьте: !возврат")
        log.info("Telegram Text: order %s created lot=%s",oid,lot)
    except Exception:log.exception("Telegram Text: new order failed")

def handle_message(c,e):
    try:
        m=getattr(e,"message",None) or e
        if getattr(m,"by_bot",False):return
        own=getattr(getattr(c,"account",None),"id",None)
        if own is not None and getattr(m,"author_id",None) is not None and str(own)==str(m.author_id):return
        v=text_of(m)
        if not v:return
        oid,o=find_order(m)
        if not oid or not o:return
        if o.get("buyer_id") is not None and getattr(m,"author_id",None) is not None and str(o["buyer_id"])!=str(m.author_id):return
        chat=getattr(m,"chat_id",None) or o.get("chat_id")
        if is_refund(v):refund(oid);return
        st=o.get("status")
        if st==S_USERNAME:
            u=normalize_username(v)
            if not u:return send_funpay(chat,"❌ Некорректный Telegram username. Отправьте @username или !возврат.")
            update_order(oid,username=u,status=S_CONFIRM,error=None);send_funpay(chat,f"📋 Получатель: {u}\n\nЕсли всё верно — отправьте «+».\nЕсли хотите изменить — отправьте новый @username.\n\n❗ Для возврата: !возврат");return
        if st==S_CONFIRM:
            if is_plus(v):update_order(oid,status=S_TEXT);send_funpay(chat,"💬 Отлично. Теперь отправьте текст, который нужно отправить этому пользователю.\n\n❗ Для отмены: !возврат");return
            u=normalize_username(v)
            if u:update_order(oid,username=u);send_funpay(chat,f"📋 Получатель изменён: {u}\n\nЕсли всё верно — отправьте «+».");return
            send_funpay(chat,"❓ Отправьте «+» для подтверждения, новый @username или !возврат.");return
        if st==S_TEXT:
            if len(v)>MAX_TEXT:return send_funpay(chat,"❌ Максимальная длина текста — 4096 символов.")
            update_order(oid,text=v,status=S_SENDING,error=None)
            if not ensure_worker():update_order(oid,status=S_ERROR,error="worker_unavailable");send_funpay(chat,"❌ Telegram-модуль не удалось запустить. Отправьте + для повторной попытки или !возврат.");return
            _worker.submit(oid);send_funpay(chat,"⏳ Проверяю возможность отправки и отправляю сообщение...");return
        if st==S_PAID:
            if not is_plus(v):send_funpay(chat,"⚠️ Отключите плату за сообщения в Telegram, затем отправьте «+».");return
            update_order(oid,status=S_SENDING,error=None);_worker.submit(oid);send_funpay(chat,"⏳ Повторно проверяю плату за сообщения и отправляю текст...");return
        if st==S_ERROR:
            if is_plus(v) and o.get("username") and o.get("text"):
                if ensure_worker():update_order(oid,status=S_SENDING,error=None);_worker.submit(oid);send_funpay(chat,"⏳ Повторная попытка отправки запущена.")
                return
            u=normalize_username(v)
            if u:update_order(oid,username=u,status=S_CONFIRM,error=None);send_funpay(chat,f"📋 Получатель: {u}\n\nОтправьте + для подтверждения.");return
            send_funpay(chat,"❓ Отправьте + для повторной попытки, новый @username или !возврат.")
    except Exception:log.exception("Telegram Text: message handler failed")

def old_mode(c,e):
    chat=getattr(e,"chat",None)
    if chat is None:return
    def run():
        try:
            hist=c.account.get_chat_history(chat.id,last_message_id=None,interlocutor_username=getattr(chat,"name",None))
            for m in reversed(hist or []):
                if getattr(m,"by_bot",False):continue
                if getattr(m,"author_id",None)==getattr(c.account,"id",None):continue
                if text_of(m):handle_message(c,SimpleNamespace(message=m));return
        except Exception:log.exception("Telegram Text: old mode handler failed")
    threading.Thread(target=run,name="telegram-text-old-mode",daemon=True).start()

def post_init(c):
    global _cardinal
    _cardinal=c;BASE.mkdir(parents=True,exist_ok=True);load_state()
    try:
        ensure_worker();tg=getattr(c,"telegram",None)
        if tg is None:log.error("Telegram Text: telegram manager unavailable");return
        cmds=[("text_account","Показать/настроить Telegram аккаунт",True),("text_account_reset","Повторно авторизовать Telegram аккаунт",False),("text_lots","Показать привязанные лоты",True),("text_bind","Привязать лот",False),("text_unbind","Отвязать лот",False)]
        try:c.add_telegram_commands(UUID,cmds)
        except Exception:log.exception("Telegram Text: command menu registration failed")
        tg.msg_handler(cmd_account,commands=["text_account"]);tg.msg_handler(cmd_reset,commands=["text_account_reset"]);tg.msg_handler(cmd_lots,commands=["text_lots"]);tg.msg_handler(cmd_bind,commands=["text_bind"]);tg.msg_handler(cmd_unbind,commands=["text_unbind"]);tg.msg_handler(auth_message,func=lambda m:bool(_auth.get(int(m.from_user.id))))
        log.info("Telegram Text v%s loaded",VERSION)
    except Exception:log.exception("Telegram Text: registration failed")

def post_stop(c):
    global _worker,_cardinal
    if _worker:
        try:_worker.stop()
        except Exception:log.exception("Telegram Text: stop failed")
    _worker=None;_cardinal=None

BIND_TO_POST_INIT=[post_init];BIND_TO_NEW_ORDER=[bind_order];BIND_TO_NEW_MESSAGE=[handle_message];BIND_TO_LAST_CHAT_MESSAGE_CHANGED=[old_mode];BIND_TO_POST_STOP=[post_stop];BIND_TO_DELETE=None
