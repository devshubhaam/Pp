import asyncio
import logging
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TelegramError

from config import LINK_BASE_URL

log = logging.getLogger("delivery")


def _after_kb(token: str) -> InlineKeyboardMarkup:
    link = f"{LINK_BASE_URL}/f/{token}"
    share = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote('🔥🎁 Grab this file!', safe='')}"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📤 Share with friends", url=share)],
            [InlineKeyboardButton("🏠 Home", callback_data="home"), InlineKeyboardButton("🆘 Help", callback_data="help")],
        ]
    )


async def send_file(bot, chat_id: int, doc: dict, manager) -> bool:
    caption = doc.get("caption") or ""
    token = doc["token"]
    kb = _after_kb(token)

    for loc in doc["locations"]:
        for _attempt in range(2):
            try:
                await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=loc["channel"],
                    message_id=loc["msg"],
                    caption=caption,
                    reply_markup=kb,
                )
                return True
            except RetryAfter as e:
                await asyncio.sleep(min(e.retry_after, 5))
            except TelegramError as e:
                log.warning("copy failed token=%s channel=%s: %s", token, loc["channel"], e)
                await manager.alert_once(
                    f"{token}:{loc['channel']}",
                    f"⚠️ Delivery failed: token {token}, channel {loc['channel']}\n{e}\n"
                    f"Trying the backup channel.",
                )
                break
    return False
