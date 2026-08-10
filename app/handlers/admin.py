"""Admin command handlers: stats, health check, reindex, admin panel."""

import asyncio
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from app.config.settings import settings
from app.handlers.indexer import backfill_history
from app.utils.logger import logger
from motor.motor_asyncio import AsyncIOMotorClient


def _is_owner(user_id: Optional[int]) -> bool:
    """Check if the given user ID is the bot owner.

    Checks BOT_OWNER first, then falls back to OWNER_ID for backward compatibility.
    """
    if user_id is None:
        return False
    owner = settings.BOT_OWNER or settings.OWNER_ID
    return owner is not None and int(user_id) == int(owner)


def register_admin_handlers(client: Client, mongo):
    """
    Registers owner-only admin commands:
    /stats, /health, /reindex, /admin, /clear_all.
    Callbacks (C| and A|) are handled by `handle_admin_callback`, dispatched
    from the merged callback handler in `app.handlers.search`.
    """

    # Ensure admin tasks storage
    if not hasattr(client, "_admin_tasks"):
        setattr(client, "_admin_tasks", {})

    # --- /stats command ---
    @client.on_message(filters.command("stats"))
    async def _stats(client: Client, message: Message):
        if not _is_owner(message.from_user.id):
            await message.reply_text("Unauthorized")
            return

        if mongo is None:
            await message.reply_text("Stats: DB unavailable")
            return

        files_col = mongo.db.get_collection("files")
        index_state = mongo.db.get_collection("index_state").find_one({}) or {}
        total_files = files_col.count_documents({})
        duplicates = files_col.count_documents({"is_duplicate": True})
        last_msg = index_state.get("last_message_id", "N/A")

        await message.reply_text(
            f"Total files: {total_files}\nDuplicates: {duplicates}\nLast indexed (sample): {last_msg}"
        )

    # --- /health command ---
    @client.on_message(filters.command("health"))
    async def _health(client: Client, message: Message):
        if not _is_owner(message.from_user.id):
            await message.reply_text("Unauthorized")
            return

        if mongo is None:
            await message.reply_text("Health: DB unavailable")
            return

        try:
            mongo.client.admin.command("ping")
            await message.reply_text("OK: MongoDB reachable")
        except Exception:
            await message.reply_text("ERROR: MongoDB ping failed")

    # --- /reindex command ---
    @client.on_message(filters.command("reindex"))
    async def _reindex(client: Client, message: Message):
        if not _is_owner(message.from_user.id):
            await message.reply_text("Unauthorized")
            return

        if mongo is None:
            await message.reply_text("Reindex: DB unavailable")
            return

        args = message.text.split(maxsplit=1)
        if len(args) == 1:
            await message.reply_text("Usage: /reindex <chat_id>\nExample: /reindex -1001234567890")
            return

        try:
            target_chat_id = int(args[1].strip())
        except Exception:
            await message.reply_text("Invalid chat ID")
            return

        await message.reply_text(f"Starting backfill for {target_chat_id} (background)")
        loop = asyncio.get_running_loop()
        loop.create_task(backfill_history(client, mongo, target_chat_id))

    # --- /admin panel ---
    @client.on_message(filters.command("admin"))
    async def _admin_panel(client: Client, message: Message):
        if not _is_owner(message.from_user.id):
            await message.reply_text("Unauthorized")
            return

        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Start Backfill", callback_data="A|start"),
                    InlineKeyboardButton("Stop Backfill", callback_data="A|stop"),
                ],
                [InlineKeyboardButton("Stats", callback_data="A|stats")],
            ]
        )
        await message.reply_text("Admin panel", reply_markup=kb)

    # --- /clear_all ---
    @client.on_message(filters.command("clear_all"))
    async def _clear_all(client: Client, message: Message):
        if not _is_owner(message.from_user.id):
            await message.reply_text("Unauthorized")
            return

        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Delete", callback_data="C|delete"),
                    InlineKeyboardButton("Drop", callback_data="C|drop"),
                ],
                [InlineKeyboardButton("Cancel", callback_data="C|cancel")],
            ]
        )
        await message.reply_text(
            "Clear ALL indexed files?\nDelete = safer, Drop = faster.",
            reply_markup=kb,
        )


