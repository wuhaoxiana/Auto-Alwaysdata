#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AlwaysData 自动登录 / 保活（重写版）

相比原版的改动
================================
1. 删除 5 家第三方网页代理，只保留 CroxyProxy（aproxy / bestproxy / nsocks /
   lumiproxy / proxy.cc 全部移除）。
2. 修掉「假成功」：原版用 page.get_by_text("Administration") 判断已登录，而登录页
   的 h1 恰好是「Se connecter : administration」，大小写不敏感子串匹配必然命中，
   于是打开登录页就 return True，凭据输入段永远执行不到。
   新判定 = 用同一个 context 重新请求 https://admin.alwaysdata.com/ ，**返回 200**
   才算登录成功（未登录时服务端 302 到 /login/?next=/，实测）。
3. TG 只在复验通过后发送**登录后的后台截图**（full_page）。失败时发送的是失败页
   截图，且 caption 明确写「未登录」，不会再出现「图是登录页、文字说成功」。
4. 默认**不经过第三方网页代理提交凭据**。实测 CroxyProxy 能把 AlwaysData
   的登录请求代转成功（错误密码能拿到站点原文报错），但密码必须经过他们的
   服务器，且会话 cookie 落在代理域名下 → 无法服务端复验、无法证明已登录。
   所以它只用于诊断「目标站点此刻是否可达」。确需强行走它，设
   ALLOW_CREDENTIALS_VIA_PROXY_CC=1，结果会标注「未经服务端复验」。
5. 勾选「Rester connecté pendant 2 semaines」(name=alive)，让会话活 2 周，
   这才对保活有实际意义（原版只 page.reload()，等于什么都没做）。
