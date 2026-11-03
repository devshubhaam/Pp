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
    ROLE,
    SYNC_INTERVAL,
    WEBHOOK_SECRET,
)

log = logging.getLogger("manager")
fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)
TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")


def _dec(token_enc: str) -> str:
    return fernet.decrypt(token_enc.encode()).decode()


class BotManager:
    def __init__(self):
        self.apps: dict[str, Application] = {}
        self.main: Application | None = None
        self._running_tokens: dict[str, str] = {}
        self._cache: tuple[float, list[str]] | None = None
        self._alerted: set[str] = set()
        self._lock = asyncio.Lock()

    def secret_for(self, bot_id: str) -> str:
        return hashlib.sha256(f"{WEBHOOK_SECRET}:{bot_id}".encode()).hexdigest()

    async def _build(self, token: str, register, bot_id: str) -> Application:
        app = (
            Application.builder()
            .token(token)
            .updater(None)
            .concurrent_updates(True)
            .build()
        )
        app.bot_data["manager"] = self
        app.bot_data["bot_id"] = bot_id
        register(app)
        await app.initialize()
        await app.start()
        return app

    async def notify_owner(self, text: str):
        try:
            if self.main:
                await self.main.bot.send_message(OWNER_ID, text)
            else:
                async with Bot(MAIN_BOT_TOKEN) as b:
                    await b.send_message(OWNER_ID, text)
        except Exception as e:  # noqa: BLE001
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
        if ROLE == "main":
            self.main = await self._build(MAIN_BOT_TOKEN, handlers.register_main_handlers, "main")
            await self.main.bot.set_webhook(
                url=f"{BASE_URL}/webhook/main",
                secret_token=self.secret_for("main"),
                allowed_updates=["message"],
            )
            self.apps["main"] = self.main
        else:
            await self.sync_once()

    def background_loop(self):
        return self.health_loop() if ROLE == "main" else self.sync_loop()

    async def stop(self):
        for app in list(self.apps.values()):
            try:
                await app.stop()
                await app.shutdown()
            except Exception:  # noqa: BLE001
                pass
        self.apps.clear()

    # ---------- WORKER: secondary bots ----------
    async def launch_secondary(self, b: dict):
        bot_id = b["bot_id"]
        app = await self._build(_dec(b["token_enc"]), handlers.register_delivery_handlers, bot_id)
        await app.bot.set_webhook(
            url=f"{BASE_URL}/webhook/{bot_id}",
            secret_token=self.secret_for(bot_id),
            allowed_updates=["message"],
        )
        self.apps[bot_id] = app
        self._running_tokens[bot_id] = b["token_enc"]
        self._cache = None

    async def stop_secondary(self, bot_id: str, delete_webhook: bool = False):
        app = self.apps.pop(bot_id, None)
        self._running_tokens.pop(bot_id, None)
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
            await self.launch_secondary(b)
            log.info("launched @%s", b["username"])
        except InvalidToken:
            await self._mark_dead(b)
        except Exception as e:  # noqa: BLE001
            log.warning("launch failed @%s: %s", b.get("username"), e)

    async def sync_once(self):
        want = {
            b["bot_id"]: b
            for b in await db.list_bots()
            if b["status"] in ("active", "pending")
        }
        for bot_id in list(self.apps):
            b = want.get(bot_id)
            if b is None:
                await self.stop_secondary(bot_id, delete_webhook=True)
            elif self._running_tokens.get(bot_id) != b["token_enc"]:
                await self.stop_secondary(bot_id)
        for bot_id, b in want.items():
            if bot_id not in self.apps:
                await self._safe_launch(b)

    async def sync_loop(self):
        while True:
            try:
                await self.sync_once()
            except Exception:  # noqa: BLE001
                log.exception("sync error")
            await asyncio.sleep(SYNC_INTERVAL)

    # ---------- MAIN: registry + health ----------
    async def _mark_dead(self, b: dict):
        await db.set_status(b["bot_id"], "dead")
        await self.stop_secondary(b["bot_id"])
        left = len(await db.list_bots("active"))
        await self.notify_owner(
            f"🔴 @{b['username']} dead (suspend/delete/token revoked).\n"
            f"Active bots bache: {left}\n"
            f"Naya bot add karo: /addbot <token>"
        )

    async def release_bot(self, b: dict):
        try:
            async with Bot(_dec(b["token_enc"])) as tmp:
                await tmp.delete_webhook()
        except Exception:  # noqa: BLE001
            pass

    async def add_bot(self, token: str) -> str:
        if not TOKEN_RE.match(token):
            return "❌ Token ka format galat hai."
        try:
            async with Bot(token) as tmp:
                me = await tmp.get_me()
                ok, missing = await self.check_admin(tmp)
        except InvalidToken:
            return "❌ Token invalid ya revoked hai."
        except TelegramError as e:
            return f"❌ Telegram error: {e}"

        bot_id = str(me.id)
        status = "active" if ok else "pending"
        async with self._lock:
            await db.upsert_bot(bot_id, me.username, fernet.encrypt(token.encode()).decode(), status)

        if ok:
            return (
                f"✅ @{me.username} save ho gaya.\n"
                f"Worker {SYNC_INTERVAL}s ke andar ise webhook pe live kar dega."
            )
        return (
            f"🟡 @{me.username} save ho gaya (pending).\n"
            f"Isko in channels me admin banao: {', '.join(map(str, missing))}\n"
            f"Admin banate hi {HEALTH_INTERVAL // 60} min ke andar auto active ho jayega."
        )

    async def check_all(self):
        for b in await db.list_bots():
            if b["status"] in ("dead", "disabled"):
                continue
            try:
                async with Bot(_dec(b["token_enc"])) as bot:
                    await bot.get_me()
                    if b["status"] == "pending":
                        ok, _ = await self.check_admin(bot)
                        if ok:
                            await db.set_status(b["bot_id"], "active")
                            await self.notify_owner(f"🟢 @{b['username']} ab active hai.")
            except InvalidToken:
                await self._mark_dead(b)
            except Exception as e:  # noqa: BLE001
                log.warning("health check skipped @%s: %s", b["username"], e)

    async def health_loop(self):
        while True:
            await asyncio.sleep(HEALTH_INTERVAL)
            try:
                await self.check_all()
            except Exception:  # noqa: BLE001
                log.exception("health loop error")

    async def active_usernames(self) -> list[str]:
        if self._cache and time.time() - self._cache[0] < 30:
            return self._cache[1]
        names = [b["username"] for b in await db.list_bots("active")]
        self._cache = (time.time(), names)
        return names
