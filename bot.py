import os
import html
import asyncio
import logging
import secrets
import threading
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, HTTPServer

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_IDS = {int(x) for x in os.environ["ADMIN_IDS"].split(",") if x.strip()}
REVEAL_PRICE = int(os.environ.get("REVEAL_PRICE", "50"))  # Stars

pool = None


# =====================================================================
#  MA'LUMOTLAR BAZASI
# =====================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    tg_id BIGINT UNIQUE NOT NULL,
    first_name TEXT,
    username TEXT,
    code TEXT UNIQUE NOT NULL,
    blocked BOOLEAN NOT NULL DEFAULT FALSE,
    active_chat INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chats (
    id SERIAL PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    guest_id INTEGER NOT NULL REFERENCES users(id),
    closed BOOLEAN NOT NULL DEFAULT FALSE,
    revealed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, guest_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    sender_id INTEGER NOT NULL,
    kind TEXT,
    content TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS relay (
    tg_chat BIGINT NOT NULL,
    tg_msg BIGINT NOT NULL,
    chat_id INTEGER NOT NULL,
    PRIMARY KEY (tg_chat, tg_msg)
);
CREATE TABLE IF NOT EXISTS payments (
    charge_id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    stars INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def fetchone(sql, params=()):
    async with pool.connection() as con:
        cur = await con.execute(sql, params)
        try:
            return await cur.fetchone()
        except Exception:
            return None


async def fetchall(sql, params=()):
    async with pool.connection() as con:
        cur = await con.execute(sql, params)
        return await cur.fetchall()


async def execute(sql, params=()):
    async with pool.connection() as con:
        await con.execute(sql, params)


async def upsert_user(tu):
    row = await fetchone("SELECT * FROM users WHERE tg_id=%s", (tu.id,))
    if row:
        if row["first_name"] != tu.first_name or row["username"] != tu.username:
            await execute(
                "UPDATE users SET first_name=%s, username=%s WHERE id=%s",
                (tu.first_name, tu.username, row["id"]),
            )
            row["first_name"], row["username"] = tu.first_name, tu.username
        return row
    for _ in range(5):
        code = secrets.token_urlsafe(6)
        row = await fetchone(
            "INSERT INTO users (tg_id, first_name, username, code) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *",
            (tu.id, tu.first_name, tu.username, code),
        )
        if row:
            return row
        row = await fetchone("SELECT * FROM users WHERE tg_id=%s", (tu.id,))
        if row:
            return row
    raise RuntimeError("Foydalanuvchi yaratib bo'lmadi")


# =====================================================================
#  YORDAMCHI FUNKSIYALAR
# =====================================================================
def esc(s):
    return html.escape(s or "")


def is_admin_tg(tg_id):
    return tg_id in ADMIN_IDS


def kind_of(msg):
    for k in ("text", "photo", "video", "voice", "video_note", "audio",
              "document", "sticker", "animation"):
        if getattr(msg, k, None):
            return k
    return "other"


def link_of(context, user):
    return f"https://t.me/{context.bot.username}?start={user['code']}"


def menu_keyboard(context, user):
    link = link_of(context, user)
    share = (
        "https://t.me/share/url?url=" + quote(link)
        + "&text=" + quote("Menga anonim xabar yozing 👇")
    )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📤 Havolani ulashish", url=share)],
            [
                InlineKeyboardButton("🔄 Yangi havola", callback_data="newlink"),
                InlineKeyboardButton("ℹ️ Qanday ishlaydi", callback_data="help"),
            ],
        ]
    )


def panel_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Statistika", callback_data="stats")],
            [InlineKeyboardButton("🚫 Bloklanganlar", callback_data="blocked")],
            [InlineKeyboardButton("📢 E'lon yuborish", callback_data="broadcast")],
        ]
    )


