"""Live events for WebSocket subscribers.

Topics: "blocks" (new blocks and chain switches), "mempool" (new waiting
transfers), and "address:<address>" (anything touching that address).
A subscriber that falls too far behind is dropped rather than slowing the node.
"""

import asyncio

QUEUE_LIMIT = 1000


class Subscription:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.topics: set[str] = set()
        self.overflowed = False

    def deliver(self, message: dict) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.overflowed = True


class EventBus:
    def __init__(self) -> None:
        # Indexed by topic, so an event costs nothing for subscribers that don't want it.
        self._by_topic: dict[str, set[Subscription]] = {}

    def subscribe(self) -> Subscription:
        return Subscription()

    def add_topics(self, subscription: Subscription, topics: list[str], limit: int) -> None:
        """Add topics to a subscription, up to `limit` topics in total."""
        for topic in topics:
            if len(subscription.topics) >= limit:
                break
            subscription.topics.add(topic)
            self._by_topic.setdefault(topic, set()).add(subscription)

    def unsubscribe(self, subscription: Subscription) -> None:
        for topic in subscription.topics:
            subscribers = self._by_topic.get(topic)
            if subscribers is not None:
                subscribers.discard(subscription)
                if not subscribers:
                    del self._by_topic[topic]
        subscription.topics.clear()

    def publish(self, topic: str, message: dict) -> None:
        for subscription in list(self._by_topic.get(topic, ())):
            subscription.deliver(message)
