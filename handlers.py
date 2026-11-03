import asyncio
import html
import logging
import random
import secrets

from telegram import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
    CHANNEL_URL,
    LINK_BASE_URL,
    OWNER_ID,
    PRIMARY_CHANNEL,
    SUPPORT_URL,
    VERIFY_REQUIRED,
)
from delivery import send_file

log = logging.getLogger("handlers")
HTML = ParseMode.HTML

owner_only = filters.User(user_id=OWNER_ID) & filters.ChatType.PRIVATE

# Reactions the bot is allowed to use on messages
REACTIONS = ["🔥", "⚡", "🎉", "🤩", "💯", "🏆", "😎", "👀"]

_tasks: set = set()


def bg(coro):
    """Fire-and-forget so replies never wait on side effects."""
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def react(msg, emoji: str | None = None):
    try:
        await msg.set_reaction(emoji or random.choice(REACTIONS))
    except TelegramError:
        pass


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


async def edit(message, text: str, kb=None):
    try:
        await message.edit_text(text, parse_mode=HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("edit failed: %s", e)
    except TelegramError as e:
        log.warning("edit failed: %s", e)


# ===================== SECONDARY (delivery) BOTS =====================
def home_text(user) -> str:
    name = html.escape((user.first_name if user else None) or "friend")
    return (
        f"🎉🔥 <b>Welcome, {name}!</b> 🔥🎉\n\n"
        "📦 Tap any <b>permanent file link</b> and I'll deliver your file right here ⚡️🚀\n"
        "💸 100% free • 🛡 Safe • 🏎 Super fast\n\n"
        "👇✨ Pick an option below ✨👇"
    )


HOW_TEXT = (
    "📖✨ <b>How it works</b> ✨📖\n\n"
    "1️⃣ Open a permanent link 🔗\n"
    "2️⃣ Tap <b>Verify & Get Video</b> 🔐\n"
    "3️⃣ Watch one short ad 📺\n"
    "4️⃣ Your file lands in this chat 🎁🚀💎"
)
FAQ_TEXT = (
    "❓🤔 <b>FAQ</b> 🤔❓\n\n"
    "💸 <b>Is it free?</b> Yes, totally! 🎉\n"
    "📺 <b>Why an ad?</b> It keeps this service running 🛠\n"
    "😕 <b>Ad not showing?</b> Turn off AdBlock / Private DNS 🛡 and retry 🔄\n"
    "⏳ <b>File didn't arrive?</b> Wait a few seconds, then tap your link again ⚡️"
)
HELP_TEXT = (
    "🆘🛟 <b>Need help?</b> 🛟🆘\n\n"
    "1️⃣ Open your file link again 🔗\n"
    "2️⃣ Tap <b>Verify & Get Video</b> and finish the ad 📺\n"
    "3️⃣ Still stuck? Reach out below 👇💬"
)
WHY_ALERT = "📺 One short ad keeps this service free for everyone!\n🎁 After it, your file drops straight into this chat ⚡"


def kb_home() -> InlineKeyboardMarkup:
    rows = [[btn("📖 How it works", "how"), btn("❓ FAQ", "faq")], [btn("🆘 Help", "help")]]
    extra = []
    if CHANNEL_URL:
        extra.append(InlineKeyboardButton("📢 Updates", url=CHANNEL_URL))
    if SUPPORT_URL:
        extra.append(InlineKeyboardButton("💬 Support", url=SUPPORT_URL))
    if extra:
        rows.append(extra)
    return InlineKeyboardMarkup(rows)


def kb_back(*more: InlineKeyboardButton) -> InlineKeyboardMarkup:
    rows = []
    if more:
        rows.append(list(more))
    rows.append([btn("🏠 Home", "home")])
    return InlineKeyboardMarkup(rows)


def kb_help() -> InlineKeyboardMarkup:
    more = [InlineKeyboardButton("💬 Contact support", url=SUPPORT_URL)] if SUPPORT_URL else []
    return kb_back(*more)


async def deliver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    bg(react(msg))

    if not context.args:
        await msg.reply_text(home_text(user), parse_mode=HTML, reply_markup=kb_home())
        return

    token = context.args[0]
    if chat.type != "private":
        link = f"https://t.me/{context.bot.username}?start={token}"
        await msg.reply_text(
            "🔒💬 Please open this link in a <b>private chat</b> with me 👇",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Open private chat", url=link)]]),
        )
        return

    status = await msg.reply_text("🔎⚡ <b>Checking your link…</b>", parse_mode=HTML)

    doc = await db.get_file(token)
    if not doc:
        await edit(
            status,
            "❌😵 <b>Invalid or expired link</b>\n\nDouble-check the link and try again 🔗",
            kb_back(btn("🆘 Help", "help")),
        )
        return

    manager = context.application.bot_data["manager"]
    retry = InlineKeyboardButton("🔄 Try again", url=f"https://t.me/{context.bot.username}?start={token}")

    if not VERIFY_REQUIRED:
        await edit(status, "🎁🚀 <b>Sending your file…</b>")
        ok = await send_file(context.bot, chat.id, doc, manager)
        if ok:
            await edit(status, "✅🎉 <b>Delivered!</b> Enjoy 🍿🔥", kb_back())
        else:
            await edit(
                status,
                "😕💤 <b>File temporarily unavailable</b>\n\nPlease try again in a few seconds ⏳",
                kb_back(retry),
            )
        return

    sid = secrets.token_urlsafe(12)
    await db.create_session(
        sid=sid,
        token=token,
        user_id=user.id,
        chat_id=chat.id,
        bot_id=context.application.bot_data["bot_id"],
        required=ADS_REQUIRED,
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Verify & Get Video 🎬", web_app=WebAppInfo(url=f"{BASE_URL}/app?sid={sid}"))],
            [btn("❓ Why an ad?", "why"), btn("🆘 Help", "help")],
        ]
    )
    await edit(
        status,
        "🔐🎬 <b>Almost there!</b> 🎬🔐\n\n"
        "📺 Tap the button and watch one short ad ⚡️\n"
        "🎁 Your file drops right into this chat 🚀💎",
        kb,
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    if data == "why":
        await q.answer(WHY_ALERT, show_alert=True)
        return
    await q.answer()  # instant feedback, no spinner

    screens = {
        "home": (home_text(q.from_user), kb_home()),
        "how": (HOW_TEXT, kb_back(btn("❓ FAQ", "faq"))),
        "faq": (FAQ_TEXT, kb_back(btn("📖 How it works", "how"))),
        "help": (HELP_TEXT, kb_help()),
    }
    screen = screens.get(data)
    if not screen:
        return
    text, kb = screen
    m = q.message
    try:
        if getattr(m, "text", None):  # text message → edit in place (smooth)
            await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb)
        else:  # e.g. buttons under a delivered video → send a fresh message
            await m.reply_text(text, parse_mode=HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("callback edit failed: %s", e)
    except TelegramError as e:
        log.warning("callback failed: %s", e)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bg(react(update.effective_message, "🤔"))
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=HTML, reply_markup=kb_help())