def owner_keyboard(chat):
    if chat["revealed"]:
        reveal = InlineKeyboardButton("👤 Yuboruvchini ko'rish", callback_data=f"rev:{chat['id']}")
    else:
        reveal = InlineKeyboardButton(
            f"👁 Kim yozdi? ⭐ {REVEAL_PRICE}", callback_data=f"rev:{chat['id']}"
        )
    return InlineKeyboardMarkup(
        [
            [reveal],
            [InlineKeyboardButton("🚫 Bu odamni bloklash", callback_data=f"oblk:{chat['id']}")],
        ]
    )


def admin_msg_keyboard(a_id, b_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"🚫 #{a_id}", callback_data=f"ablk:{a_id}"),
                InlineKeyboardButton(f"🚫 #{b_id}", callback_data=f"ablk:{b_id}"),
            ]
        ]
    )


async def deliver(bot, to_tg, msg, title, keyboard=None):
    """Xabarni chiroyli ko'rinishda yetkazadi. Yuborilgan xabar id'larini qaytaradi."""
    if msg.text and len(msg.text) < 3500:
        sent = await bot.send_message(
            to_tg,
            f"{title}\n\n{esc(msg.text)}",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return [sent.message_id]
    head = await bot.send_message(to_tg, title, parse_mode=ParseMode.HTML)
    cp = await bot.copy_message(
        chat_id=to_tg,
        from_chat_id=msg.chat_id,
        message_id=msg.message_id,
        reply_to_message_id=head.message_id,
        reply_markup=keyboard,
    )
    return [head.message_id, cp.message_id]


HELP_TEXT = (
    "ℹ️ <b>Qanday ishlaydi</b>\n\n"
    "1️⃣ Shaxsiy havolangizni do'stlaringizga yoki kanalingizga joylang.\n"
    "2️⃣ Havolani ochgan odam sizga ismini ko'rsatmasdan yozadi.\n"
    "3️⃣ Xabarga <b>reply</b> qilib javob bering, suhbat davom etadi.\n\n"
    "⭐ Xohlasangiz, Telegram Stars evaziga yuboruvchi kimligini ko'rishingiz mumkin.\n"
    "🚫 Bezovta qilgan odamni bir tugma bilan bloklang.\n\n"
    "⚠️ <b>Qoidalar:</b> suhbatlar xavfsizlik uchun admin tomonidan ko'rib turilishi mumkin. "
    "Haqorat, tahdid va yolg'on gaplar taqiqlanadi; buzganlar bloklanadi."
)


async def show_menu(context, chat_id, user, intro=None):
    text = (
        (intro + "\n\n" if intro else "")
        + f"👋 <b>Salom, {esc(user['first_name'])}!</b>\n\n"
        "Sizning shaxsiy havolangiz:\n"
        f"🔗 {link_of(context, user)}\n\n"
        "Uni ulashing. Odamlar sizga <b>anonim</b> xabar yozishi mumkin, "
        "siz esa ularga javob berasiz."
    )
    await context.bot.send_message(
        chat_id,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=menu_keyboard(context, user),
        disable_web_page_preview=True,
    )


async def send_reveal_info(bot, to_tg, chat_id):
    row = await fetchone(
        "SELECT u.* FROM chats c JOIN users u ON u.id = c.guest_id WHERE c.id=%s",
        (chat_id,),
    )
    if not row:
        return
    uname = f"@{esc(row['username'])}" if row["username"] else "yo'q"
    await bot.send_message(
        to_tg,
        f"👤 <b>Yuboruvchi · Suhbat #{chat_id}</b>\n\n"
        f"Ism: {esc(row['first_name'])}\n"
        f"Username: {uname}\n"
        f"ID: <code>{row['tg_id']}</code>",
        parse_mode=ParseMode.HTML,
    )


# =====================================================================
#  /start VA MENYU
# =====================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await upsert_user(update.effective_user)
    if user["blocked"]:
        return
    if context.args:
        await open_link(update, context, user, context.args[0])
        return
    await show_menu(context, update.effective_chat.id, user)


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await upsert_user(update.effective_user)
    if user["blocked"]:
        return
    await show_menu(context, update.effective_chat.id, user)


async def open_link(update, context, user, code):
    owner = await fetchone(
        "SELECT * FROM users WHERE code=%s AND blocked=FALSE", (code,)
    )
    if not owner:
        await update.message.reply_text("😕 Bu havola ishlamaydi yoki eskirgan.")
        return
    if owner["id"] == user["id"]:
        await show_menu(context, update.effective_chat.id, user,
                        intro="😄 Bu sizning o'z havolangiz.")
        return
    chat = await fetchone(
        "INSERT INTO chats (owner_id, guest_id) VALUES (%s,%s) "
        "ON CONFLICT (owner_id, guest_id) DO UPDATE SET owner_id = EXCLUDED.owner_id "
        "RETURNING *",
        (owner["id"], user["id"]),
    )
    if chat["closed"]:
        await update.message.reply_text("😕 Bu havola orqali yozib bo'lmaydi.")
        return
    await execute("UPDATE users SET active_chat=%s WHERE id=%s", (chat["id"], user["id"]))
    await update.message.reply_text(
        "✍️ <b>Anonim suhbat</b>\n\n"
        "Xabaringizni shu yerga yozing. U havola egasiga ismingiz "
        "ko'rsatilmasdan yetkaziladi. Matn, rasm, ovozli xabar yuborishingiz mumkin.\n\n"
        "⚠️ <b>Bilib qo'ying:</b>\n"
        "• Suhbat xavfsizlik uchun admin tomonidan ko'rilishi mumkin.\n"
        "• Havola egasi pullik xizmat orqali yuboruvchi kimligini ochishi mumkin.\n"
        "• Haqorat va tahdid taqiqlanadi, buzganlar bloklanadi.",
        parse_mode=ParseMode.HTML,
    )


# =====================================================================
#  XABARLARNI YO'NALTIRISH
# =====================================================================
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = await upsert_user(update.effective_user)
    if user["blocked"]:
        return

    if is_admin_tg(user["tg_id"]) and context.user_data.get("broadcast"):
        await do_broadcast(update, context)
        return

    chat = None
    if msg.reply_to_message:
        r = await fetchone(
            "SELECT chat_id FROM relay WHERE tg_chat=%s AND tg_msg=%s",
            (msg.chat_id, msg.reply_to_message.message_id),
        )
        if not r:
            await msg.reply_text(
                "Bu xabarga javob yubora olmayman. Suhbatdoshingizning xabariga reply qiling."
            )
            return
        chat = await fetchone("SELECT * FROM chats WHERE id=%s", (r["chat_id"],))
    elif user["active_chat"]:
        chat = await fetchone("SELECT * FROM chats WHERE id=%s", (user["active_chat"],))

    if not chat or user["id"] not in (chat["owner_id"], chat["guest_id"]):
        await show_menu(
            context, msg.chat_id, user,
            intro="💡 Xabar yuborish uchun birovning havolasini oching. "
                  "Sizga kelgan xabarga javob berish uchun uni <b>reply</b> qiling.",
        )
        return

    is_owner = user["id"] == chat["owner_id"]
    other_id = chat["guest_id"] if is_owner else chat["owner_id"]
    other = await fetchone("SELECT * FROM users WHERE id=%s", (other_id,))

    if chat["closed"]:
        await msg.reply_text("🚫 Bu suhbat yopilgan.")
        return
    if other["blocked"]:
        await msg.reply_text("Xabarni yetkazib bo'lmadi.")
        return

    kind = kind_of(msg)
    content = msg.text or msg.caption or f"[{kind}]"
    await execute(
        "INSERT INTO messages (chat_id, sender_id, kind, content) VALUES (%s,%s,%s,%s)",
        (chat["id"], user["id"], kind, content[:2000]),
    )

    try:
        if is_owner:
            ids = await deliver(
                context.bot, other["tg_id"], msg,
                f"💬 <b>Havola egasidan javob</b>",
            )
        else:
            ids = await deliver(
                context.bot, other["tg_id"], msg,
                f"💌 <b>Yangi anonim xabar</b> · <i>Suhbat #{chat['id']}</i>",
                owner_keyboard(chat),
            )
    except Exception as e:
        logging.warning("Yetkazib bo'lmadi: %s", e)
        await msg.reply_text("Xabarni yetkazib bo'lmadi (foydalanuvchi botni to'xtatgan bo'lishi mumkin).")
        return

    for mid in ids:
        await execute(
            "INSERT INTO relay (tg_chat, tg_msg, chat_id) VALUES (%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (other["tg_id"], mid, chat["id"]),
        )

    # Admin nazorati
    sender_label = f"Egasi #{user['id']}" if is_owner else f"Anonim #{user['id']}"
    receiver_label = f"Anonim #{other['id']}" if is_owner else f"Egasi #{other['id']}"
    for admin in ADMIN_IDS:
        if admin in (user["tg_id"], other["tg_id"]):
            continue
        try:
            await deliver(
                context.bot, admin, msg,
                f"👁 <b>Nazorat</b> · Suhbat #{chat['id']}\n"
                f"{sender_label} → {receiver_label}",
                admin_msg_keyboard(user["id"], other["id"]),
            )
        except Exception as e:
            logging.warning("Adminga yuborib bo'lmadi: %s", e)

    try:
        await msg.set_reaction("👍")
    except Exception:
        await msg.reply_text("✅ Yuborildi")


# =====================================================================
#  TELEGRAM STARS TO'LOVI
# =====================================================================
async def send_invoice(context, user, chat_id):
    await context.bot.send_invoice(
        chat_id=user["tg_id"],
        title="Yuboruvchini ko'rish",
        description=f"Suhbat #{chat_id} da yozayotgan odamning ismi va username'ini ko'rish.",
        payload=f"rev:{chat_id}",
        currency="XTR",
        prices=[LabeledPrice("Yuboruvchi kimligi", REVEAL_PRICE)],
        provider_token="",
    )


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    ok = False
    try:
        kind, cid = q.invoice_payload.split(":")
        user = await fetchone("SELECT * FROM users WHERE tg_id=%s", (q.from_user.id,))
        chat = await fetchone("SELECT * FROM chats WHERE id=%s", (int(cid),))
        ok = bool(kind == "rev" and user and chat
                  and chat["owner_id"] == user["id"] and not user["blocked"])
    except Exception:
        ok = False
    if ok:
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="To'lovni amalga oshirib bo'lmadi.")


