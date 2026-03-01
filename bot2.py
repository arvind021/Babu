import os, logging, sqlite3, aiohttp
from dotenv import load_dotenv
load_dotenv()
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode

# ══════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY   = os.getenv("API_KEY")
OWNER_ID  = int(os.getenv("OWNER_ID"))
API_BASE  = "https://pan-seven-eta.vercel.app/"
FREE_USES = 2

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════
def db():
    con = sqlite3.connect("bot.db")
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      INTEGER PRIMARY KEY,
            username     TEXT,
            full_name    TEXT,
            free_used    INTEGER DEFAULT 0,
            approved_limit INTEGER DEFAULT 0,
            approved_used  INTEGER DEFAULT 0,
            status       TEXT DEFAULT 'free',
            joined_at    TEXT DEFAULT (datetime('now'))
        );
        """)

def upsert_user(u):
    with db() as con:
        con.execute("""
            INSERT INTO users (user_id, username, full_name)
            VALUES (?,?,?)
            ON CONFLICT(user_id) DO UPDATE
            SET username=excluded.username, full_name=excluded.full_name
        """, (u.id, u.username or "", u.full_name))

def get_user(user_id):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

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

def get_all_users():
    with db() as con:
        return con.execute("SELECT * FROM users ORDER BY joined_at DESC").fetchall()


# ══════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════
async def fetch_info(query):
    params = {"key": API_KEY, "q": query}
    async with aiohttp.ClientSession() as s:
        async with s.get(API_BASE, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return await r.json()

def bool_icon(v): return "✅" if v else "❌"

def format_result(d):
    ph = d.get("phone_info", {})
    phone = ""
    if ph and ph.get("success") and ph.get("number"):
        phone = (
            f"\n📞 *Phone*\n"
            f"┌ Number : `{ph['number']}`\n"
            f"├ Country: {ph.get('country','?')}\n"
            f"└ Code   : `{ph.get('country_code','?')}`\n"
        )
    return (
        f"╔══ 🔍 *LOOKUP RESULT* ══╗\n\n"
        f"👤 *Profile*\n"
        f"┌ Name    : *{d.get('full_name') or '—'}*\n"
        f"├ Username: `{d.get('username') or '—'}`\n"
        f"├ User ID : `{d.get('user_id','—')}`\n"
        f"├ Status  : {d.get('status','—')}\n"
        f"├ DC ID   : {d.get('dc_id','—')}\n"
        f"└ Bio     : _{d.get('bio') or 'No bio'}_\n\n"
        f"🏷 *Flags*\n"
        f"┌ Bot     : {bool_icon(d.get('is_bot',False))}\n"
        f"├ Verified: {bool_icon(d.get('is_verified',False))}\n"
        f"├ Premium : {bool_icon(d.get('is_premium',False))}\n"
        f"├ Scam    : {bool_icon(d.get('is_scam',False))}\n"
        f"└ Fake    : {bool_icon(d.get('is_fake',False))}\n"
        f"{phone}"
        f"\n⏱ _{d.get('response_time','—')}_"
    )


# ══════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════
def main_menu_kb(user_id=None):
    kb = [
        [
            InlineKeyboardButton("🔍 Username Lookup", callback_data="do_username"),
            InlineKeyboardButton("🆔 User ID Lookup",  callback_data="do_userid"),
        ],
        [InlineKeyboardButton("📊 My Account", callback_data="my_account")],
    ]
    if user_id == OWNER_ID:
        kb.append([InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")])
    return InlineKeyboardMarkup(kb)

def request_access_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Owner se Access Maango", callback_data="request_access")],
        [InlineKeyboardButton("📊 My Account", callback_data="my_account")],
    ])

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

def owner_panel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 All Users",      callback_data="owner_users")],
        [InlineKeyboardButton("📢 Broadcast",      callback_data="owner_broadcast")],
        [InlineKeyboardButton("🏠 Main Menu",      callback_data="main_menu")],
    ])

def approve_kb(user_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 50 Uses",  callback_data=f"approve_{user_id}_50"),
            InlineKeyboardButton("✅ 100 Uses", callback_data=f"approve_{user_id}_100"),
        ],
        [
            InlineKeyboardButton("✅ 200 Uses", callback_data=f"approve_{user_id}_200"),
            InlineKeyboardButton("✅ 500 Uses", callback_data=f"approve_{user_id}_500"),
        ],
        [InlineKeyboardButton("❌ Reject",      callback_data=f"reject_{user_id}")],
    ])


# ══════════════════════════════════════════════════
#  CORE LOOKUP
# ══════════════════════════════════════════════════
async def perform_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str):
    user_id = update.effective_user.id
    upsert_user(update.effective_user)

    allowed, kind = can_use(user_id)

    if not allowed:
        u = get_user(user_id)
        if u and u["status"] == "pending":
            text = (
                "⏳ *Tumhara request pending hai!*\n\n"
                "Owner se approval ka wait karo.\n"
                "Approve hone ke baad notification aayega. 🔔"
            )
            kb = back_kb()
        else:
            text = (
                "🚫 *Free limit khatam ho gayi!*\n\n"
                f"Tumhe `{FREE_USES}` free lookups mile the — sab use ho gaye.\n\n"
                "📩 Owner se access maango — wo tumhe limit set karke approve karega."
            )
            kb = request_access_kb()

        if update.message:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            await update.callback_query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    rem, _ = get_remaining(user_id)
    if update.message:
        msg = await update.message.reply_text("⏳ *Fetching...*", parse_mode=ParseMode.MARKDOWN)
    else:
        msg = await update.callback_query.message.reply_text("⏳ *Fetching...*", parse_mode=ParseMode.MARKDOWN)

    try:
        data = await fetch_info(query)
        if "error" in data or data.get("success") == False:
            err = data.get("message") or data.get("error") or "Unknown error"
            await msg.edit_text(f"❌ *Error:* `{err}`", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
            return

        consume(user_id)
        rem_after, _ = get_remaining(user_id)

        text = format_result(data) + f"\n\n_Remaining uses: {rem_after}_"
        pic  = data.get("profile_pic")
        kb   = result_kb(data.get("username"))

        if pic:
            await msg.delete()
            send = update.message.reply_photo if update.message else update.callback_query.message.reply_photo
            await send(pic, caption=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

        # Warn if last free use
        if rem_after == 0:
            u = get_user(user_id)
            if u and u["status"] == "free":
                warn_kb = request_access_kb()
                if update.message:
                    await update.message.reply_text(
                        "⚠️ *Ye tumhara aakhri free use tha!*\n\nAb access ke liye owner se approve karwao.",
                        parse_mode=ParseMode.MARKDOWN, reply_markup=warn_kb
                    )
                else:
                    await update.callback_query.message.reply_text(
                        "⚠️ *Ye tumhara aakhri free use tha!*\n\nAb access ke liye owner se approve karwao.",
                        parse_mode=ParseMode.MARKDOWN, reply_markup=warn_kb
                    )

    except aiohttp.ClientError:
        await msg.edit_text("❌ Network error. Baad me try karo.", reply_markup=back_kb())
    except Exception as e:
        logger.error(e)
        await msg.edit_text(f"❌ Error: `{e}`", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())


# ══════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    uid = update.effective_user.id
    rem, _ = get_remaining(uid)
    await update.message.reply_text(
        f"👋 *Welcome, {update.effective_user.first_name}!*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🤖 TG Lookup Bot — Telegram user info fetch karo!\n\n"
        f"🆓 *Free lookups: `{rem}`*\n\n"
        f"👇 Option chuno:",
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

    # Owner: broadcast
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
        await msg.edit_text(f"✅ Done! Sent: {sent} | Failed: {failed}")
        return

    # Owner: custom approve limit
    if waiting == "custom_approve":
        target_id = ctx.user_data.pop("approve_target", None)
        ctx.user_data.pop("waiting", None)
        if text.isdigit() and target_id:
            limit = int(text)
            approve_user(target_id, limit)
            await update.message.reply_text(f"✅ User `{target_id}` ko {limit} uses approve kiya!", parse_mode=ParseMode.MARKDOWN)
            try:
                await ctx.bot.send_message(
                    target_id,
                    f"🎉 *Access Approved!*\n\nOwner ne tumhe *{limit} lookups* approve kiye hain!\n\nAb `/start` karo aur use karo.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass
        else:
            await update.message.reply_text("❌ Sirf number daalo.", reply_markup=back_kb())
        return

    # Search input
    if waiting in ("username", "userid"):
        ctx.user_data.pop("waiting", None)
        await perform_lookup(update, ctx, text)
        return

    # Auto-detect
    if text.startswith("@") or text.lstrip("-").isdigit():
        await perform_lookup(update, ctx, text)
    else:
        await update.message.reply_text(
            "🤔 `@username` ya User ID bhejo, ya menu use karo:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(update.effective_user.id)
        )

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid  = update.effective_user.id

    # ── Main Menu ──
    if data == "main_menu":
        ctx.user_data.clear()
        rem, _ = get_remaining(uid)
        await q.message.edit_text(
            f"🤖 *TG Lookup Bot*\n━━━━━━━━━━━━━━━━━━\n\n"
            f"🆓 Remaining: `{rem}` lookups\n\n👇 Option chuno:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(uid)
        )

    # ── Search ──
    elif data == "do_username":
        ctx.user_data["waiting"] = "username"
        await q.message.edit_text(
            "🔍 *Username Lookup*\n\n`@username` type karke bhejo:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb()
        )

    elif data == "do_userid":
        ctx.user_data["waiting"] = "userid"
        await q.message.edit_text(
            "🆔 *User ID Lookup*\n\nNumeric User ID type karke bhejo:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb()
        )

    elif data == "search_again":
        ctx.user_data["waiting"] = "username"
        await q.message.reply_text(
            "🔍 `@username` ya ID bhejo:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb()
        )

    # ── Request Access ──
    elif data == "request_access":
        u = get_user(uid)
        if u and u["status"] == "pending":
            await q.answer("⏳ Pehle se request bheja hua hai! Wait karo.", show_alert=True)
            return

        set_pending(uid)

        # Notify owner
        try:
            await ctx.bot.send_message(
                OWNER_ID,
                f"🔔 *Access Request!*\n\n"
                f"User: [{update.effective_user.full_name}](tg://user?id={uid})\n"
                f"ID: `{uid}`\n"
                f"Username: @{update.effective_user.username or '—'}\n\n"
                f"Kitne uses dene hain?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=approve_kb(uid)
            )
        except Exception as e:
            logger.error(f"Owner notify failed: {e}")

        await q.message.edit_text(
            "📩 *Request bhej diya!*\n\n"
            "Owner approve karega aur tumhe notification aayega. ⏳\n\n"
            "_Thoda wait karo..._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb()
        )

    # ── My Account ──
    elif data == "my_account":
        u = get_user(uid)
        rem, kind = get_remaining(uid)
        free_left = max(0, FREE_USES - (u["free_used"] if u else 0))

        if kind == "approved":
            plan_text = f"✅ Approved | {u['approved_limit']-u['approved_used']}/{u['approved_limit']} left"
        elif kind == "pending":
            plan_text = "⏳ Approval pending..."
        else:
            plan_text = "❌ Not approved"

        await q.message.edit_text(
            f"📊 *My Account*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 {update.effective_user.full_name}\n"
            f"🆔 `{uid}`\n\n"
            f"🆓 Free uses left : `{free_left}/{FREE_USES}`\n"
            f"💼 Access status  : {plan_text}\n"
            f"✅ Total remaining: `{rem}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📩 Request More Access", callback_data="request_access")],
                [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")],
            ])
        )

    # ── Owner Panel ──
    elif data == "owner_panel":
        if uid != OWNER_ID:
            await q.answer("❌ Access denied!", show_alert=True)
            return
        users = get_all_users()
        pending = [u for u in users if u["status"] == "pending"]
        await q.message.edit_text(
            f"👑 *Owner Panel*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👥 Total Users    : {len(users)}\n"
            f"⏳ Pending Requests: {len(pending)}\n\n"
            f"Action chuno:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=owner_panel_kb()
        )

    elif data == "owner_users":
        if uid != OWNER_ID:
            return
        users = get_all_users()
        text = f"👥 *All Users ({len(users)})*\n━━━━━━━━━━━━━━━━━━\n\n"
        for u in users[:25]:
            status_icon = {"free":"🆓","pending":"⏳","approved":"✅","exhausted":"🚫"}.get(u["status"],"❓")
            rem = max(0, FREE_USES - u["free_used"]) + max(0, u["approved_limit"] - u["approved_used"])
            text += f"{status_icon} `{u['user_id']}` — {u['full_name']} | Rem: {rem}\n"
        if len(users) > 25:
            text += f"\n_...aur {len(users)-25} users_"

        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=owner_panel_kb())

    elif data == "owner_broadcast":
        if uid != OWNER_ID:
            return
        ctx.user_data["waiting"] = "broadcast"
        await q.message.edit_text(
            "📢 *Broadcast*\n\nJo message bhejni hai wo type karo:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb()
        )

    # ── Approve / Reject ──
    elif data.startswith("approve_"):
        if uid != OWNER_ID:
            await q.answer("❌ Access denied!", show_alert=True)
            return
        parts = data.split("_")
        target_id = int(parts[1])
        limit = int(parts[2])

        approve_user(target_id, limit)
        await q.message.edit_text(
            f"✅ *Approved!*\nUser `{target_id}` ko *{limit} uses* diye gaye.",
            parse_mode=ParseMode.MARKDOWN
        )
        try:
            await ctx.bot.send_message(
                target_id,
                f"🎉 *Access Approved!*\n\n"
                f"Owner ne tumhe *{limit} lookups* approve kiye hain!\n\n"
                f"Ab lookup karo 👇",
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
            await ctx.bot.send_message(
                target_id,
                "❌ *Request Rejected*\n\nOwner ne abhi approve nahi kiya.\nBaad me dobara try karo.",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass


# ══════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("lookup", lookup_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, smart_message))
    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