async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    bg(react(msg, "👀"))
    await msg.reply_text(
        "🤔💭 I only understand <b>file links</b>!\n\nTap a permanent link to get your file 🔗🎁",
        parse_mode=HTML,
        reply_markup=kb_home(),
    )


def register_delivery_handlers(app: Application):
    app.add_handler(CommandHandler("start", deliver_start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(home|how|faq|help|why)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, fallback_text))


# ===================== MAIN (manager) BOT =====================
MENU_TEXT = (
    "🛠⚡ <b>Control Center</b> ⚡🛠\n\n"
    "📤 Send me any file → instant upload + backup + permanent link 🔗🔥\n\n"
    "🤖 /addbot &lt;token&gt;\n"
    "🗑 /removebot @username\n"
    "📋 /bots\n"
    "⚪ /disable @username\n"
    "🟢 /enable @username"
)
ADD_TEXT = (
    "➕🤖 <b>Add a bot</b>\n\n"
    "1️⃣ Create a bot in @BotFather 🧪\n"
    "2️⃣ Send <code>/addbot YOUR_TOKEN</code> 🔑\n"
    "3️⃣ Make it admin in both channels 👑\n\n"
    "🔒 I delete your token message right away."
)
UPLOAD_TEXT = (
    "📤💾 <b>Upload a file</b>\n\n"
    "Just send any video, photo or document 🎬📸📄\n"
    "I save it to both channels and reply with a permanent link 🔗🔥"
)


def kb_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("🤖 My Bots", "m_bots"), btn("➕ Add Bot", "m_add")],
            [btn("📤 How to upload", "m_up"), btn("🔄 Refresh", "m_home")],
        ]
    )


def kb_after(*more: InlineKeyboardButton) -> InlineKeyboardMarkup:
    rows = [list(more)] if more else []
    rows.append([btn("🤖 My Bots", "m_bots"), btn("🏠 Menu", "m_home")])
    return InlineKeyboardMarkup(rows)


async def bots_text() -> str:
    items = await db.list_bots()
    if not items:
        return "🤖💤 <b>No bots yet</b>\n\nAdd one with <code>/addbot TOKEN</code> ➕"
    icons = {"active": "🟢", "pending": "🟡", "dead": "🔴", "disabled": "⚪"}
    lines = [f"{icons.get(b['status'], '❔')} @{html.escape(b['username'])} — {b['status']}" for b in items]
    return "🤖📋 <b>Your bots</b>\n\n" + "\n".join(lines)


async def owner_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bg(react(update.effective_message, "🔥"))
    await update.effective_message.reply_text(MENU_TEXT, parse_mode=HTML, reply_markup=kb_menu())


