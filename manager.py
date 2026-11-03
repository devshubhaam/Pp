import asyncio
import hashlib
import logging
import re
import time

from cryptography.fernet import Fernet
from telegram import Bot
from telegram.error import InvalidToken, TelegramError
from telegram.ext import Application

import db
import handlers
from config import (
    BASE_URL,
    CHANNELS,
    FERNET_KEY,
    HEALTH_INTERVAL,
    MAIN_BOT_TOKEN,
    OWNER_ID,
    WEBHOOK_SECRET,
)

log = logging.getLogger("manager")
fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)
TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")


class BotManager:
    def __init__(self):
        self.apps: dict[str, Application] = {}
        self.main: Application | None = None
        self._cache: tuple[float, list[str]] | None = None
        self._alerted: set[str] = set()
        self._lock = asyncio.Lock()

    # ---------- helpers ----------
    def secret_for(self, bot_id: str) -> str:
        return hashlib.sha256(f"{WEBHOOK_SECRET}:{bot_id}".encode()).hexdigest()

    async def _build(self, token: str, register) -> Application:
        app = (
            Application.builder()
            .token(token)
            .updater(None)
            .concurrent_updates(True)
            .build()
        )
        app.bot_data["manager"] = self
        register(app)
        await app.initialize()
        await app.start()
        return app

    async def notify_owner(self, text: str):
        try:
            if self.main:
                await self.main.bot.send_message(OWNER_ID, text)
        except TelegramError as e:
            log.warning("owner notify failed: %s", e)

    async def alert_once(self, key: str, text: str):
        if key in self._alerted:
            return
        self._alerted.add(key)
        await self.notify_owner(text)

    async def check_admin(self, bot: Bot):
        missing = []
        for ch in CHANNELS:
            try:
                m = await bot.get_chat_member(ch, bot.id)
                if m.status not in ("administrator", "creator"):
                    missing.append(ch)
            except TelegramError:
                missing.append(ch)
        return (not missing), missing

    # ---------- lifecycle ----------
    async def start(self):
        await db.init()
        self.main = await self._build(MAIN_BOT_TOKEN, handlers.register_main_handlers)
        await self.main.bot.set_webhook(
            url=f"{BASE_URL}/webhook/main",
            secret_token=self.secret_for("main"),
            allowed_updates=["message"],
        )
        self.apps["main"] = self.main

        for b in await db.list_bots():
            if b["status"] in ("active", "pending"):
                await self._safe_launch(b)

    async def stop(self):
        for app in list(self.apps.values()):
            try:
                await app.stop()
                await app.shutdown()
            except Exception:  # noqa: BLE001
                pass
        self.apps.clear()

    # ---------- secondary bots ----------
    async def launch_secondary(self, bot_id: str, token: str):
        app = await self._build(token, handlers.register_delivery_handlers)
        await app.bot.set_webhook(
            url=f"{BASE_URL}/webhook/{bot_id}",
            secret_token=self.secret_for(bot_id),
            allowed_updates=["message"],
        )
        self.apps[bot_id] = app

    async def stop_secondary(self, bot_id: str, delete_webhook: bool = False):
        app = self.apps.pop(bot_id, None)
        self._cache = None
        if not app:
            return
        if delete_webhook:
            try:
                await app.bot.delete_webhook()
            except TelegramError:
                pass
        try:
            await app.stop()
            await app.shutdown()
        except Exception:  # noqa: BLE001
            pass

    async def _safe_launch(self, b: dict):
        try:
            await self.launch_secondary(b["bot_id"], fernet.decrypt(b["token_enc"].encode()).decode())
        except InvalidToken:
            await self._mark_dead(b)
        except Exception as e:  # noqa: BLE001
            log.warning("launch failed @%s: %s", b.get("username"), e)

    async def _mark_dead(self, b: dict):
        await db.set_status(b["bot_id"], "dead")
        await self.stop_secondary(b["bot_id"])
        left = len(await db.list_bots("active"))
        await self.notify_owner(
            f"🔴 @{b['username']} dead (suspend/delete/token revoked).\n"
            f"Active bots bache: {left}\n"
            f"Naya bot add karo: /addbot <token>"
        )

    async def add_bot(self, token: str) -> str:
        if not TOKEN_RE.match(token):
            return "❌ Token ka format galat hai."
        try:
            async with Bot(token) as tmp:
                me = await tmp.get_me()
        except InvalidToken:
            return "❌ Token invalid ya revoked hai."
        except TelegramError as e:
            return f"❌ Telegram error: {e}"

        bot_id = str(me.id)
        async with self._lock:
            await self.stop_secondary(bot_id)
            await db.upsert_bot(bot_id, me.username, fernet.encrypt(token.encode()).decode(), "pending")
            try:
                await self.launch_secondary(bot_id, token)
            except TelegramError as e:
                return f"❌ Webhook set nahi hua: {e}"

            ok, missing = await self.check_admin(self.apps[bot_id].bot)
            if ok:
                await db.set_status(bot_id, "active")
                self._cache = None
                return f"✅ @{me.username} active. Ab ye bhi redirect pool me hai."
            return (
                f"🟡 @{me.username} save ho gaya (pending).\n"
                f"Isko in channels me admin banao: {', '.join(map(str, missing))}\n"
                f"Admin banate hi {HEALTH_INTERVAL // 60} min ke andar auto active ho jayega."
            )

    # ---------- health check ----------
    async def check_all(self):
        for b in await db.list_bots():
            if b["status"] in ("dead", "disabled"):
                continue
            bot_id = b["bot_id"]
            try:
                app = self.apps.get(bot_id)
                if not app:
                    await self.launch_secondary(
                        bot_id, fernet.decrypt(b["token_enc"].encode()).decode()
                    )
                    app = self.apps[bot_id]
                await app.bot.get_me()

                if b["status"] == "pending":
                    ok, _ = await self.check_admin(app.bot)
                    if ok:
                        await db.set_status(bot_id, "active")
                        self._cache = None
                        await self.notify_owner(f"🟢 @{b['username']} ab active hai.")
            except InvalidToken:
                await self._mark_dead(b)
            except Exception as e:  # noqa: BLE001 (network glitch: dead mark mat karo)
                log.warning("health check skipped @%s: %s", b["username"], e)

    async def health_loop(self):
        while True:
            await asyncio.sleep(HEALTH_INTERVAL)
            try:
                await self.check_all()
            except Exception:  # noqa: BLE001
                log.exception("health loop error")

    # ---------- redirect pool ----------
    async def active_usernames(self) -> list[str]:
        if self._cache and time.time() - self._cache[0] < 30:
            return self._cache[1]
        names = [b["username"] for b in await db.list_bots("active")]
        self._cache = (time.time(), names)
        return names
