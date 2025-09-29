import os
import re
import sqlite3
import random
from datetime import datetime, timedelta, timezone, time
from dotenv import load_dotenv

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
    ChatMemberHandler, filters
)

# ===================== Config =====================

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = "chromos.db"

# 21:00 Argentina = 00:00 UTC (AR es UTC-3 y no usa DST)
RESET_UTC_TIME = time(hour=0, minute=0, tzinfo=timezone.utc)

RECENT_DAYS_WINDOW = 7
DAILY_START_BALANCE = 75      # saldo después de cada reset diario (21hs AR)
ALERT_THRESHOLD = 21          # si un usuario recibe >21 en el día, avisar

# --- Inmunidad y respuestas especiales ---
IMMUNE_USERS = {"luz_nasser"}  # usernames en minúsculas, sin @
SPECIAL_USERS = {
    "luz_nasser": "Ella no, pero vos sí."
}


MENTION_RE = re.compile(r"@([A-Za-z0-9_]{5,})")

# ===================== DB =====================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          chat_id   INTEGER NOT NULL,
          user_id   INTEGER NOT NULL,
          username  TEXT,
          last_seen TIMESTAMP NULL,
          balance   INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (chat_id, user_id)
        );

        -- stats por día (el “día” cambia a las 00:00 UTC = 21hs AR)
        CREATE TABLE IF NOT EXISTS daily_stats (
          chat_id   INTEGER NOT NULL,
          user_id   INTEGER NOT NULL,
          day       DATE    NOT NULL,
          given     INTEGER NOT NULL DEFAULT 0,
          received  INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (chat_id, user_id, day),
          FOREIGN KEY (chat_id, user_id) REFERENCES users(chat_id, user_id) ON DELETE CASCADE
        );

        -- quiénes fueron “Mogólico del día” hoy
        CREATE TABLE IF NOT EXISTS daily_selection (
          chat_id INTEGER NOT NULL,
          day     DATE    NOT NULL,
          user_id INTEGER NOT NULL,
          PRIMARY KEY (chat_id, day, user_id),
          FOREIGN KEY (chat_id, user_id) REFERENCES users(chat_id, user_id) ON DELETE CASCADE
        );
        """)
    print("DB OK")

# ===================== Helpers =====================

def now_utc():
    return datetime.now(timezone.utc)

def today_key():  # clave de día (cambia a las 00:00 UTC)
    return now_utc().date()

def upsert_user(chat_id: int, user_id: int, username: str | None):
    with db() as conn:
        # Si es nuevo, lo creamos con balance DAILY_START_BALANCE
        conn.execute("""
            INSERT INTO users (chat_id, user_id, username, last_seen, balance)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id)
            DO UPDATE SET username=excluded.username, last_seen=excluded.last_seen
        """, (chat_id, user_id, username, now_utc(), DAILY_START_BALANCE))

def seen_user(chat_id: int, user_id: int, username: str | None):
    upsert_user(chat_id, user_id, username)

def get_recent_users(chat_id: int):
    cutoff = now_utc() - timedelta(days=RECENT_DAYS_WINDOW)
    with db() as conn:
        rows = conn.execute("""
            SELECT user_id, COALESCE(username, '')
            FROM users
            WHERE chat_id=? AND last_seen >= ?
        """, (chat_id, cutoff.isoformat())).fetchall()

    # excluir inmunes por username (comparación en minúsculas)
    filtered = []
    for uid, uname in rows:
        if (uname or "").lower() in IMMUNE_USERS:
            continue
        filtered.append((uid, uname))
    return filtered


def ensure_stats_row(chat_id: int, user_id: int, day):
    with db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO daily_stats (chat_id, user_id, day)
            VALUES (?, ?, ?)
        """, (chat_id, user_id, str(day)))

def adjust_balance(chat_id: int, user_id: int, delta: int):
    with db() as conn:
        row = conn.execute("SELECT balance FROM users WHERE chat_id=? AND user_id=?",
                           (chat_id, user_id)).fetchone()
        if not row:
            return False, 0
        bal = row[0]
        new_bal = bal + delta
        if new_bal < 0:
            return False, bal
        conn.execute("UPDATE users SET balance=? WHERE chat_id=? AND user_id=?",
                     (new_bal, chat_id, user_id))
        return True, new_bal

