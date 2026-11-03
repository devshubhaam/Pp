import re
import time

from motor.motor_asyncio import AsyncIOMotorClient

from config import DB_NAME, MONGO_URI

_client = AsyncIOMotorClient(MONGO_URI)
_db = _client[DB_NAME]
bots = _db.bots
files = _db.files


async def init():
    await bots.create_index("bot_id", unique=True)
    await bots.create_index("username")
    await files.create_index("token", unique=True)


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
            "locations": locations,  # [{"channel": -100.., "msg": 123}, ...]
            "caption": caption,
            "created_at": time.time(),
        }
    )


async def get_file(token: str):
    return await files.find_one({"token": token})