async def on_owner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("🚫 Owner only", show_alert=True)
        return
    await q.answer()
    data = q.data
    if data == "m_bots":
        text, kb = await bots_text(), InlineKeyboardMarkup(
            [[btn("🔄 Refresh", "m_bots"), btn("➕ Add Bot", "m_add")], [btn("🏠 Menu", "m_home")]]
        )
    elif data == "m_add":
        text, kb = ADD_TEXT, InlineKeyboardMarkup([[btn("🤖 My Bots", "m_bots"), btn("🏠 Menu", "m_home")]])
    elif data == "m_up":
        text, kb = UPLOAD_TEXT, InlineKeyboardMarkup([[btn("🏠 Menu", "m_home")]])
    else:
        text, kb = MENU_TEXT, kb_menu()
    try:
        await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("owner callback failed: %s", e)
    except TelegramError as e:
        log.warning("owner callback failed: %s", e)


async def addbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    try:
        await msg.delete()
    except TelegramError:
        pass

    if not context.args:
        await context.bot.send_message(msg.chat_id, "ℹ️ Use: /addbot &lt;token&gt;", parse_mode=HTML, reply_markup=kb_after())
        return

    status = await context.bot.send_message(msg.chat_id, "⏳🔑 <b>Checking token…</b>", parse_mode=HTML)
    manager = context.application.bot_data["manager"]
    result = await manager.add_bot(context.args[0].strip())
    await edit(status, html.escape(result), kb_after())


async def _find(update, context):
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("ℹ️ Send a username: /cmd @username", reply_markup=kb_after())
        return None
    b = await db.get_bot_by_username(context.args[0])
    if not b:
        await msg.reply_text("❌🔍 That bot isn't in the registry.", reply_markup=kb_after())
    return b


async def removebot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = await _find(update, context)
    if not b:
        return
    manager = context.application.bot_data["manager"]
    await manager.release_bot(b)
    await db.delete_bot(b["bot_id"])
    bg(react(update.effective_message, "🔥"))
    await update.effective_message.reply_text(f"🗑✅ @{b['username']} removed.", reply_markup=kb_after())


async def disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = await _find(update, context)
    if not b:
        return
    manager = context.application.bot_data["manager"]
    await db.set_status(b["bot_id"], "disabled")
    await manager.release_bot(b)
    await update.effective_message.reply_text(f"⚪😴 @{b['username']} disabled.", reply_markup=kb_after())


async def enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = await _find(update, context)
    if not b:
        return
    manager = context.application.bot_data["manager"]
    await db.set_status(b["bot_id"], "pending")
    await manager.check_all()
    b = await db.get_bot_by_username(b["username"])
    await update.effective_message.reply_text(
        f"🟢⚡ @{b['username']} → {b['status']}\n(The worker will bring the webhook live in 1-2 min.)",
        reply_markup=kb_after(),
    )


async def bots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bg(react(update.effective_message, "👀"))
    await update.effective_message.reply_text(
        await bots_text(),
        parse_mode=HTML,
        reply_markup=InlineKeyboardMarkup(
            [[btn("🔄 Refresh", "m_bots"), btn("➕ Add Bot", "m_add")], [btn("🏠 Menu", "m_home")]]
        ),
    )


async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    status = await msg.reply_text("⏳💾 <b>Uploading & backing up…</b>", parse_mode=HTML)

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
        bg(react(msg, "😢"))
        await edit(
            status,
            f"❌😢 <b>Primary channel upload failed</b>\n<code>{html.escape(str(e))}</code>\n\n👑 Make the bot an admin of the channel.",
            kb_after(),
        )
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
        warn = f"\n\n⚠️ Backup copy failed: <code>{html.escape(str(e))}</code>"

    await db.save_file(token, locations, original_caption)
    link = f"{LINK_BASE_URL}/f/{token}"
    bg(react(msg, "🔥"))
    await edit(
        status,
        f"✅🎉 <b>Saved & backed up!</b> 🔥💾\n\n🔗 <code>{html.escape(link)}</code>{warn}",
        kb_after(
            InlineKeyboardButton("🔗 Open", url=link),
            InlineKeyboardButton("📋 Copy", copy_text=CopyTextButton(text=link)),
        ),
    )


async def owner_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bg(react(update.effective_message, "🤔"))
    await update.effective_message.reply_text(MENU_TEXT, parse_mode=HTML, reply_markup=kb_menu())


def register_main_handlers(app: Application):
    app.add_handler(CommandHandler("start", owner_start, filters=owner_only))
    app.add_handler(CommandHandler("addbot", addbot, filters=owner_only))
    app.add_handler(CommandHandler("removebot", removebot, filters=owner_only))
    app.add_handler(CommandHandler("disable", disable, filters=owner_only))
    app.add_handler(CommandHandler("enable", enable, filters=owner_only))
    app.add_handler(CommandHandler("bots", bots_cmd, filters=owner_only))
    app.add_handler(CallbackQueryHandler(on_owner_callback, pattern=r"^m_"))
    app.add_handler(MessageHandler(owner_only & filters.ATTACHMENT & ~filters.COMMAND, upload))
    app.add_handler(MessageHandler(owner_only & filters.TEXT & ~filters.COMMAND, owner_text))
