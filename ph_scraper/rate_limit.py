"""令牌桶限流器，带随机抖动，避免请求过于规律被识别为机器人。

sleep 在锁外进行：多线程下各线程领取自己的"放行时刻"后立即释放锁，
等待互不阻塞，吞吐由 min_interval 精确控制。
"""
import random
import threading
import time


class RateLimiter:
    def __init__(self, min_interval: float = 0.3, jitter: float = 0.4):
        """min_interval: 两次请求之间的最小间隔秒数; jitter: 额外随机延迟上限."""
        self.min_interval = min_interval
        self.jitter = jitter
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        delay = self.min_interval + random.uniform(0, self.jitter)
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + delay
        sleep_for = slot - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)


def backoff_sleep(attempt: int, base: float = 5.0, cap: float = 120.0):
    """指数退避 + 抖动: attempt 从 0 开始。"""
    t = min(cap, base * (2 ** attempt))
    time.sleep(t * random.uniform(0.7, 1.3))
