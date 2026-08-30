import os, re, json, logging, aiohttp, asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from html import escape as he
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

# ══════════════════════════════════════════════════════════════════
#  ⚙️  CONFIG
# ══════════════════════════════════════════════════════════════════
BOT_TOKEN      = os.getenv("BOT_TOKEN")
API_KEY        = os.getenv("API_KEY")   # tg-to-num API key
OWNER_ID       = int(os.getenv("OWNER_ID"))
OWNER_USERNAME = "l_Smoke_ll"
MONGO_URI      = os.getenv("MONGO_URI", "mongodb+srv://yb131567_db_user:R8zxuvc9Qn999Arg@cluster0.drjaxl8.mongodb.net/telegram_bot?retryWrites=true&w=majority")
SUPPORT_GROUP  = "https://t.me/+6JT140NC2VtkODk1"
LOG_GROUP_ID   = int(os.getenv("LOG_GROUP_ID", "0"))
API_BASE       = "https://tg-to-num-six.vercel.app/"
PHONE_API_BASE = "https://num-to-info-ten.vercel.app/"

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
HTML = ParseMode.HTML


# ══════════════════════════════════════════════════════════════════
#  🛡️  SAFE HELPERS
# ══════════════════════════════════════════════════════════════════
def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)

async def safe_send(fn, text: str, reply_markup=None, parse_mode=HTML):
    try:
        return await fn(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        if any(x in str(e).lower() for x in ("can't parse", "bad request", "invalid", "entities")):
            logger.warning("HTML rejected, retrying plain: %s", e)
            try:
                return await fn(strip_html(text), parse_mode=None, reply_markup=reply_markup)
            except Exception as e2:
                logger.error("Plain fallback failed: %s", e2)
        else:
            raise

async def safe_edit(msg, text: str, reply_markup=None, parse_mode=HTML):
    try:
        await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        err = str(e).lower()
        if any(x in err for x in ("can't parse", "bad request", "invalid", "entities")):
            logger.warning("HTML rejected in edit, retrying plain: %s", e)
            try:
                await msg.edit_text(strip_html(text), parse_mode=None, reply_markup=reply_markup)
            except Exception as e2:
                logger.error("Plain edit fallback failed: %s", e2)
        elif "message to edit not found" in err:
            try:
                await msg.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            except Exception as e2:
                logger.error("safe_edit reply fallback: %s", e2)
        elif "message is not modified" in err:
            pass
        else:
            logger.error("safe_edit error: %s", e)


# ══════════════════════════════════════════════════════════════════
#  🗄️  DATABASE
# ══════════════════════════════════════════════════════════════════
_mongo_client = None
_mdb          = None

def get_db():
    return _mdb

async def init_db():
    global _mongo_client, _mdb
    _mongo_client = AsyncIOMotorClient(MONGO_URI)
    _mdb          = _mongo_client.get_default_database()
    # Create indexes
    await _mdb.users.create_index("user_id", unique=True)
    await _mdb.lookup_history.create_index("user_id")
    logger.info("✅ MongoDB connected")

# ── Default user doc ──
def _default_user(uid, username="", full_name="", language_code=""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "user_id": uid, "username": username,
        "full_name": full_name, "language_code": language_code,
        "total_lookups": 0, "total_phone_lookups": 0,
        "last_seen": now, "joined_at": now,
    }

async def upsert_user(u):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    default = _default_user(u.id, u.username or "", u.full_name or "", u.language_code or "")
    set_fields = {"username": u.username or "", "full_name": u.full_name or "",
                  "language_code": u.language_code or "", "last_seen": now}
    on_insert = {k: v for k, v in default.items() if k not in set_fields}
    await _mdb.users.update_one(
        {"user_id": u.id},
        {"$set": set_fields, "$setOnInsert": on_insert},
        upsert=True,
    )

async def save_lookup(user_id, query, ltype, result_name="", result_id="", phone=""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await _mdb.lookup_history.insert_one({
        "user_id": user_id, "query": query, "type": ltype,
        "result_name": result_name, "result_id": result_id,
        "phone": phone, "searched_at": now,
    })
    col = "total_phone_lookups" if ltype == "phone" else "total_lookups"
    await _mdb.users.update_one({"user_id": user_id}, {"$inc": {col: 1}})

async def get_user(user_id):
    return await _mdb.users.find_one({"user_id": user_id}, {"_id": 0})

async def get_all_users():
    cursor = _mdb.users.find({}, {"_id": 0}).sort("joined_at", -1)
    return await cursor.to_list(length=None)

async def get_user_history(user_id, limit=10):
    cursor = _mdb.lookup_history.find(
        {"user_id": user_id}, {"_id": 0}
    ).sort("searched_at", -1).limit(limit)
    return await cursor.to_list(length=None)

def is_owner(user_id):
    return user_id == OWNER_ID


# ══════════════════════════════════════════════════════════════════
#  📣  LOG GROUP NOTIFIER
# ══════════════════════════════════════════════════════════════════
async def notify_log_group(bot, text: str, kb=None):
    """Send notification to log group if configured"""
    if not LOG_GROUP_ID:
        return
    try:
        await bot.send_message(LOG_GROUP_ID, text, parse_mode=HTML, reply_markup=kb)
    except Exception as e:
        logger.warning("Log group notify failed: %s", e)

# ══════════════════════════════════════════════════════════════════
#  🌐  API CALLS
# ══════════════════════════════════════════════════════════════════
async def fetch_info(query: str) -> dict:
    """Username/ID → phone number via tg-to-num API"""
    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    async with aiohttp.ClientSession() as s:
        async with s.get(API_BASE, params={"key": API_KEY, "q": query}, timeout=timeout) as r:
            if r.status != 200:
                return {"success": False, "message": f"API Error {r.status}"}
            return await r.json(content_type=None)

async def fetch_phone_info(number: str) -> dict:
    """Phone number → details via num-to-info API"""
    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{PHONE_API_BASE}?num={number}", timeout=timeout) as r:
            if r.status != 200:
                return {"success": False, "message": f"API Error {r.status}"}
            return await r.json(content_type=None)



# ══════════════════════════════════════════════════════════════════
#  🎨  HELPERS & KEYBOARDS
# ══════════════════════════════════════════════════════════════════
def hv(val, fallback="—", maxlen=300) -> str:
    if val is None or str(val).strip().lower() in ("", "null", "none"):
        return fallback
    s = str(val).strip()
    return he(s[:maxlen] + "…" if len(s) > maxlen else s)

STATUS_MAP = {
    "recently":      "🟡 Recently",
    "online":        "🟢 Online",
    "offline":       "🔴 Offline",
    "long_time_ago": "⚫ Long ago",
    "within_week":   "🟠 Within week",
    "within_month":  "🔵 Within month",
}
DC_MAP = {1:"DC1 🇺🇸 Miami", 2:"DC2 🇳🇱 Amsterdam",
          3:"DC3 🇺🇸 Miami", 4:"DC4 🇳🇱 Amsterdam", 5:"DC5 🇸🇬 Singapore"}
def bi(v): return "✅" if v else "❌"

def format_tg_result(d: dict) -> str:
    uname  = d.get("username") or ""
    uid    = d.get("user_id") or "—"
    fname  = d.get("full_name") or d.get("first_name") or "—"
    bio    = d.get("bio")
    status = STATUS_MAP.get(d.get("status",""), hv(d.get("status","")))
    dc_id  = d.get("dc_id")
    dc     = DC_MAP.get(dc_id, "—") if dc_id else "—"
    cc     = d.get("common_chats_count") or 0
    ph     = d.get("phone_info") or {}
    phone_line = ""
    if isinstance(ph, dict) and ph.get("success") and ph.get("number"):
        phone_line = (
            "\n📞 <b>Phone</b>\n"
            f"├ Number  : <code>{hv(ph.get('number'))}</code>\n"
            f"└ Country : {hv(ph.get('country'))} {hv(ph.get('country_code',''))}\n"
        )
    flags = []
    if d.get("is_scam"):       flags.append("🚨 Scam")
    if d.get("is_fake"):       flags.append("⚠️ Fake")
    if d.get("is_restricted"): flags.append("🔒 Restricted")
    flags_line = "\n⚠️ " + " | ".join(flags) + "\n" if flags else ""
    name_line = f"👤 <b>{hv(fname)}</b>"
    if uname:
        name_line += f" | @{he(uname)}"
    return (
        f"{name_line}\n"
        f"🆔 <code>{uid}</code>\n"
        f"👁 Status  : {status}\n"
        f"🖥 DC      : {dc}\n"
        + (f"💬 Common : {cc}\n" if cc else "")
        + (f"📝 Bio    : <i>{hv(bio, maxlen=150)}</i>\n" if bio else "")
        + "\n"
        + f"🤖 Bot     : {bi(d.get('is_bot'))}\n"
        + f"✅ Verified: {bi(d.get('is_verified'))}\n"
        + f"⭐ Premium : {bi(d.get('is_premium'))}\n"
        + flags_line
        + phone_line
        + f"\n✦ <b>Made by @{OWNER_USERNAME}</b>"
    )

def format_phone_result(d: dict, number: str) -> str:
    """
    Actual API: num-to-info-ten.vercel.app
    {
      "success": true,
      "result": {
        "results": [
          { "mobile", "name", "fname", "address", "alt", "circle", "id", "email" }
        ]
      }
    }
    """
    ICONS = {
        "name":    "👤",
        "fname":   "👨",
        "address": "🏠",
        "alt":     "📞",
        "circle":  "📡",
        "id":      "🪪",
        "email":   "📧",
        "mobile":  "📱",
    }
    SKIP = {"mobile", "success", "cached", "proxyused", "attempt",
            "credit", "developer", "status", "count", "search_time"}

    # ── Extract rows from actual API format ──
    rows = []
    if isinstance(d, dict):
        result_obj = d.get("result") or {}
        if isinstance(result_obj, dict):
            rows = result_obj.get("results") or []

    lines = f"📱 <b>{he(number)}</b>\n\n"

    if not rows:
        lines += "❌ <b>No data found</b>\n"
    else:
        # Deduplicate
        seen = set()
        unique = []
        for r in rows:
            key = (r.get("name",""), r.get("fname",""), r.get("address",""))
            if key not in seen:
                seen.add(key)
                unique.append(r)

        for i, r in enumerate(unique):
            if i > 0:
                lines += "─────────────────────\n"
            for k, v in r.items():
                if k.lower() in SKIP:
                    continue
                if not v or str(v).strip() in ("", "null", "none", "0"):
                    continue
                icon  = ICONS.get(k.lower(), "•")
                label = k.replace("_", " ").title()
                lines += f"{icon} <b>{label}</b> : {hv(str(v), maxlen=300)}\n"
        lines += "\n"

    lines += f"✦ <b>Made by @{OWNER_USERNAME}</b>"
    return lines


def main_menu_kb(user_id=None):
    """Main menu keyboard - completely free, no premium options"""
    kb = [
        [InlineKeyboardButton("📊 My Profile",    callback_data="my_account"),
         InlineKeyboardButton("📜 History",       callback_data="my_history")],
        [InlineKeyboardButton("💬 Support",        url=SUPPORT_GROUP),
         InlineKeyboardButton("👑 Contact Owner",  url=f"https://t.me/{OWNER_USERNAME}")],
    ]
    if user_id and is_owner(user_id):
        kb.append([InlineKeyboardButton("🛠 Owner Panel", callback_data="owner_panel")])
    return InlineKeyboardMarkup(kb)

def two_button_kb(query: str):
    """
    2 buttons for search options:
    📱 Telegram  → tg-to-num API (username/ID → telegram lookup)
    📞 Number Info → num-to-info API (phone number details)
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Telegram",    callback_data=f"search_tg:{query}"),
         InlineKeyboardButton("📞 Number Info", callback_data=f"search_ph:{query}")],
    ])

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]])

def cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]])

def owner_panel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 All Users",      callback_data="owner_users"),
         InlineKeyboardButton("📊 Stats",          callback_data="owner_stats")],
        [InlineKeyboardButton("📢 Broadcast",      callback_data="owner_broadcast")],
        [InlineKeyboardButton("🏠 Main Menu",      callback_data="main_menu")],
    ])

async def user_profile_text(user_id: int, full_name: str) -> str:
    u = await get_user(user_id)
    fname  = he(full_name or "User")
    role   = "👑 Owner" if is_owner(user_id) else "👤 User"
    return (
        f"{role}  |  👤 <b>{fname}</b>  |  🆔 <code>{user_id}</code>\n\n"
        f"📊 <b>Stats</b>\n"
        f"├ Username Lookups : {(u['total_lookups'] if u else 0)}\n"
        f"├ Phone Lookups    : {(u['total_phone_lookups'] if u else 0)}\n"
        f"└ Joined           : {(u['joined_at'] or '')[:10] if u else '—'}\n\n"
        f"✦ <b>Made by @{OWNER_USERNAME}</b>"
    )


# ══════════════════════════════════════════════════════════════════
#  🔎  CORE LOOKUPS
# ══════════════════════════════════════════════════════════════════
async def perform_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str):
    user_id = update.effective_user.id
    await upsert_user(update.effective_user)

    reply_fn = update.message.reply_text if update.message else update.callback_query.message.reply_text
    msg = await reply_fn("🔍 <b>Searching Telegram...</b>", parse_mode=HTML)

    try:
        await asyncio.sleep(0.5)
        data = await asyncio.wait_for(fetch_info(query), timeout=15)

        if not data or data.get("success") == False or "error" in data:
            err = data.get("message") or data.get("error") or "Not found"
            await safe_edit(msg, f"❌ <b>Error:</b> <code>{he(str(err))}</code>", reply_markup=back_kb())
            return

        await save_lookup(user_id, query, "username",
                    result_name=data.get("full_name", ""),
                    result_id=str(data.get("user_id", "")),
                    phone=(data.get("phone_info") or {}).get("number", ""))

        text = format_tg_result(data)

        pic = data.get("profile_pic")
        if pic:
            await msg.delete()
            send_photo = (update.message.reply_photo if update.message
                          else update.callback_query.message.reply_photo)
            try:
                await send_photo(pic, caption=text[:1024], parse_mode=HTML)
            except BadRequest:
                await safe_send(reply_fn, text)
        else:
            await safe_edit(msg, text)

    except asyncio.TimeoutError:
        await safe_edit(msg, "❌ <b>Timeout!</b> API ne 15 sec mein reply nahi kiya.", reply_markup=back_kb())
    except aiohttp.ClientError:
        await safe_edit(msg, "❌ <b>Network Error!</b>", reply_markup=back_kb())
    except Exception as e:
        logger.exception("perform_lookup")
        await safe_edit(msg, f"❌ <b>Error:</b> <code>{he(str(e))}</code>", reply_markup=back_kb())


async def perform_phone_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE, number: str):
    user_id = update.effective_user.id
    await upsert_user(update.effective_user)

    number = number.strip().replace(" ", "").replace("-", "")
    if not number.lstrip("+").isdigit() or len(number.lstrip("+")) < 7:
        reply = update.message.reply_text if update.message else update.callback_query.message.reply_text
        await safe_send(reply,
                        "❌ <b>Invalid number!</b>\n"
                        "Example: <code>9876543210</code> or <code>+919876543210</code>",
                        reply_markup=back_kb())
        return

    reply_fn = update.message.reply_text if update.message else update.callback_query.message.reply_text
    msg = await reply_fn("📱 <b>Searching phone info...</b>", parse_mode=HTML)

    try:
        await asyncio.sleep(0.5)
        data = await asyncio.wait_for(fetch_phone_info(number), timeout=15)

        if not data or data.get("success") == False or "error" in data:
            err = data.get("message") or data.get("error") or "Not found"
            await safe_edit(msg, f"❌ <b>Error:</b> <code>{he(str(err))}</code>", reply_markup=back_kb())
            return

        await save_lookup(user_id, number, "phone",
                    result_name=data.get("name", ""), phone=number)

        text = format_phone_result(data, number)
        await safe_edit(msg, text)

    except asyncio.TimeoutError:
        await safe_edit(msg, "❌ <b>Timeout!</b>", reply_markup=back_kb())
    except aiohttp.ClientError:
        await safe_edit(msg, "❌ <b>Network Error!</b>", reply_markup=back_kb())
    except Exception as e:
        logger.exception("perform_phone_lookup")
        await safe_edit(msg, f"❌ <b>Error:</b> <code>{he(str(e))}</code>", reply_markup=back_kb())


# ══════════════════════════════════════════════════════════════════
#  📟  COMMANDS
# ══════════════════════════════════════════════════════════════════
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await upsert_user(update.effective_user)
    uid   = update.effective_user.id
    fname = he(update.effective_user.first_name or "User")

    u = await get_user(uid)
    total_lookups = (u["total_lookups"] if u else 0) + (u["total_phone_lookups"] if u else 0)

    await safe_send(
        update.message.reply_text,
        f"🤖 <b>Smoke Bot - COMPLETELY FREE</b>\n\n"
        f"👋 Welcome, <b>{fname}</b>!\n\n"
        f"Username, User ID ya Phone Number bhejo — 2 options milenge:\n"
        f"📱 <b>Telegram Info</b> — username se details nikalo\n"
        f"📞 <b>Number Info</b> — phone number se info nikalo\n\n"
        f"📊 <b>Tumhare Searches</b>\n"
        f"└ Total Lookups : <code>{total_lookups}</code>\n"
        f"\n✦ <b>Made by @{OWNER_USERNAME}</b>",
        reply_markup=main_menu_kb(uid),
    )


# ══════════════════════════════════════════════════════════════════
#  💬  SMART MESSAGE HANDLER
# ══════════════════════════════════════════════════════════════════
async def smart_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    uid     = update.effective_user.id
    waiting = ctx.user_data.get("waiting")
    await upsert_user(update.effective_user)

    # ── Broadcast (owner only) ──
    if waiting == "broadcast":
        if uid != OWNER_ID:
            return
        ctx.user_data.pop("waiting", None)
        users = await get_all_users()
        sent = failed = 0
        msg = await update.message.reply_text("📢 Broadcasting...")
        for u in users:
            try:
                await ctx.bot.send_message(u["user_id"], text, parse_mode=HTML)
                sent += 1
            except Exception:
                failed += 1
        await safe_edit(msg,
                        f"✅ <b>Broadcast Done!</b>\n├ Sent   : {sent}\n└ Failed : {failed}")
        return

    # ── Waiting for phone after Number Info click ──
    if ctx.user_data.pop("waiting_ph", False):
        await perform_phone_lookup(update, ctx, text)
        return

    # ── Auto-detect input ──
    is_phone    = text.lstrip("+").isdigit() and len(text.lstrip("+")) >= 7
    is_userid   = text.lstrip("-").isdigit() and not is_phone
    is_username = text.startswith("@")

    if is_username or is_userid or is_phone:
        # Hamesha 2 buttons - Telegram aur Number Info
        await safe_send(
            update.message.reply_text,
            f"🔍 <b>Detected:</b> <code>{he(text)}</code>\n\n"
            f"Search direction choose karo:",
            reply_markup=two_button_kb(text),
        )
    else:
        await safe_send(
            update.message.reply_text,
            f"🤖 <b>Smoke Bot</b>\n\n"
            f"Kuch bhejo:\n"
            f"• <code>@username</code> — Telegram username\n"
            f"• <code>123456789</code> — Telegram User ID\n"
            f"• <code>9876543210</code> — Phone number\n\n"
            f"✦ <b>Made by @{OWNER_USERNAME}</b>",
            reply_markup=main_menu_kb(uid),
        )


# ══════════════════════════════════════════════════════════════════
#  🖱️  BUTTON HANDLER
# ══════════════════════════════════════════════════════════════════
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data
    uid  = update.effective_user.id

    async def edit(text, kb=None):
        try:
            await q.message.edit_text(text, parse_mode=HTML, reply_markup=kb)
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                logger.warning("edit: %s", e)

    # ══════════════════════════════════════════════════
    #  SEARCH TRIGGERS
    # ══════════════════════════════════════════════════
    if data.startswith("search_tg:"):
        query = data[len("search_tg:"):]
        try: await q.message.delete()
        except Exception: pass
        await perform_lookup(update, ctx, query)
        return

    if data.startswith("search_ph:"):
        number = data[len("search_ph:"):]
        try: await q.message.delete()
        except Exception: pass
        # Check karo ki actual phone number hai ya nahi
        clean = number.lstrip("+").replace(" ","").replace("-","")
        if clean.isdigit() and len(clean) >= 7:
            await perform_phone_lookup(update, ctx, number)
        else:
            # Username/ID tha — phone number maango
            ctx.user_data["waiting_ph"] = True
            await safe_send(
                update.callback_query.message.reply_text,
                "📞 <b>Number Info</b>\n\nPhone number bhejo:\n<i>Example: 9876543210</i>",
                reply_markup=cancel_kb(),
            )
        return

    # ══════════════════════════════════════════════════
    #  MAIN MENU
    # ══════════════════════════════════════════════════
    if data == "main_menu":
        ctx.user_data.clear()
        u = await get_user(uid)
        total_lookups = (u["total_lookups"] if u else 0) + (u["total_phone_lookups"] if u else 0)
        fname   = he(update.effective_user.first_name or "User")
        await edit(
            f"🤖 <b>Smoke Bot</b>\n\n"
            f"👋 <b>{fname}</b>\n\n"
            f"📊 <b>Tumhare Searches</b>\n"
            f"└ Total : <code>{total_lookups}</code>\n\n"
            f"✦ <b>Made by @{OWNER_USERNAME}</b>",
            main_menu_kb(uid),
        )

    # ══════════════════════════════════════════════════
    #  MY PROFILE
    # ══════════════════════════════════════════════════
    elif data == "my_account":
        text = await user_profile_text(uid, update.effective_user.full_name or "User")
        await edit(text, InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
        ]))

    # ══════════════════════════════════════════════════
    #  HISTORY
    # ══════════════════════════════════════════════════
    elif data == "my_history":
        history = await get_user_history(uid, 10)
        if not history:
            await edit("📜 <b>Search History</b>\n\nKoi search nahi ki abhi!", back_kb()); return
        lines = "📜 <b>Recent Searches</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        for h in history:
            icon = "📱" if h["type"] == "phone" else "🔍"
            lines += (
                f"{icon} <code>{he(h['query'] or '')}</code>"
                f" → <b>{he(h['result_name'] or '—')}</b>\n"
                f"<i>  {(h['searched_at'] or '')[:16]}</i>\n\n"
            )
        await edit(lines, back_kb())

    # ══════════════════════════════════════════════════
    #  👑 OWNER PANEL
    # ══════════════════════════════════════════════════
    elif data == "owner_panel":
        if not is_owner(uid): await q.answer("❌ Access denied!", show_alert=True); return
        users = await get_all_users()
        await edit(
            f"🛠 <b>Owner Panel</b>\n━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Total Users : {len(users)}\n\n"
            f"Action chuno:",
            owner_panel_kb(),
        )

    elif data == "owner_stats":
        if not is_owner(uid): return
        users   = await get_all_users()
        total_u = sum(u["total_lookups"] for u in users)
        total_p = sum(u["total_phone_lookups"] for u in users)
        await edit(
            f"📊 <b>Bot Statistics</b>\n━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Total Users        : {len(users)}\n"
            f"🔍 Username Lookups   : {total_u}\n"
            f"📱 Phone Lookups      : {total_p}\n"
            f"📈 Total              : {total_u + total_p}",
            owner_panel_kb(),
        )

    elif data == "owner_users":
        if not is_owner(uid): return
        users  = await get_all_users()
        output = f"👥 <b>All Users ({len(users)})</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        for u in users[:20]:
            ru = u["total_lookups"]
            rp = u["total_phone_lookups"]
            output += (
                f"🔍{ru}  📱{rp}  |  <code>{u['user_id']}</code> <b>{he(u['full_name'] or 'User')}</b>\n"
                f"   🕐 {(u['last_seen'] or '')[:10]}\n\n"
            )
        if len(users) > 20:
            output += f"<i>...and {len(users)-20} more</i>"
        await edit(output, owner_panel_kb())

    elif data == "owner_broadcast":
        if not is_owner(uid): return
        ctx.user_data["waiting"] = "broadcast"
        await edit("📢 <b>Broadcast</b>\n\nSabhi users ko bhejni wali message type karo:", cancel_kb())


# ══════════════════════════════════════════════════════════════════
#  🚀  MAIN
# ══════════════════════════════════════════════════════════════════
async def post_init(app):
    await init_db()

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, smart_message))
    logger.info("🤖 Smoke Bot Started - COMPLETELY FREE!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
