#!/usr/bin/env python3
"""命令行入口。

示例:
  python cli.py daily 2026-06-04          # 抓指定日期
  python cli.py daily today               # 抓今天
  python cli.py weekly 2026-W23           # 抓指定周
  python cli.py weekly this               # 抓本周
  python cli.py monthly 2026-06           # 抓指定月
  python cli.py monthly this              # 抓本月
  python cli.py daily today -o out.xlsx --no-resolve
"""
import argparse
import logging
import sys
import time
from datetime import date

from ph_scraper.export import default_filename, export_any
from ph_scraper.rate_limit import RateLimiter
from ph_scraper.scraper import Cancelled, PHLeaderboardScraper, iso_week, ph_today
from ph_scraper.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def parse_target(period: str, target: str):
    today = ph_today()  # ProductHunt 以美国太平洋时间换日
    if period == "daily":
        if target in ("today", "now"):
            d = today
        else:
            d = date.fromisoformat(target)
        return {"year": d.year, "month": d.month, "day": d.day}, d.isoformat()
    if period == "weekly":
        if target in ("this", "now", "today"):
            y, w = today.isocalendar()[0], iso_week(today)
        else:
            y, w = target.upper().replace("W", "").split("-")
            y, w = int(y), int(w)
        return {"year": y, "week": w}, f"{y}-W{w}"
    if period == "monthly":
        if target in ("this", "now", "today"):
            y, m = today.year, today.month
        else:
            y, m = map(int, target.split("-"))
        return {"year": y, "month": m}, f"{y}-{m:02d}"
    raise ValueError(period)


def main():
    ap = argparse.ArgumentParser(description="ProductHunt 排行榜抓取器")
    ap.add_argument("period", choices=["daily", "weekly", "monthly"])
    ap.add_argument("target", help="日期/周/月，如 2026-06-04 / 2026-W23 / 2026-06，或 today/this")
    ap.add_argument("-o", "--output", default=None, help="输出 xlsx 路径")
    ap.add_argument("--no-resolve", action="store_true", help="不解析跳转后的真实链接（更快）")
    ap.add_argument("--min-interval", type=float, default=0.3, help="请求最小间隔秒数")
    ap.add_argument("--workers", type=int, default=8, help="真实链接解析并发数")
    ap.add_argument("--no-db", action="store_true", help="不写入本地 SQLite 产品库")
    ap.add_argument("--no-resume", action="store_true", help="忽略断点，从头抓取")
    args = ap.parse_args()

    kwargs, label = parse_target(args.period, args.target)
    out = args.output or default_filename(args.period, label)
    ckpt_key = None if args.no_resume else f"{args.period}_{label}"

    scraper = PHLeaderboardScraper(
        rate_limiter=RateLimiter(min_interval=args.min_interval, jitter=0.4),
        progress_cb=lambda msg: print("  " + msg, flush=True),
        resolve_workers=args.workers,
    )
    print(f"开始抓取 {args.period} {label} ...")
    t0 = time.monotonic()
    try:
        products = scraper.fetch_all(args.period, resolve_urls=not args.no_resolve,
                                     checkpoint_key=ckpt_key, **kwargs)
    except Cancelled as c:
        products = c.products
        print(f"已取消，保留 {len(products)} 条已抓数据")
    if not products:
        print("没有抓到任何产品！", file=sys.stderr)
        sys.exit(2)
    export_any(products, out, sheet_title=f"{args.period}_{label}")
    if not args.no_db:
        st = Store()
        st.task_id = st.task_start(args.period, label)
        st.upsert_products(products)
        st.task_finish(st.task_id, len(products))
        print(f"已写入产品库 {st.path}（库内共 {st.count()} 条）")
        st.close()
    dt = time.monotonic() - t0
    s = scraper.stats
    print(f"完成: {len(products)} 个产品 -> {out}")
    print(f"统计: 耗时 {dt / 60:.1f} 分钟, 请求 {s['requests']} 次, "
          f"429 限流 {s['http_429']} 次, 翻页 {s['pages']} 页")


if __name__ == "__main__":
    main()