async def handle_admin_callback(client: Client, cq: CallbackQuery):
    """Handle admin callbacks (C| clear actions and A| admin panel).

    Registered as a plain function and dispatched from the merged callback
    handler in `app.handlers.search` so admin and search callbacks never
    collide on the same `on_callback_query` event.
    """
    mongo = getattr(client, "_mongo", None)

    data = cq.data or ""

    if not _is_owner(cq.from_user.id if cq.from_user else None):
        await cq.answer("Unauthorized", show_alert=True)
        return

    # --- CLEAR ACTIONS (C|) ---
    if data.startswith("C|"):
        action = data.split("|", 1)[1]

        try:
            performed = False
            if mongo is not None:
                try:
                    if action == "delete":
                        mongo.db.get_collection("files").delete_many({})
                        mongo.db.get_collection("index_state").delete_many({})
                    elif action == "drop":
                        try:
                            mongo.db.drop_collection("files")
                        except Exception:
                            pass
                        try:
                            mongo.db.drop_collection("index_state")
                        except Exception:
                            pass
                    performed = True
                except Exception:
                    logger.exception("Admin clear: attached mongo delete/drop failed, will fallback to async client")

            if not performed:
                # Fallback: temporary AsyncIOMotorClient
                try:
                    mclient = AsyncIOMotorClient(settings.MONGO_URI)
                    mdb = mclient[settings.DB_NAME]
                    if action == "delete":
                        await mdb.get_collection("files").delete_many({})
                        await mdb.get_collection("index_state").delete_many({})
                    elif action == "drop":
                        try:
                            await mdb.drop_collection("files")
                        except Exception:
                            pass
                        try:
                            await mdb.drop_collection("index_state")
                        except Exception:
                            pass
                    try:
                        mclient.close()
                    except Exception:
                        pass
                    performed = True
                except Exception:
                    logger.exception("Admin clear: fallback AsyncIOMotorClient operation failed")

            # Only report success if the operation actually ran; otherwise tell
            # the user the action failed instead of lying about it.
            if action == "delete":
                if performed:
                    await cq.message.edit_text("Documents deleted.")
                    await cq.answer("Deleted")
                else:
                    await cq.answer("Failed to delete", show_alert=True)
            elif action == "drop":
                if performed:
                    await cq.message.edit_text("Collections dropped.")
                    await cq.answer("Dropped")
                else:
                    await cq.answer("Failed to drop", show_alert=True)
            else:
                await cq.message.edit_text("Cancelled.")
                await cq.answer()
        except Exception as e:
            logger.exception("Admin clear callback failed: {}", e)
            await cq.answer("Error", show_alert=True)
        return

    # --- ADMIN PANEL ACTIONS (A|) ---
    if not data.startswith("A|"):
        return

    cmd = data.split("|")[1]
    target_chat = settings.TARGET_CHAT_ID or settings.OWNER_ID or settings.BOT_OWNER
    if target_chat:
        target_chat = int(target_chat)

    # Keep task tracking on the client itself so it survives across callbacks;
    # fall back to a fresh dict and persist it if the attribute is missing.
    admin_tasks = getattr(client, "_admin_tasks", None)
    if admin_tasks is None:
        admin_tasks = {}
        try:
            client._admin_tasks = admin_tasks
        except Exception:
            pass

    if cmd == "start":
        if not target_chat:
            await cq.answer("No target chat configured", show_alert=True)
            return
        task = admin_tasks.get(target_chat)
        if task and not task.done():
            await cq.answer("Backfill already running", show_alert=True)
            return
        loop = asyncio.get_running_loop()
        task = loop.create_task(backfill_history(client, mongo, target_chat))
        admin_tasks[target_chat] = task
        await cq.answer("Backfill started")

    elif cmd == "stop":
        task = admin_tasks.get(target_chat)
        if not task:
            await cq.answer("No running backfill", show_alert=True)
            return
        task.cancel()
        await cq.answer("Backfill cancelled")

    elif cmd == "stats":
        if mongo is None:
            await cq.answer("Stats: DB unavailable", show_alert=True)
            return
        try:
            files_col = mongo.db.get_collection("files")
            total_files = files_col.count_documents({})
            duplicates = files_col.count_documents({"is_duplicate": True})
            await cq.message.reply_text(f"Total files: {total_files}\nDuplicates: {duplicates}")
            await cq.answer()
        except Exception:
            await cq.answer("Failed to fetch stats", show_alert=True)

    else:
        await cq.answer()
