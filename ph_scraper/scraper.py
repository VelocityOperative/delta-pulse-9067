"""ProductHunt 排行榜爬虫核心。

流程:
1. 建立 Cloudflare 会话（手动 Cookie / 缓存会话 / 直连 TLS 指纹 / 自动浏览器 / FlareSolverr）;
2. POST /frontend/graphql 按 cursor 翻页抓取全部产品（每页 ~17 条），
   GraphQL 直接返回 product.websiteUrl（跳转后真实链接）;
3. 极少数缺失官网的产品回退解析 /r/p/<id> 重定向;
4. 支持取消（cancel_event）、断点续传（checkpoint_key）、结构化进度回调。
"""
import concurrent.futures
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import queries
from .cf_session import (CFSession, PH_BASE, clear_cached_session,
                         load_cached_session, load_manual_cookies,
                         save_cached_session, solve_via_browser)
from .proxy_pool import ProxyPool
from .rate_limit import RateLimiter, backoff_sleep

log = logging.getLogger(__name__)

GRAPHQL_URL = PH_BASE + "/frontend/graphql"

CHECKPOINT_EVERY_PAGES = 30


class Cancelled(Exception):
    """用户取消任务。携带已抓到的部分数据。"""

    def __init__(self, products):
        super().__init__("任务已取消")
        self.products = products


@dataclass
class Product:
    post_id: str = ""
    rank: str = ""
    name: str = ""
    tagline: str = ""           # 描述
    keywords: str = ""          # 关键词（全部话题）
    category: str = ""          # 分类（第一个话题）
    ph_url: str = ""            # producthunt 原始链接
    real_url: str = ""          # 跳转后真实链接
    votes: int = 0              # 票数
    comments: int = 0           # 评论数
    daily_rank: str = ""
    weekly_rank: str = ""
    monthly_rank: str = ""
    featured_at: str = ""       # 发布时间
    product_slug: str = ""
    post_slug: str = ""
    shortened_url: str = ""     # /r/p/<id>
    thumbnail: str = ""
    extra: dict = field(default_factory=dict)


def leaderboard_url(period: str, *, year: int, month: int | None = None,
                    day: int | None = None, week: int | None = None) -> str:
    if period == "daily":
        return f"{PH_BASE}/leaderboard/daily/{year}/{month}/{day}/all"
    if period == "weekly":
        return f"{PH_BASE}/leaderboard/weekly/{year}/{week}/all"
    if period == "monthly":
        return f"{PH_BASE}/leaderboard/monthly/{year}/{month}/all"
    raise ValueError(period)


def iso_week(d: date) -> int:
    return d.isocalendar()[1]


def _us_pacific_is_dst(d: date) -> bool:
    """美国夏令时区间：3 月第二个周日 ~ 11 月第一个周日。"""
    def nth_sunday(year: int, month: int, n: int) -> date:
        first = date(year, month, 1)
        first_sunday = 1 + (6 - first.weekday()) % 7
        return date(year, month, first_sunday + (n - 1) * 7)
    if d.month < 3 or d.month > 11:
        return False
    if 3 < d.month < 11:
        return True
    if d.month == 3:
        return d >= nth_sunday(d.year, 3, 2)
    return d < nth_sunday(d.year, 11, 1)  # 11 月


