import os, re, logging, sqlite3, aiohttp, asyncio
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
API_KEY        = os.getenv("API_KEY")
OWNER_ID       = int(os.getenv("OWNER_ID"))
OWNER_USERNAME = "l_Smoke_ll"
API_BASE       = "https://pan-seven-eta.vercel.app/"
PHONE_API_BASE = "https://num-to-info-ten.vercel.app/"
FREE_USES      = 2
PHONE_FREE     = 2

PHONE_PLANS = [
    ("50", 50), ("100", 100), ("150", 150), ("200", 200),
    ("250", 250), ("300", 300), ("350", 350), ("450", 450),
]

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
HTML = ParseMode.HTML


# ══════════════════════════════════════════════════════════════════
#  🛡️  SAFE SEND / EDIT HELPERS
# ══════════════════════════════════════════════════════════════════
def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)

async def safe_send(fn, text: str, reply_markup=None, parse_mode=HTML):
    """
    Call fn(text, ...). If Telegram returns 400 (broken HTML),
    retry with plain text. Never raises on parse errors.
    """
    kwargs = dict(parse_mode=parse_mode, reply_markup=reply_markup)
    try:
        return await fn(text, **kwargs)
    except BadRequest as e:
        msg = str(e).lower()
        if any(x in msg for x in ("can't parse", "bad request", "invalid", "entities")):
            logger.warning("HTML rejected by Telegram (%s) — retrying plain text", e)
            kwargs["parse_mode"] = None
            try:
                return await fn(strip_html(text), **kwargs)
            except Exception as e2:
                logger.error("Plain-text fallback failed: %s", e2)
        else:
            raise

async def safe_edit(msg, text: str, reply_markup=None, parse_mode=HTML):
    """
    Edit a message safely:
      • 'Message to edit not found'  → reply instead
      • 'Message is not modified'    → silently ignore
      • HTML parse error             → retry plain text
    """
    async def _do_edit(t, pm):
        await msg.edit_text(t, parse_mode=pm, reply_markup=reply_markup)

    async def _do_reply(t, pm):
        await msg.reply_text(t, parse_mode=pm, reply_markup=reply_markup)

    try:
        await safe_send(_do_edit, text, reply_markup=None, parse_mode=parse_mode)
    except BadRequest as e:
        err = str(e).lower()
        if "message to edit not found" in err:
            try:
                await safe_send(_do_reply, text, reply_markup=None, parse_mode=parse_mode)
            except Exception as e2:
                logger.error("safe_edit reply fallback failed: %s", e2)
        elif "message is not modified" in err:
            pass  # identical content — ignore
        else:
            logger.error("safe_edit unhandled error: %s", e)


