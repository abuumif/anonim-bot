import os
import sqlite3
import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ["ADMIN_IDS"].split(",") if x.strip()}
DB_PATH = os.environ.get("DB_PATH", "bot.db")

WELCOME = (
    "Assalomu alaykum! Bu anonim bot.\n"
    "Xabaringizni yozing, u ismingiz ko'rsatilmasdan adminga yetkaziladi."
)


# ---------- Ma'lumotlar bazasi ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE,
            blocked INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS links (
            admin_chat INTEGER,
            admin_msg INTEGER,
            user_id INTEGER,
            PRIMARY KEY (admin_chat, admin_msg)
        );
        CREATE TABLE IF NOT EXISTS log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER
        );
        """
    )
    con.commit()
    con.close()


def get_user(tg_id):
    con = db()
    con.execute("INSERT OR IGNORE INTO users (tg_id) VALUES (?)", (tg_id,))
    con.commit()
    row = con.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    con.close()
    return row


def set_blocked(uid, value):
    con = db()
    con.execute("UPDATE users SET blocked=? WHERE id=?", (value, uid))
    con.commit()
    con.close()


# ---------- Yordamchi ----------
def is_admin(update: Update) -> bool:
    return update.effective_user.id in ADMIN_IDS


def panel_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Statistika", callback_data="stats")],
            [InlineKeyboardButton("🚫 Bloklanganlar", callback_data="blocked")],
            [InlineKeyboardButton("📢 E'lon yuborish", callback_data="broadcast")],
        ]
    )


# ---------- Buyruqlar ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        await update.message.reply_text(
            "Admin paneli. Foydalanuvchi xabariga reply qilsangiz, javob unga boradi.",
            reply_markup=panel_keyboard(),
        )
    else:
        await update.message.reply_text(WELCOME)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text("Admin paneli:", reply_markup=panel_keyboard())


async def unblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Misol: /unblock 12")
        return
    set_blocked(int(context.args[0]), 0)
    await update.message.reply_text(f"Anonim #{context.args[0]} blokdan chiqarildi.")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    context.user_data["broadcast"] = False
    await update.message.reply_text("Bekor qilindi.")


# ---------- Xabarlar ----------
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        await from_admin(update, context)
    else:
        await from_user(update, context)


async def from_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = get_user(update.effective_user.id)
    if user["blocked"]:
        return

    con = db()
    con.execute("INSERT INTO log (user_id) VALUES (?)", (user["id"],))
    con.commit()
    con.close()

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚫 Bloklash", callback_data=f"block:{user['id']}")]]
    )

    for admin in ADMIN_IDS:
        try:
            header = await context.bot.send_message(
                admin, f"📩 Yangi xabar — Anonim #{user['id']}"
            )
            copy = await context.bot.copy_message(
                chat_id=admin,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                reply_to_message_id=header.message_id,
                reply_markup=keyboard,
            )
            con = db()
            for mid in (header.message_id, copy.message_id):
                con.execute(
                    "INSERT OR REPLACE INTO links VALUES (?,?,?)",
                    (admin, mid, user["id"]),
                )
            con.commit()
            con.close()
        except Exception as e:
            logging.warning("Adminga yuborib bo'lmadi: %s", e)

    await msg.reply_text("✅ Xabaringiz yuborildi.")


async def from_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    # E'lon rejimi
    if context.user_data.get("broadcast"):
        context.user_data["broadcast"] = False
        con = db()
        users = con.execute("SELECT tg_id FROM users WHERE blocked=0").fetchall()
        con.close()
        sent = 0
        for u in users:
            try:
                await context.bot.copy_message(
                    chat_id=u["tg_id"],
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                )
                sent += 1
            except Exception:
                pass
            await asyncio.sleep(0.05)
        await msg.reply_text(f"📢 {sent} ta foydalanuvchiga yuborildi.")
        return

    # Javob berish
    if msg.reply_to_message:
        con = db()
        row = con.execute(
            "SELECT users.tg_id FROM links JOIN users ON users.id = links.user_id "
            "WHERE links.admin_chat=? AND links.admin_msg=?",
            (msg.chat_id, msg.reply_to_message.message_id),
        ).fetchone()
        con.close()
        if not row:
            await msg.reply_text("Bu xabarga javob yubora olmayman.")
            return
        try:
            await context.bot.copy_message(
                chat_id=row["tg_id"],
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
            )
            await msg.reply_text("✅ Javob yuborildi.")
        except Exception:
            await msg.reply_text("❌ Yuborib bo'lmadi (foydalanuvchi botni to'xtatgan bo'lishi mumkin).")
        return

    await msg.reply_text("Panel uchun /admin yozing. Javob berish uchun xabarga reply qiling.")


# ---------- Tugmalar ----------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    data = q.data

    if data == "stats":
        con = db()
        users = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        msgs = con.execute("SELECT COUNT(*) c FROM log").fetchone()["c"]
        blocked = con.execute("SELECT COUNT(*) c FROM users WHERE blocked=1").fetchone()["c"]
        con.close()
        await q.message.reply_text(
            f"📊 Statistika\nFoydalanuvchilar: {users}\nXabarlar: {msgs}\nBloklanganlar: {blocked}"
        )

    elif data == "blocked":
        con = db()
        rows = con.execute("SELECT id FROM users WHERE blocked=1").fetchall()
        con.close()
        if not rows:
            await q.message.reply_text("Bloklanganlar yo'q.")
        else:
            ids = ", ".join(f"#{r['id']}" for r in rows)
            await q.message.reply_text(
                f"Bloklanganlar: {ids}\nBlokdan chiqarish: /unblock raqam"
            )

    elif data == "broadcast":
        context.user_data["broadcast"] = True
        await q.message.reply_text(
            "E'lon matnini yuboring (rasm ham bo'ladi). Bekor qilish: /cancel"
        )

    elif data.startswith("block:"):
        uid = int(data.split(":")[1])
        set_blocked(uid, 1)
        await q.message.reply_text(f"🚫 Anonim #{uid} bloklandi.")


# ---------- Hosting uchun kichik veb-server ----------
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def serve():
    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


def main():
    init_db()
    threading.Thread(target=serve, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("unblock", unblock_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_message))
    app.run_polling()


if __name__ == "__main__":
    main()
