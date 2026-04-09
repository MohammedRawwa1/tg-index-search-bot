import asyncio
import random
from typing import Callable, Any


async def retry_async(func: Callable[..., Any], *args, retries: int = 3, base: float = 0.5, max_backoff: float = 8.0, jitter: float = 0.2, retry_exceptions: tuple = (Exception,), **kwargs):
    """Run `func(*args, **kwargs)` with exponential backoff on failure.

    `func` must be an awaitable/coroutine function.
    Returns the result of `func` or raises the last exception on final failure.
    """
    attempt = 0
    delay = base
    while True:
        attempt += 1
        try:
            return await func(*args, **kwargs)
        except retry_exceptions as exc:
            if attempt >= retries:
                raise
            # jittered sleep
            jitter_val = random.uniform(0, jitter * delay)
            await asyncio.sleep(min(delay + jitter_val, max_backoff))
            delay = min(delay * 2, max_backoff)