def add_given_received(chat_id: int, giver_id: int, recipient_id: int, amount: int, day):
    with db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO daily_stats (chat_id, user_id, day, given, received)
            VALUES (?, ?, ?, 0, 0)
        """, (chat_id, giver_id, str(day)))
        conn.execute("""
            INSERT OR IGNORE INTO daily_stats (chat_id, user_id, day, given, received)
            VALUES (?, ?, ?, 0, 0)
        """, (chat_id, recipient_id, str(day)))
        conn.execute("""
            UPDATE daily_stats
               SET given = given + ?
             WHERE chat_id=? AND user_id=? AND day=?
        """, (amount, chat_id, giver_id, str(day)))
        conn.execute("""
            UPDATE daily_stats
               SET received = received + ?
             WHERE chat_id=? AND user_id=? AND day=?
        """, (amount, chat_id, recipient_id, str(day)))

def get_received_today(chat_id: int, user_id: int, day):
    with db() as conn:
        row = conn.execute("""
            SELECT received FROM daily_stats
             WHERE chat_id=? AND user_id=? AND day=?
        """, (chat_id, user_id, str(day))).fetchone()
    return row[0] if row else 0

def mark_selection_today(chat_id: int, user_id: int, day):
    with db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO daily_selection (chat_id, day, user_id)
            VALUES (?, ?, ?)
        """, (chat_id, str(day), user_id))

def list_today_highlights(chat_id: int, day):
    with db() as conn:
        rec = conn.execute("""
            SELECT u.user_id, COALESCE(u.username,''), s.received
              FROM daily_stats s
              JOIN users u ON u.chat_id=s.chat_id AND u.user_id=s.user_id
             WHERE s.chat_id=? AND s.day=? AND s.received > ?
             ORDER BY s.received DESC
        """, (chat_id, str(day), ALERT_THRESHOLD)).fetchall()
        sel = conn.execute("""
            SELECT u.user_id, COALESCE(u.username,'')
              FROM daily_selection d
              JOIN users u ON u.chat_id=d.chat_id AND u.user_id=d.user_id
             WHERE d.chat_id=? AND d.day=?
        """, (chat_id, str(day))).fetchall()
    return rec, sel

def format_mention(uid: int, uname: str):
    return f"@{uname}" if uname else f"[usuario](tg://user?id={uid})"

# ===================== Handlers =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot activo.\n"
        "Comandos:\n"
        "• /down — Elige el mogólico del día\n"
        "• /regalar @usuario cantidad — Regalar cromosomas (de tu saldo)\n"
        "• /check — Mogolicos del día (>21 recibidos + random del día)\n"
        "• /randomdown @usuario — chequea si el usuario es mogólico"
    )

async def comandos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/down - Elige el mogólico del día\n"
        "/regalar @usuario cantidad — Regalar cromosomas (de tu saldo)\n"
        "/check - Mogolicos del día (>21 recibidos + random del día)\n"
        "/randomdown @usuario — chequea si el usuario es mogólico"
    )

async def seen_member(update: Update, _: ContextTypes.DEFAULT_TYPE):
    chat = update.chat_member.chat
    user = update.chat_member.from_user
    seen_user(chat.id, user.id, user.username)

async def any_group_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # solo para marcar actividad
    if update.effective_chat and update.effective_user:
        seen_user(update.effective_chat.id, update.effective_user.id, update.effective_user.username)

