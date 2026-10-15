from __future__ import annotations

from dataclasses import dataclass
import random


@dataclass(frozen=True)
class RetryPolicy:
    base_seconds: float = 2.0
    maximum_seconds: float = 300.0
    jitter_ratio: float = 0.20

    def delay(self, attempts: int) -> float:
        raw = min(self.maximum_seconds, self.base_seconds * (2 ** max(0, attempts - 1)))
        jitter = raw * self.jitter_ratio
        return max(0.0, raw + random.uniform(-jitter, jitter))
