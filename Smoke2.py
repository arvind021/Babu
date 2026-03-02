import os, logging, sqlite3, aiohttp
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode

# ══════════════════════════════════════════════════════════════════
#  ⚙️  CONFIG
# ══════════════════════════════════════════════════════════════════
BOT_TOKEN      = os.getenv("BOT_TOKEN")
API_KEY        = os.getenv("API_KEY")
OWNER_ID       = int(os.getenv("OWNER_ID"))
OWNER_USERNAME = "@l_Smoke_ll"
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
    async with aiohttp.ClientSession() as s:
        async with s.get(API_BASE, params=params, timeout=aiohttp.ClientTimeout(total=60)) as r:
            return await r.json()

async def fetch_phone_info(number):
    url = f"{PHONE_API_BASE}?num={number}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
            return await r.json()


# ══════════════════════════════════════════════════════════════════
#  🎨  FORMATTERS
# ══════════════════════════════════════════════════════════════════
def bi(v): return "✅" if v else "❌"

def format_result(d, rem_after):
    ph = d.get("phone_info", {})
    phone_block = ""
    if ph and ph.get("success") and ph.get("number"):
        phone_block = (
            f"\n┌─────────────────────────\n"
            f"│  📞 *PHONE DETECTED*\n"
            f"├─────────────────────────\n"
            f"│  Number  » `{ph['number']}`\n"
            f"│  Country » {ph.get('country','—')} {ph.get('country_code','')}\n"
            f"└─────────────────────────\n"
        )

    restricted_block = ""
    if d.get("is_restricted"):
        restricted_block = f"\n⚠️ Restricted » _{d.get('restriction_reason','Yes')}_\n"

    common = f"│  Common Chats » {d.get('common_chats_count',0)}\n" if d.get('common_chats_count') is not None else ""

    return (
        f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃   🔍  *LOOKUP  RESULT*   ┃\n"
        f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
        f"👤 *PROFILE*\n"
        f"┌─────────────────────────\n"
        f"│  Full Name  » *{d.get('full_name') or '—'}*\n"
        f"│  First Name » {d.get('first_name') or '—'}\n"
        f"│  Last Name  » {d.get('last_name') or '—'}\n"
        f"│  Username   » `{d.get('username') or '—'}`\n"
        f"│  User ID    » `{d.get('user_id','—')}`\n"
        f"│  Status     » {d.get('status','—')}\n"
        f"│  DC ID      » {d.get('dc_id') or '—'}\n"
        f"{common}"
        f"│  Bio        » _{d.get('bio') or 'No bio'}_\n"
        f"└─────────────────────────\n\n"
        f"🏷 *FLAGS*\n"
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
        f"🔢 Remaining » `{rem_after}` lookups\n"
        f"⏱ Response  » _{d.get('response_time','—')}_\n\n"
        f"✦ *Powered by @l\\_Smoke\\_ll*"
    )

def format_phone_result(d, number, rem_after):
    return (
        f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃   📱  *PHONE  LOOKUP*    ┃\n"
        f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
        f"📞 *Number » `{number}`*\n\n"
        f"📋 *DETAILS*\n"
        f"┌─────────────────────────\n"
        f"│  Name     » *{d.get('name') or d.get('full_name') or '—'}*\n"
        f"│  Carrier  » {d.get('carrier') or d.get('operator') or '—'}\n"
        f"│  Country  » {d.get('country') or '—'}\n"
        f"│  Region   » {d.get('region') or d.get('state') or '—'}\n"
        f"│  Type     » {d.get('line_type') or d.get('type') or '—'}\n"
        f"│  Valid    » {bi(d.get('valid', True))}\n"
        f"└─────────────────────────\n\n"
        f"🔢 Remaining » `{rem_after}` lookups\n\n"
        f"✦ *Powered by @l\\_Smoke\\_ll*"
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
        [InlineKeyboardButton("📩 Request Access",      callback_data="request_access")],
        [InlineKeyboardButton("💬 Contact Owner",       url=f"https://t.me/{OWNER_USERNAME.lstrip('@')}")],
        [InlineKeyboardButton("🏠 Main Menu",           callback_data="main_menu")],
    ])

