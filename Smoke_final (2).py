import os, logging, sqlite3, aiohttp, asyncio
from html import escape as he   # HTML escape for all dynamic content
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode
from telegram.error import BadRequest

# ══════════════════════════════════════════════════════════════════
#  ⚙️  CONFIG
# ══════════════════════════════════════════════════════════════════
BOT_TOKEN      = os.getenv("BOT_TOKEN")
API_KEY        = os.getenv("API_KEY")
OWNER_ID       = int(os.getenv("OWNER_ID"))
OWNER_USERNAME = "l_Smoke_ll"          # without @ — used in links
API_BASE       = "https://pan-seven-eta.vercel.app/"
PHONE_API_BASE = "https://num-to-info-ten.vercel.app/"
FREE_USES      = 2
PHONE_FREE     = 2

PHONE_PLANS = [
    ("50",  50),  ("100", 100),
    ("150", 150), ("200", 200),
    ("250", 250), ("300", 300),
    ("350", 350), ("450", 450),
]

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

HTML = ParseMode.HTML   # shortcut

# ══════════════════════════════════════════════════════════════════
#  🛡️  SAFE EDIT HELPER  — prevents "Message to edit not found"
# ══════════════════════════════════════════════════════════════════
async def safe_edit(msg, text, parse_mode=None, reply_markup=None):
    """Edit a message safely. If message is gone, send a new one."""
    try:
        await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        err = str(e).lower()
        if "message to edit not found" in err or "message is not modified" in err:
            try:
                await msg.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            except Exception:
                pass
        else:
            raise




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
            SET username=excluded.username,
                full_name=excluded.full_name,
                language_code=excluded.language_code,
                last_seen=excluded.last_seen
        """, (u.id, u.username or "", u.full_name, u.language_code or "", now))

def save_lookup(user_id, query, ltype, result_name="", result_id="", phone=""):
    with db() as con:
        con.execute("""
            INSERT INTO lookup_history (user_id, query, type, result_name, result_id, phone)
            VALUES (?,?,?,?,?,?)
        """, (user_id, query, ltype, result_name, result_id, phone))
        if ltype == "phone":
            con.execute("UPDATE users SET total_phone_lookups=total_phone_lookups+1 WHERE user_id=?", (user_id,))
        else:
            con.execute("UPDATE users SET total_lookups=total_lookups+1 WHERE user_id=?", (user_id,))

def get_user(user_id):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def get_all_users():
    with db() as con:
        return con.execute("SELECT * FROM users ORDER BY joined_at DESC").fetchall()

def get_user_history(user_id, limit=5):
    with db() as con:
        return con.execute("""
            SELECT * FROM lookup_history WHERE user_id=?
            ORDER BY searched_at DESC LIMIT ?
        """, (user_id, limit)).fetchall()

# ── Username/ID Lookup Quota ──
def get_remaining(user_id):
    u = get_user(user_id)
    if not u:
        return FREE_USES, "free"
    free_left = max(0, FREE_USES - u["free_used"])
    if free_left > 0:
        return free_left, "free"
    if u["status"] == "approved":
        left = u["approved_limit"] - u["approved_used"]
        return max(0, left), "approved"
    return 0, u["status"]

def can_use(user_id):
    rem, kind = get_remaining(user_id)
    return rem > 0, kind

def consume(user_id):
    u = get_user(user_id)
    if not u:
        return False
    free_left = max(0, FREE_USES - u["free_used"])
    with db() as con:
        if free_left > 0:
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
            UPDATE users
            SET status='approved', approved_limit=approved_limit+?, approved_used=0
            WHERE user_id=?
        """, (limit, user_id))

# ── Phone Lookup Quota ──
def get_phone_remaining(user_id):
    u = get_user(user_id)
    if not u:
        return PHONE_FREE, "free"
    free_left = max(0, PHONE_FREE - u["phone_free_used"])
    if free_left > 0:
        return free_left, "free"
    if u["phone_status"] == "approved":
        left = u["phone_approved_limit"] - u["phone_approved_used"]
        return max(0, left), "approved"
    return 0, u["phone_status"]