async def down(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    sender = update.effective_user
    seen_user(chat.id, sender.id, sender.username)

    candidates = get_recent_users(chat.id)
    if not candidates:
        await update.message.reply_text("No encuentro usuarios activos en la última semana.")
        return

    uid, uname = random.choice(candidates)
    mention = format_mention(uid, uname)
    await update.message.reply_text(
        f"El mogólico del día es {mention}",
        parse_mode=ParseMode.MARKDOWN
    )
    mark_selection_today(chat.id, uid, today_key())

async def regalar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    sender = update.effective_user
    text = update.message.text or ""
    seen_user(chat.id, sender.id, sender.username)

    m = MENTION_RE.search(text)
    nums = re.findall(r"\d+", text)
    if not (m and nums):
        await update.message.reply_text("Uso: /regalar @usuario cantidad (ej: /regalar @pepito 10)")
        return

    uname = m.group(1)
    amount = int(nums[-1])
    if amount <= 0:
        await update.message.reply_text("La cantidad debe ser mayor a 0.")
        return

    # buscar destinatario por username
    with db() as conn:
        row = conn.execute("""
            SELECT user_id, COALESCE(username,'') FROM users
             WHERE chat_id=? AND username=?
        """, (chat.id, uname)).fetchone()
    if not row:
        await update.message.reply_text("No encuentro a ese usuario (todavía no lo vi activo en este grupo).")
        return
    dest_id, dest_uname = row

    if dest_id == sender.id:
        await update.message.reply_text("No podés regalarte a vos mismo.")
        return

    # descontar del que regala
    ok, new_bal = adjust_balance(chat.id, sender.id, -amount)
    if not ok:
        with db() as conn:
            rowb = conn.execute("SELECT balance FROM users WHERE chat_id=? AND user_id=?",
                                (chat.id, sender.id)).fetchone()
        bal = rowb[0] if rowb else 0
        await update.message.reply_text(f"No te alcanza el saldo. Te quedan {bal} cromosomas.")
        return

    # acreditar contadores del día
    day = today_key()
    ensure_stats_row(chat.id, sender.id, day)
    ensure_stats_row(chat.id, dest_id, day)
    add_given_received(chat.id, sender.id, dest_id, amount, day)

    dest_mention = format_mention(dest_id, dest_uname or uname)
    await update.message.reply_text(
        f"Listo: regalaste {amount} cromosomas a {dest_mention}. Te quedan {new_bal}.",
        parse_mode=ParseMode.MARKDOWN
    )

    # aviso si total recibido del día alcanza o supera el umbral (>= 21)
    total_rec = get_received_today(chat.id, dest_id, day)
    if total_rec >= ALERT_THRESHOLD:
        await update.message.reply_text(
            f"¡{dest_mention} recibió {total_rec} cromosomas (≥ {ALERT_THRESHOLD})!",
            parse_mode=ParseMode.MARKDOWN
        )
        # además pasa a "personaje del día"
        mark_selection_today(chat.id, dest_id, day)



async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    day = today_key()
    recibieron, seleccionados = list_today_highlights(chat.id, day)

    lines = []
    if recibieron:
        lines.append("*Recibieron > 21 hoy:*")
        for uid, uname, rec in recibieron:
            lines.append(f"• {format_mention(uid, uname)} — recibió {rec}")
    if seleccionados:
        lines.append("\n*Mogólico del día:*")
        # evitar duplicados si hay varios /down en el mismo día
        seen = set()
        for uid, uname in seleccionados:
            if uid in seen: 
                continue
            seen.add(uid)
            lines.append(f"• {format_mention(uid, uname)}")

    if not lines:
        await update.message.reply_text("Hoy no hay destacados aún.")
        return

    await update.message.reply_text("📋 *Lista del día*\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def randomdown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    text = update.message.text or ""

    m = MENTION_RE.search(text)
    if not m:
        await update.message.reply_text("Uso: /randomdown @usuario")
        return

    uname = m.group(1)
    # verificar que lo conozcamos en este chat
    with db() as conn:
        row = conn.execute("""
            SELECT user_id FROM users WHERE chat_id=? AND username=?
        """, (chat.id, uname)).fetchone()
    if not row:
        await update.message.reply_text("No encuentro a ese usuario (todavía no lo vi activo en este grupo).")
        return

    mention = f"@{uname}"
    respuestas = [
        f"{mention} está re mogólico hoy 🔥",
        f"a {mention} no le agarró el daun todavía 😌",
   ]

# elegimos al azar
eleccion = random.choice([0, 1])  # 0 = ON, 1 = a salvo

if eleccion == 0:
    # mensaje ON
    await update.message.reply_text(respuestas[0])
    # marcar como "mogólico del día"
    mark_selection_today(chat.id, row[0], today_key())
else:
    # mensaje a salvo
    await update.message.reply_text(respuestas[1])

# ===================== Reset diario =====================

def do_daily_reset(context: ContextTypes.DEFAULT_TYPE):
    # A las 00:00 UTC (21:00 AR): setear balances a 75
    with db() as conn:
        conn.execute("UPDATE users SET balance=?", (DAILY_START_BALANCE,))
    # No limpiamos tablas diarias: /check mira solo el 'day' actual,
    # así que en el nuevo día los contadores empiezan en 0 naturalmente.

# ===================== Main =====================

def main():
    init_db()
    if not BOT_TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en .env")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # programar reset diario (21:00 AR = 00:00 UTC)
    app.job_queue.run_daily(do_daily_reset, time=RESET_UTC_TIME, name="daily_reset")

    # comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("comandos", comandos))
    app.add_handler(CommandHandler("down", down))
    app.add_handler(CommandHandler("regalar", regalar))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("randomdown", randomdown))

    # tracking de actividad
    app.add_handler(ChatMemberHandler(seen_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.ALL, any_group_msg))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
