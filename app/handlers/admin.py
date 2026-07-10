"""Admin command handlers: stats, health check, reindex, admin panel."""

import asyncio
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from app.config.settings import settings
from app.handlers.indexer import backfill_history


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
    /stats, /health, /reindex, /admin and associated callbacks.
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

    # --- Callback handler ---
    @client.on_callback_query()
    async def _admin_callback(client: Client, cq: CallbackQuery):
        data = cq.data or ""
        if not data.startswith("A|"):
            return

        if not _is_owner(cq.from_user.id if cq.from_user else None):
            await cq.answer("Unauthorized", show_alert=True)
            return

        cmd = data.split("|")[1]
        target_chat = settings.TARGET_CHAT_ID or settings.OWNER_ID or settings.BOT_OWNER
        if target_chat:
            target_chat = int(target_chat)

        admin_tasks = getattr(client, "_admin_tasks")

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
