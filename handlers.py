import logging
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
from config import (
    ADS_REQUIRED,
    BACKUP_CHANNEL,
    BASE_URL,
    LINK_BASE_URL,
    OWNER_ID,
    PRIMARY_CHANNEL,
    VERIFY_REQUIRED,
)
from delivery import send_file

log = logging.getLogger("handlers")

owner_only = filters.User(user_id=OWNER_ID) & filters.ChatType.PRIVATE


# ===================== SECONDARY (delivery) BOTS =====================
async def deliver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not context.args:
        await msg.reply_text("Permanent link kholo, file mil jayegi.")
        return
    if chat.type != "private":
        await msg.reply_text("Ye link bot ke private chat me kholo.")
        return

    token = context.args[0]
    doc = await db.get_file(token)
    if not doc:
        await msg.reply_text("❌ Link invalid hai.")
        return

    manager = context.application.bot_data["manager"]

    if not VERIFY_REQUIRED:
        if not await send_file(context.bot, chat.id, doc, manager):
            await msg.reply_text("😕 File abhi available nahi hai. Thodi der baad try karo.")
        return

    sid = secrets.token_urlsafe(12)
    await db.create_session(
        sid=sid,
        token=token,
        user_id=update.effective_user.id,
        chat_id=chat.id,
        bot_id=context.application.bot_data["bot_id"],
        required=ADS_REQUIRED,
    )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("▶️ Verify & Get Video", web_app=WebAppInfo(url=f"{BASE_URL}/app?sid={sid}"))]]
    )
    await msg.reply_text(
        "🔐 Video pane ke liye neeche button dabao aur ek chhota ad dekho.\n"
        "Ad ke baad video yahin chat me aa jayega.",
        reply_markup=kb,
    )


def register_delivery_handlers(app: Application):
    app.add_handler(CommandHandler("start", deliver_start))


# ===================== MAIN (manager) BOT =====================
HELP = (
    "🛠 *Main bot*\n\n"
    "File bhejo → upload + backup + permanent link\n\n"
    "/addbot <token>\n"
    "/removebot @username\n"
    "/bots\n"
    "/disable @username\n"
    "/enable @username"
)


async def owner_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP, parse_mode="Markdown")


async def addbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    try:
        await msg.delete()
    except TelegramError:
        pass

    if not context.args:
        await context.bot.send_message(msg.chat_id, "Use: /addbot <token>")
        return

    manager = context.application.bot_data["manager"]
    result = await manager.add_bot(context.args[0].strip())
    await context.bot.send_message(msg.chat_id, result)


async def _find(update, context):
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Username do: /cmd @username")
        return None
    b = await db.get_bot_by_username(context.args[0])
    if not b:
        await msg.reply_text("❌ Ye bot registry me nahi hai.")
    return b


async def removebot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = await _find(update, context)
    if not b:
        return
    manager = context.application.bot_data["manager"]
    await manager.release_bot(b)
    await db.delete_bot(b["bot_id"])
    await update.effective_message.reply_text(f"🗑 @{b['username']} remove ho gaya.")


async def disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = await _find(update, context)
    if not b:
        return
    manager = context.application.bot_data["manager"]
    await db.set_status(b["bot_id"], "disabled")
    await manager.release_bot(b)
    await update.effective_message.reply_text(f"⚪ @{b['username']} disabled.")


async def enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = await _find(update, context)
    if not b:
        return
    manager = context.application.bot_data["manager"]
    await db.set_status(b["bot_id"], "pending")
    await manager.check_all()
    b = await db.get_bot_by_username(b["username"])
    await update.effective_message.reply_text(
        f"@{b['username']} → {b['status']}\n(Worker 1-2 min me webhook live kar dega.)"
    )


async def bots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = await db.list_bots()
    if not items:
        await update.effective_message.reply_text("Koi bot nahi. /addbot <token> use karo.")
        return
    icons = {"active": "🟢", "pending": "🟡", "dead": "🔴", "disabled": "⚪"}
    lines = [f"{icons.get(b['status'], '❔')} @{b['username']} — {b['status']}" for b in items]
    await update.effective_message.reply_text("\n".join(lines))


async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    token = secrets.token_urlsafe(6)
    original_caption = (msg.caption or "")[:900]
    channel_caption = f"{original_caption}\n\nID: {token}".strip()

    try:
        a = await context.bot.copy_message(
            chat_id=PRIMARY_CHANNEL,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
            caption=channel_caption,
        )
    except TelegramError as e:
        await msg.reply_text(f"❌ Primary channel me upload fail: {e}\nBot ko channel me admin banao.")
        return

    locations = [{"channel": PRIMARY_CHANNEL, "msg": a.message_id}]
    warn = ""
    try:
        b = await context.bot.copy_message(
            chat_id=BACKUP_CHANNEL,
            from_chat_id=PRIMARY_CHANNEL,
            message_id=a.message_id,
        )
        locations.append({"channel": BACKUP_CHANNEL, "msg": b.message_id})
    except TelegramError as e:
        warn = f"\n⚠️ Backup copy fail: {e}"

    await db.save_file(token, locations, original_caption)
    await msg.reply_text(f"✅ Saved\n{LINK_BASE_URL}/f/{token}{warn}")


def register_main_handlers(app: Application):
    app.add_handler(CommandHandler("start", owner_start, filters=owner_only))
    app.add_handler(CommandHandler("addbot", addbot, filters=owner_only))
    app.add_handler(CommandHandler("removebot", removebot, filters=owner_only))
    app.add_handler(CommandHandler("disable", disable, filters=owner_only))
    app.add_handler(CommandHandler("enable", enable, filters=owner_only))
    app.add_handler(CommandHandler("bots", bots_cmd, filters=owner_only))
    app.add_handler(
        MessageHandler(owner_only & filters.ATTACHMENT & ~filters.COMMAND, upload)
    )
