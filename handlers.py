import asyncio
import logging
import secrets

from telegram import Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
from config import BACKUP_CHANNEL, BASE_URL, OWNER_ID, PRIMARY_CHANNEL

log = logging.getLogger("handlers")

owner_only = filters.User(user_id=OWNER_ID) & filters.ChatType.PRIVATE


# =====================================================================
# SECONDARY (delivery) BOTS
# =====================================================================
async def deliver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Permanent link kholo, file mil jayegi.")
        return

    token = context.args[0]
    doc = await db.get_file(token)
    if not doc:
        await msg.reply_text("❌ Link invalid hai.")
        return

    manager = context.application.bot_data["manager"]
    caption = doc.get("caption") or ""

    for loc in doc["locations"]:
        for _attempt in range(2):
            try:
                await context.bot.copy_message(
                    chat_id=msg.chat_id,
                    from_chat_id=loc["channel"],
                    message_id=loc["msg"],
                    caption=caption,
                )
                return
            except RetryAfter as e:
                await asyncio.sleep(min(e.retry_after, 5))
            except TelegramError as e:
                log.warning("copy failed token=%s channel=%s: %s", token, loc["channel"], e)
                await manager.alert_once(
                    f"{token}:{loc['channel']}",
                    f"⚠️ Delivery fail: token {token}, channel {loc['channel']}\n{e}\nBackup se try kiya ja raha hai.",
                )
                break  # next location (backup)

    await msg.reply_text("😕 File abhi available nahi hai. Thodi der baad try karo.")


def register_delivery_handlers(app: Application):
    app.add_handler(CommandHandler("start", deliver_start))


# =====================================================================
# MAIN (manager) BOT  -- owner only
# =====================================================================
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
    try:  # token chat me na dikhe
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
    await manager.stop_secondary(b["bot_id"], delete_webhook=True)
    await db.delete_bot(b["bot_id"])
    await update.effective_message.reply_text(f"🗑 @{b['username']} remove ho gaya.")


async def disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = await _find(update, context)
    if not b:
        return
    manager = context.application.bot_data["manager"]
    await db.set_status(b["bot_id"], "disabled")
    await manager.stop_secondary(b["bot_id"], delete_webhook=True)
    await update.effective_message.reply_text(f"⚪ @{b['username']} disabled.")


async def enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = await _find(update, context)
    if not b:
        return
    manager = context.application.bot_data["manager"]
    await db.set_status(b["bot_id"], "pending")
    await manager.check_all()
    b = await db.get_bot_by_username(b["username"])
    await update.effective_message.reply_text(f"@{b['username']} → {b['status']}")


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
    token = secrets.token_urlsafe(6)  # ~8 chars
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
    await msg.reply_text(f"✅ Saved\n{BASE_URL}/f/{token}{warn}")


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