def can_use_phone(user_id):
    rem, kind = get_phone_remaining(user_id)
    return rem > 0, kind

def consume_phone(user_id):
    u = get_user(user_id)
    if not u:
        return False
    free_left = max(0, PHONE_FREE - u["phone_free_used"])
    with db() as con:
        if free_left > 0:
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
            UPDATE users
            SET phone_status='approved', phone_approved_limit=phone_approved_limit+?, phone_approved_used=0
            WHERE user_id=?
        """, (limit, user_id))


# ══════════════════════════════════════════════════════════════════
#  🌐  API CALLS
# ══════════════════════════════════════════════════════════════════
async def fetch_info(query):
    params = {"key": API_KEY, "q": query}
    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    async with aiohttp.ClientSession() as s:
        async with s.get(API_BASE, params=params, timeout=timeout) as r:
            if r.status != 200:
                return {"success": False, "message": f"API Error {r.status}"}
            return await r.json()

async def fetch_phone_info(number):
    url = f"{PHONE_API_BASE}?num={number}"
    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=timeout) as r:
            if r.status != 200:
                return {"success": False, "message": f"API Error {r.status}"}
            return await r.json()


# ══════════════════════════════════════════════════════════════════
#  🎨  FORMATTERS  (HTML parse mode — dynamic content via he())
# ══════════════════════════════════════════════════════════════════
def bi(v): return "✅" if v else "❌"

def hv(val, fallback="—"):
    """HTML-escape a value; return fallback if empty/None."""
    return he(str(val)) if val else fallback

def owner_link():
    return f"<a href='https://t.me/{OWNER_USERNAME}'>@{OWNER_USERNAME}</a>"

def format_result(d, rem_after):
    # ── Phone info block (from API's phone_info field) ──
    ph = d.get("phone_info", {}) or {}
    phone_block = ""
    if ph.get("success") and ph.get("number"):
        phone_block = (
            f"\n┌─────────────────────────\n"
            f"│  📞 <b>PHONE DETECTED</b>\n"
            f"├─────────────────────────\n"
            f"│  Number  » <code>{hv(ph.get('number'))}</code>\n"
            f"│  Country » {hv(ph.get('country'))} {hv(ph.get('country_code',''))}\n"
            f"└─────────────────────────\n"
        )

    # ── Restriction block ──
    restricted_block = ""
    if d.get("is_restricted"):
        restricted_block = f"\n⚠️ <b>Restricted:</b> <i>{hv(d.get('restriction_reason', 'Yes'))}</i>\n"

    # ── Common chats (only show if > 0) ──
    cc = d.get("common_chats_count", 0) or 0
    common = f"│  Common Chats » {cc}\n" if cc else ""

    # ── Username display ──
    uname = d.get("username") or ""
    uname_display = f"@{he(uname)}" if uname else "—"

    # ── Search type badge ──
    stype = d.get("search_type", "")
    badge = "🔍 Username Search" if "username" in stype else "🆔 ID Search"

    # ── Status emoji ──
    status = d.get("status") or "—"
    status_map = {
        "recently":    "🟡 Recently",
        "online":      "🟢 Online",
        "offline":     "🔴 Offline",
        "long_time_ago": "⚫ Long ago",
        "within_week": "🟠 Within week",
        "within_month": "🔵 Within month",
    }
    status_display = status_map.get(status, hv(status))

    # ── DC location ──
    dc_map = {1: "🇺🇸 Miami", 2: "🇳🇱 Amsterdam", 3: "🇺🇸 Miami", 4: "🇳🇱 Amsterdam", 5: "🇸🇬 Singapore"}
    dc_id   = d.get("dc_id")
    dc_disp = f"DC{dc_id} {dc_map.get(dc_id, '')}" if dc_id else "—"

    return (
        f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃   🔍  <b>LOOKUP RESULT</b>   ┃\n"
        f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n"
        f"<i>{badge}</i>\n\n"
        f"👤 <b>PROFILE</b>\n"
        f"┌─────────────────────────\n"
        f"│  Full Name  » <b>{hv(d.get('full_name'))}</b>\n"
        f"│  First Name » {hv(d.get('first_name'))}\n"
        f"│  Last Name  » {hv(d.get('last_name'))}\n"
        f"│  Username   » <code>{uname_display}</code>\n"
        f"│  User ID    » <code>{hv(d.get('user_id', '—'))}</code>\n"
        f"│  Last Seen  » {status_display}\n"
        f"│  DC Server  » {dc_disp}\n"
        f"{common}"
        f"│  Bio        » <i>{hv(d.get('bio'), 'No bio')}</i>\n"
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
        f"{phone_block}\n"
        f"🔢 Remaining  » <code>{rem_after}</code> lookups\n"
        f"⏱ Response   » <i>{hv(d.get('response_time', '—'))}</i>\n\n"
        f"✦ <b>Powered by {owner_link()}</b>"
    )


def format_phone_result(data, number, rem_after):
    """
    Phone leak API returns either:
      - {"results": [...], "phone": "...", ...}   (multi-DB leak format)
      - A flat dict with direct fields
    """
    results = data.get("results") if isinstance(data, dict) else None

    # ── Multi-result leak format ──
    if results and isinstance(results, list):
        success_results = [r for r in results if r.get("success")]

        if not success_results:
            return (
                f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃   📱  <b>PHONE LOOKUP</b>    ┃\n"
                f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
                f"📞 Number » <code>{he(number)}</code>\n\n"
                f"❌ <b>No data found</b> in any database.\n\n"
                f"🔢 Remaining » <code>{rem_after}</code> lookups\n\n"
                f"✦ <b>Powered by {owner_link()}</b>"
            )

        lines = (
            f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃   📱  <b>PHONE LOOKUP</b>    ┃\n"
            f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
            f"📞 Number » <code>{he(number)}</code>\n"
            f"🗄 Found in <b>{len(success_results)}</b> database(s)\n\n"
        )

        for i, r in enumerate(success_results, 1):
            db_name = hv(r.get("source") or r.get("database") or r.get("db") or f"DB {i}")
            lines += f"┌─────── <b>#{i} {db_name}</b>\n"
            # Show all non-meta fields dynamically
            skip_keys = {"success", "source", "database", "db", "message", "error"}
            for k, v in r.items():
                if k in skip_keys or not v:
                    continue
                label = k.replace("_", " ").title()
                lines += f"│  {label} » {hv(v)}\n"
            lines += "└─────────────────────────\n\n"

        lines += (
            f"🔢 Remaining » <code>{rem_after}</code> lookups\n\n"
            f"✦ <b>Powered by {owner_link()}</b>"
        )
        return lines

    # ── Simple flat response format ──
    return (
        f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃   📱  <b>PHONE LOOKUP</b>    ┃\n"
        f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
        f"📞 Number » <code>{he(number)}</code>\n\n"
        f"📋 <b>DETAILS</b>\n"
        f"┌─────────────────────────\n"
        f"│  Name     » <b>{hv(data.get('name') or data.get('full_name'))}</b>\n"
        f"│  Carrier  » {hv(data.get('carrier') or data.get('operator'))}\n"
        f"│  Country  » {hv(data.get('country'))}\n"
        f"│  Region   » {hv(data.get('region') or data.get('state'))}\n"
        f"│  Type     » {hv(data.get('line_type') or data.get('type'))}\n"
        f"│  Valid    » {bi(data.get('valid', True))}\n"
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

def approve_kb(user_id, uname):
    """Owner sees limit buttons: 10 / 25 / 50 / 100 lookups."""
    limit_btns = [
        InlineKeyboardButton(f"✅ {lim}", callback_data=f"approve_{user_id}_{lim}")
        for lim in [10, 25, 50, 100]
    ]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Message User", url=f"tg://user?id={user_id}")],
        limit_btns[:2],
        limit_btns[2:],
        [InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}")],
    ])

def phone_plans_kb(user_id):
    rows = []
    row = []
    for label, reqs in PHONE_PLANS:
        row.append(InlineKeyboardButton(f"✅ {label}", callback_data=f"papprove_{user_id}_{reqs}"))
        if len(row) == 4:
            rows.append(row)
            row = []
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
        [InlineKeyboardButton("👥 All Users",   callback_data="owner_users")],
        [InlineKeyboardButton("📊 Stats",       callback_data="owner_stats")],
        [InlineKeyboardButton("📢 Broadcast",   callback_data="owner_broadcast")],
        [InlineKeyboardButton("🏠 Main Menu",   callback_data="main_menu")],
    ])


# ══════════════════════════════════════════════════════════════════
#  🔎  CORE LOOKUPS
# ══════════════════════════════════════════════════════════════════
async def perform_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str):
    user_id = update.effective_user.id
    upsert_user(update.effective_user)
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
        await reply(text, parse_mode=HTML, reply_markup=kb)
        return

    reply_fn = update.message.reply_text if update.message else update.callback_query.message.reply_text
    msg = await reply_fn("⏳ <b>Searching...</b>\n<code>▰▰▱▱▱▱▱▱▱▱</code> 20%", parse_mode=HTML)

    try:
        await asyncio.sleep(1)
        await safe_edit(msg, "⏳ <b>Searching...</b>\n<code>▰▰▰▰▰▱▱▱▱▱</code> 50%", parse_mode=HTML)

        data = await asyncio.wait_for(fetch_info(query), timeout=15)

        if "error" in data or data.get("success") == False:
            err = data.get("message") or data.get("error") or "Unknown error"
            await safe_edit(msg, f"❌ <b>Error:</b> <code>{he(str(err))}</code>", parse_mode=HTML, reply_markup=back_kb())
            return

        await safe_edit(msg, "⏳ <b>Searching...</b>\n<code>▰▰▰▰▰▰▰▰▱▱</code> 80%", parse_mode=HTML)

        consume(user_id)
        rem_after, _ = get_remaining(user_id)

        save_lookup(
            user_id, query, "username",
            result_name=data.get("full_name", ""),
            result_id=str(data.get("user_id", "")),
            phone=data.get("phone_info", {}).get("number", "")
        )

        text = format_result(data, rem_after)
        pic  = data.get("profile_pic")
        kb   = result_kb(data.get("username"))

        if pic:
            await msg.delete()
            send = update.message.reply_photo if update.message else update.callback_query.message.reply_photo
            await send(pic, caption=text, parse_mode=HTML, reply_markup=kb)
        else:
            await safe_edit(msg, text, parse_mode=HTML, reply_markup=kb)

        if rem_after == 0:
            u = get_user(user_id)
            if u and u["status"] == "free":
                warn = update.message.reply_text if update.message else update.callback_query.message.reply_text
                await warn(
                    f"⚠️ <b>Last Free Lookup Used!</b>\n\nMore access ke liye contact karo: {owner_link()}",
                    parse_mode=HTML, reply_markup=request_access_kb()
                )

    except (aiohttp.ClientError, asyncio.TimeoutError):
        await safe_edit(msg, 
            "❌ <b>Network Error!</b>\nAPI slow hai ya down hai. Thodi der baad try karo.",
            parse_mode=HTML, reply_markup=back_kb()
        )
    except Exception as e:
        logger.error(e)
        await safe_edit(msg, f"❌ <b>Error:</b> <code>{he(str(e))}</code>", parse_mode=HTML, reply_markup=back_kb())


async def perform_phone_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE, number: str):
    user_id = update.effective_user.id
    upsert_user(update.effective_user)

    number = number.strip().replace(" ", "").replace("-", "")
    if not number.lstrip("+").isdigit():
        reply = update.message.reply_text if update.message else update.callback_query.message.reply_text
        await reply(
            "❌ <b>Invalid Number!</b>\nExample: <code>9876543210</code> ya <code>+919876543210</code>",
            parse_mode=HTML, reply_markup=back_kb()
        )
        return

    allowed, _ = can_use_phone(user_id)
    if not allowed:
        u = get_user(user_id)
        if u and u["phone_status"] == "pending":
            text = "⏳ <b>Phone Request Pending...</b>\n\nOwner approve karega. 🔔"
            kb = back_kb()
        else:
            text = (
                "🚫 <b>Phone Lookup Limit Khatam!</b>\n\n"
                f"Tumhare <code>{PHONE_FREE}</code> free phone lookups use ho gaye.\n\n"
                f"Plan lo — owner se contact karo: {owner_link()}"
            )
            kb = phone_request_access_kb()
        reply = update.message.reply_text if update.message else update.callback_query.message.reply_text
        await reply(text, parse_mode=HTML, reply_markup=kb)
        return

    reply_fn = update.message.reply_text if update.message else update.callback_query.message.reply_text
    msg = await reply_fn("⏳ <b>Searching...</b>\n<code>▰▰▱▱▱▱▱▱▱▱</code> 20%", parse_mode=HTML)

    try:
        await asyncio.sleep(1)
        await safe_edit(msg, "⏳ <b>Searching...</b>\n<code>▰▰▰▰▰▱▱▱▱▱</code> 50%", parse_mode=HTML)

        data = await asyncio.wait_for(fetch_phone_info(number), timeout=15)

        # ✅ Error check PEHLE, consume BAAD mein
        if "error" in data or data.get("success") == False:
            err = data.get("message") or data.get("error") or "Unknown error"
            await safe_edit(msg, f"❌ <b>Error:</b> <code>{he(str(err))}</code>", parse_mode=HTML, reply_markup=back_kb())
            return

        await safe_edit(msg, "⏳ <b>Searching...</b>\n<code>▰▰▰▰▰▰▰▰▱▱</code> 80%", parse_mode=HTML)

        consume_phone(user_id)
        rem_after, _ = get_phone_remaining(user_id)

        save_lookup(user_id, number, "phone",
                    result_name=data.get("name", ""),
                    phone=number)

        text = format_phone_result(data, number, rem_after)
        await safe_edit(msg, text, parse_mode=HTML, reply_markup=phone_result_kb())

        if rem_after == 0:
            u = get_user(user_id)
            if u and u["phone_status"] == "free":
                warn = update.message.reply_text if update.message else update.callback_query.message.reply_text
                await warn(
                    f"⚠️ <b>Last Free Phone Lookup!</b>\n\nPlan ke liye: {owner_link()}",
                    parse_mode=HTML, reply_markup=phone_request_access_kb()
                )

    except (aiohttp.ClientError, asyncio.TimeoutError):
        await safe_edit(msg, 
            "❌ <b>Network Error!</b> API slow hai. Baad me try karo.",
            parse_mode=HTML, reply_markup=back_kb()
        )
    except Exception as e:
        logger.error(e)
        await safe_edit(msg, f"❌ <b>Error:</b> <code>{he(str(e))}</code>", parse_mode=HTML, reply_markup=back_kb())


# ══════════════════════════════════════════════════════════════════
#  📟  COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    uid     = update.effective_user.id
    rem, _  = get_remaining(uid)
    prem, _ = get_phone_remaining(uid)
    fname   = he(update.effective_user.first_name or "User")
    await update.message.reply_text(
        f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃    🤖  <b>SMOKE  BOT</b>      ┃\n"
        f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
        f"👋 Welcome, <b>{fname}!</b>\n\n"
        f"Telegram users ka complete info fetch karo.\n\n"
        f"📊 <b>Your Balance</b>\n"
        f"┌ 🔍 Username/ID » <code>{rem}</code> free\n"
        f"└ 📱 Phone Lookup » <code>{prem}</code> free\n\n"
        f"👇 Choose an option:",
        parse_mode=HTML,
        reply_markup=main_menu_kb(uid)
    )

async def lookup_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        await perform_lookup(update, ctx, ctx.args[0])
    else:
        await update.message.reply_text(
            "⚠️ Usage: <code>/lookup @username</code> ya <code>/lookup 123456789</code>",
            parse_mode=HTML, reply_markup=back_kb()
        )

async def smart_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    waiting = ctx.user_data.get("waiting")

    if waiting == "broadcast" and update.effective_user.id == OWNER_ID:
        ctx.user_data.pop("waiting", None)
        users  = get_all_users()
        sent   = failed = 0
        msg    = await update.message.reply_text("📢 Broadcasting...")
        for u in users:
            try:
                await ctx.bot.send_message(u["user_id"], text, parse_mode=HTML)
                sent += 1
            except:
                failed += 1
        await safe_edit(msg, 
            f"✅ <b>Broadcast Done!</b>\n├ Sent   : {sent}\n└ Failed : {failed}",
            parse_mode=HTML
        )
        return

    if waiting in ("username", "userid"):
        ctx.user_data.pop("waiting", None)
        await perform_lookup(update, ctx, text)
        return

    if waiting == "phone":
        ctx.user_data.pop("waiting", None)
        await perform_phone_lookup(update, ctx, text)
        return

    # Auto detect
    if text.startswith("@"):
        await perform_lookup(update, ctx, text)
    elif text.lstrip("+").isdigit() and len(text) >= 10:
        await perform_phone_lookup(update, ctx, text)
    elif text.lstrip("-").isdigit():
        await perform_lookup(update, ctx, text)
    else:
        await update.message.reply_text(
            "🤔 <code>@username</code>, User ID ya Phone Number bhejo, ya menu use karo:",
            parse_mode=HTML,
            reply_markup=main_menu_kb(update.effective_user.id)
        )


# ══════════════════════════════════════════════════════════════════
#  🖱️  BUTTON HANDLER
# ══════════════════════════════════════════════════════════════════
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data
    uid  = update.effective_user.id

    # ── Main Menu ──
    if data == "main_menu":
        ctx.user_data.clear()
        rem, _  = get_remaining(uid)
        prem, _ = get_phone_remaining(uid)
        await q.message.edit_text(
            f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃    🤖  <b>SMOKE  BOT</b>      ┃\n"
            f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
            f"📊 <b>Your Balance</b>\n"
            f"┌ 🔍 Username/ID » <code>{rem}</code> remaining\n"
            f"└ 📱 Phone Lookup » <code>{prem}</code> remaining\n\n"
            f"👇 Choose an option:",
            parse_mode=HTML,
            reply_markup=main_menu_kb(uid)
        )

    # ── Search ──
    elif data == "do_username":
        ctx.user_data["waiting"] = "username"
        await q.message.edit_text(
            "🔍 <b>Username Lookup</b>\n\n<code>@username</code> type karke bhejo:\n<i>Example: @durov</i>",
            parse_mode=HTML, reply_markup=cancel_kb()
        )

    elif data == "do_userid":
        ctx.user_data["waiting"] = "userid"
        await q.message.edit_text(
            "🆔 <b>User ID Lookup</b>\n\nNumeric User ID bhejo:\n<i>Example: 12345678</i>",
            parse_mode=HTML, reply_markup=cancel_kb()
        )

    elif data == "search_again":
        ctx.user_data["waiting"] = "username"
        await q.message.reply_text(
            "🔍 <b>New Search</b>\n<code>@username</code> ya ID bhejo:",
            parse_mode=HTML, reply_markup=cancel_kb()
        )

    # ── Phone ──
    elif data == "do_phone":
        ctx.user_data["waiting"] = "phone"
        prem, _ = get_phone_remaining(uid)
        await q.message.edit_text(
            f"📱 <b>Phone Number Lookup</b>\n\n"
            f"🆓 Free remaining: <code>{prem}</code>\n\n"
            f"Phone number bhejo:\n<i>Example: 9876543210</i>",
            parse_mode=HTML, reply_markup=cancel_kb()
        )

    elif data == "search_phone_again":
        ctx.user_data["waiting"] = "phone"
        await q.message.reply_text(
            "📱 Phone number bhejo:",
            parse_mode=HTML, reply_markup=cancel_kb()
        )

    # ── My Account ──
    elif data == "my_account":
        u = get_user(uid)
        rem, kind   = get_remaining(uid)
        prem, pkind = get_phone_remaining(uid)

        def plan_str(kind, u, lim_key, used_key):
            if kind == "approved" and u:
                return f"✅ Active ({u[lim_key]-u[used_key]}/{u[lim_key]} left)"
            elif kind == "pending":
                return "⏳ Pending approval"
            return "❌ Not approved"

        fname = he(update.effective_user.full_name or "User")
        await q.message.edit_text(
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
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📩 Request Username Access", callback_data="request_access")],
                [InlineKeyboardButton("📱 Request Phone Access",    callback_data="phone_request_access")],
                [InlineKeyboardButton("🏠 Menu",                    callback_data="main_menu")],
            ])
        )

    # ── My History ──
    elif data == "my_history":
        history = get_user_history(uid, 10)
        if not history:
            await q.message.edit_text(
                "📜 <b>Search History</b>\n\nAbhi koi search nahi ki!",
                parse_mode=HTML, reply_markup=back_kb()
            )
            return
        lines = "📜 <b>Recent Searches (Last 10)</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        for hh in history:
            icon = "📱" if hh["type"] == "phone" else "🔍"
            name = he(hh["result_name"] or "—")
            qval = he(hh["query"] or "")
            time = (hh["searched_at"] or "")[:16]
            lines += f"{icon} <code>{qval}</code> » <b>{name}</b>\n<i>  {time}</i>\n\n"
        await q.message.edit_text(lines, parse_mode=HTML, reply_markup=back_kb())

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
                f"📌 Kitne uses dene hain? Choose karo:",
                parse_mode=HTML,
                reply_markup=approve_kb(uid, uname)
            )
        except Exception as e:
            logger.error(f"Owner notify failed: {e}")
        await q.message.edit_text(
            "📩 <b>Request Sent!</b>\n\n"
            "Owner tumhara request review karega.\n"
            "Approve hone pe notification milega. 🔔",
            parse_mode=HTML, reply_markup=back_kb()
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
                reply_markup=phone_plans_kb(uid)
            )
        except Exception as e:
            logger.error(f"Owner notify failed: {e}")
        await q.message.edit_text(
            "📩 <b>Phone Access Request Sent!</b>\n\n"
            "Owner plan approve karega.\n"
            "Notification aayega jab approve ho. 🔔",
            parse_mode=HTML, reply_markup=back_kb()
        )

    # ── Approve/Reject Username ──
    elif data.startswith("approve_"):
        if uid != OWNER_ID:
            await q.answer("❌ Access denied!", show_alert=True)
            return
        parts = data.split("_")
        target_id, limit = int(parts[1]), int(parts[2])
        approve_user(target_id, limit)
        await q.message.edit_text(
            f"✅ <b>Approved!</b>\nUser <code>{target_id}</code> → <b>{limit} lookups</b> granted.",
            parse_mode=HTML
        )
        try:
            await ctx.bot.send_message(
                target_id,
                f"🎉 <b>Access Approved!</b>\n\n"
                f"Owner ne tumhe <b>{limit} Username/ID lookups</b> approve kiye!\n\n"
                f"Ab search karo 👇",
                parse_mode=HTML,
                reply_markup=main_menu_kb(target_id)
            )
        except Exception as e:
            logger.warning(f"Notify failed: {e}")

    elif data.startswith("reject_"):
        if uid != OWNER_ID:
            await q.answer("❌ Access denied!", show_alert=True)
            return
        target_id = int(data[7:])
        with db() as con:
            con.execute("UPDATE users SET status='free' WHERE user_id=?", (target_id,))
        await q.message.edit_text(
            f"❌ <b>Rejected!</b> User <code>{target_id}</code>", parse_mode=HTML
        )
        try:
            await ctx.bot.send_message(
                target_id,
                "❌ <b>Request Rejected</b>\n\nBaad me dobara try karo.",
                parse_mode=HTML
            )
        except:
            pass

    # ── Approve/Reject Phone ──
    elif data.startswith("papprove_"):
        if uid != OWNER_ID:
            await q.answer("❌ Access denied!", show_alert=True)
            return
        parts = data.split("_")
        target_id, limit = int(parts[1]), int(parts[2])
        approve_phone_user(target_id, limit)
        await q.message.edit_text(
            f"✅ <b>Phone Approved!</b>\nUser <code>{target_id}</code> → <b>{limit} phone lookups</b> granted.",
            parse_mode=HTML
        )
        try:
            await ctx.bot.send_message(
                target_id,
                f"🎉 <b>Phone Access Approved!</b>\n\n"
                f"Owner ne tumhe <b>{limit} Phone lookups</b> approve kiye!\n\n"
                f"Ab search karo 👇",
                parse_mode=HTML,
                reply_markup=main_menu_kb(target_id)
            )
        except Exception as e:
            logger.warning(f"Notify failed: {e}")

    elif data.startswith("preject_"):
        if uid != OWNER_ID:
            await q.answer("❌ Access denied!", show_alert=True)
            return
        target_id = int(data[8:])
        with db() as con:
            con.execute("UPDATE users SET phone_status='free' WHERE user_id=?", (target_id,))
        await q.message.edit_text(
            f"❌ <b>Rejected!</b> User <code>{target_id}</code>", parse_mode=HTML
        )
        try:
            await ctx.bot.send_message(
                target_id,
                "❌ <b>Phone Request Rejected</b>\n\nBaad me dobara try karo.",
                parse_mode=HTML
            )
        except:
            pass

    # ── Owner Panel ──
    elif data == "owner_panel":
        if uid != OWNER_ID:
            await q.answer("❌ Access denied!", show_alert=True)
            return
        users     = get_all_users()
        pending_u = [u for u in users if u["status"] == "pending"]
        pending_p = [u for u in users if u["phone_status"] == "pending"]
        await q.message.edit_text(
            f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃    👑  <b>OWNER PANEL</b>     ┃\n"
            f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
            f"📊 <b>Overview</b>\n"
            f"┌ Total Users      » {len(users)}\n"
            f"├ Pending Username » {len(pending_u)}\n"
            f"└ Pending Phone    » {len(pending_p)}\n\n"
            f"Action chuno:",
            parse_mode=HTML,
            reply_markup=owner_panel_kb()
        )

    elif data == "owner_stats":
        if uid != OWNER_ID:
            return
        users     = get_all_users()
        total_lu  = sum(u["total_lookups"] for u in users)
        total_ph  = sum(u["total_phone_lookups"] for u in users)
        approved  = [u for u in users if u["status"] == "approved"]
        papproved = [u for u in users if u["phone_status"] == "approved"]
        await q.message.edit_text(
            f"📊 <b>Bot Statistics</b>\n━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Total Users         » {len(users)}\n"
            f"✅ Approved (Username) » {len(approved)}\n"
            f"✅ Approved (Phone)    » {len(papproved)}\n\n"
            f"🔍 Total Username Lookups » {total_lu}\n"
            f"📱 Total Phone Lookups    » {total_ph}\n"
            f"📈 Total Searches         » {total_lu + total_ph}",
            parse_mode=HTML,
            reply_markup=owner_panel_kb()
        )

    elif data == "owner_users":
        if uid != OWNER_ID:
            return
        users  = get_all_users()
        output = f"👥 <b>All Users ({len(users)})</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        for u in users[:20]:
            si    = {"free": "🆓", "pending": "⏳", "approved": "✅", "exhausted": "🚫"}.get(u["status"], "❓")
            pi    = {"free": "🆓", "pending": "⏳", "approved": "✅"}.get(u["phone_status"], "❓")
            rem   = max(0, FREE_USES - u["free_used"]) + max(0, u["approved_limit"] - u["approved_used"])
            prem  = max(0, PHONE_FREE - u["phone_free_used"]) + max(0, u["phone_approved_limit"] - u["phone_approved_used"])
            fname = he(u["full_name"] or "User")
            output += f"{si}{pi} <code>{u['user_id']}</code> <b>{fname}</b>\n    🔍{rem} 📱{prem} | 🕐{(u['last_seen'] or '')[:10]}\n\n"
        if len(users) > 20:
            output += f"<i>...aur {len(users)-20} users</i>"
        await q.message.edit_text(output, parse_mode=HTML, reply_markup=owner_panel_kb())

    elif data == "owner_broadcast":
        if uid != OWNER_ID:
            return
        ctx.user_data["waiting"] = "broadcast"
        await q.message.edit_text(
            "📢 <b>Broadcast Message</b>\n\nSabhi users ko bhejni wali message type karo:",
            parse_mode=HTML, reply_markup=cancel_kb()
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
