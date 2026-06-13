#!/usr/bin/env python3
"""ProductHunt 排行榜抓取器 - 桌面程序 (Tkinter)。

功能:
- 日榜 / 周榜 / 月榜，目标用下拉框选择（无格式错误）
- 进度条 + 可取消（取消后已抓数据仍可导出）
- 结果表格预览（可排序、双击打开链接），导出 Excel / CSV / JSON
- 断点续传、SQLite 产品库去重累积
- Cloudflare 全部失败时弹 Cookie 引导向导
- 记住上次的设置（~/.ph_scraper/config.json）
"""
import json
import logging
import queue
import sys
import threading
import webbrowser
from datetime import date, timedelta
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

from ph_scraper.export import default_filename, export_any
from ph_scraper.paths import config_file
from ph_scraper.rate_limit import RateLimiter
from ph_scraper.scraper import Cancelled, PHLeaderboardScraper, iso_week, ph_today
from ph_scraper.store import Store

logging.basicConfig(level=logging.INFO)

COLS = [("name", "产品名称", 160), ("tagline", "描述", 260),
        ("category", "分类", 100), ("votes", "票数", 60),
        ("real_url", "真实链接", 220), ("ph_url", "PH链接", 200)]


def _enable_dpi_awareness():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def week_range(year: int, week: int) -> str:
    try:
        start = date.fromisocalendar(year, week, 1)
        end = start + timedelta(days=6)
        return f"{start.month}/{start.day}–{end.month}/{end.day}"
    except ValueError:
        return ""


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ProductHunt 排行榜抓取器")
        self.geometry("900x640")
        self._q = queue.Queue()
        self._worker = None
        self._cancel = threading.Event()
        self._products = []
        self._cfg = self._load_cfg()
        self._build()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 配置 ----------

    def _load_cfg(self) -> dict:
        try:
            return json.loads(config_file().read_text(encoding="utf-8"))
        except Exception:
            return {}

    # 设置项默认值
    _DEFAULTS = {
        "interval": 0.3,
        "workers": 8,
        "export_dir": "",
        "browser_path": "",
        "enable_browser": True,
        "request_timeout": 30,
        "browser_timeout": 120,
        "proxy": "",
        "auto_export": True,
        "auto_fallback": True,
    }

    def _cfg_get(self, key):
        val = self._cfg.get(key)
        return self._DEFAULTS.get(key) if val is None else val

    def _save_cfg(self):
        try:
            data = {k: self._cfg_get(k) for k in self._DEFAULTS}
            try:
                data["interval"] = self.interval.get()
                data["workers"] = self.workers.get()
            except Exception:
                pass
            self._cfg.update(data)
            config_file().write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _on_close(self):
        self._cancel.set()
        self._save_cfg()
        self.destroy()

    # ---------- UI ----------

    def _build(self):
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        top = ttk.LabelFrame(frm, text="抓取目标", padding=10)
        top.pack(fill="x")

        self.period = tk.StringVar(value="daily")
        for i, (lbl, val) in enumerate([("日榜", "daily"), ("周榜", "weekly"), ("月榜", "monthly")]):
            ttk.Radiobutton(top, text=lbl, value=val, variable=self.period,
                            command=self._on_period).grid(row=0, column=i, padx=6)

        today = ph_today()
        self.sel_year = tk.IntVar(value=today.year)
        self.sel_month = tk.IntVar(value=today.month)
        self.sel_day = tk.IntVar(value=today.day)
        self.sel_week = tk.IntVar(value=iso_week(today))

        self.date_frame = ttk.Frame(top)
        self.date_frame.grid(row=0, column=3, padx=(24, 0), sticky="w")
        self.week_hint = ttk.Label(top, text="")
        self.week_hint.grid(row=0, column=4, padx=8)
        self._on_period()

        quick = ttk.Frame(top)
        quick.grid(row=1, column=0, columnspan=6, pady=(10, 0), sticky="w")
        ttk.Button(quick, text="抓取今天", command=lambda: self._quick("daily")).pack(side="left", padx=4)
        ttk.Button(quick, text="抓取本周", command=lambda: self._quick("weekly")).pack(side="left", padx=4)
        ttk.Button(quick, text="抓取本月", command=lambda: self._quick("monthly")).pack(side="left", padx=4)

        opts = ttk.LabelFrame(frm, text="选项", padding=10)
        opts.pack(fill="x", pady=(10, 0))
        ttk.Label(opts, text="请求间隔(秒):").grid(row=0, column=0, padx=(0, 4))
        self.interval = tk.DoubleVar(value=self._cfg.get("interval", 0.3))
        ttk.Spinbox(opts, from_=0.1, to=10, increment=0.1, textvariable=self.interval,
                    width=6).grid(row=0, column=1)
        ttk.Label(opts, text="解析并发线程:").grid(row=0, column=2, padx=(20, 4))
        self.workers = tk.IntVar(value=self._cfg.get("workers", 8))
        ttk.Spinbox(opts, from_=1, to=32, increment=1, textvariable=self.workers,
                    width=6).grid(row=0, column=3)
        self.use_db = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="写入本地产品库(去重累积)",
                        variable=self.use_db).grid(row=0, column=4, padx=(20, 0))

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=10)
        self.start_btn = ttk.Button(btns, text="开始抓取", command=self._start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="停止", command=self._stop, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        self.export_btn = ttk.Button(btns, text="导出...", command=self._export, state="disabled")
        self.export_btn.pack(side="left", padx=8)
        self.settings_btn = ttk.Button(btns, text="设置", command=self._open_settings)
        self.settings_btn.pack(side="left", padx=8)
        self.status = ttk.Label(btns, text="就绪")
        self.status.pack(side="left", padx=12)

        self.progress = ttk.Progressbar(frm, mode="indeterminate")
        self.progress.pack(fill="x")

        nb = ttk.Notebook(frm)
        nb.pack(fill="both", expand=True, pady=(8, 0))

        logf = ttk.Frame(nb)
        nb.add(logf, text="日志")
        self.log = tk.Text(logf, height=16, state="disabled", font=("Consolas", 9))
        ls = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=ls.set)
        ls.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)

        tabf = ttk.Frame(nb)
        nb.add(tabf, text="结果预览")
        self.tree = ttk.Treeview(tabf, columns=[c for c, _, _ in COLS],
                                 show="headings")
        for cid, head, width in COLS:
            self.tree.heading(cid, text=head,
                              command=lambda c=cid: self._sort_tree(c, False))
            self.tree.column(cid, width=width, anchor="w")
        ts = ttk.Scrollbar(tabf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ts.set)
        ts.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self._open_link)
        self._nb = nb

    def _on_period(self):
        for w in self.date_frame.winfo_children():
            w.destroy()
        p = self.period.get()
        years = list(range(ph_today().year, 2012, -1))

        def combo(var, values, width=6):
            cb = ttk.Combobox(self.date_frame, values=values, width=width,
                              state="readonly")
            cb.set(var.get())
            cb.bind("<<ComboboxSelected>>",
                    lambda e, v=var, c=cb: (v.set(int(c.get())), self._update_hint()))
            cb.pack(side="left", padx=2)
            return cb

        ttk.Label(self.date_frame, text="年:").pack(side="left")
        combo(self.sel_year, years)
        if p == "daily":
            ttk.Label(self.date_frame, text="月:").pack(side="left")
            combo(self.sel_month, list(range(1, 13)), 4)
            ttk.Label(self.date_frame, text="日:").pack(side="left")
            combo(self.sel_day, list(range(1, 32)), 4)
        elif p == "weekly":
            ttk.Label(self.date_frame, text="周:").pack(side="left")
            combo(self.sel_week, list(range(1, 54)), 4)
        else:
            ttk.Label(self.date_frame, text="月:").pack(side="left")
            combo(self.sel_month, list(range(1, 13)), 4)
        self._update_hint()

    def _update_hint(self):
        if self.period.get() == "weekly":
            self.week_hint.config(
                text=f"(W{self.sel_week.get()}: "
                     f"{week_range(self.sel_year.get(), self.sel_week.get())})")
        else:
            self.week_hint.config(text="")

    def _quick(self, period):
        today = ph_today()
        self.period.set(period)
        self.sel_year.set(today.year)
        self.sel_month.set(today.month)
        self.sel_day.set(today.day)
        self.sel_week.set(iso_week(today))
        self._on_period()
        self._start()

    # ---------- 日志/进度 ----------

    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "status":
                    self.status.config(text=payload)
                elif kind == "phase":
                    phase, cur, total = payload
                    if phase == "pages":
                        self.progress.configure(mode="indeterminate")
                        self.status.config(text=f"抓取中... 已抓 {cur} 条")
                    elif phase == "resolve" and total:
                        self.progress.stop()
                        self.progress.configure(mode="determinate", maximum=total,
                                                value=cur)
                        self.status.config(text=f"解析链接 {cur}/{total} "
                                                f"({cur * 100 // total}%)")
                elif kind == "done":
                    products, cancelled = payload
                    self._finish(products, cancelled)
                elif kind == "error":
                    self._finish([], False)
                    self.status.config(text="出错")
                    self._cookie_wizard_or_error(payload)
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _finish(self, products, cancelled):
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._products = products
        self.start_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self.export_btn.config(state="normal" if products else "disabled")
        if products:
            note = "（已取消，部分数据）" if cancelled else ""
            self.status.config(text=f"完成，共 {len(products)} 个产品{note}")
            self.title(f"完成 {len(products)} 条 — PH抓取器")
            self._fill_tree(products)
            self._nb.select(1)
            if self.use_db.get():
                try:
                    st = Store()
                    st.upsert_products(products)
                    self._log(f"已写入产品库（库内共 {st.count()} 条）: {st.path}")
                    st.close()
                except Exception as e:
                    self._log(f"写入产品库失败: {e}")
            if not cancelled and self._cfg_get("auto_export"):
                self._export()

    def _fill_tree(self, products):
        self.tree.delete(*self.tree.get_children())
        for p in sorted(products, key=lambda x: -x.votes):
            self.tree.insert("", "end", values=(
                p.name, p.tagline, p.category, p.votes, p.real_url, p.ph_url))

    def _sort_tree(self, col, reverse):
        items = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        try:
            items.sort(key=lambda t: int(t[0]), reverse=reverse)
        except ValueError:
            items.sort(reverse=reverse)
        for i, (_, k) in enumerate(items):
            self.tree.move(k, "", i)
        self.tree.heading(col, command=lambda: self._sort_tree(col, not reverse))

    def _open_link(self, event):
        item = self.tree.focus()
        if not item:
            return
        vals = self.tree.item(item, "values")
        url = vals[4] or vals[5]
        if url:
            webbrowser.open(url)

    # ---------- 抓取 ----------

    def _target(self):
        p = self.period.get()
        y = self.sel_year.get()
        if p == "daily":
            m, d = self.sel_month.get(), self.sel_day.get()
            date(y, m, d)  # 校验日期合法
            return {"year": y, "month": m, "day": d}, f"{y}-{m:02d}-{d:02d}"
        if p == "weekly":
            w = self.sel_week.get()
            return {"year": y, "week": w}, f"{y}-W{w}"
        m = self.sel_month.get()
        return {"year": y, "month": m}, f"{y}-{m:02d}"

    def _start(self):
        if self._worker and self._worker.is_alive():
            return
        try:
            kwargs, label = self._target()
        except ValueError:
            messagebox.showerror("错误", "日期不合法（该月没有这一天）")
            return
        period = self.period.get()
        interval = self.interval.get()
        workers = self.workers.get()
        enable_browser = bool(self._cfg_get("enable_browser"))
        browser_path = str(self._cfg_get("browser_path") or "")
        request_timeout = int(self._cfg_get("request_timeout") or 30)
        browser_timeout = int(self._cfg_get("browser_timeout") or 120)
        proxy = str(self._cfg_get("proxy") or "")
        auto_fallback = bool(self._cfg_get("auto_fallback"))
        self._cancel = threading.Event()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.export_btn.config(state="disabled")
        self.status.config(text="抓取中...")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self._log(f"=== 开始抓取 {period} {label} ===")
        self._label = label
        cancel = self._cancel

        def run():
            try:
                scraper = PHLeaderboardScraper(
                    rate_limiter=RateLimiter(min_interval=interval, jitter=0.4),
                    progress_cb=lambda m: self._q.put(("log", m)),
                    phase_cb=lambda ph, c, t: self._q.put(("phase", (ph, c, t))),
                    resolve_workers=workers,
                    cancel_event=cancel,
                    enable_browser_cf=enable_browser,
                    browser_path=browser_path,
                    browser_timeout=browser_timeout,
                    request_timeout=request_timeout,
                    proxy=proxy,
                )
                products = scraper.fetch_all(
                    period, checkpoint_key=f"{period}_{label}", **kwargs)
                # 日榜抓到 0 条且开启了自动回退：改抓太平洋时间前一天
                if (not products and period == "daily" and auto_fallback
                        and not cancel.is_set() and "day" in kwargs):
                    try:
                        prev = date(kwargs["year"], kwargs["month"],
                                    kwargs["day"]) - timedelta(days=1)
                        self._q.put(("log",
                            f"今天(太平洋时间)榜单暂无数据，自动改抓前一天 "
                            f"{prev.year}-{prev.month:02d}-{prev.day:02d} ..."))
                        self._q.put(("log",
                            "=== 自动回退抓取 daily "
                            f"{prev.year}-{prev.month:02d}-{prev.day:02d} ==="))
                        fb_label = f"{prev.year}-{prev.month:02d}-{prev.day:02d}"
                        products = scraper.fetch_all(
                            period, checkpoint_key=f"{period}_{fb_label}",
                            year=prev.year, month=prev.month, day=prev.day)
                    except Exception as fe:
                        self._q.put(("log", f"自动回退失败: {fe}"))
                self._q.put(("done", (products, False)))
            except Cancelled as c:
                self._q.put(("done", (c.products, True)))
            except Exception as e:
                self._q.put(("error", str(e)))

        self._worker = threading.Thread(target=run, daemon=True)
        self._worker.start()

    def _stop(self):
        self._cancel.set()
        self.status.config(text="正在停止...")
        self.cancel_btn.config(state="disabled")

    # ---------- 导出 ----------

    def _export(self):
        if not self._products:
            return
        period = self.period.get()
        label = getattr(self, "_label", "export")
        fn = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            initialdir=self._cfg.get("export_dir") or None,
            initialfile=default_filename(period, label),
            filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv"), ("JSON", "*.json")])
        if not fn:
            return
        export_any(self._products, fn, sheet_title=f"{period}_{label}")
        import os
        self._cfg["export_dir"] = os.path.dirname(fn)
        self._save_cfg()
        self._log(f"已导出: {fn}")
        self.status.config(text=f"已导出 {len(self._products)} 条 -> {fn}")

    # ---------- 设置 ----------

    def _open_settings(self):
        SettingsDialog(self)

    # ---------- Cookie 向导 ----------

    def _cookie_wizard_or_error(self, msg: str):
        if "Cloudflare" not in msg:
            messagebox.showerror("错误", msg)
            return
        if not messagebox.askyesno(
                "Cloudflare 验证失败",
                "所有自动验证方式均失败（含自动浏览器验证）。\n\n"
                "请确认电脑上已安装 Chrome 或 Edge 浏览器后重试。\n\n"
                "是否打开「手动 Cookie 向导」作为最后手段？"):
            return
        CookieWizard(self)


