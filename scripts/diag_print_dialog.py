import asyncio, pathlib, sys, os, traceback
_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path: sys.path.insert(0, str(_ROOT))

from pyrogram import Client

async def main():
    session_name = os.getenv("SESSION_NAME") or "user_session"
    # Accept TELETHON_SESSION as alias for SESSION_STRING
    session_string = os.getenv("SESSION_STRING") or os.getenv("TELETHON_SESSION")

    # Ensure local .session files are created in the project root (not the current CWD).
    session_path_env = os.getenv("SESSION_PATH")
    if session_path_env:
        session_target = session_path_env
    else:
        session_target = str(_ROOT / session_name)
    # Accept TELETHON_* env names as aliases for API credentials
    api_id = os.getenv("API_ID") or os.getenv("TELETHON_API_ID")
    api_hash = os.getenv("API_HASH") or os.getenv("TELETHON_API_HASH")
    kwargs = {}
    if api_id:
        try:
            kwargs["api_id"] = int(api_id)
        except Exception:
            kwargs["api_id"] = api_id
    if api_hash:
        kwargs["api_hash"] = api_hash

    if session_string:
        client = Client(session_target, session_string=session_string, **kwargs)
    else:
        client = Client(session_target, **kwargs)

    await client.start()
    try:
        target = -1002347599837
        found = False
        async for d in client.get_dialogs(limit=1000):
            chat = d.chat
            if chat.id == target or getattr(chat, "title", None) == "VAULT of Success":
                print("FOUND", chat.id, getattr(chat,'title',None), getattr(chat,'username',None))
                print("dir(chat):", [x for x in dir(chat) if not x.startswith('_')])
                try:
                    print("chat.__dict__:", chat.__dict__)
                except Exception as e:
                    print("chat.__dict__ error:", e)
                try:
                    print("dialog object repr:", repr(d))
                    print("dialog.__dict__:", d.__dict__)
                except Exception as e:
                    print("dialog repr error:", e)

                try:
                    # Try resolve_peer if available
                    try:
                        rp = await client.resolve_peer(chat)
                        print("resolve_peer result:", rp)
                    except Exception as re:
                        print("resolve_peer error:", type(re), re)

                    from pyrogram.raw import functions, types
                    aid = getattr(chat, "id", None)
                    ahash = getattr(chat, "access_hash", None)
                    print("access_hash attribute:", ahash)
                    if ahash:
                        input_channel = types.InputChannel(channel_id=abs(aid), access_hash=ahash)
                        try:
                            full = await client.invoke(functions.channels.GetFullChannel(channel=input_channel))
                            print("GetFullChannel success:", full)
                        except Exception as e:
                            print("GetFullChannel error:", type(e), e)
                    else:
                        print("No access_hash on chat; trying raw messages.GetDialogs() to inspect raw chat objects")
                        try:
                            res = await client.invoke(functions.messages.GetDialogs(offset_date=0, offset_id=0, offset_peer=types.InputPeerEmpty(), limit=10, hash=0))
                            print("messages.GetDialogs result chats length:", len(res.chats))
                            for ch in res.chats:
                                try:
                                    cid = getattr(ch,'id',None)
                                    ctitle = getattr(ch,'title',None)
                                    print("raw chat:", type(ch), cid, ctitle, getattr(ch,'access_hash',None))
                                except Exception as e:
                                    print("raw chat print error:", e)
                        except Exception as e:
                            print("GetDialogs error:", type(e), e)
                except Exception as e:
                    print("raw functions error:", e)
                found = True
                break
        if not found:
            print("Dialog not found in get_dialogs()")
    finally:
        await client.stop()

asyncio.run(main())