# ══════════════════════════════════════════════════════════════════
#  🗄️  DATABASE
# ══════════════════════════════════════════════════════════════════
def db():
    con = sqlite3.connect("bot.db")
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id              INTEGER PRIMARY KEY,
            username             TEXT,
            full_name            TEXT,
            language_code        TEXT,
            free_used            INTEGER DEFAULT 0,
            approved_limit       INTEGER DEFAULT 0,
            approved_used        INTEGER DEFAULT 0,
            status               TEXT DEFAULT 'free',
            phone_free_used      INTEGER DEFAULT 0,
            phone_approved_limit INTEGER DEFAULT 0,
            phone_approved_used  INTEGER DEFAULT 0,
            phone_status         TEXT DEFAULT 'free',
            total_lookups        INTEGER DEFAULT 0,
            total_phone_lookups  INTEGER DEFAULT 0,
            last_seen            TEXT,
            joined_at            TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS lookup_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            query       TEXT,
            type        TEXT,
            result_name TEXT,
            result_id   TEXT,
            phone       TEXT,
            searched_at TEXT DEFAULT (datetime('now'))
        );
        """)

def upsert_user(u):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db() as con:
        con.execute("""
            INSERT INTO users (user_id, username, full_name, language_code, last_seen)
            VALUES (?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE
            SET username=excluded.username, full_name=excluded.full_name,
                language_code=excluded.language_code, last_seen=excluded.last_seen
        """, (u.id, u.username or "", u.full_name or "", u.language_code or "", now))

def save_lookup(user_id, query, ltype, result_name="", result_id="", phone=""):
    with db() as con:
        con.execute("""
            INSERT INTO lookup_history (user_id,query,type,result_name,result_id,phone)
            VALUES (?,?,?,?,?,?)
        """, (user_id, query, ltype, result_name, result_id, phone))
        col = "total_phone_lookups" if ltype == "phone" else "total_lookups"
        con.execute(f"UPDATE users SET {col}={col}+1 WHERE user_id=?", (user_id,))

def get_user(user_id):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def get_all_users():
    with db() as con:
        return con.execute("SELECT * FROM users ORDER BY joined_at DESC").fetchall()

def get_user_history(user_id, limit=10):
    with db() as con:
        return con.execute("""
            SELECT * FROM lookup_history WHERE user_id=?
            ORDER BY searched_at DESC LIMIT ?
        """, (user_id, limit)).fetchall()

# ── Username / ID Quota ──
def get_remaining(user_id):
    u = get_user(user_id)
    if not u:
        return FREE_USES, "free"
    free_left = max(0, FREE_USES - u["free_used"])
    if free_left > 0:
        return free_left, "free"
    if u["status"] == "approved":
        return max(0, u["approved_limit"] - u["approved_used"]), "approved"
    return 0, u["status"]

def can_use(user_id):
    rem, kind = get_remaining(user_id)
    return rem > 0, kind

def consume(user_id):
    u = get_user(user_id)
    if not u:
        return False
    with db() as con:
        if max(0, FREE_USES - u["free_used"]) > 0:
            con.execute("UPDATE users SET free_used=free_used+1 WHERE user_id=?", (user_id,))
            return True
        if u["status"] == "approved" and u["approved_used"] < u["approved_limit"]:
            con.execute("UPDATE users SET approved_used=approved_used+1 WHERE user_id=?", (user_id,))
            return True
    return False

def set_pending(user_id):
    with db() as con:
        con.execute("UPDATE users SET status='pending' WHERE user_id=?", (user_id,))

def approve_user(user_id, limit):
    with db() as con:
        con.execute("""
            UPDATE users SET status='approved',
            approved_limit=approved_limit+?, approved_used=0
            WHERE user_id=?
        """, (limit, user_id))

# ── Phone Quota ──
def get_phone_remaining(user_id):
    u = get_user(user_id)
    if not u:
        return PHONE_FREE, "free"
    free_left = max(0, PHONE_FREE - u["phone_free_used"])
    if free_left > 0:
        return free_left, "free"
    if u["phone_status"] == "approved":
        return max(0, u["phone_approved_limit"] - u["phone_approved_used"]), "approved"
    return 0, u["phone_status"]

def can_use_phone(user_id):
    rem, kind = get_phone_remaining(user_id)
    return rem > 0, kind

def consume_phone(user_id):
    u = get_user(user_id)
    if not u:
        return False
    with db() as con:
        if max(0, PHONE_FREE - u["phone_free_used"]) > 0:
            con.execute("UPDATE users SET phone_free_used=phone_free_used+1 WHERE user_id=?", (user_id,))
            return True
        if u["phone_status"] == "approved" and u["phone_approved_used"] < u["phone_approved_limit"]:
            con.execute("UPDATE users SET phone_approved_used=phone_approved_used+1 WHERE user_id=?", (user_id,))
            return True
    return False

def set_phone_pending(user_id):
    with db() as con:
        con.execute("UPDATE users SET phone_status='pending' WHERE user_id=?", (user_id,))

def approve_phone_user(user_id, limit):
    with db() as con:
        con.execute("""
            UPDATE users SET phone_status='approved',
            phone_approved_limit=phone_approved_limit+?, phone_approved_used=0
            WHERE user_id=?
        """, (limit, user_id))


# ══════════════════════════════════════════════════════════════════
#  🌐  API CALLS
# ══════════════════════════════════════════════════════════════════
async def fetch_info(query: str) -> dict:
    timeout = aiohttp.ClientTimeout(total=60, connect=10)
    async with aiohttp.ClientSession() as s:
        async with s.get(API_BASE, params={"key": API_KEY, "q": query}, timeout=timeout) as r:
            if r.status != 200:
                return {"success": False, "message": f"API Error {r.status}"}
            return await r.json(content_type=None)

async def fetch_phone_info(number: str) -> dict:
    timeout = aiohttp.ClientTimeout(total=60, connect=10)
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{PHONE_API_BASE}?num={number}", timeout=timeout) as r:
            if r.status != 200:
                return {"success": False, "message": f"API Error {r.status}"}
            return await r.json(content_type=None)


# ══════════════════════════════════════════════════════════════════
#  🎨  FORMATTERS
# ══════════════════════════════════════════════════════════════════
def hv(val, fallback="—", maxlen=300) -> str:
    """HTML-escape a value. Handles None, 'null', empty, truncation."""
    if val is None or str(val).strip().lower() in ("", "null", "none"):
        return fallback
    s = str(val).strip()
    if len(s) > maxlen:
        s = s[:maxlen] + "…"
    return he(s)

def bi(v) -> str:
    return "✅" if v else "❌"

def owner_link() -> str:
    return f"<a href='https://t.me/{OWNER_USERNAME}'>@{OWNER_USERNAME}</a>"

# ── Status map for user last-seen ──
STATUS_MAP = {
    "recently":     "🟡 Recently",
    "online":       "🟢 Online",
    "offline":      "🔴 Offline",
    "long_time_ago":"⚫ Long ago",
    "within_week":  "🟠 Within week",
    "within_month": "🔵 Within month",
}

# ── DC server locations ──
DC_MAP = {1: "DC1 🇺🇸 Miami", 2: "DC2 🇳🇱 Amsterdam",
          3: "DC3 🇺🇸 Miami", 4: "DC4 🇳🇱 Amsterdam", 5: "DC5 🇸🇬 Singapore"}

def format_result(d: dict, rem_after: int) -> str:
    """Format username/ID lookup result from API response."""

    # ── Username ──
    uname = d.get("username") or ""
    uname_display = f"@{he(uname)}" if uname else "—"

    # ── Last seen status ──
    raw_status = d.get("status") or "—"
    status_display = STATUS_MAP.get(raw_status, hv(raw_status))

    # ── DC server ──
    dc_id = d.get("dc_id")
    dc_display = DC_MAP.get(dc_id, "—") if dc_id else "—"

    # ── Common chats (only if > 0) ──
    cc = d.get("common_chats_count") or 0
    common_line = f"│  Common Chats » {cc}\n" if cc else ""

    # ── Restriction ──
    restricted_block = ""
    if d.get("is_restricted"):
        restricted_block = (
            f"\n⚠️ <b>Restricted:</b> "
            f"<i>{hv(d.get('restriction_reason', 'Yes'), maxlen=100)}</i>\n"
        )

    # ── Embedded phone info ──
    ph = d.get("phone_info") or {}
    phone_block = ""
    if isinstance(ph, dict) and ph.get("success") and ph.get("number"):
        phone_block = (
            f"\n┌─────────────────────────\n"
            f"│  📞 <b>PHONE DETECTED</b>\n"
            f"├─────────────────────────\n"
            f"│  Number   » <code>{hv(ph.get('number'))}</code>\n"
            f"│  Country  » {hv(ph.get('country'))} {hv(ph.get('country_code', ''))}\n"
            f"└─────────────────────────"
        )

    # ── Search type badge ──
    stype = d.get("search_type", "")
    badge = "🔍 Username" if "username" in stype else "🆔 User ID"

    return (
        f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃   🔍  <b>LOOKUP RESULT</b>   ┃\n"
        f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n"
        f"<i>Type: {badge}</i>\n\n"

        f"👤 <b>PROFILE</b>\n"
        f"┌─────────────────────────\n"
        f"│  Full Name  » <b>{hv(d.get('full_name'))}</b>\n"
        f"│  First Name » {hv(d.get('first_name'))}\n"
        f"│  Last Name  » {hv(d.get('last_name'))}\n"
        f"│  Username   » <code>{uname_display}</code>\n"
        f"│  User ID    » <code>{hv(d.get('user_id', '—'))}</code>\n"
        f"│  Last Seen  » {status_display}\n"
        f"│  DC Server  » {dc_display}\n"
        f"{common_line}"
        f"│  Bio        » <i>{hv(d.get('bio'), 'No bio', maxlen=200)}</i>\n"
        f"└─────────────────────────\n\n"

        f"🏷 <b>FLAGS</b>\n"
        f"┌─────────────────────────\n"
        f"│  Bot        » {bi(d.get('is_bot'))}\n"
        f"│  Verified   » {bi(d.get('is_verified'))}\n"
        f"│  Premium ⭐ » {bi(d.get('is_premium'))}\n"
        f"│  Scam       » {bi(d.get('is_scam'))}\n"
        f"│  Fake       » {bi(d.get('is_fake'))}\n"
        f"│  Restricted » {bi(d.get('is_restricted'))}\n"
        f"└─────────────────────────\n"
        f"{restricted_block}"
        f"{phone_block}\n\n"

        f"🔢 Remaining  » <code>{rem_after}</code> lookups\n"
        f"⏱ Response   » <i>{hv(d.get('response_time', '—'))}</i>\n\n"
        f"✦ <b>Powered by {owner_link()}</b>"
    )


def format_phone_result(d: dict, number: str, rem_after: int) -> str:
    """
    Format phone lookup. Handles two API response shapes:
      A) {"results": [...], ...}   — multi-DB leak format
      B) flat dict                 — simple carrier/country format
    """
    results = d.get("results") if isinstance(d, dict) else None

    # ── Shape A: multi-DB results array ──
    if isinstance(results, list):
        success_results = [r for r in results if isinstance(r, dict) and r.get("success")]

        header = (
            f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃   📱  <b>PHONE LOOKUP</b>    ┃\n"
            f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
            f"📞 Number » <code>{he(number)}</code>\n"
        )

        if not success_results:
            return (
                header +
                f"\n❌ <b>No data found</b> in any database.\n\n"
                f"🔢 Remaining » <code>{rem_after}</code> lookups\n\n"
                f"✦ <b>Powered by {owner_link()}</b>"
            )

        body = f"🗄 Found in <b>{len(success_results)}</b> database(s)\n\n"
        skip = {"success", "source", "database", "db", "message", "error"}
        for i, r in enumerate(success_results, 1):
            src = hv(r.get("source") or r.get("database") or r.get("db") or f"DB {i}")
            body += f"┌─────── <b>#{i} {src}</b>\n"
            for k, v in r.items():
                if k in skip or not v:
                    continue
                label = he(k.replace("_", " ").title())
                body += f"│  {label} » {hv(v, maxlen=150)}\n"
            body += "└─────────────────────────\n\n"

        return (
            header + body +
            f"🔢 Remaining » <code>{rem_after}</code> lookups\n\n"
            f"✦ <b>Powered by {owner_link()}</b>"
        )

    # ── Shape B: flat dict ──
    return (
        f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃   📱  <b>PHONE LOOKUP</b>    ┃\n"
        f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
        f"📞 Number » <code>{he(number)}</code>\n\n"
        f"📋 <b>DETAILS</b>\n"
        f"┌─────────────────────────\n"
        f"│  Name     » <b>{hv(d.get('name') or d.get('full_name'))}</b>\n"
        f"│  Carrier  » {hv(d.get('carrier') or d.get('operator'))}\n"
        f"│  Country  » {hv(d.get('country'))}\n"
        f"│  Region   » {hv(d.get('region') or d.get('state'))}\n"
        f"│  Type     » {hv(d.get('line_type') or d.get('type'))}\n"
        f"│  Valid    » {bi(d.get('valid', True))}\n"
        f"└─────────────────────────\n\n"
        f"🔢 Remaining » <code>{rem_after}</code> lookups\n\n"
        f"✦ <b>Powered by {owner_link()}</b>"
    )


# ══════════════════════════════════════════════════════════════════
#  ⌨️  KEYBOARDS
# ══════════════════════════════════════════════════════════════════
def main_menu_kb(user_id=None):
    kb = [
        [
            InlineKeyboardButton("🔍 Username Lookup", callback_data="do_username"),
            InlineKeyboardButton("🆔 User ID Lookup",  callback_data="do_userid"),
        ],
        [InlineKeyboardButton("📱 Phone Number Lookup", callback_data="do_phone")],
        [
            InlineKeyboardButton("📊 My Account",  callback_data="my_account"),
            InlineKeyboardButton("📜 My History",  callback_data="my_history"),
        ],
    ]
    if user_id == OWNER_ID:
        kb.append([InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")])
    return InlineKeyboardMarkup(kb)

def request_access_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Request Access",  callback_data="request_access")],
        [InlineKeyboardButton("💬 Contact Owner",   url=f"https://t.me/{OWNER_USERNAME}")],
        [InlineKeyboardButton("🏠 Main Menu",       callback_data="main_menu")],
    ])

def phone_request_access_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Request Phone Access", callback_data="phone_request_access")],
        [InlineKeyboardButton("💬 Contact Owner",        url=f"https://t.me/{OWNER_USERNAME}")],
        [InlineKeyboardButton("🏠 Main Menu",            callback_data="main_menu")],
    ])

def approve_kb(user_id, _uname):
    rows = [
        [InlineKeyboardButton("💬 Message User", url=f"tg://user?id={user_id}")],
    ]
    btns = [InlineKeyboardButton(f"✅ {lim}", callback_data=f"approve_{user_id}_{lim}")
            for lim in [10, 25, 50, 100]]
    rows.append(btns[:2])
    rows.append(btns[2:])
    rows.append([InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}")])
    return InlineKeyboardMarkup(rows)

def phone_plans_kb(user_id):
    rows = []
    row = []
    for label, val in PHONE_PLANS:
        row.append(InlineKeyboardButton(f"✅ {label}", callback_data=f"papprove_{user_id}_{val}"))
        if len(row) == 4:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("💬 Message User", url=f"tg://user?id={user_id}")])
    rows.append([InlineKeyboardButton("❌ Reject",       callback_data=f"preject_{user_id}")])
    return InlineKeyboardMarkup(rows)

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]])

def cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]])

def result_kb(username=None):
    row = []
    if username:
        row.append(InlineKeyboardButton("🔗 Open Profile", url=f"https://t.me/{username.lstrip('@')}"))
    row.append(InlineKeyboardButton("🔍 Search Again", callback_data="search_again"))
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]])

def phone_result_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Search Again", callback_data="search_phone_again")],
        [InlineKeyboardButton("🏠 Menu",          callback_data="main_menu")],
    ])

def owner_panel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 All Users",  callback_data="owner_users")],
        [InlineKeyboardButton("📊 Stats",      callback_data="owner_stats")],
        [InlineKeyboardButton("📢 Broadcast",  callback_data="owner_broadcast")],
        [InlineKeyboardButton("🏠 Main Menu",  callback_data="main_menu")],
    ])


# ══════════════════════════════════════════════════════════════════
#  🔎  CORE LOOKUPS
# ══════════════════════════════════════════════════════════════════
async def _progress(msg, step: int):
    bars   = ["▰▰▱▱▱▱▱▱▱▱ 20%", "▰▰▰▰▰▱▱▱▱▱ 50%", "▰▰▰▰▰▰▰▰▰▰ 100%"]
    text   = f"⏳ <b>Searching...</b>\n<code>{bars[step]}</code>"
    try:
        await msg.edit_text(text, parse_mode=HTML)
    except Exception:
        pass

async def perform_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str):
    user_id = update.effective_user.id
    upsert_user(update.effective_user)

    # ── Quota check ──
    allowed, _ = can_use(user_id)
    if not allowed:
        u = get_user(user_id)
        if u and u["status"] == "pending":
            text = (
                "⏳ <b>Request Pending...</b>\n\n"
                "Owner tumhara access approve karega.\n"
                "Approve hone pe notification milega. 🔔"
            )
            kb = back_kb()
        else:
            text = (
                "🚫 <b>Free Limit Exhausted!</b>\n\n"
                f"Tumhare <code>{FREE_USES}</code> free lookups khatam ho gaye.\n\n"
                f"Access ke liye owner se contact karo: {owner_link()}"
            )
            kb = request_access_kb()
        reply = update.message.reply_text if update.message else update.callback_query.message.reply_text
        await safe_send(reply, text, reply_markup=kb)
        return

    # ── Progress message ──
    reply_fn = update.message.reply_text if update.message else update.callback_query.message.reply_text
    msg = await reply_fn("⏳ <b>Searching...</b>\n<code>▰▰▱▱▱▱▱▱▱▱ 20%</code>", parse_mode=HTML)

    try:
        await asyncio.sleep(0.8)
        await _progress(msg, 1)

        data = await asyncio.wait_for(fetch_info(query), timeout=60)

        # Error check PEHLE, consume BAAD mein
        if not data or data.get("success") == False or "error" in data:
            err = data.get("message") or data.get("error") or "Unknown error"
            await safe_edit(msg, f"❌ <b>Error:</b> <code>{he(str(err))}</code>",
                            reply_markup=back_kb())
            return

        await _progress(msg, 2)
        consume(user_id)
        rem_after, _ = get_remaining(user_id)

        save_lookup(user_id, query, "username",
                    result_name=data.get("full_name", ""),
                    result_id=str(data.get("user_id", "")),
                    phone=(data.get("phone_info") or {}).get("number", ""))

        text = format_result(data, rem_after)
        kb   = result_kb(data.get("username"))
        pic  = data.get("profile_pic")

        if pic:
            await msg.delete()
            send_photo = (update.message.reply_photo if update.message
                          else update.callback_query.message.reply_photo)
            caption = text[:1020] + "…" if len(text) > 1024 else text
            try:
                await send_photo(pic, caption=caption, parse_mode=HTML, reply_markup=kb)
            except BadRequest:
                await safe_send(reply_fn, text, reply_markup=kb)
        else:
            await safe_edit(msg, text, reply_markup=kb)

        # Send raw JSON data
        import json as _json
        clean = {k: v for k, v in data.items() if k not in ("credit","owner","admin","help_group","note","your_usage","key_name")}
        clean["made_by"] = "@l_Smoke_ll"
        json_text = _json.dumps(clean, indent=2, ensure_ascii=False)
        chunks = [json_text[i:i+4000] for i in range(0, len(json_text), 4000)]
        for chunk in chunks:
            await reply_fn("<pre><code>" + chunk + "</code></pre>", parse_mode=HTML)

        # Warn on last lookup
        if rem_after == 0:
            u = get_user(user_id)
            if u and u["status"] == "free":
                warn = (update.message.reply_text if update.message
                        else update.callback_query.message.reply_text)
                await safe_send(
                    warn,
                    f"⚠️ <b>Last Free Lookup Used!</b>\n\n"
                    f"More access ke liye: {owner_link()}",
                    reply_markup=request_access_kb(),
                )

    except asyncio.TimeoutError:
        await safe_edit(msg,
                        "❌ <b>Timeout!</b> API ne reply nahi kiya. Baad mein try karo.",
                        reply_markup=back_kb())
    except aiohttp.ClientError as e:
        await safe_edit(msg,
                        "❌ <b>Network Error!</b> API se connect nahi ho saka.",
                        reply_markup=back_kb())
    except Exception as e:
        logger.exception("perform_lookup error")
        await safe_edit(msg, f"❌ <b>Error:</b> <code>{he(str(e))}</code>",
                        reply_markup=back_kb())


async def perform_phone_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE, number: str):
    user_id = update.effective_user.id
    upsert_user(update.effective_user)

    # ── Validate ──
    number = number.strip().replace(" ", "").replace("-", "")
    if not number.lstrip("+").isdigit() or len(number.lstrip("+")) < 7:
        reply = update.message.reply_text if update.message else update.callback_query.message.reply_text
        await safe_send(reply,
                        "❌ <b>Invalid Number!</b>\n"
                        "Example: <code>9876543210</code> ya <code>+919876543210</code>",
                        reply_markup=back_kb())
        return

    # ── Quota check ──
    allowed, _ = can_use_phone(user_id)
    if not allowed:
        u = get_user(user_id)
        if u and u["phone_status"] == "pending":
            text = "⏳ <b>Phone Request Pending...</b>\n\nOwner approve karega. 🔔"
            kb   = back_kb()
        else:
            text = (
                "🚫 <b>Phone Lookup Limit Khatam!</b>\n\n"
                f"Tumhare <code>{PHONE_FREE}</code> free phone lookups khatam ho gaye.\n\n"
                f"Plan ke liye contact karo: {owner_link()}"
            )
            kb = phone_request_access_kb()
        reply = update.message.reply_text if update.message else update.callback_query.message.reply_text
        await safe_send(reply, text, reply_markup=kb)
        return

    # ── Progress message ──
    reply_fn = update.message.reply_text if update.message else update.callback_query.message.reply_text
    msg = await reply_fn("⏳ <b>Searching...</b>\n<code>▰▰▱▱▱▱▱▱▱▱ 20%</code>", parse_mode=HTML)

    try:
        await asyncio.sleep(0.8)
        await _progress(msg, 1)

        data = await asyncio.wait_for(fetch_phone_info(number), timeout=60)

        # Error check PEHLE, consume BAAD mein
        if not data or data.get("success") == False or "error" in data:
            err = data.get("message") or data.get("error") or "Unknown error"
            await safe_edit(msg, f"❌ <b>Error:</b> <code>{he(str(err))}</code>",
                            reply_markup=back_kb())
            return

        await _progress(msg, 2)
        consume_phone(user_id)
        rem_after, _ = get_phone_remaining(user_id)

        save_lookup(user_id, number, "phone",
                    result_name=data.get("name", ""), phone=number)

        text = format_phone_result(data, number, rem_after)
        await safe_edit(msg, text, reply_markup=phone_result_kb())

        # Send raw JSON data
        import json as _json
        clean_p = {k: v for k, v in data.items() if k not in ("credit","owner","admin","help_group","note","your_usage","key_name")}
        clean_p["made_by"] = "@l_Smoke_ll"
        json_text = _json.dumps(clean_p, indent=2, ensure_ascii=False)
        chunks = [json_text[i:i+4000] for i in range(0, len(json_text), 4000)]
        phone_reply = (update.message.reply_text if update.message
                       else update.callback_query.message.reply_text)
        for chunk in chunks:
            await phone_reply("<pre><code>" + chunk + "</code></pre>", parse_mode=HTML)

        if rem_after == 0:
            u = get_user(user_id)
            if u and u["phone_status"] == "free":
                warn = (update.message.reply_text if update.message
                        else update.callback_query.message.reply_text)
                await safe_send(warn,
                                f"⚠️ <b>Last Free Phone Lookup!</b>\n\n"
                                f"Plan ke liye: {owner_link()}",
                                reply_markup=phone_request_access_kb())

    except asyncio.TimeoutError:
        await safe_edit(msg,
                        "❌ <b>Timeout!</b> API ne reply nahi kiya. Baad mein try karo.",
                        reply_markup=back_kb())
    except aiohttp.ClientError:
        await safe_edit(msg,
                        "❌ <b>Network Error!</b> API se connect nahi ho saka.",
                        reply_markup=back_kb())
    except Exception as e:
        logger.exception("perform_phone_lookup error")
        await safe_edit(msg, f"❌ <b>Error:</b> <code>{he(str(e))}</code>",
                        reply_markup=back_kb())


# ══════════════════════════════════════════════════════════════════
#  📟  COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    uid     = update.effective_user.id
    rem, _  = get_remaining(uid)
    prem, _ = get_phone_remaining(uid)
    fname   = he(update.effective_user.first_name or "User")
    await safe_send(
        update.message.reply_text,
        f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃    🤖  <b>SMOKE  BOT</b>      ┃\n"
        f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
        f"👋 Welcome, <b>{fname}!</b>\n\n"
        f"Telegram users ka complete info fetch karo.\n\n"
        f"📊 <b>Your Balance</b>\n"
        f"┌ 🔍 Username/ID  » <code>{rem}</code> free\n"
        f"└ 📱 Phone Lookup » <code>{prem}</code> free\n\n"
        f"👇 Choose an option:",
        reply_markup=main_menu_kb(uid),
    )

async def lookup_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        await perform_lookup(update, ctx, ctx.args[0])
    else:
        await safe_send(
            update.message.reply_text,
            "⚠️ Usage: <code>/lookup @username</code> ya <code>/lookup 123456789</code>",
            reply_markup=back_kb(),
        )


# ══════════════════════════════════════════════════════════════════
#  💬  SMART MESSAGE HANDLER
# ══════════════════════════════════════════════════════════════════
async def smart_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    waiting = ctx.user_data.get("waiting")

    # ── Broadcast (owner only) ──
    if waiting == "broadcast" and update.effective_user.id == OWNER_ID:
        ctx.user_data.pop("waiting", None)
        users   = get_all_users()
        sent    = failed = 0
        msg     = await update.message.reply_text("📢 Broadcasting...")
        for u in users:
            try:
                await ctx.bot.send_message(u["user_id"], text, parse_mode=HTML)
                sent += 1
            except Exception:
                failed += 1
        await safe_edit(msg,
                        f"✅ <b>Broadcast Done!</b>\n├ Sent   : {sent}\n└ Failed : {failed}")
        return

    # ── Waiting for specific input ──
    if waiting in ("username", "userid"):
        ctx.user_data.pop("waiting", None)
        await perform_lookup(update, ctx, text)
        return

    if waiting == "phone":
        ctx.user_data.pop("waiting", None)
        await perform_phone_lookup(update, ctx, text)
        return

    # ── Auto-detect ──
    if text.startswith("@"):
        await perform_lookup(update, ctx, text)
    elif text.lstrip("+").isdigit() and len(text.lstrip("+")) >= 10:
        await perform_phone_lookup(update, ctx, text)
    elif text.lstrip("-").isdigit():
        await perform_lookup(update, ctx, text)
    else:
        await safe_send(
            update.message.reply_text,
            "🤔 <code>@username</code>, User ID ya Phone Number bhejo, ya menu use karo:",
            reply_markup=main_menu_kb(update.effective_user.id),
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
                raise

    # ── Main Menu ──
    if data == "main_menu":
        ctx.user_data.clear()
        rem, _  = get_remaining(uid)
        prem, _ = get_phone_remaining(uid)
        await edit(
            f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃    🤖  <b>SMOKE  BOT</b>      ┃\n"
            f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
            f"📊 <b>Your Balance</b>\n"
            f"┌ 🔍 Username/ID  » <code>{rem}</code> remaining\n"
            f"└ 📱 Phone Lookup » <code>{prem}</code> remaining\n\n"
            f"👇 Choose an option:",
            main_menu_kb(uid),
        )

    elif data == "do_username":
        ctx.user_data["waiting"] = "username"
        await edit(
            "🔍 <b>Username Lookup</b>\n\n"
            "<code>@username</code> type karke bhejo:\n<i>Example: @durov</i>",
            cancel_kb(),
        )

    elif data == "do_userid":
        ctx.user_data["waiting"] = "userid"
        await edit(
            "🆔 <b>User ID Lookup</b>\n\n"
            "Numeric User ID bhejo:\n<i>Example: 12345678</i>",
            cancel_kb(),
        )

    elif data == "search_again":
        ctx.user_data["waiting"] = "username"
        await q.message.reply_text(
            "🔍 <b>New Search</b>\n<code>@username</code> ya ID bhejo:",
            parse_mode=HTML, reply_markup=cancel_kb(),
        )

    elif data == "do_phone":
        ctx.user_data["waiting"] = "phone"
        prem, _ = get_phone_remaining(uid)
        await edit(
            f"📱 <b>Phone Number Lookup</b>\n\n"
            f"🆓 Free remaining: <code>{prem}</code>\n\n"
            f"Phone number bhejo:\n<i>Example: 9876543210</i>",
            cancel_kb(),
        )

    elif data == "search_phone_again":
        ctx.user_data["waiting"] = "phone"
        await q.message.reply_text(
            "📱 Phone number bhejo:",
            parse_mode=HTML, reply_markup=cancel_kb(),
        )

    # ── My Account ──
    elif data == "my_account":
        u       = get_user(uid)
        rem, kind   = get_remaining(uid)
        prem, pkind = get_phone_remaining(uid)

        def plan_str(kind, u, lk, uk):
            if kind == "approved" and u:
                return f"✅ Active ({u[lk]-u[uk]}/{u[lk]} left)"
            if kind == "pending":
                return "⏳ Pending"
            return "❌ Not approved"

        fname = he(update.effective_user.full_name or "User")
        await edit(
            f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃     📊  <b>MY ACCOUNT</b>     ┃\n"
            f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
            f"👤 <b>{fname}</b>\n"
            f"🆔 <code>{uid}</code>\n\n"
            f"🔍 <b>Username/ID Lookup</b>\n"
            f"┌ Free Used  » {u['free_used'] if u else 0}/{FREE_USES}\n"
            f"├ Plan       » {plan_str(kind, u, 'approved_limit', 'approved_used')}\n"
            f"└ Remaining  » <code>{rem}</code>\n\n"
            f"📱 <b>Phone Lookup</b>\n"
            f"┌ Free Used  » {u['phone_free_used'] if u else 0}/{PHONE_FREE}\n"
            f"├ Plan       » {plan_str(pkind, u, 'phone_approved_limit', 'phone_approved_used')}\n"
            f"└ Remaining  » <code>{prem}</code>\n\n"
            f"📈 <b>Total Lookups</b>\n"
            f"┌ Username/ID » {u['total_lookups'] if u else 0}\n"
            f"└ Phone       » {u['total_phone_lookups'] if u else 0}\n\n"
            f"📅 Joined » {(u['joined_at'] or '')[:10] if u else '—'}",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📩 Request Username Access", callback_data="request_access")],
                [InlineKeyboardButton("📱 Request Phone Access",    callback_data="phone_request_access")],
                [InlineKeyboardButton("🏠 Menu",                    callback_data="main_menu")],
            ]),
        )

    # ── My History ──
    elif data == "my_history":
        history = get_user_history(uid, 10)
        if not history:
            await edit("📜 <b>Search History</b>\n\nAbhi koi search nahi ki!", back_kb())
            return
        lines = "📜 <b>Recent Searches (Last 10)</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        for h in history:
            icon = "📱" if h["type"] == "phone" else "🔍"
            lines += (
                f"{icon} <code>{he(h['query'] or '')}</code> "
                f"» <b>{he(h['result_name'] or '—')}</b>\n"
                f"<i>  {(h['searched_at'] or '')[:16]}</i>\n\n"
            )
        await edit(lines, back_kb())

    # ── Request Access ──
    elif data == "request_access":
        u = get_user(uid)
        if u and u["status"] == "pending":
            await q.answer("⏳ Request already sent! Wait karo.", show_alert=True)
            return
        set_pending(uid)
        uname = he(update.effective_user.username or "—")
        full  = he(update.effective_user.full_name or "User")
        try:
            await ctx.bot.send_message(
                OWNER_ID,
                f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃   🔔  <b>ACCESS REQUEST</b>   ┃\n"
                f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
                f"👤 <b>User Details</b>\n"
                f"┌ Name     » <a href='tg://user?id={uid}'>{full}</a>\n"
                f"├ ID       » <code>{uid}</code>\n"
                f"├ Username » @{uname}\n"
                f"└ Type     » Username/ID Lookup\n\n"
                f"📌 Kitne uses dene hain?",
                parse_mode=HTML,
                reply_markup=approve_kb(uid, uname),
            )
        except Exception as e:
            logger.error("Owner notify failed: %s", e)
        await edit(
            "📩 <b>Request Sent!</b>\n\n"
            "Owner tumhara request review karega.\n"
            "Approve hone pe notification milega. 🔔",
            back_kb(),
        )

    elif data == "phone_request_access":
        u = get_user(uid)
        if u and u["phone_status"] == "pending":
            await q.answer("⏳ Request already sent! Wait karo.", show_alert=True)
            return
        set_phone_pending(uid)
        uname = he(update.effective_user.username or "—")
        full  = he(update.effective_user.full_name or "User")
        try:
            await ctx.bot.send_message(
                OWNER_ID,
                f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃  📱  <b>PHONE ACCESS REQ</b>  ┃\n"
                f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
                f"👤 <b>User Details</b>\n"
                f"┌ Name     » <a href='tg://user?id={uid}'>{full}</a>\n"
                f"├ ID       » <code>{uid}</code>\n"
                f"├ Username » @{uname}\n"
                f"└ Type     » Phone Lookup\n\n"
                f"📌 Plan choose karo:",
                parse_mode=HTML,
                reply_markup=phone_plans_kb(uid),
            )
        except Exception as e:
            logger.error("Owner notify failed: %s", e)
        await edit(
            "📩 <b>Phone Access Request Sent!</b>\n\n"
            "Owner plan approve karega.\n"
            "Notification aayega jab approve ho. 🔔",
            back_kb(),
        )

    # ── Approve Username ──
    elif data.startswith("approve_"):
        if uid != OWNER_ID:
            await q.answer("❌ Access denied!", show_alert=True); return
        parts = data.split("_")
        target_id, limit = int(parts[1]), int(parts[2])
        approve_user(target_id, limit)
        await edit(f"✅ <b>Approved!</b>\nUser <code>{target_id}</code> → <b>{limit} lookups</b>")
        try:
            await ctx.bot.send_message(
                target_id,
                f"🎉 <b>Access Approved!</b>\n\n"
                f"Owner ne tumhe <b>{limit} Username/ID lookups</b> approve kiye!\n\nAb search karo 👇",
                parse_mode=HTML, reply_markup=main_menu_kb(target_id),
            )
        except Exception as e:
            logger.warning("Notify failed: %s", e)

    # ── Reject Username ──
    elif data.startswith("reject_"):
        if uid != OWNER_ID:
            await q.answer("❌ Access denied!", show_alert=True); return
        target_id = int(data[7:])
        with db() as con:
            con.execute("UPDATE users SET status='free' WHERE user_id=?", (target_id,))
        await edit(f"❌ <b>Rejected!</b> User <code>{target_id}</code>")
        try:
            await ctx.bot.send_message(target_id,
                "❌ <b>Request Rejected</b>\n\nBaad me dobara try karo.", parse_mode=HTML)
        except Exception:
            pass

    # ── Approve Phone ──
    elif data.startswith("papprove_"):
        if uid != OWNER_ID:
            await q.answer("❌ Access denied!", show_alert=True); return
        parts = data.split("_")
        target_id, limit = int(parts[1]), int(parts[2])
        approve_phone_user(target_id, limit)
        await edit(f"✅ <b>Phone Approved!</b>\nUser <code>{target_id}</code> → <b>{limit} lookups</b>")
        try:
            await ctx.bot.send_message(
                target_id,
                f"🎉 <b>Phone Access Approved!</b>\n\n"
                f"Owner ne tumhe <b>{limit} Phone lookups</b> approve kiye!\n\nAb search karo 👇",
                parse_mode=HTML, reply_markup=main_menu_kb(target_id),
            )
        except Exception as e:
            logger.warning("Notify failed: %s", e)

    # ── Reject Phone ──
    elif data.startswith("preject_"):
        if uid != OWNER_ID:
            await q.answer("❌ Access denied!", show_alert=True); return
        target_id = int(data[8:])
        with db() as con:
            con.execute("UPDATE users SET phone_status='free' WHERE user_id=?", (target_id,))
        await edit(f"❌ <b>Rejected!</b> User <code>{target_id}</code>")
        try:
            await ctx.bot.send_message(target_id,
                "❌ <b>Phone Request Rejected</b>\n\nBaad me dobara try karo.", parse_mode=HTML)
        except Exception:
            pass

    # ── Owner Panel ──
    elif data == "owner_panel":
        if uid != OWNER_ID:
            await q.answer("❌ Access denied!", show_alert=True); return
        users     = get_all_users()
        pending_u = [u for u in users if u["status"] == "pending"]
        pending_p = [u for u in users if u["phone_status"] == "pending"]
        await edit(
            f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃    👑  <b>OWNER PANEL</b>     ┃\n"
            f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
            f"📊 <b>Overview</b>\n"
            f"┌ Total Users      » {len(users)}\n"
            f"├ Pending Username » {len(pending_u)}\n"
            f"└ Pending Phone    » {len(pending_p)}\n\n"
            f"Action chuno:",
            owner_panel_kb(),
        )

    elif data == "owner_stats":
        if uid != OWNER_ID: return
        users     = get_all_users()
        total_lu  = sum(u["total_lookups"] for u in users)
        total_ph  = sum(u["total_phone_lookups"] for u in users)
        approved  = [u for u in users if u["status"] == "approved"]
        papproved = [u for u in users if u["phone_status"] == "approved"]
        await edit(
            f"📊 <b>Bot Statistics</b>\n━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Total Users           » {len(users)}\n"
            f"✅ Approved (Username)   » {len(approved)}\n"
            f"✅ Approved (Phone)      » {len(papproved)}\n\n"
            f"🔍 Username Lookups      » {total_lu}\n"
            f"📱 Phone Lookups         » {total_ph}\n"
            f"📈 Total Searches        » {total_lu + total_ph}",
            owner_panel_kb(),
        )

    elif data == "owner_users":
        if uid != OWNER_ID: return
        users  = get_all_users()
        output = f"👥 <b>All Users ({len(users)})</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        for u in users[:20]:
            si    = {"free":"🆓","pending":"⏳","approved":"✅","exhausted":"🚫"}.get(u["status"],"❓")
            pi    = {"free":"🆓","pending":"⏳","approved":"✅"}.get(u["phone_status"],"❓")
            rem   = max(0, FREE_USES - u["free_used"]) + max(0, u["approved_limit"] - u["approved_used"])
            prem  = max(0, PHONE_FREE - u["phone_free_used"]) + max(0, u["phone_approved_limit"] - u["phone_approved_used"])
            fname = he(u["full_name"] or "User")
            output += (
                f"{si}{pi} <code>{u['user_id']}</code> <b>{fname}</b>\n"
                f"   🔍{rem} 📱{prem} | 🕐{(u['last_seen'] or '')[:10]}\n\n"
            )
        if len(users) > 20:
            output += f"<i>...aur {len(users)-20} users</i>"
        await edit(output, owner_panel_kb())

    elif data == "owner_broadcast":
        if uid != OWNER_ID: return
        ctx.user_data["waiting"] = "broadcast"
        await edit(
            "📢 <b>Broadcast Message</b>\n\nSabhi users ko bhejni wali message type karo:",
            cancel_kb(),
        )


# ══════════════════════════════════════════════════════════════════
#  🚀  MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("lookup", lookup_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, smart_message))
    logger.info("🤖 Smoke Bot Started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