class CookieWizard(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("手动 Cookie 向导")
        self.geometry("680x420")
        self.grab_set()
        pad = {"padx": 12, "pady": 4}
        steps = (
            "1. 点击下方按钮，用浏览器打开 producthunt.com 并等待页面正常显示；\n"
            "2. 按 F12 打开开发者工具 → Network(网络) → 刷新页面 → 点第一个请求；\n"
            "3. 在 Request Headers 里完整复制 cookie 一行的值，粘贴到下面第一个框；\n"
            "4. 同样复制 user-agent 的值，粘贴到第二个框，然后点「保存」。")
        ttk.Label(self, text=steps, justify="left").pack(anchor="w", **pad)
        ttk.Button(self, text="打开 producthunt.com",
                   command=lambda: webbrowser.open("https://www.producthunt.com")
                   ).pack(anchor="w", **pad)
        ttk.Label(self, text="Cookie（须包含 cf_clearance=...）:").pack(anchor="w", **pad)
        self.cookie = tk.Text(self, height=6)
        self.cookie.pack(fill="x", **pad)
        ttk.Label(self, text="User-Agent:").pack(anchor="w", **pad)
        self.ua = tk.Text(self, height=2)
        self.ua.pack(fill="x", **pad)
        ttk.Button(self, text="保存", command=self._save).pack(**pad)

    def _save(self):
        cookie = self.cookie.get("1.0", "end").strip()
        ua = self.ua.get("1.0", "end").strip()
        if "cf_clearance" not in cookie:
            messagebox.showerror("错误", "Cookie 中缺少 cf_clearance", parent=self)
            return
        from pathlib import Path
        target = (Path(sys.executable).parent if getattr(sys, "frozen", False)
                  else Path(__file__).resolve().parent) / "ph_cookies.json"
        target.write_text(json.dumps({"cookie": cookie, "user_agent": ua},
                                     ensure_ascii=False, indent=1), encoding="utf-8")
        messagebox.showinfo("已保存", f"已写入 {target}\n请重新点击「开始抓取」。",
                            parent=self)
        self.destroy()


class SettingsDialog(tk.Toplevel):
    """常规设置面板：浏览器路径、超时、代理、自动验证开关、导出/回退等。"""

    def __init__(self, master: "App"):
        super().__init__(master)
        self.app = master
        self.title("设置")
        self.geometry("620x460")
        self.grab_set()
        self.resizable(False, False)
        g = master._cfg_get

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        # --- 浏览器自动验证 ---
        bf = ttk.LabelFrame(frm, text="Cloudflare 自动浏览器验证", padding=10)
        bf.pack(fill="x")
        self.enable_browser = tk.BooleanVar(value=bool(g("enable_browser")))
        ttk.Checkbutton(bf, text="启用自动浏览器验证（直连失败时自动启动 Chrome/Edge 过验证）",
                        variable=self.enable_browser).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(bf, text="浏览器路径(留空自动查找):").grid(
            row=1, column=0, sticky="w", pady=(8, 0))
        self.browser_path = tk.StringVar(value=str(g("browser_path") or ""))
        ttk.Entry(bf, textvariable=self.browser_path, width=46).grid(
            row=1, column=1, pady=(8, 0))
        ttk.Button(bf, text="浏览...", command=self._pick_browser).grid(
            row=1, column=2, padx=(6, 0), pady=(8, 0))
        ttk.Label(bf, text="浏览器验证超时(秒):").grid(
            row=2, column=0, sticky="w", pady=(8, 0))
        self.browser_timeout = tk.IntVar(value=int(g("browser_timeout") or 120))
        ttk.Spinbox(bf, from_=30, to=600, increment=10,
                    textvariable=self.browser_timeout, width=8).grid(
            row=2, column=1, sticky="w", pady=(8, 0))

        # --- 网络 ---
        nf = ttk.LabelFrame(frm, text="网络", padding=10)
        nf.pack(fill="x", pady=(10, 0))
        ttk.Label(nf, text="请求超时(秒):").grid(row=0, column=0, sticky="w")
        self.request_timeout = tk.IntVar(value=int(g("request_timeout") or 30))
        ttk.Spinbox(nf, from_=5, to=300, increment=5,
                    textvariable=self.request_timeout, width=8).grid(
            row=0, column=1, sticky="w")
        ttk.Label(nf, text="代理(可选, 如 http://127.0.0.1:7890):").grid(
            row=1, column=0, sticky="w", pady=(8, 0))
        self.proxy = tk.StringVar(value=str(g("proxy") or ""))
        ttk.Entry(nf, textvariable=self.proxy, width=40).grid(
            row=1, column=1, sticky="w", pady=(8, 0))

        # --- 输出与行为 ---
        of = ttk.LabelFrame(frm, text="输出与行为", padding=10)
        of.pack(fill="x", pady=(10, 0))
        ttk.Label(of, text="默认导出目录:").grid(row=0, column=0, sticky="w")
        self.export_dir = tk.StringVar(value=str(g("export_dir") or ""))
        ttk.Entry(of, textvariable=self.export_dir, width=40).grid(
            row=0, column=1, sticky="w")
        ttk.Button(of, text="浏览...", command=self._pick_dir).grid(
            row=0, column=2, padx=(6, 0))
        self.auto_export = tk.BooleanVar(value=bool(g("auto_export")))
        ttk.Checkbutton(of, text="抓取完成后自动弹出导出窗口",
                        variable=self.auto_export).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.auto_fallback = tk.BooleanVar(value=bool(g("auto_fallback")))
        ttk.Checkbutton(
            of, text="日榜「今天」无数据时自动改抓前一天（太平洋时间换日）",
            variable=self.auto_fallback).grid(
            row=2, column=0, columnspan=3, sticky="w")

        bbar = ttk.Frame(frm)
        bbar.pack(fill="x", pady=(14, 0))
        ttk.Button(bbar, text="保存", command=self._save).pack(side="right")
        ttk.Button(bbar, text="取消", command=self.destroy).pack(
            side="right", padx=8)
        ttk.Button(bbar, text="恢复默认", command=self._reset).pack(side="left")

    def _pick_browser(self):
        fn = filedialog.askopenfilename(
            parent=self, title="选择 Chrome / Edge 可执行文件",
            filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        if fn:
            self.browser_path.set(fn)

    def _pick_dir(self):
        d = filedialog.askdirectory(parent=self, title="选择默认导出目录")
        if d:
            self.export_dir.set(d)

    def _reset(self):
        d = self.app._DEFAULTS
        self.enable_browser.set(bool(d["enable_browser"]))
        self.browser_path.set(d["browser_path"])
        self.browser_timeout.set(int(d["browser_timeout"]))
        self.request_timeout.set(int(d["request_timeout"]))
        self.proxy.set(d["proxy"])
        self.export_dir.set(d["export_dir"])
        self.auto_export.set(bool(d["auto_export"]))
        self.auto_fallback.set(bool(d["auto_fallback"]))

    def _save(self):
        cfg = self.app._cfg
        cfg["enable_browser"] = bool(self.enable_browser.get())
        cfg["browser_path"] = self.browser_path.get().strip()
        cfg["browser_timeout"] = int(self.browser_timeout.get())
        cfg["request_timeout"] = int(self.request_timeout.get())
        cfg["proxy"] = self.proxy.get().strip()
        cfg["export_dir"] = self.export_dir.get().strip()
        cfg["auto_export"] = bool(self.auto_export.get())
        cfg["auto_fallback"] = bool(self.auto_fallback.get())
        self.app._save_cfg()
        self.app._log("设置已保存")
        self.destroy()


if __name__ == "__main__":
    _enable_dpi_awareness()
    App().mainloop()
