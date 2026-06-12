# ProductHunt 排行榜抓取器（桌面程序）

按 **日 / 周 / 月** 抓取 ProductHunt 排行榜（`/all` 全量榜单）的全部产品，并导出 Excel。

支持的榜单 URL 形式：
- 日榜：`https://www.producthunt.com/leaderboard/daily/2026/6/4/all`
- 周榜：`https://www.producthunt.com/leaderboard/weekly/2026/23/all`
- 月榜：`https://www.producthunt.com/leaderboard/monthly/2026/6/all`

导出字段：排名、产品名称、描述、关键词、分类、ProductHunt 原始链接、**跳转后真实链接**（已去除 `?ref=producthunt`）、票数、评论数、日/周/月排名、发布时间、缩略图链接。

## 工作原理（网站分析结论）

1. **Cloudflare 防护**：ProductHunt 全站（含其内部 GraphQL 接口）由 Cloudflare Turnstile 防护。
   实测 10000+ 个免费代理 **无一** 能直接通过盘问，因此本程序用
   [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)（开源，本地 Docker 运行）自动过盘问，
   拿到 `cf_clearance` cookie 后，用 `curl_cffi` 的 Chrome TLS 指纹模拟发起请求。
2. **数据接口**：排行榜数据来自 ProductHunt 内部 GraphQL（`/frontend/graphql`），
   程序使用精简查询（只取需要的字段，响应体积减 60%+，见 `ph_scraper/queries.py`），
   并按 APQ 规范附带查询文本的 sha256 哈希，按 cursor 翻页直到 `hasNextPage=false`，保证抓全。
3. **真实链接**：GraphQL 直接返回 `product.websiteUrl`（产品官网），无需逐条请求跳转——
   2 万条的月榜约 13 分钟抓完；极少数缺失官网的产品自动回退解析 `/r/p/<id>` 重定向。
   所有链接自动去除 `ref=producthunt` / `utm_*` 跟踪参数。
4. **限流**：翻页与重定向解析各自独立的令牌桶限流通道（sleep 在锁外，多线程不串行），
   遇 429 指数退避 + 随机抖动，会话失效自动重新过 Cloudflare。
5. **免费代理池**：`ph_scraper/proxy_pool.py` 从 4 个公开代理源拉取并验证代理，
   可用于分散真实链接解析请求 / 本机 IP 被封时的备用通道（注意：免费代理过不了 PH 的 Cloudflare，主通道始终是 FlareSolverr）。

## v2 新增功能

- **断点续传**：每 30 页自动保存断点（`~/.ph_scraper/checkpoints/`），中途中断后重跑同一目标自动续传；`--no-resume` 可从头抓。
- **SQLite 产品库**：所有抓到的产品按 post id 去重累积到 `~/.ph_scraper/products.db`，可用任意 SQLite 工具查询历史数据；`--no-db` 可关闭。
- **会话缓存**：成功的 Cloudflare 会话缓存 24 小时（`~/.ph_scraper/session.json`），启动免重新验证。
- **GUI**：进度条 + 可随时停止（保留已抓数据可导出）、日期下拉选择、结果表格预览（可排序、双击打开链接）、Cookie 引导向导、记住上次设置。
- **导出格式**：Excel / CSV / JSON（按扩展名自动选择）。
- **单元测试**：`python -m unittest discover tests`。

## 过 Cloudflare 验证的几种方式（程序自动按顺序尝试：手动 Cookie → 缓存会话 → 直连 → 自动浏览器 → FlareSolverr）

1. **直连（零配置）**：用 Chrome TLS 指纹直接请求，住宅/家用宽带 IP 通常直接放行。
2. **自动浏览器（推荐，零配置）**：自动启动电脑上的 Chrome 或 Edge 浏览器过 Cloudflare 验证，
   全自动提取 Cookie，无需任何手动操作。浏览器窗口会短暂弹出，验证完自动关闭。
   需要 DrissionPage：`pip install DrissionPage`（打包 exe 已内置）。
3. **FlareSolverr（需 Docker，全自动续期）**：本地容器自动过验证，适合长期挂机定时跑。
4. **手动 Cookie（最后手段）**：在 exe（或项目根目录）旁放一个 `ph_cookies.json`（GUI 里也有引导向导自动生成）：
   ```json
   {
     "cookie": "从浏览器复制的完整 Cookie 串，必须包含 cf_clearance=...",
     "user_agent": "你浏览器的 User-Agent（须与拿 cookie 的浏览器一致）"
   }
   ```

## 安装

```bash
# 1. Python 3.10+
pip install -r requirements.txt

# 2. Docker（用于运行 FlareSolverr；程序会自动拉起容器，也可手动启动）：
docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
```

## 使用

### 桌面 GUI

```bash
python gui.py
```

- 点「抓取今天 / 本周 / 本月」一键抓取并导出；
- 或选日/周/月榜后用下拉框选择年/月/日（周榜显示对应日期范围）；
- 抓取中可随时点「停止」，已抓数据仍可导出；
- 抓完自动切到「结果预览」表格（点列头排序、双击行打开产品官网），「导出...」支持 xlsx/csv/json。

### 命令行

```bash
python cli.py daily 2026-06-04        # 指定日期
python cli.py daily today             # 今天
python cli.py weekly 2026-W23         # 指定周（ISO 周号）
python cli.py weekly this             # 本周
python cli.py monthly 2026-06         # 指定月
python cli.py monthly this            # 本月
python cli.py daily today -o 输出.csv               # 按扩展名导出 CSV/JSON/Excel
python cli.py monthly 2026-06 --min-interval 0.15   # 更快的翻页间隔
python cli.py monthly 2026-06 --no-resume --no-db   # 从头抓、不写产品库
```

### 每天自动抓取（Windows 任务计划程序）

```powershell
schtasks /Create /SC DAILY /ST 23:30 /TN "PH日榜" /TR "C:\path\ph-scraper-cli.exe daily today -o C:\data\ph_%date%.xlsx"
```

## 文件结构

```
producthunt-scraper/
├── gui.py                  # 桌面 GUI 入口 (Tkinter)
├── cli.py                  # 命令行入口
├── requirements.txt
├── tests/                  # 单元测试
└── ph_scraper/
    ├── cf_session.py       # Cloudflare 过盘问 + TLS 指纹会话 + 会话缓存
    ├── queries.py          # 精简 GraphQL 查询 (APQ)
    ├── scraper.py          # 核心爬虫：翻页抓取 + 真实链接 + 取消/断点
    ├── store.py            # SQLite 产品库 + 断点续传
    ├── paths.py            # 应用数据目录 ~/.ph_scraper
    ├── proxy_pool.py       # 免费代理池
    ├── rate_limit.py       # 限流器（锁外 sleep + 指数退避）
    └── export.py           # Excel / CSV / JSON 导出
```

## 注意事项

- 周号为 ISO 周（与 ProductHunt 的 weekly URL 一致）。
- 若 ProductHunt 更新了 GraphQL schema 导致查询失效，修改 `ph_scraper/queries.py` 中的字段即可。
- 默认抓的是 `/all`（全部产品，含未 Featured 的）；月榜可达 2 万+条，约 13 分钟。
