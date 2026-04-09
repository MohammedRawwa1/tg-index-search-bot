import asyncio

from app.config.settings import settings
from app.services.mongo import MongoService
from app.bot import BotManager
from app.handlers import indexer, search
from app.utils.logger import logger


async def _start() -> None:
    # initialize mongo
    mongo = MongoService(settings.MONGO_URI, settings.DB_NAME, settings.MONGO_CONNECT_TIMEOUT_MS)
    try:
        mongo.connect()
        mongo.ensure_indexes()
    except Exception as exc:
        logger.error("MongoDB not available, continuing without DB: {}", exc)
        # allow bot to run without Mongo for development/testing
        mongo = None

    # initialize bot clients (may be multiple)
    try:
        from app.services.session_store import SessionStore
        session_store = SessionStore(mongo) if mongo is not None else None
    except Exception:
        session_store = None

    bot_mgr = BotManager(settings.API_CREDENTIALS, session_store=session_store)
    logger.info("Loaded {} client credential(s)", len(bot_mgr.clients))

    # register handlers across clients
    for client in bot_mgr.clients:
        # expose mongo/db on client for handlers to use
        if mongo is not None:
            setattr(client, "_mongo", mongo)
            try:
                setattr(client, "_bot_db", mongo.db)
            except Exception:
                setattr(client, "_bot_db", None)
        indexer.register_indexer(client, mongo)
        search.register_search_handlers(client, mongo)
        # admin handlers
        try:
            from app.handlers.admin import register_admin_handlers
            register_admin_handlers(client, mongo)
        except Exception:
            pass

    if not bot_mgr.clients:
        logger.warning("No API credentials found — bot will run without clients.")
    await bot_mgr.start_all()

    # run forever
    await asyncio.Event().wait()


def main() -> None:
    try:
        asyncio.run(_start())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()