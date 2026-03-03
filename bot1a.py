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
API_KEY        = os.getenv("API_KEY")   # tg-to-num API key (e.g. "Ansh")
OWNER_ID       = int(os.getenv("OWNER_ID"))
OWNER_USERNAME = "l_Smoke_ll"
MONGO_URI      = os.getenv("MONGO_URI", "mongodb+srv://yb131567_db_user:R8zxuvc9Qn999Arg@cluster0.drjaxl8.mongodb.net/telegram_bot?retryWrites=true&w=majority")
SUPPORT_GROUP  = "https://t.me/+your_support_group_link"   # ← apna link dalo
API_BASE       = "https://tg-to-num-six.vercel.app/"
PHONE_API_BASE = "https://num-to-info-ten.vercel.app/"
FREE_USES      = 2
PHONE_FREE     = 2

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
# ══════════════════════════════════════════════════════════════════
#  🗄️  MONGODB  (motor — async)
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
    await _mdb.admins.create_index("admin_id", unique=True)
    await _mdb.lookup_history.create_index("user_id")
    logger.info("✅ MongoDB connected")

# ── Default user doc ──
def _default_user(uid, username="", full_name="", language_code=""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "user_id": uid, "username": username,
        "full_name": full_name, "language_code": language_code,
        "free_used": 0, "approved_limit": 0, "approved_used": 0,
        "status": "free",
        "phone_free_used": 0, "phone_approved_limit": 0, "phone_approved_used": 0,
        "phone_status": "free",
        "total_lookups": 0, "total_phone_lookups": 0,
        "last_seen": now, "joined_at": now,
    }

async def upsert_user(u):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    default = _default_user(u.id, u.username or "", u.full_name or "", u.language_code or "")
    # Remove fields that are in $set from $setOnInsert to avoid conflict
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

# ── Admin helpers ──
async def get_admins():
    cursor = _mdb.admins.find({}, {"_id": 0}).sort("added_at", -1)
    return await cursor.to_list(length=None)

async def get_admin(admin_id):
    return await _mdb.admins.find_one({"admin_id": admin_id}, {"_id": 0})

async def add_admin(admin_id, username, full_name, added_by,
                    can_approve=1, can_broadcast=0, can_view_users=1):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await _mdb.admins.update_one(
        {"admin_id": admin_id},
        {"$set": {"username": username, "full_name": full_name,
                  "added_by": added_by, "can_approve": can_approve,
                  "can_broadcast": can_broadcast, "can_view_users": can_view_users,
                  "added_at": now}},
        upsert=True,
    )

async def remove_admin(admin_id):
    await _mdb.admins.delete_one({"admin_id": admin_id})

async def is_admin(user_id):
    if user_id == OWNER_ID: return True
    return await get_admin(user_id) is not None

def is_owner(user_id):
    return user_id == OWNER_ID

# ── Quota helpers ──
async def get_remaining(user_id):
    u = await get_user(user_id)
    if not u: return FREE_USES, "free"
    fl = max(0, FREE_USES - u.get("free_used", 0))
    if fl > 0: return fl, "free"
    if u.get("status") == "approved":
        return max(0, u.get("approved_limit", 0) - u.get("approved_used", 0)), "approved"
    return 0, u.get("status", "free")

async def can_use(user_id):
    rem, kind = await get_remaining(user_id)
    return rem > 0, kind

async def consume(user_id):
    u = await get_user(user_id)
    if not u: return False
    if max(0, FREE_USES - u.get("free_used", 0)) > 0:
        await _mdb.users.update_one({"user_id": user_id}, {"$inc": {"free_used": 1}})
        return True
    if u.get("status") == "approved" and u.get("approved_used", 0) < u.get("approved_limit", 0):
        await _mdb.users.update_one({"user_id": user_id}, {"$inc": {"approved_used": 1}})
        return True
    return False

async def set_pending(user_id):
    await _mdb.users.update_one({"user_id": user_id}, {"$set": {"status": "pending"}})

async def approve_user(user_id, limit):
    await _mdb.users.update_one(
        {"user_id": user_id},
        {"$inc": {"approved_limit": limit}, "$set": {"status": "approved", "approved_used": 0}}
    )

async def get_phone_remaining(user_id):
    u = await get_user(user_id)
    if not u: return PHONE_FREE, "free"
    fl = max(0, PHONE_FREE - u.get("phone_free_used", 0))
    if fl > 0: return fl, "free"
    if u.get("phone_status") == "approved":
        return max(0, u.get("phone_approved_limit", 0) - u.get("phone_approved_used", 0)), "approved"
    return 0, u.get("phone_status", "free")

async def can_use_phone(user_id):
    rem, kind = await get_phone_remaining(user_id)
    return rem > 0, kind