def phone_request_access_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Request Phone Access", callback_data="phone_request_access")],
        [InlineKeyboardButton("💬 Contact Owner",        url=f"https://t.me/{OWNER_USERNAME.lstrip('@')}")],
        [InlineKeyboardButton("🏠 Main Menu",            callback_data="main_menu")],
    ])

def approve_kb(user_id, uname):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 User ko Message Karo", url=f"tg://user?id={user_id}")],
        [InlineKeyboardButton("✅ Approve", callback_data=f"approve_ask_{user_id}")],
        [InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{user_id}")],
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
    rows.append([InlineKeyboardButton("❌ Reject",        callback_data=f"preject_{user_id}")])
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
                "⏳ *Request Pending...*\n\n"
                "Owner tumhara access approve karega.\n"
                "Notification aayega jab approve ho. 🔔"
            )
            kb = back_kb()
        else:
            text = (
                "🚫 *Free Limit Exhausted!*\n\n"
                f"Tumhare `{FREE_USES}` free lookups khatam ho gaye.\n\n"
                f"Access ke liye owner se contact karo:\n{OWNER_USERNAME}"
            )
            kb = request_access_kb()
        reply = update.message.reply_text if update.message else update.callback_query.message.reply_text
        await reply(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    reply_fn = update.message.reply_text if update.message else update.callback_query.message.reply_text
    msg = await reply_fn(
        "⏳ *Searching...*\n`▰▰▰▰▰▱▱▱▱▱` 50%",
        parse_mode=ParseMode.MARKDOWN
    )

    try:
        data = await fetch_info(query)
        if "error" in data or data.get("success") == False:
            err = data.get("message") or data.get("error") or "Unknown error"
            await msg.edit_text(f"❌ *Error:* `{err}`", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
            return

        consume(user_id)
        rem_after, _ = get_remaining(user_id)

        # Save to history
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
            await send(pic, caption=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

        if rem_after == 0:
            u = get_user(user_id)
            if u and u["status"] == "free":
                warn = update.message.reply_text if update.message else update.callback_query.message.reply_text
                await warn(
                    f"⚠️ *Last Free Lookup Used!*\n\nMore access ke liye contact karo: {OWNER_USERNAME}",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=request_access_kb()
                )

    except aiohttp.ClientError:
        await msg.edit_text("❌ *Network Error!*\nAPI se connect nahi ho paya. Baad me try karo.", reply_markup=back_kb())
    except Exception as e:
        logger.error(e)
        await msg.edit_text(f"❌ *Error:* `{e}`", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())


async def perform_phone_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE, number: str):
    user_id = update.effective_user.id
    upsert_user(update.effective_user)

    number = number.strip().replace(" ", "").replace("-", "")
    if not number.lstrip("+").isdigit():
        reply = update.message.reply_text if update.message else update.callback_query.message.reply_text
        await reply("❌ *Invalid Number!*\nExample: `9876543210` ya `+919876543210`", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
        return

    allowed, _ = can_use_phone(user_id)
    if not allowed:
        u = get_user(user_id)
        if u and u["phone_status"] == "pending":
            text = "⏳ *Phone Request Pending...*\n\nOwner approve karega. 🔔"
            kb = back_kb()
        else:
            text = (
                "🚫 *Phone Lookup Limit Khatam!*\n\n"
                f"Tumhare `{PHONE_FREE}` free phone lookups use ho gaye.\n\n"
                f"Plan lo — owner se contact karo:\n{OWNER_USERNAME}"
            )
            kb = phone_request_access_kb()
        reply = update.message.reply_text if update.message else update.callback_query.message.reply_text
        await reply(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    reply_fn = update.message.reply_text if update.message else update.callback_query.message.reply_text
    msg = await reply_fn(
        "⏳ *Searching...*\n`▰▰▰▰▰▱▱▱▱▱` 50%",
        parse_mode=ParseMode.MARKDOWN
    )

    try:
        data = await fetch_phone_info(number)
        consume_phone(user_id)
        rem_after, _ = get_phone_remaining(user_id)

        save_lookup(user_id, number, "phone",
                    result_name=data.get("name", ""),
                    phone=number)

        text = format_phone_result(data, number, rem_after)
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=phone_result_kb())

        if rem_after == 0:
            u = get_user(user_id)
            if u and u["phone_status"] == "free":
                warn = update.message.reply_text if update.message else update.callback_query.message.reply_text
                await warn(
                    f"⚠️ *Last Free Phone Lookup!*\n\nPlan ke liye: {OWNER_USERNAME}",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=phone_request_access_kb()
                )

    except aiohttp.ClientError:
        await msg.edit_text("❌ *Network Error!* Baad me try karo.", reply_markup=back_kb())
    except Exception as e:
        logger.error(e)
        await msg.edit_text(f"❌ *Error:* `{e}`", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())


# ══════════════════════════════════════════════════════════════════
#  📟  COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    uid  = update.effective_user.id
    rem, _  = get_remaining(uid)
    prem, _ = get_phone_remaining(uid)
    await update.message.reply_text(
        f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃    🤖  *SMOKE  BOT*      ┃\n"
        f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
        f"👋 Welcome, *{update.effective_user.first_name}!*\n\n"
        f"Telegram users ka complete info fetch karo.\n\n"
        f"📊 *Your Balance*\n"
        f"┌ 🔍 Username/ID » `{rem}` free\n"
        f"└ 📱 Phone Lookup » `{prem}` free\n\n"
        f"👇 Choose an option:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(uid)
    )

async def lookup_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        await perform_lookup(update, ctx, ctx.args[0])
    else:
        await update.message.reply_text(
            "⚠️ Usage: `/lookup @username` ya `/lookup 123456789`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb()
        )

async def smart_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    waiting = ctx.user_data.get("waiting")

    if waiting == "broadcast" and update.effective_user.id == OWNER_ID:
        ctx.user_data.pop("waiting", None)
        users = get_all_users()
        sent = failed = 0
        msg = await update.message.reply_text("📢 Broadcasting...")
        for u in users:
            try:
                await ctx.bot.send_message(u["user_id"], text, parse_mode=ParseMode.MARKDOWN)
                sent += 1
            except:
                failed += 1
        await msg.edit_text(f"✅ Broadcast Done!\n├ Sent   : {sent}\n└ Failed : {failed}")
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
            "🤔 `@username`, User ID ya Phone Number bhejo, ya menu use karo:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(update.effective_user.id)
        )


# ══════════════════════════════════════════════════════════════════
#  🖱️  BUTTON HANDLER
# ══════════════════════════════════════════════════════════════════
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
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
            f"┃    🤖  *SMOKE  BOT*      ┃\n"
            f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
            f"📊 *Your Balance*\n"
            f"┌ 🔍 Username/ID » `{rem}` remaining\n"
            f"└ 📱 Phone Lookup » `{prem}` remaining\n\n"
            f"👇 Choose an option:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(uid)
        )

    # ── Search ──
    elif data == "do_username":
        ctx.user_data["waiting"] = "username"
        await q.message.edit_text(
            "🔍 *Username Lookup*\n\n`@username` type karke bhejo:\n_Example: @durov_",
            parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb()
        )

    elif data == "do_userid":
        ctx.user_data["waiting"] = "userid"
        await q.message.edit_text(
            "🆔 *User ID Lookup*\n\nNumeric User ID bhejo:\n_Example: 12345678_",
            parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb()
        )

    elif data == "search_again":
        ctx.user_data["waiting"] = "username"
        await q.message.reply_text(
            "🔍 *New Search*\n`@username` ya ID bhejo:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb()
        )

    # ── Phone ──
    elif data == "do_phone":
        ctx.user_data["waiting"] = "phone"
        prem, _ = get_phone_remaining(uid)
        await q.message.edit_text(
            f"📱 *Phone Number Lookup*\n\n"
            f"🆓 Free remaining: `{prem}`\n\n"
            f"Phone number bhejo:\n_Example: 9876543210_",
            parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb()
        )

    elif data == "search_phone_again":
        ctx.user_data["waiting"] = "phone"
        await q.message.reply_text(
            "📱 Phone number bhejo:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb()
        )

    # ── My Account ──
    elif data == "my_account":
        u = get_user(uid)
        rem, kind   = get_remaining(uid)
        prem, pkind = get_phone_remaining(uid)
        fl  = max(0, FREE_USES - (u["free_used"] if u else 0))
        pfl = max(0, PHONE_FREE - (u["phone_free_used"] if u else 0))

        def plan_str(kind, u, lim_key, used_key):
            if kind == "approved":
                return f"✅ Active ({u[lim_key]-u[used_key]}/{u[lim_key]} left)"
            elif kind == "pending":
                return "⏳ Pending approval"
            return "❌ Not approved"

        await q.message.edit_text(
            f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃     📊  *MY ACCOUNT*     ┃\n"
            f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
            f"👤 *{update.effective_user.full_name}*\n"
            f"🆔 `{uid}`\n\n"
            f"🔍 *Username/ID Lookup*\n"
            f"┌ Free Used  » {u['free_used'] if u else 0}/{FREE_USES}\n"
            f"├ Plan       » {plan_str(kind, u, 'approved_limit', 'approved_used')}\n"
            f"└ Remaining  » `{rem}`\n\n"
            f"📱 *Phone Lookup*\n"
            f"┌ Free Used  » {u['phone_free_used'] if u else 0}/{PHONE_FREE}\n"
            f"├ Plan       » {plan_str(pkind, u, 'phone_approved_limit', 'phone_approved_used')}\n"
            f"└ Remaining  » `{prem}`\n\n"
            f"📈 *Total Lookups*\n"
            f"┌ Username/ID » {u['total_lookups'] if u else 0}\n"
            f"└ Phone       » {u['total_phone_lookups'] if u else 0}\n\n"
            f"📅 Joined » {(u['joined_at'] or '')[:10] if u else '—'}",
            parse_mode=ParseMode.MARKDOWN,
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
                "📜 *Search History*\n\nAbhi koi search nahi ki!",
                parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb()
            )
            return
        text = "📜 *Recent Searches (Last 10)*\n━━━━━━━━━━━━━━━━━━\n\n"
        for h in history:
            icon = "📱" if h["type"] == "phone" else "🔍"
            name = h["result_name"] or "—"
            time = (h["searched_at"] or "")[:16]
            text += f"{icon} `{h['query']}` » *{name}*\n_  {time}_\n\n"
        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())

    # ── Request Access ──
    elif data == "request_access":
        u = get_user(uid)
        if u and u["status"] == "pending":
            await q.answer("⏳ Request already sent! Wait karo.", show_alert=True)
            return
        set_pending(uid)
        uname = update.effective_user.username or "—"
        try:
            await ctx.bot.send_message(
                OWNER_ID,
                f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃   🔔  *ACCESS REQUEST*   ┃\n"
                f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
                f"👤 *User Details*\n"
                f"┌ Name     » [{update.effective_user.full_name}](tg://user?id={uid})\n"
                f"├ ID       » `{uid}`\n"
                f"├ Username » @{uname}\n"
                f"└ Type     » Username/ID Lookup\n\n"
                f"📌 Kitne uses dene hain? Choose karo:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=approve_kb(uid, uname)
            )
        except Exception as e:
            logger.error(f"Owner notify failed: {e}")

        await q.message.edit_text(
            "📩 *Request Sent!*\n\n"
            "Owner tumhara request review karega.\n"
            "Approve hone pe notification milega. 🔔",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb()
        )

    elif data == "phone_request_access":
        u = get_user(uid)
        if u and u["phone_status"] == "pending":
            await q.answer("⏳ Request already sent! Wait karo.", show_alert=True)
            return
        set_phone_pending(uid)
        uname = update.effective_user.username or "—"
        try:
            await ctx.bot.send_message(
                OWNER_ID,
                f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃  📱  *PHONE ACCESS REQ*  ┃\n"
                f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
                f"👤 *User Details*\n"
                f"┌ Name     » [{update.effective_user.full_name}](tg://user?id={uid})\n"
                f"├ ID       » `{uid}`\n"
                f"├ Username » @{uname}\n"
                f"└ Type     » Phone Lookup\n\n"
                f"📌 Plan choose karo:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=phone_plans_kb(uid)
            )
        except Exception as e:
            logger.error(f"Owner notify failed: {e}")

        await q.message.edit_text(
            "📩 *Phone Access Request Sent!*\n\n"
            "Owner plan approve karega.\n"
            "Notification aayega jab approve ho. 🔔",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb()
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
            f"✅ *Approved!*\nUser `{target_id}` → *{limit} lookups* granted.",
            parse_mode=ParseMode.MARKDOWN
        )
        try:
            await ctx.bot.send_message(
                target_id,
                f"🎉 *Access Approved!*\n\n"
                f"Owner ne tumhe *{limit} Username/ID lookups* approve kiye!\n\n"
                f"Ab search karo 👇",
                parse_mode=ParseMode.MARKDOWN,
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
        await q.message.edit_text(f"❌ *Rejected!* User `{target_id}`", parse_mode=ParseMode.MARKDOWN)
        try:
            await ctx.bot.send_message(target_id, "❌ *Request Rejected*\n\nBaad me dobara try karo.", parse_mode=ParseMode.MARKDOWN)
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
            f"✅ *Phone Approved!*\nUser `{target_id}` → *{limit} phone lookups* granted.",
            parse_mode=ParseMode.MARKDOWN
        )
        try:
            await ctx.bot.send_message(
                target_id,
                f"🎉 *Phone Access Approved!*\n\n"
                f"Owner ne tumhe *{limit} Phone lookups* approve kiye!\n\n"
                f"Ab search karo 👇",
                parse_mode=ParseMode.MARKDOWN,
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
        await q.message.edit_text(f"❌ *Rejected!* User `{target_id}`", parse_mode=ParseMode.MARKDOWN)
        try:
            await ctx.bot.send_message(target_id, "❌ *Phone Request Rejected*\n\nBaad me dobara try karo.", parse_mode=ParseMode.MARKDOWN)
        except:
            pass

    # ── Owner Panel ──
    elif data == "owner_panel":
        if uid != OWNER_ID:
            await q.answer("❌ Access denied!", show_alert=True)
            return
        users = get_all_users()
        pending_u = [u for u in users if u["status"] == "pending"]
        pending_p = [u for u in users if u["phone_status"] == "pending"]
        await q.message.edit_text(
            f"┌━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃    👑  *OWNER PANEL*     ┃\n"
            f"└━━━━━━━━━━━━━━━━━━━━━━━━━┘\n\n"
            f"📊 *Overview*\n"
            f"┌ Total Users      » {len(users)}\n"
            f"├ Pending Username » {len(pending_u)}\n"
            f"└ Pending Phone    » {len(pending_p)}\n\n"
            f"Action chuno:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=owner_panel_kb()
        )

    elif data == "owner_stats":
        if uid != OWNER_ID:
            return
        users = get_all_users()
        total_lu = sum(u["total_lookups"] for u in users)
        total_ph = sum(u["total_phone_lookups"] for u in users)
        approved = [u for u in users if u["status"] == "approved"]
        papproved = [u for u in users if u["phone_status"] == "approved"]
        await q.message.edit_text(
            f"📊 *Bot Statistics*\n━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Total Users         » {len(users)}\n"
            f"✅ Approved (Username) » {len(approved)}\n"
            f"✅ Approved (Phone)    » {len(papproved)}\n\n"
            f"🔍 Total Username Lookups » {total_lu}\n"
            f"📱 Total Phone Lookups    » {total_ph}\n"
            f"📈 Total Searches         » {total_lu + total_ph}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=owner_panel_kb()
        )

    elif data == "owner_users":
        if uid != OWNER_ID:
            return
        users = get_all_users()
        text = f"👥 *All Users ({len(users)})*\n━━━━━━━━━━━━━━━━━━\n\n"
        for u in users[:20]:
            si = {"free": "🆓", "pending": "⏳", "approved": "✅", "exhausted": "🚫"}.get(u["status"], "❓")
            pi = {"free": "🆓", "pending": "⏳", "approved": "✅"}.get(u["phone_status"], "❓")
            rem  = max(0, FREE_USES - u["free_used"]) + max(0, u["approved_limit"] - u["approved_used"])
            prem = max(0, PHONE_FREE - u["phone_free_used"]) + max(0, u["phone_approved_limit"] - u["phone_approved_used"])
            text += f"{si}{pi} `{u['user_id']}` *{u['full_name']}*\n    🔍{rem} 📱{prem} | 🕐{(u['last_seen'] or '')[:10]}\n\n"
        if len(users) > 20:
            text += f"_...aur {len(users)-20} users_"
        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=owner_panel_kb())

    elif data == "owner_broadcast":
        if uid != OWNER_ID:
            return
        ctx.user_data["waiting"] = "broadcast"
        await q.message.edit_text(
            "📢 *Broadcast Message*\n\nSabhi users ko bhejni wali message type karo:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb()
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
