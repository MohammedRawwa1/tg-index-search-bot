import asyncio
import random


class RateLimiter:
    def __init__(self, base_delay: float = 0.05, max_delay: float = 2.0):
        self.base = base_delay
        self.max = max_delay
        self._factor = 1.0

    async def wait(self):
        # jittered backoff based on factor
        delay = min(self.max, self.base * self._factor) * (1 + random.random() * 0.3)
        await asyncio.sleep(delay)

    def increase(self):
        self._factor = min(100.0, self._factor * 1.5)

    def reset(self):
        self._factor = 1.0