async def consume_phone(user_id):
    u = await get_user(user_id)
    if not u: return False
    if max(0, PHONE_FREE - u.get("phone_free_used", 0)) > 0:
        await _mdb.users.update_one({"user_id": user_id}, {"$inc": {"phone_free_used": 1}})
        return True
    if u.get("phone_status") == "approved" and u.get("phone_approved_used", 0) < u.get("phone_approved_limit", 0):
        await _mdb.users.update_one({"user_id": user_id}, {"$inc": {"phone_approved_used": 1}})
        return True
    return False

async def set_phone_pending(user_id):
    await _mdb.users.update_one({"user_id": user_id}, {"$set": {"phone_status": "pending"}})

async def approve_phone_user(user_id, limit):
    await _mdb.users.update_one(
        {"user_id": user_id},
        {"$inc": {"phone_approved_limit": limit},
         "$set": {"phone_status": "approved", "phone_approved_used": 0}}
    )

async def update_admin_perm(admin_id, field, value):
    await _mdb.admins.update_one({"admin_id": admin_id}, {"$set": {field: value}})



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

def format_tg_result(d: dict, rem: int) -> str:
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
        + f"\n🔢 Remaining : <code>{rem}</code> lookups\n"
        + f"✦ <b>Made by @{OWNER_USERNAME}</b>"
    )

def format_phone_result(d: dict, number: str, rem: int) -> str:
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

    lines += (
        f"🔢 Remaining : <code>{rem}</code> lookups\n"
        f"✦ <b>Made by @{OWNER_USERNAME}</b>"
    )
    return lines


async def limit_exhausted_msg(user_id: int, kind: str = "lookup") -> str:
    u = await get_user(user_id)
    if u and u["status"] == "pending":
        return (
            "⏳ <b>Request Pending...</b>\n\n"
            "Owner/Admin tumhara access approve karega.\n"
            "Approve hone pe notification milega. 🔔"
        )
    label = "Phone" if kind == "phone" else "Username/ID"
    return (
        f"⚠️ <b>Tumhara {label} limit temporarily khatam ho gaya hai.</b>\n\n"
        f"Aur searches karne ke liye subscription lo.\n\n"
        f"<b>Owner se contact karo subscription ke liye:</b>"
    )

def limit_exhausted_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Owner se Contact Karo", url=f"https://t.me/{OWNER_USERNAME}")],
        [InlineKeyboardButton("💬 Support Group",         url=SUPPORT_GROUP)],
    ])

# ── Approve keyboards — used by both owner & admin ──
def approve_kb(user_id):
    btns = [InlineKeyboardButton(f"✅ {lim}", callback_data=f"approve_{user_id}_{lim}")
            for lim in [10, 25, 50, 100]]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Message User", url=f"tg://user?id={user_id}")],
        btns[:2], btns[2:],
        [InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}")],
    ])

def phone_plans_kb(user_id):
    PLANS = [("50",50),("100",100),("150",150),("200",200),
             ("250",250),("300",300),("350",350),("450",450)]
    rows, row = [], []
    for label, val in PLANS:
        row.append(InlineKeyboardButton(f"✅ {label}", callback_data=f"papprove_{user_id}_{val}"))
        if len(row) == 4:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("💬 Message User", url=f"tg://user?id={user_id}")])
    rows.append([InlineKeyboardButton("❌ Reject", callback_data=f"preject_{user_id}")])
    return InlineKeyboardMarkup(rows)

def main_menu_kb(user_id=None, user_is_admin=False):
    """Pass user_is_admin=True if user is admin (checked async before calling)"""
    kb = [
        [InlineKeyboardButton("📊 My Profile",    callback_data="my_account"),
         InlineKeyboardButton("📜 History",       callback_data="my_history")],
        [InlineKeyboardButton("👑 Contact Owner", url=f"https://t.me/{OWNER_USERNAME}"),
         InlineKeyboardButton("💬 Support",       url=SUPPORT_GROUP)],
    ]
    if user_id and user_is_admin and not is_owner(user_id):
        kb.append([InlineKeyboardButton("🛡 Admin Panel", callback_data="admin_panel")])
    if user_id and is_owner(user_id):
        kb.append([InlineKeyboardButton("🛠 Owner Panel", callback_data="owner_panel")])
    return InlineKeyboardMarkup(kb)