6. 去掉满地的 except: pass：所有异常都会写进日志并发到 TG。
7. 选择器改为真实字段：input[name="login"] / input[name="password"](#id_password)
   / #id_alive / #form-login。原版的 input[name="email"] 根本不存在。

用法
================================
    pip install playwright requests && playwright install chromium

    export AD_ACCOUNTS_CC='[{"username":"a@b.com","password":"xxx"}]'
    #   （也认 ACCOUNTS_JSON_CC，即原 workflow 里的 secret 名字）
    # 或者用单账号：
    export AD_USERNAME_CC=a@b.com AD_PASSWORD_CC=xxx
    # 只跑其中一个（workflow_dispatch 的 account 输入就是传给它的）：
    export AD_ONLY_CC=2            # 序号（1 起）/ 完整邮箱 / 邮箱前缀；空或 all = 全部
    # TG 通知：
    export TG_BOT_TOKEN=xxx TG_CHAT_ID=123
    # 可选：
    export ALLOW_CREDENTIALS_VIA_PROXY_CC=0   # 1 = 允许经 CroxyProxy 提交密码（有泄露风险）
    export STAY_LOGGED_IN_CC=1                # 1 = 勾选「保持登录 2 周」
    export HEADLESS_CC=1
    export AD_TIMEOUT_CC=45                   # 登录后等待跳转的秒数
    export SHOT_DIR_CC=./shots

    python3 main.py

退出码：0 = 全部成功，1 = 有失败，2 = 配置错误。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import urllib.parse


import requests
from playwright.sync_api import (
    BrowserContext,
    Frame,
    Page,
    sync_playwright,
)
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout

BASE = "https://admin.alwaysdata.com"
LOGIN_URL = f"{BASE}/login/"
HOME_URL = f"{BASE}/"

CROXY_HOME = "https://www.croxyproxy.com/"

# 真实字段（已用 curl 抓登录页核对）
SEL_EMAIL = '#id_login, input[name="login"]'
SEL_PASSWORD = '#id_password, input[name="password"]'
SEL_ALIVE = '#id_alive, input[name="alive"]'
SEL_FORM = "#form-login"
SEL_SUBMIT = '#form-login button[type="submit"], #form-login input[type="submit"]'
# 实测：AlwaysData 的错误框是 <div class="errors"><h3>…</h3><ul><li>…</li></ul></div>
# 它没有 role="alert"，也没有 .alert / .errorlist，别照着 Bootstrap 猜。
SEL_ERROR = (
    "div.errors li, div.errors h3, .errors li, .errors h3, "
    '[role="alert"], .alert-danger, .errorlist li, .errorlist'
)
# 兜底关键词：站点改版或切到英文界面时，仍能从正文里找到原因
ERROR_KEYWORDS = (
    "incorrect", "erreur", "error", "invalid", "failed", "échec",
    "trop de tentatives", "captcha", "bloqué", "blocked", "suspendu",
)

# ---------------------------------------------------------------- 配置


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


class Config:
    def __init__(self) -> None:
        self.accounts = self._load_accounts()
        self.tg_token = os.environ.get("TG_BOT_TOKEN", "").strip()
        self.tg_chat = os.environ.get("TG_CHAT_ID", "").strip()
        self.allow_proxy_creds = env_bool("ALLOW_CREDENTIALS_VIA_PROXY_CC", False)
        self.stay_logged_in = env_bool("STAY_LOGGED_IN_CC", True)
        self.headless = env_bool("HEADLESS_CC", True)
        self.timeout = env_int("AD_TIMEOUT_CC", 45)
        self.shot_dir = os.environ.get("SHOT_DIR_CC", "shots")
        self.probe_timeout = env_int("PROXY_PROBE_TIMEOUT_CC", 45)
        # 可选：用系统自带 chromium（本地调试常见；CI 里保持空即可）
        self.chrome_path = os.environ.get("CHROME_PATH_CC", "").strip()
        # 手动只跑某个账号：序号(1 起) / 完整邮箱 / 邮箱前缀 / all
        self.only = (os.environ.get("AD_ONLY_CC") or "").strip()

    @staticmethod
    def _load_accounts() -> list[dict[str, str]]:
        # 兼容两种变量名：AD_ACCOUNTS_CC（本脚本）与 ACCOUNTS_JSON_CC（原 workflow）
        raw = (os.environ.get("AD_ACCOUNTS_CC")
               or os.environ.get("ACCOUNTS_JSON_CC")
               or "").strip()
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise SystemExit(f"AD_ACCOUNTS_CC / ACCOUNTS_JSON_CC 不是合法 JSON: {e}")
            if isinstance(data, dict):
                data = [data]
            out = []
            for item in data:
                u = str(item.get("username") or item.get("email") or "").strip()
                p = str(item.get("password") or "")
                if u and p:
                    out.append({"username": u, "password": p})
            return out
        u = os.environ.get("AD_USERNAME_CC", "").strip()
        p = os.environ.get("AD_PASSWORD_CC", "")
        return [{"username": u, "password": p}] if u and p else []


def mask_email(addr: str) -> str:
    if "@" not in addr:
        return addr[:2] + "***" if len(addr) > 2 else "***"
    user, domain = addr.split("@", 1)
    keep = user[:1] if len(user) > 1 else ""
    return f"{keep}***@{domain}"


def select_accounts(
    accounts: list[dict[str, str]], only: str
) -> tuple[list[dict[str, str]], str]:
    """
    按 AD_ONLY_CC 挑选要跑的账号，返回 (选中的账号, 错误说明)。

    支持：空/all/* → 全部；纯数字 → 1 起的序号；完整邮箱；邮箱前缀；子串。
    匹配到多个或一个都没有时返回错误，绝不"猜一个"跑。
    """
    only = (only or "").strip()
    if not only or only.lower() in ("all", "*", "全部", "所有"):
        return accounts, ""

    if only.isdigit():
        idx = int(only)
        if 1 <= idx <= len(accounts):
            return [accounts[idx - 1]], ""
        return [], f"序号 {idx} 超出范围（共 {len(accounts)} 个账号，可用 1-{len(accounts)}）"

    low = only.lower()
    hit = [a for a in accounts if a["username"].lower() == low]
    if hit:
        return hit, ""

    hit = [a for a in accounts if a["username"].split("@")[0].lower() == low]
    if len(hit) == 1:
        return hit, ""
    if len(hit) > 1:  # 同名前缀跨多个域名，不替你选
        return [], (f"「{only}」对应 {len(hit)} 个账号（"
                    + ", ".join(mask_email(a["username"]) for a in hit)
                    + "），请写完整邮箱或改用序号")

    hit = [a for a in accounts if low in a["username"].lower()]
    if len(hit) == 1:
        return hit, ""
    if len(hit) > 1:
        names = ", ".join(f"{i}" for i in range(1, len(accounts) + 1)
                          if accounts[i - 1] in hit)
        return [], f"「{only}」匹配到多个账号（序号 {names}），请写更精确的邮箱或改用序号"
    return [], (
        f"没有匹配「{only}」的账号（共 {len(accounts)} 个）。"
        f"可用：序号 1-{len(accounts)}、完整邮箱、或邮箱前缀"
    )


# ---------------------------------------------------------------- Telegram


class Telegram:
    def __init__(self, token: str, chat: str) -> None:
        self.token = token
        self.chat = chat
        self.ok = bool(token and chat)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send(self, text: str) -> bool:
        if not self.ok:
            return False
        try:
            r = requests.post(
                self._url("sendMessage"),
                data={
                    "chat_id": self.chat,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
                timeout=30,
            )
            if r.status_code != 200:
                print(f"  [TG] sendMessage 失败 {r.status_code}: {r.text[:200]}")
                return False
            return True
        except requests.RequestException as e:
            print(f"  [TG] sendMessage 异常: {e}")
            return False

    def photo(self, path: str, caption: str) -> bool:
        if not self.ok:
            return False
        if not os.path.exists(path):
            print(f"  [TG] 截图不存在，改为纯文本发送: {path}")
            return self.send(caption)
        try:
            with open(path, "rb") as f:
                r = requests.post(
                    self._url("sendPhoto"),
                    data={
                        "chat_id": self.chat,
                        "caption": caption[:1024],
                        "parse_mode": "HTML",
                    },
                    files={"photo": f},
                    timeout=60,
                )
            if r.status_code != 200:
                print(f"  [TG] sendPhoto 失败 {r.status_code}: {r.text[:200]}")
                return self.send(caption)
            return True
        except requests.RequestException as e:
            print(f"  [TG] sendPhoto 异常: {e}")
            return self.send(caption)


# ---------------------------------------------------------------- 页面工具


def first_error_text(page: Page) -> str:
    """抓登录失败的原因：先用实测出来的 div.errors，再用关键词兜底。"""
    texts: list[str] = []
    try:
        for sel in ("div.errors li", "div.errors h3", '[role="alert"]',
                    ".alert-danger", ".errorlist li"):
            loc = page.locator(sel)
            n = min(loc.count(), 3)
            for i in range(n):
                t = (loc.nth(i).inner_text() or "").strip()
                t = re.sub(r"\s+", " ", t)
                if t and t not in texts:
                    texts.append(t)
            if texts:
                return " | ".join(texts)[:300]
    except PWError:
        pass

    # 兜底：从可见文本里找第一个带错误关键词的行（排除登录页标题）
    try:
        body = page.evaluate("document.body ? document.body.innerText : ''") or ""
    except PWError:
        return ""
    for line in str(body).splitlines():
        line = line.strip()
        if not line or "administration" in line.lower():
            continue
        low = line.lower()
        if any(k in low for k in ERROR_KEYWORDS):
            return line[:300]
    return ""


def find_login_frame(page: Page) -> Frame | None:
    """在主页和所有 iframe 里找登录表单（CroxyProxy 常把目标页塞进 iframe）。"""
    for frame in page.frames:
        try:
            if frame.locator(SEL_PASSWORD).count() > 0:
                return frame
        except PWError:
            continue
    return None


def is_login_url(url: str) -> bool:
    try:
        return "/login" in urllib.parse.urlparse(url).path
    except ValueError:
        return True


def shot(page: Page, path: str, full_page: bool = False) -> str | None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        page.screenshot(path=path, full_page=full_page)
        return path
    except PWError as e:
        print(f"  [截图失败] {path}: {e}")
        return None


class LoginError(RuntimeError):
    pass


# ---------------------------------------------------------------- 登录流程


def submit_credentials(
    page: Page, frame: Frame, username: str, password: str, stay: bool
) -> None:
    """在 frame 里填表并提交。任何一步失败都抛 LoginError（带原因）。"""
    try:
        frame.wait_for_selector(SEL_EMAIL, state="visible", timeout=30000)
    except PWTimeout:
        raise LoginError("找不到邮箱输入框（页面没渲染出登录表单）")

    frame.fill(SEL_EMAIL, username)
    frame.fill(SEL_PASSWORD, password)

    if stay:
        try:
            box = frame.locator(SEL_ALIVE)
            if box.count() > 0 and box.first.is_visible() and not box.first.is_checked():
                box.first.check()
        except PWError as e:
            print(f"  [警告] 勾选「保持登录」失败（不影响登录）: {e}")

    try:
        btn = frame.locator(SEL_SUBMIT)
        if btn.count() > 0:
            btn.first.click()
        else:
            frame.press(SEL_PASSWORD, "Enter")
    except PWError as e:
        raise LoginError(f"提交表单失败: {e}")


def wait_direct_outcome(page: Page, timeout_s: int) -> tuple[str, str]:
    """
    直连模式下判断提交结果，返回 ("ok"|"error"|"timeout", 说明)。

    判据只认 **URL 是否离开 /login/**：
      离开 → 候选成功（真正的放行判定交给 verify_session 的服务端复验）；
      仍在 /login/ 且出现错误框 → 立即判失败，并把站点原文带回；
      仍在 /login/ 且无错误 → 等满 timeout（可能被拦、可能网络卡）。

    注意：不要把「页面上没有密码框」当成功条件。后台页面本身也可能存在
    input[name="password"]（改密表单），那样会把成功误判成超时。
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            url = page.url
            on_login = is_login_url(url)
        except PWError as e:
            return "error", f"页面状态读取失败: {e}"

        if not on_login:
            return "ok", url

        err = first_error_text(page)
        if err:
            return "error", err

        if time.monotonic() >= deadline:
            return "timeout", f"等待 {timeout_s}s 仍在登录页，当前 URL: {url}"
        page.wait_for_timeout(400)


def verify_session(context: BrowserContext) -> tuple[bool, str]:
    """
    服务端复验：用同一个 context（共享 cookie）重新请求首页。
    未登录 → 302 到 /login/?next=/（实测）；已登录 → 200。
    """
    try:
        resp = context.request.get(HOME_URL, max_redirects=0, timeout=30000)
    except PWError as e:
        return False, f"复验请求失败: {e}"

    cookies = {}
    try:
        cookies = {c["name"]: c["value"] for c in context.cookies()}
    except PWError:
        pass
    has_session = bool(cookies.get("sessionid"))

    if resp.status == 200:
        return True, f"GET {HOME_URL} → 200，sessionid={'有' if has_session else '无'}"
    loc = resp.headers.get("location", "") if hasattr(resp, "headers") else ""
    return False, f"GET {HOME_URL} → {resp.status} {loc}".strip()


class CroxyProxyProbe:
    """
    只保留 CroxyProxy，默认**仅用于探测目标站点是否可达**。

    实测（2026-10-24）：它能渲染出 AlwaysData 登录表单（约 11-15s），而且提交
    错误密码时能拿到站点原文「Vos identifiants sont incorrects.」——说明 POST
    被它服务端代转、认证请求真的到达了 AlwaysData。换句话说它能"登录成功"。

    但代价是：
      · 密码必须经过他们的服务器（他们要在服务端代你 POST），页面里还嵌着
        Reflect4 的广告条；
      · 会话 cookie 落在代理域名下（实测 frame URL 形如
        https://51.158.204.82/login/?__cpo=<base64>），拿不到 admin.alwaysdata.com
        的 sessionid，因此**无法做服务端复验**，也就无法证明"已登录"；
      · 截图只是代理页面的截图，不代表账号状态。
    所以默认不在这里提交凭据；确需启用见 ALLOW_CREDENTIALS_VIA_PROXY_CC。
    """

    name = "CroxyProxy"
    trusted = False

    def __init__(self, timeout: int) -> None:
        self.timeout = timeout

    def open(self, page: Page, target: str) -> Frame:
        page.goto(CROXY_HOME, timeout=60000)
        page.wait_for_selector("#url", state="visible", timeout=30000)
        page.fill("#url", target)
        page.click("#requestSubmit")

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            frame = find_login_frame(page)
            if frame:
                return frame
            page.wait_for_timeout(500)
        raise LoginError(
            f"CroxyProxy {self.timeout}s 内没有渲染出目标登录表单"
            "（常见原因：它弹了验证码、限流，或目标站脚本被代理改写后失效）"
        )


# ---------------------------------------------------------------- 单账号执行


class Runner:
    def __init__(self, cfg: Config, tg: Telegram) -> None:
        self.cfg = cfg
        self.tg = tg
        self.logs: list[str] = []

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line)
        self.logs.append(line)

    # -- 主流程 --------------------------------------------------------

    def run(self, index: int, account: dict[str, str]) -> bool:
        masked = mask_email(account["username"])
        self.logs = []
        self.log(f"开始处理 {masked}")

        shots: dict[str, str | None] = {}
        tag = f"{index:02d}_{re.sub(r'[^0-9a-zA-Z]', '_', account['username'])[:20]}"

        with sync_playwright() as p:
            launch_kwargs: dict[str, object] = {
                "headless": self.cfg.headless,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            if self.cfg.chrome_path:
                launch_kwargs["executable_path"] = self.cfg.chrome_path
                self.log(f"使用指定浏览器: {self.cfg.chrome_path}")
            browser = p.chromium.launch(**launch_kwargs)  # type: ignore[arg-type]
            context = browser.new_context(
                viewport={"width": 1366, "height": 900},
                locale="fr-FR",
                timezone_id="Europe/Paris",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            try:
                return self._run_in_context(
                    page, context, account, masked, tag, shots
                )
            except Exception as e:  # 兜底：异常也必须让用户看到
                self.log(f"未捕获异常: {e}")
                traceback.print_exc()
                err_shot = shot(page, os.path.join(self.cfg.shot_dir, f"{tag}_99_error.png"))
                self.notify(
                    ok=False,
                    caption=f"❌ {masked} 运行异常\n<code>{e}</code>",
                    shot=err_shot,
                )
                return False
            finally:
                try:
                    context.close()
                    browser.close()
                except PWError:
                    pass

    def _run_in_context(
        self,
        page: Page,
        context: BrowserContext,
        account: dict[str, str],
        masked: str,
        tag: str,
        shots: dict[str, str | None],
    ) -> bool:
        # ---- 1. 直连加载登录页 ----
        self.log("直连打开登录页…")
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except (PWTimeout, PWError) as e:
            self.log(f"直连打开失败: {e}")
            return self.proxy_fallback(page, account, masked, tag, shots, str(e))

        if find_login_frame(page) is None:
            # 可能已经是登录态（cookie 不会跨 context，通常是站点改版或被拦）
            err = first_error_text(page) or "页面上没有登录表单"
            self.log(f"登录页异常: {err}")
            s = shot(page, os.path.join(self.cfg.shot_dir, f"{tag}_01_notlogin.png"))
            return self.proxy_fallback(page, account, masked, tag, shots, err, s)

        shots["01_login"] = shot(page, os.path.join(self.cfg.shot_dir, f"{tag}_01_login.png"))
        self.log("登录表单已渲染")

        # ---- 2. 提交凭据（直连）----
        self.log("提交账号密码…")
        try:
            submit_credentials(
                page, page.main_frame, account["username"], account["password"],
                self.cfg.stay_logged_in,
            )
        except LoginError as e:
            self.log(f"提交失败: {e}")
            s = shot(page, os.path.join(self.cfg.shot_dir, f"{tag}_02_submit_fail.png"))
            return self.fail(masked, f"提交失败: {e}", s)

        # ---- 3. 判断跳转 ---- 
        verdict, detail = wait_direct_outcome(page, self.cfg.timeout)
        shots["02_after"] = shot(page, os.path.join(self.cfg.shot_dir, f"{tag}_02_after_submit.png"))

        if verdict == "error":
            self.log(f"登录失败: {detail}")
            return self.fail(masked, f"登录被拒绝: {detail}", shots["02_after"])
        if verdict == "timeout":
            self.log(f"登录结果不明: {detail}")
            return self.fail(masked, detail, shots["02_after"])

        self.log(f"页面已跳转到 {detail}，开始服务端复验")

        # ---- 4. 服务端复验（这是唯一可信的成功判据）----
        ok, vdetail = verify_session(context)
        self.log(f"复验: {vdetail}")
        if not ok:
            return self.fail(masked, f"跳转后复验未通过 — {vdetail}", shots["02_after"])

        # ---- 5. 保活 + 截「登录后」的图 ----
        return self.finish_logged_in(page, context, masked, tag, shots)

    def finish_logged_in(
        self,
        page: Page,
        context: BrowserContext,
        masked: str,
        tag: str,
        shots: dict[str, str | None],
    ) -> bool:
        self.log("复验通过，进入后台…")
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
        except (PWTimeout, PWError) as e:
            self.log(f"打开后台失败: {e}")
            return self.fail(masked, f"已登录但打开后台失败: {e}", shots.get("02_after"))

        # 刷新一次（原版的 keepalive 就这一件事，现在放在确认已登录之后做）
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(800)
        except (PWTimeout, PWError) as e:
            self.log(f"刷新失败（不影响结论）: {e}")

        ok2, vdetail2 = verify_session(context)
        self.log(f"保活后复验: {vdetail2}")

        # 关键：这张图才是「登录后的截图」，且只有在复验通过时才拍
        dashboard = shot(
            page,
            os.path.join(self.cfg.shot_dir, f"{tag}_03_dashboard.png"),
            full_page=True,
        )

        # 刷新后掉线 = 不算成功，绝不发「✅ 已登录」+ 登录页截图
        if not ok2:
            return self.fail(
                masked,
                f"提交后复验通过，但刷新后掉线 — {vdetail2}",
                dashboard or shots.get("02_after"),
                headline="⚠️ 会话不稳定，未能保持登录",
            )
        if dashboard is None:
            return self.fail(
                masked, "已登录但截图失败", None,
                headline="⚠️ 已登录但截图失败",
            )

        caption = (
            f"✅ {masked} 已登录（服务端复验通过）\n"
            f"<b>复验:</b> <code>{vdetail2}</code>\n"
            f"<b>页面:</b> {page.url}\n"
            f"<b>保持登录 2 周:</b> {'是' if self.cfg.stay_logged_in else '否'}"
        )
        self.tg.photo(dashboard, caption)
        self.log("已发送登录后截图")
        return True

    # -- 兜底：只探测，不提交凭据 -------------------------------------- 

    def proxy_fallback(
        self,
        page: Page,
        account: dict[str, str],
        masked: str,
        tag: str,
        shots: dict[str, str | None],
        reason: str,
        prev_shot: str | None = None,
    ) -> bool:
        if not self.cfg.allow_proxy_creds:
            self.log("直连不可用 → 用 CroxyProxy 只做可达性探测（不提交凭据）")
            probe = CroxyProxyProbe(self.cfg.probe_timeout)
            try:
                frame = probe.open(page, LOGIN_URL)
            except (LoginError, PWError, PWTimeout) as e:
                self.log(f"CroxyProxy 探测也失败: {e}")
                s = shot(page, os.path.join(self.cfg.shot_dir, f"{tag}_04_proxy_fail.png"))
                return self.fail(
                    masked,
                    f"直连失败（{reason}），CroxyProxy 探测也失败（{e}）。"
                    "未提交任何凭据。",
                    s or prev_shot,
                )

            self.log("CroxyProxy 能渲染出登录表单（但未提交凭据）")
            s = shot(page, os.path.join(self.cfg.shot_dir, f"{tag}_04_proxy_probe.png"))
            self.tg.photo(
                s or "",
                f"⚠️ {masked} 未登录（诊断）\n"
                f"直连失败: {reason}\n"
                f"CroxyProxy 可达，但按策略**未提交凭据**：网页代理能看到明文密码。\n"
                f"确需强行走它，设 ALLOW_CREDENTIALS_VIA_PROXY_CC=1",
            )
            return False

        # 用户显式允许经代理提交（高风险，结果无法服务端复验）
        self.log("⚠️ ALLOW_CREDENTIALS_VIA_PROXY_CC=1：经 CroxyProxy 提交凭据")
        probe = CroxyProxyProbe(self.cfg.probe_timeout)
        try:
            frame = probe.open(page, LOGIN_URL)
            submit_credentials(
                page, frame, account["username"], account["password"],
                self.cfg.stay_logged_in,
            )
            page.wait_for_timeout(8000)
        except (LoginError, PWError, PWTimeout) as e:
            s = shot(page, os.path.join(self.cfg.shot_dir, f"{tag}_05_proxy_login.png"))
            return self.fail(masked, f"经 CroxyProxy 登录失败: {e}", s)

        s = shot(page, os.path.join(self.cfg.shot_dir, f"{tag}_05_proxy_login.png"))
        ok, vdetail = verify_session(context)  # 代理下 cookie 不在本 context，预期不通过
        if ok:
            return self.finish_logged_in(page, context, masked, tag, shots)
        self.tg.photo(
            s or "",
            f"⚠️ {masked} 结果不可信（经 CroxyProxy）\n"
            f"复验: {vdetail}\n"
            f"第三方代理下无法做服务端复验，此截图不代表已登录。",
        )
        return False

    # -- 失败上报 ------------------------------------------------------ 

    def fail(
        self,
        masked: str,
        detail: str,
        shot_path: str | None,
        headline: str = "",
    ) -> bool:
        head = headline or f"❌ {masked} 未登录"
        caption = f"{head}\n<b>原因:</b> {detail}"
        caption += "\n\n<b>日志</b>\n<code>" + "\n".join(self.logs[-6:]) + "</code>"
        if shot_path:
            self.tg.photo(shot_path, caption[:1024])
        else:
            self.tg.send(caption)
        return False

    def notify(self, ok: bool, caption: str, shot: str | None) -> None:
        if shot:
            self.tg.photo(shot, caption[:1024])
        else:
            self.tg.send(caption)


# ---------------------------------------------------------------- 入口


def main() -> int:
    cfg = Config()
    if not cfg.accounts:
        print(
            "没有账号。请设置 AD_ACCOUNTS_CC='[{\"username\":..,\"password\":..}]' "
            "或 AD_USERNAME_CC / AD_PASSWORD_CC",
            file=sys.stderr,
        )
        return 2

    accounts, sel_err = select_accounts(cfg.accounts, cfg.only)
    if sel_err:
        print(f"账号筛选失败: {sel_err}", file=sys.stderr)
        return 2
    cfg.accounts = accounts

    tg = Telegram(cfg.tg_token, cfg.tg_chat)
    os.makedirs(cfg.shot_dir, exist_ok=True)

    scope = f"仅 {mask_email(accounts[0]['username'])}" if len(accounts) == 1 and cfg.only else "全部"
    print(f"账号数: {len(accounts)}（{scope}）  代理: CroxyProxy(仅探测)  "
          f"允许经代理提交凭据: {cfg.allow_proxy_creds}  TG: {'开' if tg.ok else '关'}")

    runner = Runner(cfg, tg)
    results: list[tuple[str, bool]] = []
    for i, acc in enumerate(cfg.accounts, 1):
        ok = runner.run(i, acc)
        results.append((mask_email(acc["username"]), ok))
        if i < len(cfg.accounts):
            time.sleep(3)

    okc = sum(1 for _, o in results if o)
    print("\n===== 汇总 =====")
    for name, o in results:
        print(f"  {'✅' if o else '❌'} {name}")
    print(f"成功 {okc} / {len(results)}")
    return 0 if okc == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
