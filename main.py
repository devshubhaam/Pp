import asyncio
import hashlib
import hmac
import html
import json
import logging
import math
import random
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from telegram import Update

import db
from config import (
    ADS_REQUIRED,
    MIN_AD_SECONDS,
    MONETAG_REQUIRE_VALUED,
    MONETAG_SDK_URL,
    MONETAG_ZONE_ID,
    ROLE,
)
from delivery import send_file
from manager import BotManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

manager = BotManager()
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await manager.start()
    task = asyncio.create_task(manager.background_loop())
    yield
    task.cancel()
    await manager.stop()


app = FastAPI(lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return PlainTextResponse(f"ok ({ROLE})")


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
    if not TOKEN_RE.match(token):
        raise HTTPException(status_code=404)
    names = await manager.active_usernames()
    if not names:
        raise HTTPException(status_code=503, detail="No active bot right now")
    return RedirectResponse(f"https://t.me/{random.choice(names)}?start={token}", status_code=302)


# ===================== WORKER only: Mini App =====================
def validate_init_data(init_data: str, bot_token: str, max_age: int = 3600):
    try:
        parsed = dict(parse_qsl(init_data or "", keep_blank_values=True))
        received = parsed.pop("hash", None)
        if not received:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if time.time() - int(parsed.get("auth_date", "0")) > max_age:
            return None
        return json.loads(parsed["user"])
    except Exception:  # noqa: BLE001
        return None


if ROLE == "worker":
    _CFG = json.dumps(
        {
            "sdkFn": f"show_{MONETAG_ZONE_ID}",
            "requireValued": MONETAG_REQUIRE_VALUED,
            "required": ADS_REQUIRED,
        }
    ).replace("</", "<\\/")

    if MONETAG_ZONE_ID:
        _TAG = (
            f'<script src="{html.escape(MONETAG_SDK_URL, quote=True)}" '
            f'data-zone="{html.escape(MONETAG_ZONE_ID, quote=True)}" '
            f'data-sdk="show_{html.escape(MONETAG_ZONE_ID, quote=True)}"></script>'
        )
    else:
        _TAG = ""

    _HTML = (
        Path(__file__)
        .with_name("miniapp.html")
        .read_text(encoding="utf-8")
        .replace("__CFG__", _CFG)
        .replace("<!--MONETAG_TAG-->", _TAG)
    )

    @app.get("/app", response_class=HTMLResponse)
    async def miniapp():
        return HTMLResponse(_HTML, headers={"Cache-Control": "no-store"})

    async def _auth(body: dict):
        sid = body.get("sid")
        s = await db.get_session(sid) if isinstance(sid, str) else None
        if not s:
            return None, None, "Session expire ho gaya. Link dobara kholo."
        application = manager.apps.get(s["bot_id"])
        if not application:
            return None, None, "Bot abhi available nahi. Thodi der baad try karo."
        user = validate_init_data(body.get("init_data", ""), application.bot.token)
        if not user or user.get("id") != s["user_id"]:
            return None, None, "Verification fail. Bot ke andar se button dabao."
        return s, application, None

    @app.post("/api/ad/start")
    async def ad_start(request: Request):
        body = await request.json()
        s, _application, err = await _auth(body)
        if err:
            return {"ok": False, "error": err}
        if s["delivered"]:
            return {"ok": False, "error": "Video pehle hi mil chuka hai."}
        await db.start_ad(s["sid"])
        return {"ok": True, "ads_done": s["ads_done"], "ads_required": s["ads_required"]}

    @app.post("/api/ad/done")
    async def ad_done(request: Request):
        body = await request.json()
        s, application, err = await _auth(body)
        if err:
            return {"ok": False, "error": err}
        if s["delivered"]:
            return {"ok": False, "error": "Video pehle hi bheja ja chuka hai."}

        started = s.get("ad_started_at")
        if not started:
            return {"ok": False, "error": "Ad start nahi hua. Retry dabao."}
        elapsed = time.time() - started
        if elapsed < MIN_AD_SECONDS:
            return {"ok": True, "wait": math.ceil(MIN_AD_SECONDS - elapsed)}

        s2 = await db.complete_ad(s["sid"], MIN_AD_SECONDS)
        if not s2:
            return {"ok": False, "error": "Duplicate request."}
        if s2["ads_done"] < s2["ads_required"]:
            return {
                "ok": True,
                "delivered": False,
                "ads_done": s2["ads_done"],
                "ads_required": s2["ads_required"],
            }

        if not await db.claim_delivery(s["sid"]):
            return {"ok": False, "error": "Video pehle hi bheja ja chuka hai."}

        doc = await db.get_file(s2["token"])
        ok = bool(doc) and await send_file(application.bot, s2["chat_id"], doc, manager)
        if not ok:
            await db.unclaim_delivery(s["sid"])
            return {"ok": False, "error": "File abhi available nahi. Thodi der baad retry karo."}
        return {
            "ok": True,
            "delivered": True,
            "ads_done": s2["ads_done"],
            "ads_required": s2["ads_required"],
        }
