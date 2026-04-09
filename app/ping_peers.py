import asyncio
from pyrogram import Client
import os
from peer_manager import get_all_peers

BOT_TOKEN = os.getenv("BOT_TOKEN")

async def main():
    bot = Client("bot_session", bot_token=BOT_TOKEN)
    await bot.start()

    peers = get_all_peers()
    print(f"Testing {len(peers)} peers...")

    for chat_id in peers:
        try:
            chat = await bot.get_chat(chat_id)
            await bot.send_message(chat_id, "Ping from local test!")
            print(f"✅ Bot can reach: {chat.title or chat.first_name} ({chat.id})")
        except Exception as e:
            print(f"❌ Failed to reach {chat_id}: {e}")

    await bot.stop()

asyncio.run(main())