async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = update.message.successful_payment
    user = await upsert_user(update.effective_user)
    try:
        _, cid = p.invoice_payload.split(":")
        cid = int(cid)
    except Exception:
        return
    await fetchone(
        "INSERT INTO payments (charge_id, owner_id, chat_id, stars) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT DO NOTHING RETURNING charge_id",
        (p.telegram_payment_charge_id, user["id"], cid, p.total_amount),
    )
    await execute("UPDATE chats SET revealed=TRUE WHERE id=%s", (cid,))
    await update.message.reply_text("✅ To'lov qabul qilindi!")
    await send_reveal_info(context.bot, user["tg_id"], cid)
    for admin in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin,
                f"⭐ +{p.total_amount} Stars · Egasi #{user['id']} · Suhbat #{cid}",
            )
        except Exception:
            pass


# =====================================================================
#  TUGMALAR
# =====================================================================
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    tg_id = q.from_user.id
    chat_id = q.message.chat_id
    user = await upsert_user(q.from_user)

    if user["blocked"]:
        await q.answer()
        return

    # ---- Oddiy foydalanuvchi tugmalari ----
    if data == "help":
        await q.answer()
        await context.bot.send_message(chat_id, HELP_TEXT, parse_mode=ParseMode.HTML)
        return

    if data == "newlink":
        await q.answer()
        new_code = secrets.token_urlsafe(6)
        await execute("UPDATE users SET code=%s WHERE id=%s", (new_code, user["id"]))
        user["code"] = new_code
        await show_menu(context, chat_id, user,
                        intro="🔄 Yangi havola tayyor. Eskisi endi ishlamaydi.")
        return

    if data.startswith("rev:"):
        cid = int(data.split(":")[1])
        chat = await fetchone("SELECT * FROM chats WHERE id=%s", (cid,))
        if not chat or chat["owner_id"] != user["id"]:
            await q.answer("Ruxsat yo'q", show_alert=True)
            return
        await q.answer()
        if chat["revealed"]:
            await send_reveal_info(context.bot, tg_id, cid)
        else:
            await send_invoice(context, user, cid)
        return

    if data.startswith("oblk:"):
        cid = int(data.split(":")[1])
        chat = await fetchone("SELECT * FROM chats WHERE id=%s", (cid,))
        if not chat or chat["owner_id"] != user["id"]:
            await q.answer("Ruxsat yo'q", show_alert=True)
            return
        await execute("UPDATE chats SET closed=TRUE WHERE id=%s", (cid,))
        await q.answer("Bloklandi")
        await context.bot.send_message(
            chat_id, f"🚫 Suhbat #{cid} yopildi. Bu odam endi sizga yoza olmaydi."
        )
        return

    # ---- Admin tugmalari ----
    if not is_admin_tg(tg_id):
        await q.answer()
        return
    await q.answer()

    if data == "stats":
        r = await fetchone(
            "SELECT (SELECT COUNT(*) FROM users) AS u,"
            " (SELECT COUNT(*) FROM chats) AS c,"
            " (SELECT COUNT(*) FROM messages) AS m,"
            " (SELECT COUNT(*) FROM users WHERE blocked) AS b,"
            " (SELECT COALESCE(SUM(stars),0) FROM payments) AS s,"
            " (SELECT COUNT(*) FROM payments) AS p"
        )
        await context.bot.send_message(
            chat_id,
            "📊 <b>Statistika</b>\n\n"
            f"👥 Foydalanuvchilar: {r['u']}\n"
            f"💬 Suhbatlar: {r['c']}\n"
            f"✉️ Xabarlar: {r['m']}\n"
            f"🚫 Bloklanganlar: {r['b']}\n"
            f"⭐ Stars: {r['s']} ({r['p']} ta to'lov)",
            parse_mode=ParseMode.HTML,
        )

    elif data == "blocked":
        rows = await fetchall("SELECT id FROM users WHERE blocked ORDER BY id")
        if not rows:
            await context.bot.send_message(chat_id, "Bloklanganlar yo'q.")
        else:
            ids = ", ".join(f"#{r['id']}" for r in rows)
            await context.bot.send_message(
                chat_id, f"🚫 Bloklanganlar: {ids}\n\nBlokdan chiqarish: /unblock raqam"
            )

    elif data == "broadcast":
        context.user_data["broadcast"] = True
        await context.bot.send_message(
            chat_id, "📢 E'lon matnini yuboring (rasm ham bo'ladi). Bekor qilish: /cancel"
        )

    elif data.startswith("ablk:"):
        uid = int(data.split(":")[1])
        await execute("UPDATE users SET blocked=TRUE WHERE id=%s", (uid,))
        await context.bot.send_message(chat_id, f"🚫 #{uid} bloklandi. Qaytarish: /unblock {uid}")


