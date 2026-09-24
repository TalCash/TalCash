"""Rate limits, shared by the API and the peer-to-peer layer."""

import ipaddress
import time
from collections import OrderedDict
from collections.abc import Callable


class TokenBucket:
    """Allows `rate` units per second on average, and bursts of up to `burst` units.

    `take` never refuses: a cost bigger than what's saved up goes into debt, and the caller is told
    how long to wait. That suits a connection we read from: while we wait, we don't read, so the
    sender gets slowed down instead of us doing its work faster than we want to.
    """

    def __init__(self, rate: float, burst: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.rate = rate
        self.burst = burst
        self.clock = clock
        self.tokens = float(burst)
        self.updated = clock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def take(self, cost: float = 1.0) -> float:
        """Spend `cost`. Returns how many seconds to wait before going ahead (0 = right away)."""
        self._refill()
        self.tokens -= cost
        return 0.0 if self.tokens >= 0 else -self.tokens / self.rate

    def try_take(self, cost: float = 1.0) -> bool:
        """Spend `cost` only if it's saved up already."""
        self._refill()
        if self.tokens < cost:
            return False
        self.tokens -= cost
        return True


class HostBuckets:
    """A TokenBucket per client address. Forgets the least recently seen when there are too many."""

    def __init__(self, rate: float, burst: float, *, limit: int = 10_000,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.rate, self.burst, self.limit, self.clock = rate, burst, limit, clock
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()

    def try_take(self, host: str, cost: float = 1.0) -> bool:
        bucket = self._buckets.get(host)
        if bucket is None:
            bucket = self._buckets[host] = TokenBucket(self.rate, self.burst, self.clock)
            if len(self._buckets) > self.limit:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(host)
        return bucket.try_take(cost)


def is_loopback(host: str | None) -> bool:
    """Is this address this computer itself? (127.x.x.x, ::1, localhost)"""
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)  # ::ffff:127.0.0.1 on dual-stack sockets
    return address.is_loopback or (mapped is not None and mapped.is_loopback)
