"""Cloudflare 反爬 Session 管理。

获取 cf_clearance cookie 的几种方式：
- solve_via_browser: 自动启动本地 Chrome/Edge 过验证（DrissionPage，无需 Docker）
- FlareSolverr (本地 Docker)，未运行时会尝试自动拉起
拿到 cookie 后使用 curl_cffi 的 TLS 指纹模拟发起后续请求。
"""
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from curl_cffi import requests as creq

from .paths import session_cache_file

log = logging.getLogger(__name__)

FS_URL = "http://localhost:8191/v1"
PH_BASE = "https://www.producthunt.com"


def load_manual_cookies() -> dict | None:
    """手动 Cookie 模式（免 Docker）：

    在 exe / 脚本所在目录放一个 ph_cookies.json：
    {
      "cookie": "浏览器里复制的完整 Cookie 串（须包含 cf_clearance=...）",
      "user_agent": "浏览器的 User-Agent（须与拿 cookie 的浏览器一致）"
    }
    """
    candidates = [Path.cwd() / "ph_cookies.json"]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "ph_cookies.json")
    else:
        candidates.append(Path(__file__).resolve().parent.parent / "ph_cookies.json")
    if os.environ.get("PH_COOKIES_FILE"):
        candidates.insert(0, Path(os.environ["PH_COOKIES_FILE"]))
    for p in candidates:
        try:
            if p.is_file():
                d = json.loads(p.read_text(encoding="utf-8"))
                raw = (d.get("cookie") or "").strip()
                ua = (d.get("user_agent") or "").strip()
                if not raw:
                    continue
                cookies = {}
                for part in raw.split(";"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cookies[k.strip()] = v.strip()
                if "cf_clearance" in cookies:
                    log.info("使用手动 Cookie 文件: %s", p)
                    return {"cookies": cookies, "user_agent": ua}
                log.warning("%s 中缺少 cf_clearance，忽略", p)
        except Exception as e:
            log.warning("读取 %s 失败: %s", p, e)
    return None


def load_cached_session() -> dict | None:
    """读取上次成功的 Cloudflare 会话（cookie + UA），避免每次启动重新验证。"""
    p = session_cache_file()
    try:
        if p.is_file():
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("cookies", {}).get("cf_clearance"):
                age_h = (time.time() - d.get("saved_at", 0)) / 3600
                if age_h < 24:
                    log.info("复用缓存会话 (%.1f 小时前)", age_h)
                    return d
    except Exception as e:
        log.warning("读取会话缓存失败: %s", e)
    return None


def save_cached_session(cookies: dict, user_agent: str):
    try:
        session_cache_file().write_text(json.dumps({
            "cookies": cookies, "user_agent": user_agent,
            "saved_at": time.time(),
        }), encoding="utf-8")
    except Exception as e:
        log.warning("保存会话缓存失败: %s", e)


def clear_cached_session():
    try:
        session_cache_file().unlink(missing_ok=True)
    except Exception:
        pass


def _ensure_flaresolverr() -> bool:
    """确保 FlareSolverr 在运行，如没有则尝试启动。"""
    try:
        r = creq.get("http://localhost:8191", timeout=5)
        return True
    except Exception:
        pass
    log.info("FlareSolverr 未运行，尝试通过 Docker 启动...")
    try:
        subprocess.run(
            ["docker", "run", "-d", "--name", "flaresolverr",
             "--rm", "-p", "8191:8191",
             "ghcr.io/flaresolverr/flaresolverr:latest"],
            capture_output=True, text=True, timeout=120,
        )
        for _ in range(30):
            time.sleep(2)
            try:
                creq.get("http://localhost:8191", timeout=3)
                log.info("FlareSolverr 已启动")
                return True
            except Exception:
                pass
    except Exception as e:
        log.error("无法启动 FlareSolverr: %s", e)
    return False


def solve_cloudflare(target_url: str, max_timeout: int = 120_000) -> dict | None:
    """
    让 FlareSolverr 帮我们过 Cloudflare 盘问，返回:
    {"cookies": dict, "user_agent": str}
    """
    if not _ensure_flaresolverr():
        return None
    try:
        log.info("请求 FlareSolverr 解决 Cloudflare 挑战: %s ...", target_url)
        r = creq.post(FS_URL, json={
            "cmd": "request.get",
            "url": target_url,
            "maxTimeout": max_timeout,
        }, timeout=max_timeout // 1000 + 30)
        d = r.json()
        if d.get("status") == "ok":
            sol = d["solution"]
            cookies = {c["name"]: c["value"] for c in sol.get("cookies", [])}
            ua = sol.get("userAgent", "")
            log.info("Cloudflare 挑战已解决 (cookies: %d)", len(cookies))
            return {"cookies": cookies, "user_agent": ua}
        else:
            log.error("FlareSolverr 失败: %s", d.get("message"))
    except Exception as e:
        log.error("FlareSolverr 请求异常: %s", e)
    return None


def _find_browser_on_windows() -> str | None:
    """在 Windows 上通过注册表和常见路径查找 Chrome / Edge。"""
    if sys.platform != "win32":
        return None
    # 1) 注册表
    try:
        import winreg
        for key_path in (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
        ):
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(root, key_path) as key:
                        val = winreg.QueryValue(key, None)
                        if val and os.path.isfile(val):
                            return val
                except OSError:
                    pass
    except Exception:
        pass
    # 2) 常见路径
    for tmpl in (
        r"{PROGRAMFILES}\Google\Chrome\Application\chrome.exe",
        r"{PROGRAMFILES(X86)}\Google\Chrome\Application\chrome.exe",
        r"{LOCALAPPDATA}\Google\Chrome\Application\chrome.exe",
        r"{PROGRAMFILES}\Microsoft\Edge\Application\msedge.exe",
        r"{PROGRAMFILES(X86)}\Microsoft\Edge\Application\msedge.exe",
    ):
        try:
            p = os.path.expandvars(tmpl.replace("{", "%").replace("}", "%"))
            if os.path.isfile(p):
                return p
        except Exception:
            pass
    return None


def solve_via_browser(target_url: str = PH_BASE, timeout: int = 120,
                      progress_cb=None, browser_path: str = "") -> dict | None:
    """使用本地 Chrome/Edge 自动过 Cloudflare（无需 Docker / 手动配置）。

    自动启动系统已安装的浏览器，导航到目标页面，等待 Cloudflare 挑战完成，
    提取 cf_clearance cookie 和 User-Agent 后关闭浏览器。
    需要安装 DrissionPage（pip install DrissionPage）。
    """
    _cb = progress_cb or (lambda m: None)
    try:
        from DrissionPage import ChromiumPage, ChromiumOptions
    except ImportError:
        log.info("DrissionPage 未安装，跳过自动浏览器验证（pip install DrissionPage）")
        _cb("DrissionPage 未安装，跳过自动浏览器验证")
        return None

    page = None
    proc = None
    try:
        import socket
        import tempfile

        # 自己启动浏览器再让 DrissionPage 接管，
        # 避免 PyInstaller 窗口程序中 DrissionPage 自带启动逻辑失败的问题
        browser_path = (browser_path or os.environ.get("PH_BROWSER_PATH")
                        or _find_browser_on_windows())
        if not browser_path:
            _cb("未找到 Chrome/Edge 浏览器，跳过自动浏览器验证")
            log.warning("未找到 Chrome/Edge，无法自动浏览器验证")
            return None
        _cb(f"找到浏览器: {browser_path}")

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        user_data_dir = tempfile.mkdtemp(prefix="ph_cf_")

        args = [
            browser_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--window-size=1280,860",
            "about:blank",
        ]
        if sys.platform != "win32":
            args.insert(1, "--no-sandbox")

        _cb("正在启动浏览器...")
        popen_kw: dict = dict(stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        if sys.platform == "win32":
            popen_kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW (仅控制台)
        proc = subprocess.Popen(args, **popen_kw)

        # 等待调试端口就绪
        import urllib.request
        ready = False
        for _ in range(60):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/version", timeout=2):
                    ready = True
                    break
            except Exception:
                time.sleep(0.5)
        if not ready:
            _cb("浏览器启动失败（调试端口未就绪）")
            log.error("浏览器调试端口 %s 未就绪", port)
            return None

        page = ChromiumPage(addr_or_opts=f"127.0.0.1:{port}")
        page.get(target_url)
        _cb("浏览器已打开，等待 Cloudflare 验证通过（请勿手动关闭浏览器）...")

        deadline = time.time() + timeout
        passed = False
        turnstile_clicked = False
        while time.time() < deadline:
            try:
                title = page.title or ""
                # 检查 Cloudflare 挑战是否已完成
                if ("Just a moment" not in title
                        and "Cloudflare" not in title
                        and "Attention Required" not in title
                        and "challenge" not in title.lower()):
                    time.sleep(3)  # 等待 cookie 写入
                    passed = True
                    break
                # 尝试自动点击 Turnstile 复选框（部分 CF 挑战需要用户点击）
                if not turnstile_clicked:
                    try:
                        iframes = page.get_frames()
                        for frame in iframes:
                            try:
                                src = getattr(frame, 'url', '') or ''
                                if 'challenges.cloudflare.com' in src:
                                    cb = frame.ele('tag:input@@type=checkbox', timeout=1)
                                    if cb:
                                        cb.click()
                                        turnstile_clicked = True
                                        _cb("已自动点击 Cloudflare 验证复选框...")
                                        break
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(1)

        if not passed:
            _cb("等待 Cloudflare 验证超时")
            return None

        # 提取 cookie（CookiesList: list[dict] 格式）
        cookies: dict[str, str] = {}
        try:
            for c in page.cookies():
                if isinstance(c, dict):
                    name = str(c.get('name', ''))
                    value = str(c.get('value', ''))
                    if name:
                        cookies[name] = value
        except Exception as e:
            log.warning("提取 cookie 失败: %s", e)

        # 提取 User-Agent
        ua = ""
        try:
            ua = page.run_js('return navigator.userAgent;') or ""
        except Exception:
            pass

        if "cf_clearance" in cookies:
            _cb("浏览器自动验证成功！")
            log.info("浏览器自动验证成功，获取到 cf_clearance")
            save_cached_session(cookies, ua)
            return {"cookies": cookies, "user_agent": ua}

        _cb("浏览器验证后未获取到 cf_clearance，可能验证未完成")
        log.warning("浏览器验证完成但 cookies 中无 cf_clearance: %s",
                    list(cookies.keys()))
        return None
    except Exception as e:
        _cb(f"浏览器自动验证异常: {e}")
        log.error("浏览器自动验证异常: %s", e)
        return None
    finally:
        if page:
            try:
                page.quit()
            except Exception:
                pass
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


class CFSession:
    """带 Cloudflare cookies 的 HTTP Session，使用 curl_cffi TLS 指纹模拟。"""

    def __init__(self, cookies: dict | None = None, user_agent: str = "",
                 timeout: int = 30, proxy: str = ""):
        self.session = creq.Session(impersonate="chrome131")
        self.timeout = timeout if timeout and timeout > 0 else 30
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self.cookies = dict(cookies or {})
        if cookies:
            self.session.cookies.update(cookies)
        self._ua = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        )
        self.session.headers.update({"User-Agent": self._ua})
        self.user_agent = self._ua

    @classmethod
    def from_flaresolverr(cls, target_url: str = PH_BASE,
                          timeout: int = 30, proxy: str = "") -> "CFSession | None":
        sol = solve_cloudflare(target_url)
        if sol:
            return cls(cookies=sol["cookies"], user_agent=sol["user_agent"],
                       timeout=timeout, proxy=proxy)
        return None

    def get(self, url: str, **kw):
        kw.setdefault("timeout", self.timeout)
        return self.session.get(url, **kw)

    def post(self, url: str, **kw):
        kw.setdefault("timeout", self.timeout)
        return self.session.post(url, **kw)