def two_button_kb(query: str):
    """
    HAMESHA 2 buttons show karo:
    📱 Telegram  → tg-to-num API (username/ID → telegram lookup)
    📞 Number Info → num-to-info API (phone number details)
    
    Username/ID bhejo → Telegram button kaam karega
    Phone number bhejo → Number Info button kaam karega
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Telegram",    callback_data=f"search_tg:{query}"),
         InlineKeyboardButton("📞 Number Info", callback_data=f"search_ph:{query}")],
    ])

def platform_kb(query: str):
    return two_button_kb(query)

def phone_platform_kb(number: str):
    return two_button_kb(number)

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]])

def cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]])

def owner_panel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 All Users",      callback_data="owner_users"),
         InlineKeyboardButton("📊 Stats",          callback_data="owner_stats")],
        [InlineKeyboardButton("🛡 Manage Admins",  callback_data="owner_admins"),
         InlineKeyboardButton("📢 Broadcast",      callback_data="owner_broadcast")],
        [InlineKeyboardButton("🏠 Main Menu",      callback_data="main_menu")],
    ])

def admin_panel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Pending Users",  callback_data="admin_pending"),
         InlineKeyboardButton("📊 Stats",          callback_data="admin_stats")],
        [InlineKeyboardButton("📜 All Users",      callback_data="admin_users")],
        [InlineKeyboardButton("🏠 Main Menu",      callback_data="main_menu")],
    ])

def manage_admins_kb(admins):
    rows = []
    for a in admins:
        name = a["full_name"] or a["username"] or str(a["admin_id"])
        rows.append([
            InlineKeyboardButton(f"🛡 {name[:20]}", callback_data=f"admin_view:{a['admin_id']}"),
            InlineKeyboardButton("❌ Remove",        callback_data=f"admin_remove:{a['admin_id']}"),
        ])
    rows.append([InlineKeyboardButton("➕ Add Admin", callback_data="admin_add")])
    rows.append([InlineKeyboardButton("◀ Back",       callback_data="owner_panel")])
    return InlineKeyboardMarkup(rows)

def admin_perms_kb(target_id, can_approve, can_broadcast, can_view):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{'✅' if can_approve else '❌'} Can Approve Users",
            callback_data=f"admin_perm:{target_id}:approve:{0 if can_approve else 1}")],
        [InlineKeyboardButton(
            f"{'✅' if can_broadcast else '❌'} Can Broadcast",
            callback_data=f"admin_perm:{target_id}:broadcast:{0 if can_broadcast else 1}")],
        [InlineKeyboardButton(
            f"{'✅' if can_view else '❌'} Can View Users",
            callback_data=f"admin_perm:{target_id}:view:{0 if can_view else 1}")],
        [InlineKeyboardButton("❌ Remove Admin", callback_data=f"admin_remove:{target_id}"),
         InlineKeyboardButton("◀ Back",          callback_data="owner_admins")],
    ])

async def user_profile_text(user_id: int, full_name: str) -> str:
    u           = await get_user(user_id)
    rem, kind   = await get_remaining(user_id)
    prem, pkind = await get_phone_remaining(user_id)
    sub_u  = "✅ Active" if kind == "approved" else ("⏳ Pending" if kind == "pending" else "❌ None")
    sub_p  = "✅ Active" if pkind == "approved" else ("⏳ Pending" if pkind == "pending" else "❌ None")
    used_u = (u["free_used"] if u else 0) + (u["approved_used"] if u else 0)
    used_p = (u["phone_free_used"] if u else 0) + (u["phone_approved_used"] if u else 0)
    lim_u  = FREE_USES + (u["approved_limit"] if u else 0)
    lim_p  = PHONE_FREE + (u["phone_approved_limit"] if u else 0)
    fname  = he(full_name or "User")
    role   = "👑 Owner" if is_owner(user_id) else ("🛡 Admin" if await is_admin(user_id) else "👤 User")
    return (
        f"{role}  |  👤 <b>{fname}</b>  |  🆔 <code>{user_id}</code>\n\n"
        f"🔍 <b>Username / ID Lookup</b>\n"
        f"├ Subscription : {sub_u}\n"
        f"├ Used         : {used_u} / {lim_u}\n"
        f"└ Remaining    : <code>{rem}</code>\n\n"
        f"📱 <b>Phone Lookup</b>\n"
        f"├ Subscription : {sub_p}\n"
        f"├ Used         : {used_p} / {lim_p}\n"
        f"└ Remaining    : <code>{prem}</code>\n\n"
        f"📅 Joined : {(u['joined_at'] or '')[:10] if u else '—'}\n\n"
        f"✦ <b>Made by @{OWNER_USERNAME}</b>"
    )


# ══════════════════════════════════════════════════════════════════
#  🔎  CORE LOOKUPS
# ══════════════════════════════════════════════════════════════════
async def perform_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str):
    user_id = update.effective_user.id
    await upsert_user(update.effective_user)

    allowed, _ = await can_use(user_id)
    if not allowed:
        reply = update.message.reply_text if update.message else update.callback_query.message.reply_text
        await safe_send(reply, await limit_exhausted_msg(user_id, "lookup"),
                        reply_markup=limit_exhausted_kb())
        return

    reply_fn = update.message.reply_text if update.message else update.callback_query.message.reply_text
    msg = await reply_fn("🔍 <b>Searching Telegram...</b>", parse_mode=HTML)

    try:
        await asyncio.sleep(0.5)
        data = await asyncio.wait_for(fetch_info(query), timeout=15)

        if not data or data.get("success") == False or "error" in data:
            err = data.get("message") or data.get("error") or "Not found"
            await safe_edit(msg, f"❌ <b>Error:</b> <code>{he(str(err))}</code>", reply_markup=back_kb())
            return

        await consume(user_id)
        rem_after, _ = await get_remaining(user_id)

        await save_lookup(user_id, query, "username",
                    result_name=data.get("full_name", ""),
                    result_id=str(data.get("user_id", "")),
                    phone=(data.get("phone_info") or {}).get("number", ""))

        text = format_tg_result(data, rem_after)

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

        if rem_after == 0:
            u = await get_user(user_id)
            if u and u["status"] == "free":
                warn = update.message.reply_text if update.message else update.callback_query.message.reply_text
                await safe_send(warn, await limit_exhausted_msg(user_id, "lookup"),
                                reply_markup=limit_exhausted_kb())

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

    allowed, _ = await can_use_phone(user_id)
    if not allowed:
        reply = update.message.reply_text if update.message else update.callback_query.message.reply_text
        await safe_send(reply, await limit_exhausted_msg(user_id, "phone"),
                        reply_markup=limit_exhausted_kb())
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

        await consume_phone(user_id)
        rem_after, _ = await get_phone_remaining(user_id)
        await save_lookup(user_id, number, "phone",
                    result_name=data.get("name", ""), phone=number)

        text = format_phone_result(data, number, rem_after)
        await safe_edit(msg, text)

        if rem_after == 0:
            u = await get_user(user_id)
            if u and u["phone_status"] == "free":
                warn = update.message.reply_text if update.message else update.callback_query.message.reply_text
                await safe_send(warn, await limit_exhausted_msg(user_id, "phone"),
                                reply_markup=limit_exhausted_kb())

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
    rem, _  = await get_remaining(uid)
    prem, _ = await get_phone_remaining(uid)
    await safe_send(
        update.message.reply_text,
        f"🤖 <b>Smoke Bot</b>\n\n"
        f"👋 Welcome, <b>{fname}</b>!\n\n"
        f"Username, User ID ya Phone Number bhejo — 2 options milenge:\n"
        f"📱 <b>Telegram Info</b> — username se number nikalo\n"
        f"📞 <b>Number Info</b> — number se details nikalo\n\n"
        f"📊 <b>Tumhara Balance</b>\n"
        f"├ 🔍 Username/ID : <code>{rem}</code> searches\n"
        f"└ 📱 Phone       : <code>{prem}</code> searches\n\n"
        f"✦ <b>Made by @{OWNER_USERNAME}</b>",
        reply_markup=main_menu_kb(uid, await is_admin(uid)),
    )

async def addadmin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Owner only: /addadmin <user_id>"""
    if not is_owner(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: <code>/addadmin &lt;user_id&gt;</code>", parse_mode=HTML)
        return
    try:
        target_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    u = await get_user(target_id)
    name = u["full_name"] if u else "Unknown"
    uname = u["username"] if u else ""
    await add_admin(target_id, uname, name, update.effective_user.id)
    await update.message.reply_text(
        f"✅ <b>Admin Added!</b>\n"
        f"User <code>{target_id}</code> (<b>{he(name)}</b>) is now an admin.\n\n"
        f"Default permissions:\n"
        f"✅ Can approve users\n❌ Cannot broadcast\n✅ Can view users",
        parse_mode=HTML,
    )
    try:
        await ctx.bot.send_message(
            target_id,
            f"🛡 <b>You have been made an Admin!</b>\n\n"
            f"Ab tum users ko approve kar sakte ho.\n"
            f"Use /start karo aur Admin Panel dekho.",
            parse_mode=HTML, reply_markup=main_menu_kb(target_id, await is_admin(target_id)),
        )
    except Exception: pass

async def removeadmin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Owner only: /removeadmin <user_id>"""
    if not is_owner(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/removeadmin &lt;user_id&gt;</code>", parse_mode=HTML)
        return
    try:
        target_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID."); return
    await remove_admin(target_id)
    await update.message.reply_text(f"✅ Admin <code>{target_id}</code> removed.", parse_mode=HTML)


# ══════════════════════════════════════════════════════════════════
#  💬  SMART MESSAGE HANDLER
# ══════════════════════════════════════════════════════════════════
async def smart_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    uid     = update.effective_user.id
    waiting = ctx.user_data.get("waiting")
    await upsert_user(update.effective_user)

    # ── Broadcast (owner or admin with permission) ──
    if waiting == "broadcast":
        admin_rec = await get_admin(uid)
        if uid == OWNER_ID or (admin_rec and admin_rec["can_broadcast"]):
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

    # ── Waiting for add admin (owner only) ──
    if waiting == "add_admin_id" and is_owner(uid):
        ctx.user_data.pop("waiting", None)
        try:
            target_id = int(text)
            u = await get_user(target_id)
            name  = u["full_name"] if u else "Unknown"
            uname = u["username"] if u else ""
            await add_admin(target_id, uname, name, uid)
            await update.message.reply_text(
                f"✅ <b>Admin Added!</b>\n<code>{target_id}</code> — <b>{he(name)}</b>",
                parse_mode=HTML,
            )
            try:
                await ctx.bot.send_message(
                    target_id,
                    "🛡 <b>Tujhe Admin banaya gaya hai!</b>\n\n/start karo aur Admin Panel dekho.",
                    parse_mode=HTML, reply_markup=main_menu_kb(target_id, await is_admin(target_id)),
                )
            except Exception: pass
        except ValueError:
            await update.message.reply_text("❌ Sirf numeric User ID bhejo.")
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
            reply_markup=main_menu_kb(uid, await is_admin(uid)),
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
        rem, _  = await get_remaining(uid)
        prem, _ = await get_phone_remaining(uid)
        fname   = he(update.effective_user.first_name or "User")
        await edit(
            f"🤖 <b>Smoke Bot</b>\n\n"
            f"👋 <b>{fname}</b>\n\n"
            f"📊 <b>Tumhara Balance</b>\n"
            f"├ 🔍 Username/ID : <code>{rem}</code>\n"
            f"└ 📱 Phone       : <code>{prem}</code>\n\n"
            f"✦ <b>Made by @{OWNER_USERNAME}</b>",
            main_menu_kb(uid),
        )

    # ══════════════════════════════════════════════════
    #  MY PROFILE
    # ══════════════════════════════════════════════════
    elif data == "my_account":
        text = await user_profile_text(uid, update.effective_user.full_name or "User")
        await edit(text, InlineKeyboardMarkup([
            [InlineKeyboardButton("📩 Request Username Access", callback_data="request_access")],
            [InlineKeyboardButton("📱 Request Phone Access",   callback_data="phone_request_access")],
            [InlineKeyboardButton("🏠 Main Menu",               callback_data="main_menu")],
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
    #  REQUEST ACCESS
    # ══════════════════════════════════════════════════
    elif data == "request_access":
        u = await get_user(uid)
        if u and u["status"] == "pending":
            await q.answer("⏳ Request already sent!", show_alert=True); return
        await set_pending(uid)
        uname = he(update.effective_user.username or "—")
        full  = he(update.effective_user.full_name or "User")
        notif = (
            f"🔔 <b>ACCESS REQUEST</b>\n\n"
            f"👤 <a href='tg://user?id={uid}'>{full}</a>\n"
            f"🆔 <code>{uid}</code>  |  @{uname}\n"
            f"🔍 Type: Username/ID Lookup\n\n"
            f"Kitne uses approve karne hain?"
        )
        # Notify owner
        try:
            await ctx.bot.send_message(OWNER_ID, notif, parse_mode=HTML, reply_markup=approve_kb(uid))
        except Exception as e: logger.error("Owner notify: %s", e)
        # Notify all admins with can_approve
        for adm in await get_admins():
            if adm["can_approve"]:
                try:
                    await ctx.bot.send_message(adm["admin_id"], notif, parse_mode=HTML,
                                               reply_markup=approve_kb(uid))
                except Exception: pass
        await edit("📩 <b>Request Sent!</b>\n\nOwner/Admin approve karega. Notification aayega. 🔔", back_kb())

    elif data == "phone_request_access":
        u = await get_user(uid)
        if u and u["phone_status"] == "pending":
            await q.answer("⏳ Request already sent!", show_alert=True); return
        await set_phone_pending(uid)
        uname = he(update.effective_user.username or "—")
        full  = he(update.effective_user.full_name or "User")
        notif = (
            f"📱 <b>PHONE ACCESS REQUEST</b>\n\n"
            f"👤 <a href='tg://user?id={uid}'>{full}</a>\n"
            f"🆔 <code>{uid}</code>  |  @{uname}\n\n"
            f"Plan choose karo:"
        )
        try:
            await ctx.bot.send_message(OWNER_ID, notif, parse_mode=HTML,
                                       reply_markup=phone_plans_kb(uid))
        except Exception as e: logger.error("Owner notify: %s", e)
        for adm in await get_admins():
            if adm["can_approve"]:
                try:
                    await ctx.bot.send_message(adm["admin_id"], notif, parse_mode=HTML,
                                               reply_markup=phone_plans_kb(uid))
                except Exception: pass
        await edit("📩 <b>Phone Request Sent!</b>\n\nOwner/Admin approve karega. 🔔", back_kb())

    # ══════════════════════════════════════════════════
    #  APPROVE / REJECT  (owner & admin with permission)
    # ══════════════════════════════════════════════════
    elif data.startswith("approve_"):
        if not await is_admin(uid):
            await q.answer("❌ Access denied!", show_alert=True); return
        adm = await get_admin(uid)
        if adm and not adm["can_approve"]:
            await q.answer("❌ You don't have approve permission!", show_alert=True); return
        parts = data.split("_")
        target_id, limit = int(parts[1]), int(parts[2])
        await approve_user(target_id, limit)
        approver = he(update.effective_user.first_name or "Admin")
        await edit(f"✅ <b>Approved by {approver}!</b>\n"
                   f"User <code>{target_id}</code> → <b>{limit} lookups</b>")
        try:
            await ctx.bot.send_message(
                target_id,
                f"🎉 <b>Access Approved!</b>\n\n"
                f"<b>{limit} Username/ID lookups</b> approve ho gaye!\n\nAb search karo 👇",
                parse_mode=HTML, reply_markup=main_menu_kb(target_id, await is_admin(target_id)),
            )
        except Exception as e: logger.warning("Notify: %s", e)

    elif data.startswith("reject_"):
        if not await is_admin(uid):
            await q.answer("❌ Access denied!", show_alert=True); return
        adm = await get_admin(uid)
        if adm and not adm["can_approve"]:
            await q.answer("❌ No permission!", show_alert=True); return
        target_id = int(data[7:])
        await _mdb.users.update_one({"user_id": target_id}, {"$set": {"status": "free"}})
        await edit(f"❌ <b>Rejected</b> — User <code>{target_id}</code>")
        try: await ctx.bot.send_message(target_id, "❌ <b>Request Rejected.</b>", parse_mode=HTML)
        except Exception: pass

    elif data.startswith("papprove_"):
        if not await is_admin(uid):
            await q.answer("❌ Access denied!", show_alert=True); return
        adm = await get_admin(uid)
        if adm and not adm["can_approve"]:
            await q.answer("❌ No permission!", show_alert=True); return
        parts = data.split("_")
        target_id, limit = int(parts[1]), int(parts[2])
        await approve_phone_user(target_id, limit)
        approver = he(update.effective_user.first_name or "Admin")
        await edit(f"✅ <b>Phone Approved by {approver}!</b>\n"
                   f"User <code>{target_id}</code> → <b>{limit} lookups</b>")
        try:
            await ctx.bot.send_message(
                target_id,
                f"🎉 <b>Phone Access Approved!</b>\n\n"
                f"<b>{limit} Phone lookups</b> approve ho gaye!\n\nAb search karo 👇",
                parse_mode=HTML, reply_markup=main_menu_kb(target_id, await is_admin(target_id)),
            )
        except Exception as e: logger.warning("Notify: %s", e)

    elif data.startswith("preject_"):
        if not await is_admin(uid):
            await q.answer("❌ Access denied!", show_alert=True); return
        adm = await get_admin(uid)
        if adm and not adm["can_approve"]:
            await q.answer("❌ No permission!", show_alert=True); return
        target_id = int(data[8:])
        await _mdb.users.update_one({"user_id": target_id}, {"$set": {"phone_status": "free"}})
        await edit(f"❌ <b>Phone Rejected</b> — User <code>{target_id}</code>")
        try: await ctx.bot.send_message(target_id, "❌ <b>Phone Request Rejected.</b>", parse_mode=HTML)
        except Exception: pass

    # ══════════════════════════════════════════════════
    #  🛡 ADMIN PANEL
    # ══════════════════════════════════════════════════
    elif data == "admin_panel":
        if not await is_admin(uid) or is_owner(uid):
            await q.answer("❌ Access denied!", show_alert=True); return
        adm  = await get_admin(uid)
        name = he(update.effective_user.first_name or "Admin")
        perms = (
            f"✅ Approve Users\n" if adm["can_approve"] else "❌ Approve Users\n"
        ) + (
            f"{'✅' if adm['can_broadcast'] else '❌'} Broadcast\n"
        ) + (
            f"{'✅' if adm['can_view_users'] else '❌'} View Users"
        )
        await edit(
            f"🛡 <b>Admin Panel</b>\n\n"
            f"👤 {name}\n"
            f"🆔 <code>{uid}</code>\n\n"
            f"🔑 <b>Your Permissions:</b>\n{perms}\n\n"
            f"Action chuno:",
            admin_panel_kb(),
        )

    elif data == "admin_pending":
        if not await is_admin(uid): await q.answer("❌", show_alert=True); return
        adm = await get_admin(uid)
        if adm and not adm["can_approve"]:
            await q.answer("❌ No approve permission!", show_alert=True); return
        users = await get_all_users()
        pu = [u for u in users if u["status"] == "pending"]
        pp = [u for u in users if u["phone_status"] == "pending"]
        if not pu and not pp:
            await edit("✅ <b>No pending requests!</b>", admin_panel_kb()); return
        text = f"⏳ <b>Pending Requests</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        if pu:
            text += f"🔍 <b>Username/ID ({len(pu)})</b>\n"
            for u in pu[:10]:
                uid_val = u["user_id"]
                name_val = he(u["full_name"] or "User")
                text += f"• <a href='tg://user?id={uid_val}'>{name_val}</a> — <code>{uid_val}</code>\n"
            text += "\n"
        if pp:
            text += f"📱 <b>Phone ({len(pp)})</b>\n"
            for u in pp[:10]:
                uid_val = u["user_id"]
                name_val = he(u["full_name"] or "User")
                text += f"• <a href='tg://user?id={uid_val}'>{name_val}</a> — <code>{uid_val}</code>\n"
        await edit(text, admin_panel_kb())

    elif data == "admin_stats":
        if not await is_admin(uid): await q.answer("❌", show_alert=True); return
        users   = await get_all_users()
        total_u = sum(u["total_lookups"] for u in users)
        total_p = sum(u["total_phone_lookups"] for u in users)
        app_u   = sum(1 for u in users if u["status"] == "approved")
        app_p   = sum(1 for u in users if u["phone_status"] == "approved")
        pend_u  = sum(1 for u in users if u["status"] == "pending")
        pend_p  = sum(1 for u in users if u["phone_status"] == "pending")
        await edit(
            f"📊 <b>Bot Statistics</b>\n━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Total Users        : {len(users)}\n"
            f"✅ Approved Username  : {app_u}\n"
            f"✅ Approved Phone     : {app_p}\n"
            f"⏳ Pending Username  : {pend_u}\n"
            f"⏳ Pending Phone     : {pend_p}\n\n"
            f"🔍 Username Lookups   : {total_u}\n"
            f"📱 Phone Lookups      : {total_p}\n"
            f"📈 Total              : {total_u + total_p}",
            admin_panel_kb(),
        )

    elif data == "admin_users":
        if not await is_admin(uid): await q.answer("❌", show_alert=True); return
        adm = await get_admin(uid)
        if adm and not adm["can_view_users"]:
            await q.answer("❌ No view permission!", show_alert=True); return
        users  = await get_all_users()
        output = f"👥 <b>Users ({len(users)})</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        for u in users[:15]:
            su = {"free":"🆓","pending":"⏳","approved":"✅","exhausted":"🚫"}.get(u["status"],"❓")
            sp = {"free":"🆓","pending":"⏳","approved":"✅"}.get(u["phone_status"],"❓")
            ru = max(0,FREE_USES-u["free_used"])+max(0,u["approved_limit"]-u["approved_used"])
            rp = max(0,PHONE_FREE-u["phone_free_used"])+max(0,u["phone_approved_limit"]-u["phone_approved_used"])
            output += (
                f"{su}{sp} <code>{u['user_id']}</code> <b>{he(u['full_name'] or 'User')}</b>\n"
                f"   🔍{ru}  📱{rp}  🕐{(u['last_seen'] or '')[:10]}\n\n"
            )
        if len(users) > 15:
            output += f"<i>...and {len(users)-15} more</i>"
        await edit(output, admin_panel_kb())

    # ══════════════════════════════════════════════════
    #  👑 OWNER PANEL
    # ══════════════════════════════════════════════════
    elif data == "owner_panel":
        if not is_owner(uid): await q.answer("❌ Access denied!", show_alert=True); return
        users     = await get_all_users()
        admins    = await get_admins()
        pending_u = sum(1 for u in users if u["status"] == "pending")
        pending_p = sum(1 for u in users if u["phone_status"] == "pending")
        await edit(
            f"🛠 <b>Owner Panel</b>\n━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Total Users      : {len(users)}\n"
            f"🛡 Total Admins     : {len(admins)}\n"
            f"⏳ Pending Username : {pending_u}\n"
            f"⏳ Pending Phone    : {pending_p}\n\n"
            f"Action chuno:",
            owner_panel_kb(),
        )

    elif data == "owner_stats":
        if not is_owner(uid): return
        users   = await get_all_users()
        admins  = await get_admins()
        total_u = sum(u["total_lookups"] for u in users)
        total_p = sum(u["total_phone_lookups"] for u in users)
        app_u   = sum(1 for u in users if u["status"] == "approved")
        app_p   = sum(1 for u in users if u["phone_status"] == "approved")
        await edit(
            f"📊 <b>Bot Statistics</b>\n━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Total Users        : {len(users)}\n"
            f"🛡 Total Admins       : {len(admins)}\n"
            f"✅ Approved Username  : {app_u}\n"
            f"✅ Approved Phone     : {app_p}\n\n"
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
            su = {"free":"🆓","pending":"⏳","approved":"✅","exhausted":"🚫"}.get(u["status"],"❓")
            sp = {"free":"🆓","pending":"⏳","approved":"✅"}.get(u["phone_status"],"❓")
            ru = max(0,FREE_USES-u["free_used"])+max(0,u["approved_limit"]-u["approved_used"])
            rp = max(0,PHONE_FREE-u["phone_free_used"])+max(0,u["phone_approved_limit"]-u["phone_approved_used"])
            output += (
                f"{su}{sp} <code>{u['user_id']}</code> <b>{he(u['full_name'] or 'User')}</b>\n"
                f"   🔍{ru}  📱{rp}  🕐{(u['last_seen'] or '')[:10]}\n\n"
            )
        if len(users) > 20:
            output += f"<i>...and {len(users)-20} more</i>"
        await edit(output, owner_panel_kb())

    elif data == "owner_broadcast":
        if not is_owner(uid): return
        ctx.user_data["waiting"] = "broadcast"
        await edit("📢 <b>Broadcast</b>\n\nSabhi users ko bhejni wali message type karo:", cancel_kb())

    # ── Admin Management ──
    elif data == "owner_admins":
        if not is_owner(uid): await q.answer("❌", show_alert=True); return
        admins = await get_admins()
        if not admins:
            await edit(
                "🛡 <b>Admins</b>\n\nAbhi koi admin nahi hai.\n\n"
                "Add karne ke liye:\n<code>/addadmin &lt;user_id&gt;</code>",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Admin", callback_data="admin_add")],
                    [InlineKeyboardButton("◀ Back",       callback_data="owner_panel")],
                ]),
            )
            return
        text = f"🛡 <b>Admins ({len(admins)})</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        for a in admins:
            perms = []
            if a["can_approve"]:    perms.append("Approve")
            if a["can_broadcast"]:  perms.append("Broadcast")
            if a["can_view_users"]: perms.append("View")
            text += (
                f"🛡 <b>{he(a['full_name'] or 'Unknown')}</b>\n"
                f"   🆔 <code>{a['admin_id']}</code>  |  Perms: {', '.join(perms) or 'None'}\n\n"
            )
        await edit(text, manage_admins_kb(admins))

    elif data == "admin_add":
        if not is_owner(uid): await q.answer("❌", show_alert=True); return
        ctx.user_data["waiting"] = "add_admin_id"
        await edit(
            "➕ <b>Add Admin</b>\n\n"
            "Jis user ko admin banana hai uska <b>User ID</b> type karo:",
            cancel_kb(),
        )

    elif data.startswith("admin_view:"):
        if not is_owner(uid): await q.answer("❌", show_alert=True); return
        target_id = int(data.split(":")[1])
        adm = await get_admin(target_id)
        if not adm:
            await q.answer("Admin not found!", show_alert=True); return
        await edit(
            f"🛡 <b>Admin Details</b>\n\n"
            f"👤 <b>{he(adm['full_name'] or 'Unknown')}</b>\n"
            f"🆔 <code>{target_id}</code>\n"
            f"👤 @{he(adm['username'] or '—')}\n"
            f"📅 Added: {(adm['added_at'] or '')[:10]}\n\n"
            f"🔑 <b>Permissions (click to toggle):</b>",
            admin_perms_kb(target_id, adm["can_approve"],
                           adm["can_broadcast"], adm["can_view_users"]),
        )

    elif data.startswith("admin_perm:"):
        if not is_owner(uid): await q.answer("❌", show_alert=True); return
        _, target_id, perm, val = data.split(":")
        target_id = int(target_id)
        val       = int(val)
        col_map   = {"approve": "can_approve", "broadcast": "can_broadcast", "view": "can_view_users"}
        col       = col_map.get(perm)
        if col:
            await update_admin_perm(target_id, col, val)
        adm = await get_admin(target_id)
        if adm:
            await edit(
                f"🛡 <b>Admin Details</b>\n\n"
                f"👤 <b>{he(adm['full_name'] or 'Unknown')}</b>\n"
                f"🆔 <code>{target_id}</code>\n\n"
                f"🔑 <b>Permissions (click to toggle):</b>",
                admin_perms_kb(target_id, adm["can_approve"],
                               adm["can_broadcast"], adm["can_view_users"]),
            )

    elif data.startswith("admin_remove:"):
        if not is_owner(uid): await q.answer("❌", show_alert=True); return
        target_id = int(data.split(":")[1])
        await remove_admin(target_id)
        await q.answer("✅ Admin removed!", show_alert=True)
        # Refresh admin list
        admins = await get_admins()
        if not admins:
            await edit("🛡 <b>Admins</b>\n\nKoi admin nahi hai abhi.", owner_panel_kb())
        else:
            text = f"🛡 <b>Admins ({len(admins)})</b>\n━━━━━━━━━━━━━━━━━━\n\n"
            for a in admins:
                text += f"🛡 <b>{he(a['full_name'] or 'Unknown')}</b>  |  <code>{a['admin_id']}</code>\n\n"
            await edit(text, manage_admins_kb(admins))
        try:
            await ctx.bot.send_message(
                target_id,
                "⚠️ <b>Tumhara Admin access remove kar diya gaya hai.</b>",
                parse_mode=HTML,
            )
        except Exception: pass


# ══════════════════════════════════════════════════════════════════
#  🚀  MAIN
# ══════════════════════════════════════════════════════════════════
async def post_init(app):
    await init_db()

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("addadmin",    addadmin_cmd))
    app.add_handler(CommandHandler("removeadmin", removeadmin_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, smart_message))
    logger.info("🤖 Smoke Bot Started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
