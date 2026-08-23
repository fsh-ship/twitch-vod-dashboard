"""Process-local concurrency control for Twitch live-status extraction."""

from __future__ import annotations

import threading
from typing import Callable, TypeVar


Result = TypeVar("Result")
MAX_CONCURRENT_LIVE_STATUS_CHECKS = 2


class LiveStatusConcurrencyLimiter:
    """Bound concurrent low-level live-status operations without leaking slots."""

    def __init__(self, limit: int = MAX_CONCURRENT_LIVE_STATUS_CHECKS) -> None:
        normalized = int(limit)
        if normalized < 1:
            raise ValueError("The live-status concurrency limit must be positive.")
        self.limit = normalized
        self._semaphore = threading.BoundedSemaphore(normalized)

    def run(self, operation: Callable[..., Result], *args, **kwargs) -> Result:
        with self._semaphore:
            return operation(*args, **kwargs)


LIVE_STATUS_LIMITER = LiveStatusConcurrencyLimiter()
