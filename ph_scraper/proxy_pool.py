"""免费代理池。

从多个公开免费代理源拉取代理列表，验证可用性后轮换使用。

注意：实测（10000+ 免费代理全量扫描）几乎没有免费代理能通过
ProductHunt 的 Cloudflare 盘问，所以代理池主要用于：
1. 解析"跳转后真实链接"时分散请求（目标站点大多无 Cloudflare）；
2. 当本机 IP 被 ProductHunt 限流/封禁时作为备用通道再试一次。
主通道始终是 本机IP + FlareSolverr cookies + TLS 指纹模拟。
"""
import concurrent.futures
import logging
import random
import threading

from curl_cffi import requests as creq

log = logging.getLogger(__name__)

PROXY_SOURCES = [
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=http&proxy_format=protocolipport&format=text&timeout=5000",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
]

TEST_URL = "https://httpbin.org/ip"


class ProxyPool:
    def __init__(self, max_pool: int = 20):
        self.max_pool = max_pool
        self._working: list[str] = []
        self._lock = threading.Lock()

    def fetch_candidates(self) -> list[str]:
        out = []
        for src in PROXY_SOURCES:
            try:
                r = creq.get(src, timeout=20, impersonate="chrome")
                for line in r.text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if "://" not in line:
                        line = "http://" + line
                    out.append(line)
            except Exception as e:
                log.warning("代理源失败 %s: %s", src, e)
        random.shuffle(out)
        return list(dict.fromkeys(out))

    @staticmethod
    def _check(proxy: str) -> str | None:
        try:
            r = creq.get(TEST_URL, proxies={"http": proxy, "https": proxy},
                         timeout=8, impersonate="chrome")
            if r.status_code == 200:
                return proxy
        except Exception:
            pass
        return None

    def build(self, limit: int = 500, workers: int = 50):
        """验证候选代理，填充可用池。"""
        cands = self.fetch_candidates()[:limit]
        log.info("验证 %d 个候选代理...", len(cands))
        with concurrent.futures.ThreadPoolExecutor(workers) as ex:
            for res in ex.map(self._check, cands):
                if res:
                    with self._lock:
                        if len(self._working) < self.max_pool:
                            self._working.append(res)
                        else:
                            break
        log.info("可用代理 %d 个", len(self._working))

    def get(self) -> str | None:
        with self._lock:
            return random.choice(self._working) if self._working else None

    def drop(self, proxy: str):
        with self._lock:
            if proxy in self._working:
                self._working.remove(proxy)
