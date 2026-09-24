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
        self._subscriptions: set[Subscription] = set()

    def subscribe(self) -> Subscription:
        subscription = Subscription()
        self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self._subscriptions.discard(subscription)

    def publish(self, topic: str, message: dict) -> None:
        for subscription in list(self._subscriptions):
            if topic in subscription.topics:
                subscription.deliver(message)
