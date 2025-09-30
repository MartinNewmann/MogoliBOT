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

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = "chromos.db"

RESET_UTC_TIME = time(hour=0, minute=0, tzinfo=timezone.utc)
RECENT_DAYS_WINDOW = 7
DAILY_START_BALANCE = 75
ALERT_THRESHOLD = 21

IMMUNE_USERS = set()
OWNER_ID = int(os.getenv("OWNER_ID", "5285094498"))

MENTION_RE = re.compile(r"@([A-Za-z0-9_]{5,})")

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

        CREATE TABLE IF NOT EXISTS daily_stats (
          chat_id   INTEGER NOT NULL,
          user_id   INTEGER NOT NULL,
          day       DATE    NOT NULL,
          given     INTEGER NOT NULL DEFAULT 0,
          received  INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (chat_id, user_id, day),
          FOREIGN KEY (chat_id, user_id) REFERENCES users(chat_id, user_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS daily_selection (
          chat_id INTEGER NOT NULL,
          day     DATE    NOT NULL,
          user_id INTEGER NOT NULL,
          PRIMARY KEY (chat_id, day, user_id),
          FOREIGN KEY (chat_id, user_id) REFERENCES users(chat_id, user_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS immune_id (
          chat_id INTEGER NOT NULL,
          user_id INTEGER NOT NULL,
          PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS immune_username (
          chat_id  INTEGER NOT NULL,
          username TEXT COLLATE NOCASE NOT NULL,
          PRIMARY KEY (chat_id, username)
        );
        """)
    print("DB OK")

def now_utc():
    return datetime.now(timezone.utc)

def today_key():
    return now_utc().date()

def upsert_user(chat_id: int, user_id: int, username: str | None):
    with db() as conn:
        conn.execute("""
            INSERT INTO users (chat_id, user_id, username, last_seen, balance)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id)
            DO UPDATE SET username=excluded.username, last_seen=excluded.last_seen
        """, (chat_id, user_id, username, now_utc(), DAILY_START_BALANCE))

def seen_user(chat_id: int, user_id: int, username: str | None):
    upsert_user(chat_id, user_id, username)

def is_user_immune(chat_id: int, user_id: int | None, username: str | None) -> bool:
    uname_lc = (username or "").lower()
    if uname_lc in IMMUNE_USERS:
        return True
    with db() as conn:
        if user_id and conn.execute(
            "SELECT 1 FROM immune_id WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone():
            return True
        if uname_lc and conn.execute(
            "SELECT 1 FROM immune_username WHERE chat_id=? AND username=?",
            (chat_id, uname_lc)
        ).fetchone():
            return True
    return False

def add_immune(chat_id: int, user_id: int | None, username: str | None) -> bool:
    with db() as conn:
        try:
            if user_id:
                conn.execute("INSERT OR IGNORE INTO immune_id (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id))
            if username:
                conn.execute("INSERT OR IGNORE INTO immune_username (chat_id, username) VALUES (?, ?)", (chat_id, username.lower()))
            return True
        except Exception:
            return False

def remove_immune(chat_id: int, user_id: int | None, username: str | None) -> int:
    uname_lc = (username or "").lower()
    with db() as conn:
        total = 0
        if user_id:
            total += conn.execute("DELETE FROM immune_id WHERE chat_id=? AND user_id=?", (chat_id, user_id)).rowcount
        if uname_lc:
            total += conn.execute("DELETE FROM immune_username WHERE chat_id=? AND username=?", (chat_id, uname_lc)).rowcount
        return total

def list_immunes(chat_id: int):
    with db() as conn:
        rows_id = conn.execute("SELECT user_id FROM immune_id WHERE chat_id=?", (chat_id,)).fetchall()
        rows_un = conn.execute("SELECT username FROM immune_username WHERE chat_id=?", (chat_id,)).fetchall()
    return [(r[0], "") for r in rows_id] + [(0, r[0]) for r in rows_un]

def get_recent_users(chat_id: int):
    cutoff = now_utc() - timedelta(days=RECENT_DAYS_WINDOW)
    with db() as conn:
        rows = conn.execute("""
            SELECT user_id, COALESCE(username, '')
            FROM users
            WHERE chat_id=? AND last_seen >= ?
        """, (chat_id, cutoff.isoformat())).fetchall()
    return [(uid, uname) for uid, uname in rows if not is_user_immune(chat_id, uid, uname)]

def ensure_stats_row(chat_id: int, user_id: int, day):
    with db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO daily_stats (chat_id, user_id, day)
            VALUES (?, ?, ?)
        """, (chat_id, user_id, str(day)))

def adjust_balance(chat_id: int, user_id: int, delta: int):
    with db() as conn:
        row = conn.execute("SELECT balance FROM users WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
        if not row:
            return False, 0
        bal = row[0]
        new_bal = bal + delta
        if new_bal < 0:
            return False, bal
        conn.execute("UPDATE users SET balance=? WHERE chat_id=? AND user_id=?", (new_bal, chat_id, user_id))
        return True, new_bal

def add_given_received(chat_id: int, giver_id: int, recipient_id: int, amount: int, day):
    with db() as conn:
        conn.execute("""INSERT OR IGNORE INTO daily_stats (chat_id, user_id, day, given, received) VALUES (?, ?, ?, 0, 0)""",
                     (chat_id, giver_id, str(day)))
        conn.execute("""INSERT OR IGNORE INTO daily_stats (chat_id, user_id, day, given, received) VALUES (?, ?, ?, 0, 0)""",
                     (chat_id, recipient_id, str(day)))
        conn.execute("""UPDATE daily_stats SET given = given + ? WHERE chat_id=? AND user_id=? AND day=?""",
                     (amount, chat_id, giver_id, str(day)))
        conn.execute("""UPDATE daily_stats SET received = received + ? WHERE chat_id=? AND user_id=? AND day=?""",
                     (amount, chat_id, recipient_id, str(day)))

def get_received_today(chat_id: int, user_id: int, day):
    with db() as conn:
        row = conn.execute("""SELECT received FROM daily_stats WHERE chat_id=? AND user_id=? AND day=?""",
                           (chat_id, user_id, str(day))).fetchone()
    return row[0] if row else 0

def mark_selection_today(chat_id: int, user_id: int, day):
    with db() as conn:
        conn.execute("""INSERT OR IGNORE INTO daily_selection (chat_id, day, user_id) VALUES (?, ?, ?)""",
                     (chat_id, str(day), user_id))

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
    return f"@{uname}" if uname else f'<a href="tg://user?id={uid}">usuario</a>'

def resolve_target_from_update(update: Update, text: str):
    chat_id = update.effective_chat.id
    if update.message and update.message.reply_to_message:
        u = update.message.reply_to_message.from_user
        seen_user(chat_id, u.id, u.username)
        return u.id, (u.username or "")
    m = MENTION_RE.search(text or "")
    if m:
        uname = m.group(1)
        with db() as conn:
            row = conn.execute("""
                SELECT user_id, COALESCE(username,'') FROM users
                 WHERE chat_id=? AND LOWER(username)=LOWER(?)
            """, (chat_id, uname)).fetchone()
        if row:
            return row[0], row[1]
    nums = re.findall(r"\d{6,}", text or "")
    if nums:
        uid = int(nums[-1])
        with db() as conn:
            row = conn.execute("""
                SELECT user_id, COALESCE(username,'') FROM users
                 WHERE chat_id=? AND user_id=?
            """, (chat_id, uid)).fetchone()
        if row:
            return row[0], row[1]
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot activo.\n"
        "Comandos:\n"
        "• /down — Elige el mogólico del día (excluye inmunes)\n"
        "• /regalar — /regalar @usuario 10 | responder con /regalar 10 | /regalar <user_id> 10\n"
        "• /check — Lista del día\n"
        "• /randomdown — (reply / @ / id)\n"
        "• /esdaun <texto|@usuario>\n"
        "• /chatid — muestra el ID del chat\n"
        "Privado (owner): /immune_add /immune_remove /immune_list"
    )

async def comandos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/down — Elige el mogólico del día (excluye inmunes)\n"
        "/regalar — /regalar @usuario 10 | responder con /regalar 10 | /regalar <user_id> 10\n"
        "/check — Lista del día\n"
        "/randomdown — (reply / @ / id)\n"
        "/esdaun <texto|@usuario>\n"
        "/chatid — muestra el ID del chat\n"
        "Privado: /immune_add /immune_remove /immune_list"
    )

async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    title = getattr(chat, "title", "") or "(sin título)"
    await update.message.reply_text(f"chat_id: {chat.id}\nTítulo: {title}")

async def seen_member(update: Update, _: ContextTypes.DEFAULT_TYPE):
    chat = update.chat_member.chat
    user = update.chat_member.from_user
    seen_user(chat.id, user.id, user.username)

async def any_group_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.message.reply_text(f"El mogólico del día es {mention}", parse_mode=ParseMode.HTML)
    mark_selection_today(chat.id, uid, today_key())

async def regalar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    sender = update.effective_user
    text = update.message.text or ""
    seen_user(chat.id, sender.id, sender.username)

    target = resolve_target_from_update(update, text)
    nums = re.findall(r"\d+", text)
    amount = int(nums[-1]) if nums else None
    if not target or amount is None or amount <= 0:
        await update.message.reply_text("Uso: /regalar @usuario 10  • o •  responder con /regalar 10  • o •  /regalar <user_id> 10")
        return
    dest_id, dest_uname = target
    if dest_id == sender.id:
        await update.message.reply_text("No podés regalarte a vos mismo.")
        return

    ok, new_bal = adjust_balance(chat.id, sender.id, -amount)
    if not ok:
        with db() as conn:
            rowb = conn.execute("SELECT balance FROM users WHERE chat_id=? AND user_id=?", (chat.id, sender.id)).fetchone()
        bal = rowb[0] if rowb else 0
        await update.message.reply_text(f"No te alcanza el saldo. Te quedan {bal} cromosomas.")
        return

    day = today_key()
    ensure_stats_row(chat.id, sender.id, day)
    ensure_stats_row(chat.id, dest_id, day)
    add_given_received(chat.id, sender.id, dest_id, amount, day)

    dest_m = format_mention(dest_id, dest_uname or "")
    await update.message.reply_text(f"Listo: regalaste {amount} cromosomas a {dest_m}. Te quedan {new_bal}.", parse_mode=ParseMode.HTML)

    total_rec = get_received_today(chat.id, dest_id, day)
    if total_rec >= ALERT_THRESHOLD:
        if is_user_immune(chat.id, dest_id, dest_uname or ""):
            candidates = [(uid, uun) for (uid, uun) in get_recent_users(chat.id) if uid != dest_id]
            if not candidates:
                await update.message.reply_text("El destinatario es inmune, pero no encuentro otro usuario activo para rebotar los cromosomas.")
                return
            alt_id, alt_uname = random.choice(candidates)
            ensure_stats_row(chat.id, alt_id, day)
            with db() as conn:
                conn.execute("""
                    UPDATE daily_stats
                       SET received = CASE WHEN received >= ? THEN received - ? ELSE 0 END
                     WHERE chat_id=? AND user_id=? AND day=?
                """, (amount, amount, chat.id, dest_id, str(day)))
                conn.execute("""
                    UPDATE daily_stats
                       SET received = received + ?
                     WHERE chat_id=? AND user_id=? AND day=?
                """, (amount, chat.id, alt_id, str(day)))
            alt_total = get_received_today(chat.id, alt_id, day)
            alt_m = format_mention(alt_id, alt_uname)
            await update.message.reply_text(f"Como {dest_m} es inmune, los cromosomas le rebotan y caen en {alt_m}.", parse_mode=ParseMode.HTML)
            if alt_total >= ALERT_THRESHOLD:
                await update.message.reply_text(f"¡{alt_m} es mogólico! (≥ {ALERT_THRESHOLD})", parse_mode=ParseMode.HTML)
                mark_selection_today(chat.id, alt_id, day)
        else:
            await update.message.reply_text(f"¡{dest_m} es mogólico!  (≥ {ALERT_THRESHOLD})!", parse_mode=ParseMode.HTML)
            mark_selection_today(chat.id, dest_id, day)

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    day = today_key()
    recibieron, seleccionados = list_today_highlights(chat.id, day)

    lines = []
    if recibieron:
        lines.append("<b>Recibieron &gt; 21 hoy:</b>")
        for uid, uname, rec in recibieron:
            lines.append(f"• {format_mention(uid, uname)} — recibió {rec}")
    if seleccionados:
        lines.append("")
        lines.append("<b>Mogólico del día:</b>")
        seen_set = set()
        for uid, uname in seleccionados:
            if uid in seen_set:
                continue
            seen_set.add(uid)
            lines.append(f"• {format_mention(uid, uname)}")

    if not lines:
        await update.message.reply_text("Hoy no hay destacados aún.")
        return
    await update.message.reply_text("📋 <b>Lista del día</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)

async def randomdown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    text = update.message.text or ""
    target = resolve_target_from_update(update, text)
    if not target:
        await update.message.reply_text("Uso: /randomdown @usuario  • o •  responder con /randomdown  • o •  /randomdown <user_id>")
        return
    target_id, target_uname = target
    mention = format_mention(target_id, target_uname or "")
    if random.choice([0, 1]) == 0:
        await update.message.reply_text(f"{mention} está re mogólico hoy 🔥", parse_mode=ParseMode.HTML)
        mark_selection_today(chat.id, target_id, today_key())
    else:
        await update.message.reply_text(f"a {mention} no le agarró el daun todavía 😌", parse_mode=ParseMode.HTML)

async def esdaun(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    args = text.split(maxsplit=1)
    target_text = None
    if len(args) > 1:
        target_text = args[1].strip()
    elif update.message.reply_to_message:
        u = update.message.reply_to_message.from_user
        target_text = f"@{u.username}" if u.username else f"(usuario {u.id})"
    if not target_text:
        await update.message.reply_text("Uso: /esdaun <texto o @usuario> (o respondé a un mensaje)")
        return
    opciones = [f"Hoy {target_text} está re daun", f"Por ahora a {target_text} no se le activó el daun"]
    await update.message.reply_text(random.choice(opciones))

async def _only_private(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.type == "private"

def _is_owner(user_id: int) -> bool:
    return OWNER_ID == 0 or user_id == OWNER_ID

async def immune_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _only_private(update): return
    if not _is_owner(update.effective_user.id):
        await update.message.reply_text("No autorizado.")
        return
    text = update.message.text or ""
    m = MENTION_RE.search(text)
    target_user_id = None
    target_username = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        u = update.message.reply_to_message.from_user
        target_user_id, target_username = u.id, u.username
    elif m:
        target_username = m.group(1)
    chat_id = None
    for tok in text.split()[1:]:
        if tok.isdigit() or (tok.startswith("-") and tok[1:].isdigit()):
            chat_id = int(tok); break
    if chat_id is None or not (target_user_id or target_username):
        await update.message.reply_text("Uso: /immune_add @usuario <chat_id>  • o •  en reply: /immune_add <chat_id>")
        return
    ok = add_immune(chat_id, target_user_id, target_username)
    if ok:
        who = f"@{target_username}" if target_username else f"id={target_user_id}"
        await update.message.reply_text(f"Agregado como inmune en chat {chat_id}: {who}")
    else:
        await update.message.reply_text("No se pudo agregar.")

async def immune_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _only_private(update): return
    if not _is_owner(update.effective_user.id):
        await update.message.reply_text("No autorizado.")
        return
    text = update.message.text or ""
    m = MENTION_RE.search(text)
    target_user_id = None
    target_username = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        u = update.message.reply_to_message.from_user
        target_user_id, target_username = u.id, u.username
    elif m:
        target_username = m.group(1)
    chat_id = None
    for tok in text.split()[1:]:
        if tok.isdigit() or (tok.startswith("-") and tok[1:].isdigit()):
            chat_id = int(tok); break
    if chat_id is None or not (target_user_id or target_username):
        await update.message.reply_text("Uso: /immune_remove @usuario <chat_id>  • o •  en reply: /immune_remove <chat_id>")
        return
    removed = remove_immune(chat_id, target_user_id, target_username)
    if removed:
        who = f"@{(target_username or '')}" if target_username else f"id={target_user_id}"
        await update.message.reply_text(f"Quitado de inmunes en chat {chat_id}: {who}")
    else:
        await update.message.reply_text("No había registro para ese usuario.")

async def immune_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _only_private(update): return
    if not _is_owner(update.effective_user.id):
        await update.message.reply_text("No autorizado.")
        return
    args = (update.message.text or "").split()
    if len(args) < 2:
        await update.message.reply_text("Uso: /immune_list <chat_id>")
        return
    try:
        chat_id = int(args[1])
    except ValueError:
        await update.message.reply_text("El chat_id debe ser numérico.")
        return
    rows = list_immunes(chat_id)
    if not rows:
        await update.message.reply_text("No hay inmunes en ese chat.")
        return
    lines = []
    for uid, uname in rows:
        if uname: who = f"@{uname}"
        elif uid: who = f"id={uid}"
        else: who = "(desconocido)"
        lines.append(f"• {who}")
    await update.message.reply_text("Inmunes:\n" + "\n".join(lines))

def do_daily_reset(context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        conn.execute("UPDATE users SET balance=?", (DAILY_START_BALANCE,))

def main():
    init_db()
    if not BOT_TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en .env")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.job_queue.run_daily(do_daily_reset, time=RESET_UTC_TIME, name="daily_reset")
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("comandos", comandos))
    app.add_handler(CommandHandler("down", down))
    app.add_handler(CommandHandler("regalar", regalar))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("randomdown", randomdown))
    app.add_handler(CommandHandler("esdaun", esdaun))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("immune_add", immune_add))
    app.add_handler(CommandHandler("immune_remove", immune_remove))
    app.add_handler(CommandHandler("immune_list", immune_list))
    app.add_handler(ChatMemberHandler(seen_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.ALL, any_group_msg))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