# =====================================================================
#  ADMIN BUYRUQLARI
# =====================================================================
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin_tg(update.effective_user.id):
            return
        await func(update, context)
    return wrapper


@admin_only
async def admin_cmd(update, context):
    await update.message.reply_text("🛠 Admin paneli", reply_markup=panel_keyboard())


def _num_arg(context):
    if context.args and context.args[0].lstrip("#").isdigit():
        return int(context.args[0].lstrip("#"))
    return None


@admin_only
async def block_cmd(update, context):
    n = _num_arg(context)
    if n is None:
        await update.message.reply_text("Misol: /block 12")
        return
    await execute("UPDATE users SET blocked=TRUE WHERE id=%s", (n,))
    await update.message.reply_text(f"🚫 #{n} bloklandi.")


@admin_only
async def unblock_cmd(update, context):
    n = _num_arg(context)
    if n is None:
        await update.message.reply_text("Misol: /unblock 12")
        return
    await execute("UPDATE users SET blocked=FALSE WHERE id=%s", (n,))
    await update.message.reply_text(f"✅ #{n} blokdan chiqarildi.")


@admin_only
async def user_cmd(update, context):
    n = _num_arg(context)
    r = await fetchone("SELECT * FROM users WHERE id=%s", (n,)) if n else None
    if not r:
        await update.message.reply_text("Misol: /user 12 (topilmadi)")
        return
    uname = f"@{r['username']}" if r["username"] else "yo'q"
    await update.message.reply_text(
        f"#{r['id']}\nIsm: {r['first_name']}\nUsername: {uname}\n"
        f"ID: {r['tg_id']}\nBlok: {'ha' if r['blocked'] else 'yo`q'}"
    )


