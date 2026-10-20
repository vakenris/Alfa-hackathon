import asyncio
import json
from typing import AsyncGenerator, Dict, Set
from backend.app.core.config import settings
from backend.app.core.logging import logger

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None


class InMemoryEventBus:
    def __init__(self):
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, channel: str, message: dict):
        async with self._lock:
            if channel in self._subscribers:
                for queue in list(self._subscribers[channel]):
                    await queue.put(message)

    async def subscribe(self, channel: str) -> AsyncGenerator[dict, None]:
        queue = asyncio.Queue()
        async with self._lock:
            if channel not in self._subscribers:
                self._subscribers[channel] = set()
            self._subscribers[channel].add(queue)

        try:
            while True:
                msg = await queue.get()
                yield msg
        finally:
            async with self._lock:
                if channel in self._subscribers and queue in self._subscribers[channel]:
                    self._subscribers[channel].remove(queue)
                    if not self._subscribers[channel]:
                        del self._subscribers[channel]


class RedisEventBus:
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._redis = None

    async def _get_redis(self):
        if self._redis is None:
            self._redis = aioredis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    async def publish(self, channel: str, message: dict):
        client = await self._get_redis()
        await client.publish(channel, json.dumps(message))

    async def subscribe(self, channel: str) -> AsyncGenerator[dict, None]:
        client = await self._get_redis()
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for raw in pubsub.listen():
                if raw["type"] == "message":
                    yield json.loads(raw["data"])
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()


# Singleton bus
in_memory_bus = InMemoryEventBus()


def get_event_bus():
    if not settings.USE_IN_MEMORY_BUS and aioredis is not None:
        return RedisEventBus(settings.REDIS_URL)
    return in_memory_bus
