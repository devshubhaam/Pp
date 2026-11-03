import re
import time
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument

from config import DB_NAME, MONGO_URI, SESSION_TTL

_client = AsyncIOMotorClient(MONGO_URI)
_db = _client[DB_NAME]
bots = _db.bots
files = _db.files
sessions = _db.sessions


async def init():
    await bots.create_index("bot_id", unique=True)
    await bots.create_index("username")
    await files.create_index("token", unique=True)
    await sessions.create_index("sid", unique=True)
    await sessions.create_index("created", expireAfterSeconds=SESSION_TTL)


# ---------- bots registry ----------
async def upsert_bot(bot_id: str, username: str, token_enc: str, status: str):
    await bots.update_one(
        {"bot_id": bot_id},
        {
            "$set": {
                "username": username,
                "token_enc": token_enc,
                "status": status,
                "updated_at": time.time(),
            },
            "$setOnInsert": {"added_at": time.time()},
        },
        upsert=True,
    )


async def list_bots(status: str | None = None):
    query = {"status": status} if status else {}
    return await bots.find(query).sort("added_at", 1).to_list(length=200)


async def get_bot_by_username(username: str):
    username = username.lstrip("@")
    return await bots.find_one(
        {"username": {"$regex": f"^{re.escape(username)}$", "$options": "i"}}
    )


async def set_status(bot_id: str, status: str):
    await bots.update_one(
        {"bot_id": bot_id}, {"$set": {"status": status, "updated_at": time.time()}}
    )


async def delete_bot(bot_id: str):
    await bots.delete_one({"bot_id": bot_id})


# ---------- files ----------
async def save_file(token: str, locations: list, caption: str):
    await files.insert_one(
        {
            "token": token,
            "locations": locations,
            "caption": caption,
            "created_at": time.time(),
        }
    )


async def get_file(token: str):
    return await files.find_one({"token": token})


# ---------- verification sessions ----------
async def create_session(sid, token, user_id, chat_id, bot_id, required):
    await sessions.insert_one(
        {
            "sid": sid,
            "token": token,
            "user_id": user_id,
            "chat_id": chat_id,
            "bot_id": bot_id,
            "ads_done": 0,
            "ads_required": required,
            "ad_started_at": None,
            "delivered": False,
            "created": datetime.now(timezone.utc),
        }
    )


async def get_session(sid: str):
    return await sessions.find_one({"sid": sid})


async def start_ad(sid: str):
    await sessions.update_one(
        {"sid": sid, "delivered": False}, {"$set": {"ad_started_at": time.time()}}
    )


async def complete_ad(sid: str, min_seconds: int):
    cutoff = time.time() - min_seconds
    return await sessions.find_one_and_update(
        {
            "sid": sid,
            "delivered": False,
            "ad_started_at": {"$ne": None, "$lte": cutoff},
        },
        {"$inc": {"ads_done": 1}, "$set": {"ad_started_at": None}},
        return_document=ReturnDocument.AFTER,
    )


async def claim_delivery(sid: str) -> bool:
    r = await sessions.find_one_and_update(
        {
            "sid": sid,
            "delivered": False,
            "$expr": {"$gte": ["$ads_done", "$ads_required"]},
        },
        {"$set": {"delivered": True}},
    )
    return r is not None


async def unclaim_delivery(sid: str):
    await sessions.update_one({"sid": sid}, {"$set": {"delivered": False}})
