import asyncio
import logging

from telegram.error import RetryAfter, TelegramError

log = logging.getLogger("delivery")


async def send_file(bot, chat_id: int, doc: dict, manager) -> bool:
    caption = doc.get("caption") or ""
    token = doc["token"]

    for loc in doc["locations"]:
        for _attempt in range(2):
            try:
                await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=loc["channel"],
                    message_id=loc["msg"],
                    caption=caption,
                )
                return True
            except RetryAfter as e:
                await asyncio.sleep(min(e.retry_after, 5))
            except TelegramError as e:
                log.warning("copy failed token=%s channel=%s: %s", token, loc["channel"], e)
                await manager.alert_once(
                    f"{token}:{loc['channel']}",
                    f"⚠️ Delivery fail: token {token}, channel {loc['channel']}\n{e}\n"
                    f"Backup se try kiya ja raha hai.",
                )
                break
    return False