def pacific_now() -> datetime:
    """返回 ProductHunt 基准的美国太平洋时间当前时刻。

    ProductHunt 排行榜按太平洋时间每天 0 点重置，所以"今天/本周/本月"
    必须以太平洋时间为准，否则在中国(UTC+8)上午点"抓取今天"会查到
    一个在太平洋时间还没开始的日期，导致排行榜为空、抓取 0 条。
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        # 退化方案：手动估算（zoneinfo / tzdata 不可用时）
        utc = datetime.now(timezone.utc)
        off = -7 if _us_pacific_is_dst(utc.date()) else -8
        return utc + timedelta(hours=off)


def ph_today() -> date:
    """ProductHunt 排行榜意义上的"今天"（太平洋时间）。"""
    return pacific_now().date()


class PHLeaderboardScraper:
    def __init__(self, rate_limiter: RateLimiter | None = None,
                 proxy_pool: ProxyPool | None = None,
                 progress_cb=None, resolve_workers: int = 8,
                 cancel_event: threading.Event | None = None,
                 phase_cb=None, *, enable_browser_cf: bool = True,
                 browser_path: str = "", browser_timeout: int = 120,
                 request_timeout: int = 30, proxy: str = ""):
        """
        progress_cb(msg): 文本日志回调。
        phase_cb(phase, current, total): 结构化进度回调
            phase: "session" / "pages" / "resolve"; total 可能为 0（未知）。
        cancel_event: 置位后任务尽快停止，fetch_all 抛出 Cancelled（含已抓数据）。
        enable_browser_cf: 是否启用自动浏览器过 Cloudflare（设置项）。
        browser_path: 指定 Chrome/Edge 路径（设置项，留空自动查找）。
        browser_timeout: 浏览器自动验证最长等待秒数。
        request_timeout: 单次 HTTP 请求超时秒数。
        proxy: 代理地址，如 http://127.0.0.1:7890（留空不使用）。
        """
        self.rl = rate_limiter or RateLimiter(min_interval=0.3, jitter=0.4)
        # 重定向解析独立限流通道，避免与翻页互相排队
        self.resolve_rl = RateLimiter(min_interval=self.rl.min_interval,
                                      jitter=self.rl.jitter)
        self.proxy_pool = proxy_pool
        self.session: CFSession | None = None
        self.progress_cb = progress_cb or (lambda msg: None)
        self.phase_cb = phase_cb or (lambda phase, cur, total: None)
        self.resolve_workers = max(1, resolve_workers)
        self.cancel_event = cancel_event or threading.Event()
        self._session_lock = threading.Lock()
        self.stats = {"requests": 0, "http_429": 0, "pages": 0}
        self.enable_browser_cf = enable_browser_cf
        self.browser_path = browser_path
        self.browser_timeout = browser_timeout
        self.request_timeout = request_timeout
        self.proxy = proxy

    def _new_session(self, cookies: dict | None = None,
                     user_agent: str = "") -> CFSession:
        return CFSession(cookies=cookies, user_agent=user_agent,
                         timeout=self.request_timeout, proxy=self.proxy)

    def _check_cancel(self, products):
        if self.cancel_event.is_set():
            raise Cancelled(products)

    # ---------- 会话 ----------

    def ensure_session(self, ref_url: str):
        if self.session is not None:
            return
        self.phase_cb("session", 0, 1)

        def ok(s: CFSession) -> bool:
            r = s.get(ref_url)
            return r.status_code == 200 and "Just a moment" not in r.text[:3000]

        # 1) 手动 Cookie 模式（免 Docker）：exe 同目录放 ph_cookies.json
        manual = load_manual_cookies()
        if manual:
            self.progress_cb("使用手动 Cookie（ph_cookies.json）...")
            s = self._new_session(cookies=manual["cookies"], user_agent=manual["user_agent"])
            if ok(s):
                self.session = s
                self.progress_cb("Cloudflare 会话建立成功（手动 Cookie）")
                self.phase_cb("session", 1, 1)
                return
            self.progress_cb("手动 Cookie 已失效（请重新从浏览器复制），尝试其他方式...")
        # 2) 上次成功的会话缓存（24h 内有效则免重新验证）
        cached = load_cached_session()
        if cached:
            s = self._new_session(cookies=cached["cookies"], user_agent=cached["user_agent"])
            if ok(s):
                self.session = s
                self.progress_cb("Cloudflare 会话建立成功（复用缓存会话）")
                self.phase_cb("session", 1, 1)
                return
            clear_cached_session()
        # 3) 直接裸连：住宅/家用 IP 下 TLS 指纹模拟通常可直接通过
        self.progress_cb("尝试直接 TLS 指纹连接...")
        s = self._new_session()
        if ok(s):
            self.session = s
            save_cached_session(s.cookies, s.user_agent)
            self.progress_cb("Cloudflare 会话建立成功（直连）")
            self.phase_cb("session", 1, 1)
            return
        # 4) 自动浏览器（DrissionPage：自动启动 Chrome/Edge 过验证，无需 Docker）
        if self.enable_browser_cf:
            self.progress_cb("直连被拦截，尝试启动浏览器自动验证...")
            browser_sol = solve_via_browser(ref_url, timeout=self.browser_timeout,
                                            progress_cb=self.progress_cb,
                                            browser_path=self.browser_path)
            if browser_sol:
                s = self._new_session(cookies=browser_sol["cookies"],
                                      user_agent=browser_sol["user_agent"])
                if ok(s):
                    self.session = s
                    save_cached_session(s.cookies, s.user_agent)
                    self.progress_cb("Cloudflare 会话建立成功（自动浏览器验证）")
                    self.phase_cb("session", 1, 1)
                    return
                self.progress_cb("浏览器获取的 Cookie 验证失败，尝试 FlareSolverr...")
        else:
            self.progress_cb("自动浏览器验证已在设置中关闭，跳过")
        # 5) FlareSolverr（需要 Docker）
        self.progress_cb("尝试 FlareSolverr 过 Cloudflare 验证...")
        self.session = CFSession.from_flaresolverr(
            ref_url, timeout=self.request_timeout, proxy=self.proxy)
        if self.session is None:
            raise RuntimeError(
                "无法通过 Cloudflare 验证。所有方式均失败：\n"
                "1) 直连：当前 IP 被 Cloudflare 拦截\n"
                "2) 自动浏览器：未安装 DrissionPage 或浏览器验证失败\n"
                "   → 安装: pip install DrissionPage\n"
                "3) FlareSolverr：Docker 未安装或启动失败\n\n"
                "推荐: pip install DrissionPage 后重试（自动用 Chrome/Edge 过验证）")
        save_cached_session(self.session.cookies, self.session.user_agent)
        self.progress_cb("Cloudflare 会话建立成功（FlareSolverr）")
        self.phase_cb("session", 1, 1)

    # ---------- 抓取 ----------

    def fetch_all(self, period: str, *, year: int, month: int | None = None,
                  day: int | None = None, week: int | None = None,
                  resolve_urls: bool = True,
                  include_featured_only: bool = False,
                  checkpoint_key: str | None = None) -> list[Product]:
        """抓取指定 日/周/月 排行榜的全部产品。

        checkpoint_key: 提供后每 30 页保存断点，重跑时自动续传，完成后清除。
        """
        from . import store  # 延迟导入避免循环依赖

        ref = leaderboard_url(period, year=year, month=month, day=day, week=week)
        self.ensure_session(ref)

        products: list[Product] = []
        seen_ids: set[str] = set()
        cursor = None
        page = 0

        if checkpoint_key:
            ckpt = store.load_checkpoint(checkpoint_key)
            if ckpt and ckpt.get("products"):
                products = ckpt["products"]
                cursor = ckpt.get("cursor")
                seen_ids = {p.post_id for p in products}
                self.progress_cb(
                    f"发现断点：已抓 {len(products)} 条，从断点继续...")

        while True:
            self._check_cancel(products)
            page += 1
            self.rl.wait()
            variables = queries.build_variables(
                period, year=year, month=month, day=day, week=week,
                cursor=cursor, featured=include_featured_only)
            payload = queries.build_payload(period, variables)
            data = self._graphql(payload, ref, products)
            errs = data.get("errors")
            hf = (data.get("data") or {}).get("homefeedItems")
            if not hf:
                if errs:
                    self.progress_cb(f"GraphQL 返回错误（接口可能已变动）: {str(errs)[:200]}")
                elif page == 1:
                    self.progress_cb(
                        "该日期排行榜暂无数据。注意：ProductHunt 按【美国太平洋时间】"
                        "每天 0 点更新，今天(太平洋时间)可能尚未开始，可改抓前一天。")
                log.warning("homefeedItems 为空: %s", str(data)[:300])
                break
            edges = hf.get("edges", [])
            new = 0
            skipped_ad = 0
            for e in edges:
                node = e.get("node") or {}
                if node.get("__typename") not in (None, "Post"):
                    continue
                pid = str(node.get("id", ""))
                if not pid or pid in seen_ids:
                    continue
                # 跳过推广位（广告）：广告节点没有 /r/p/ 跳转链接
                if not node.get("shortenedUrl"):
                    skipped_ad += 1
                    continue
                seen_ids.add(pid)
                products.append(self._node_to_product(node))
                new += 1
            pi = hf.get("pageInfo", {})
            cursor = pi.get("endCursor")
            self.stats["pages"] = page
            self.progress_cb(f"第 {page} 页: +{new} 条 (累计 {len(products)})")
            # 第一页拿到原始条目却 0 新增时，说明问题出在过滤/去重而非空榜，给出诊断
            if page == 1 and new == 0 and edges:
                self.progress_cb(
                    f"诊断：本页返回 {len(edges)} 个原始条目，但因广告过滤({skipped_ad})/"
                    f"去重后新增 0 条。若持续如此请把日志发我。")
            self.phase_cb("pages", len(products), 0)
            if checkpoint_key and page % CHECKPOINT_EVERY_PAGES == 0:
                store.save_checkpoint(checkpoint_key, cursor, products)
            if not pi.get("hasNextPage") or not cursor or new == 0:
                break

        if resolve_urls:
            self._resolve_real_urls(products)
        if checkpoint_key:
            store.clear_checkpoint(checkpoint_key)
        return products

    def _graphql(self, payload: dict, referer: str,
                 partial: list | None = None) -> dict:
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": PH_BASE,
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
            "X-PH-Timezone": "America/Los_Angeles",
            "X-PH-Referer": "",
        }
        for attempt in range(5):
            self._check_cancel(partial or [])
            try:
                self.stats["requests"] += 1
                r = self.session.post(GRAPHQL_URL, json=payload, headers=headers)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (403, 503):
                    # Cloudflare 会话过期，重新解决挑战
                    self.progress_cb("会话失效，重新通过 Cloudflare 验证...")
                    clear_cached_session()
                    self.session = None
                    self.ensure_session(referer)
                elif r.status_code == 429:
                    self.stats["http_429"] += 1
                    self.progress_cb(f"被限流(429)，指数退避中(第{attempt + 1}次)...")
                    backoff_sleep(attempt)
                else:
                    log.warning("GraphQL HTTP %s: %s", r.status_code, r.text[:200])
                    time.sleep(5)
            except Cancelled:
                raise
            except Exception as e:
                log.warning("GraphQL 异常(第%d次): %s", attempt + 1, e)
                time.sleep(5)
        raise RuntimeError("GraphQL 请求连续失败")

    @staticmethod
    def _node_to_product(node: dict) -> Product:
        topics = [t["node"]["name"] for t in (node.get("topics") or {}).get("edges", [])
                  if t.get("node")]
        product = node.get("product") or {}
        product_slug = product.get("slug") or ""
        website = clean_ref(product.get("websiteUrl") or "")
        post_slug = node.get("slug") or ""
        ph_url = (f"{PH_BASE}/products/{product_slug}" if product_slug
                  else f"{PH_BASE}/posts/{post_slug}")
        return Product(
            post_id=str(node.get("id") or ""),
            name=(node.get("name") or "").strip(),
            tagline=(node.get("tagline") or "").strip(),
            keywords=", ".join(topics),
            category=topics[0] if topics else "",
            ph_url=ph_url,
            real_url=website,
            votes=int(node.get("latestScore") or 0),
            comments=int(node.get("commentsCount") or 0),
            daily_rank=str(node.get("dailyRank") or ""),
            weekly_rank=str(node.get("weeklyRank") or ""),
            monthly_rank=str(node.get("monthlyRank") or ""),
            featured_at=node.get("featuredAt") or node.get("createdAt") or "",
            product_slug=product_slug,
            post_slug=post_slug,
            shortened_url=node.get("shortenedUrl") or "",
            thumbnail=("https://ph-files.imgix.net/" + node["thumbnailImageUuid"])
                      if node.get("thumbnailImageUuid") else "",
        )

    # ---------- 真实链接解析 ----------

    def _resolve_real_urls(self, products: list[Product]):
        # GraphQL 已直接返回 product.websiteUrl，只对缺失的少数产品解析重定向
        todo = [p for p in products if not p.real_url and p.shortened_url]
        n_direct = len(products) - len(todo)
        self.progress_cb(f"真实链接: {n_direct}/{len(products)} 已由 GraphQL 直接返回，"
                         f"另 {len(todo)} 条需解析重定向")
        total = len(todo)
        if not total:
            return
        done = [0]
        lock = threading.Lock()

        def work(p: Product):
            if self.cancel_event.is_set():
                return
            p.real_url = self._resolve_redirect(PH_BASE + p.shortened_url)
            with lock:
                done[0] += 1
                n = done[0]
            self.progress_cb(f"解析真实链接 {n}/{total}: {p.name} -> {p.real_url}")
            self.phase_cb("resolve", n, total)

        with concurrent.futures.ThreadPoolExecutor(self.resolve_workers) as ex:
            list(ex.map(work, todo))
        self._check_cancel(products)

    def _resolve_redirect(self, url: str) -> str:
        for attempt in range(4):
            if self.cancel_event.is_set():
                return ""
            self.resolve_rl.wait()
            try:
                self.stats["requests"] += 1
                session = self.session
                r = session.get(url, allow_redirects=False)
                loc = r.headers.get("location") or r.headers.get("Location")
                if r.status_code in (301, 302, 303, 307, 308) and loc:
                    return clean_ref(loc)
                if r.status_code in (403, 503):
                    with self._session_lock:
                        if self.session is session:
                            self.session = None
                            self.ensure_session(PH_BASE)
                    continue
                if r.status_code == 429:
                    self.stats["http_429"] += 1
                    backoff_sleep(attempt)
                    continue
                # 没有重定向头，尝试从页面 meta refresh 提取
                m = re.search(r'url=([^">\s]+)', r.text[:2000], re.I)
                if m:
                    return clean_ref(m.group(1))
            except Exception as e:
                log.warning("解析重定向失败 %s: %s", url, e)
                time.sleep(3)
        return ""


def clean_ref(url: str) -> str:
    """去掉 ProductHunt 加的 ref/utm 跟踪参数。"""
    while re.search(r'([?&])(ref=producthunt|utm_[a-z]+=[^&]*)', url):
        url = re.sub(r'([?&])(ref=producthunt|utm_[a-z]+=[^&]*)&?', r'\1', url)
    return url.rstrip('?&')
