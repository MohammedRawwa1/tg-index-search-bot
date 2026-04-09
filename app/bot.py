from typing import List, Dict, Optional
import asyncio

from pyrogram import Client


class BotManager:
    """Manage one-or-more Pyrogram clients created from credentials.

    `api_credentials` is a list of dicts with keys: name, api_id, api_hash, bot_token (optional),
    session_key (optional) which references an encrypted session stored via SessionStore.
    """

    def __init__(self, api_credentials: List[Dict], session_store: Optional[object] = None):
        self.api_credentials = api_credentials or []
        self.session_store = session_store
        self.clients: List[Client] = []
        for idx, cred in enumerate(self.api_credentials):
            name = cred.get("name") or f"session_{idx}"
            params: Dict[str, object] = {"name": name}

            # Optional API credentials
            if cred.get("api_id") is not None:
                try:
                    params["api_id"] = int(cred.get("api_id"))
                except Exception:
                    params["api_id"] = cred.get("api_id")
            if cred.get("api_hash") is not None:
                params["api_hash"] = cred.get("api_hash")

            # Optional bot token
            if cred.get("bot_token"):
                params["bot_token"] = cred.get("bot_token")

            # Resolve session string if a session_key is provided
            session_string = None
            if cred.get("session_key") and self.session_store:
                try:
                    session_string = self.session_store.get_session_string(cred.get("session_key"))
                except Exception:
                    session_string = None
            # Fallback to inline session_string if provided directly in credentials
            if not session_string and cred.get("session_string"):
                session_string = cred.get("session_string")

            if session_string:
                params["session_string"] = session_string

            self.clients.append(Client(**params))

    async def start_all(self) -> None:
        tasks = [asyncio.create_task(c.start()) for c in self.clients]
        await asyncio.gather(*tasks)

    async def stop_all(self) -> None:
        async def _stop_client(client: Client):
            try:
                await client.stop()
            except ConnectionError:
                # Client already terminated; ignore.
                return
            except Exception:
                # Swallow other stop errors to avoid crash during shutdown.
                return

        tasks = [asyncio.create_task(_stop_client(c)) for c in self.clients]
        await asyncio.gather(*tasks)

    def register_handler(self, func):
        # Convenience: allow registering a plain function on all clients
        for client in self.clients:
            func(client)