@admin_only
async def chat_cmd(update, context):
    n = _num_arg(context)
    if not n:
        await update.message.reply_text("Misol: /chat 5")
        return
    chat = await fetchone("SELECT * FROM chats WHERE id=%s", (n,))
    if not chat:
        await update.message.reply_text("Suhbat topilmadi.")
        return
    rows = await fetchall(
        "SELECT sender_id, content, created_at FROM messages "
        "WHERE chat_id=%s ORDER BY id DESC LIMIT 30",
        (n,),
    )
    lines = [f"Suhbat #{n} · Egasi #{chat['owner_id']} · Mehmon #{chat['guest_id']}\n"]
    for r in reversed(rows):
        who = "Egasi" if r["sender_id"] == chat["owner_id"] else "Anonim"
        lines.append(f"{r['created_at']:%d.%m %H:%M} {who}: {r['content']}")
    await update.message.reply_text("\n".join(lines)[:4000])


@admin_only
async def cancel_cmd(update, context):
    context.user_data["broadcast"] = False
    await update.message.reply_text("Bekor qilindi.")


async def do_broadcast(update, context):
    msg = update.message
    context.user_data["broadcast"] = False
    users = await fetchall("SELECT tg_id FROM users WHERE NOT blocked")
    sent = 0
    for u in users:
        try:
            await context.bot.copy_message(
                chat_id=u["tg_id"], from_chat_id=msg.chat_id, message_id=msg.message_id
            )
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await msg.reply_text(f"📢 {sent} ta foydalanuvchiga yuborildi.")


# =====================================================================
#  ISHGA TUSHIRISH
# =====================================================================
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def serve():
    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


async def post_init(app: Application):
    global pool
    pool = AsyncConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        kwargs={"row_factory": dict_row, "prepare_threshold": None},
        open=False,
    )
    await pool.open()
    await execute(SCHEMA)
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Bosh menyu"),
            BotCommand("menu", "Mening havolam"),
        ]
    )


async def post_shutdown(app: Application):
    if pool:
        await pool.close()


def main():
    threading.Thread(target=serve, daemon=True).start()
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("block", block_cmd))
    app.add_handler(CommandHandler("unblock", unblock_cmd))
    app.add_handler(CommandHandler("user", user_cmd))
    app.add_handler(CommandHandler("chat", chat_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.UpdateType.MESSAGE
            & ~filters.COMMAND
            & ~filters.StatusUpdate.ALL
            & ~filters.SUCCESSFUL_PAYMENT,
            on_message,
        )
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
