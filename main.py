import asyncio
import hmac
import logging
import random
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from telegram import Update

from manager import BotManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

manager = BotManager()
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await manager.start()
    task = asyncio.create_task(manager.health_loop())
    yield
    task.cancel()
    await manager.stop()


app = FastAPI(lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return PlainTextResponse("ok")


@app.post("/webhook/{bot_id}")
async def webhook(
    bot_id: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    application = manager.apps.get(bot_id)
    expected = manager.secret_for(bot_id)
    if not application or not hmac.compare_digest(x_telegram_bot_api_secret_token or "", expected):
        raise HTTPException(status_code=403)

    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)
    return {"ok": True}


@app.get("/f/{token}")
async def permanent_link(token: str):
    """Permanent link: kisi bhi active bot pe redirect (bot badle, link wahi)."""
    if not TOKEN_RE.match(token):
        raise HTTPException(status_code=404)
    names = await manager.active_usernames()
    if not names:
        raise HTTPException(status_code=503, detail="No active bot right now")
    return RedirectResponse(f"https://t.me/{random.choice(names)}?start={token}", status_code=302)
