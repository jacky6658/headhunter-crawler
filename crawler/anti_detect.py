"""
反偵測工具 — UA 輪換、延遲、CAPTCHA 偵測、代理、瀏覽器 stealth
來源: search-plan-executor.py L34-106 + profile-reader.py L40-114
"""
import gzip
import json
import logging
import os
import random
import ssl
import time
from typing import Optional
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen, ProxyHandler, build_opener

logger = logging.getLogger(__name__)

# Playwright stealth JS（注入瀏覽器 context）
STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const arr = [1, 2, 3, 4, 5];
            arr.__proto__ = PluginArray.prototype;
            return arr;
        }
    });
    Object.defineProperty(navigator, 'languages', {
        get: () => ['zh-TW', 'zh', 'en-US', 'en']
    });
    window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };
    delete window.__playwright;
    delete window.__pwInitScripts;
"""


class AntiDetect:
    """反偵測工具集"""

    def __init__(self, config: dict):
        self.config = config
        ad_cfg = config.get('anti_detect', {})
        timeout_cfg = config.get('timeouts', {})
        captcha_cfg = config.get('captcha', {})

        # 載入 UA
        self.user_agents = self._load_user_agents()

        # 代理
        proxy_cfg = ad_cfg.get('proxy', {})
        self.proxy_enabled = proxy_cfg.get('enabled', False)
        self.proxy_list = proxy_cfg.get('list', [])
        self.proxy_strategy = proxy_cfg.get('strategy', 'round_robin')
        self._proxy_index = 0

        # SSL
        self.ssl_verify = ad_cfg.get('ssl_verify', True)
        self._ssl_ctx = self._create_ssl_context()

        # 延遲
        req_delay = ad_cfg.get('request_delay', {})
        self.request_delay_min = req_delay.get('min', 2.0)
        self.request_delay_max = req_delay.get('max', 5.0)

        page_delay = ad_cfg.get('page_delay', {})
        self.page_delay_min = page_delay.get('min', 3.0)
        self.page_delay_max = page_delay.get('max', 6.0)

        cand_delay = ad_cfg.get('candidate_delay', {})
        self.candidate_delay_min = cand_delay.get('min', 10.0)
        self.candidate_delay_max = cand_delay.get('max', 20.0)

        gh_delay = ad_cfg.get('github_delay', {})
        self.github_delay_min = gh_delay.get('min', 0.3)
        self.github_delay_max = gh_delay.get('max', 0.8)

        # 指數退避
        backoff = ad_cfg.get('backoff', {})
        self.backoff_initial = backoff.get('initial', 2.0)
        self.backoff_multiplier = backoff.get('multiplier', 2.0)
        self.backoff_max = backoff.get('max_wait', 120.0)

        # CAPTCHA 指標
        self.captcha_indicators = captcha_cfg.get('indicators', [
            'g-recaptcha', 'recaptcha', 'unusual traffic',
            'Just a moment', 'Cloudflare', '/sorry/index',
            'sitekey', 'detected unusual', 'verify you are human', 'hcaptcha',
        ])

        # 超時
        self.http_timeout = timeout_cfg.get('http_get', 15)
        self.page_load_timeout = timeout_cfg.get('page_load', 30000)
        self.profile_read_timeout = timeout_cfg.get('profile_read', 25000)

    def _load_user_agents(self) -> list:
        """從 user_agents.txt 載入"""
        ua_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'user_agents.txt')
        try:
            with open(ua_path, 'r') as f:
                agents = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            logger.info(f"載入 {len(agents)} 個 User-Agent")
            return agents
        except FileNotFoundError:
            logger.warning("user_agents.txt 不存在，使用預設 UA")
            return [
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            ]

    def _create_ssl_context(self) -> ssl.SSLContext:
        if not self.ssl_verify:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            logger.warning("SSL 驗證已停用")
            return ctx
        # 嘗試建立正常 SSL context，若系統憑證有問題則自動降級
        try:
            ctx = ssl.create_default_context()
            # 測試能否正常使用（有些 Mac 缺少系統憑證）
            import urllib.request
            test_req = urllib.request.Request('https://www.google.com',
                                              method='HEAD')
            urllib.request.urlopen(test_req, timeout=5, context=ctx)
            return ctx
        except Exception as e:
            logger.warning(f"SSL 憑證驗證失敗，自動降級為不驗證模式: {e}")
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx

    # ── UA & Headers ─────────────────────────────────────────

    def get_random_ua(self) -> str:
        return random.choice(self.user_agents)

    def get_browser_headers(self, extra: dict = None) -> dict:
        h = {
            'User-Agent': self.get_random_ua(),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Cache-Control': 'max-age=0',
        }
        if extra:
            h.update(extra)
        return h

    # ── 代理 ─────────────────────────────────────────────────

    def get_next_proxy(self) -> Optional[str]:
        if not self.proxy_enabled or not self.proxy_list:
            return None
        if self.proxy_strategy == 'random':
            return random.choice(self.proxy_list)
        # round_robin
        proxy = self.proxy_list[self._proxy_index % len(self.proxy_list)]
        self._proxy_index += 1
        return proxy

    # ── HTTP 工具 ────────────────────────────────────────────

    def http_get(self, url: str, extra_headers: dict = None, timeout: int = None) -> tuple:
        """HTTP GET，回傳 (text, status_code)"""
        timeout = timeout or self.http_timeout
        headers = self.get_browser_headers(extra_headers)
        req = Request(url, headers=headers)

        proxy = self.get_next_proxy()
        if proxy:
            opener = build_opener(ProxyHandler({'http': proxy, 'https': proxy}))
        else:
            opener = None

        try:
            if opener:
                resp = opener.open(req, timeout=timeout)
            else:
                resp = urlopen(req, timeout=timeout, context=self._ssl_ctx)
            raw = resp.read()
            enc = resp.headers.get('Content-Encoding', '')
            if 'gzip' in enc:
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            return raw.decode('utf-8', errors='replace'), resp.status
        except HTTPError as e:
            return '', e.code
        except Exception as e:
            logger.debug(f"HTTP GET 失敗 {url}: {e}")
            return '', 0

    def http_get_json(self, url: str, extra_headers: dict = None, timeout: int = None) -> tuple:
        """HTTP GET + JSON 解析，回傳 (dict, status_code)"""
        timeout = timeout or self.http_timeout
        headers = self.get_browser_headers(extra_headers)
        headers['Accept'] = 'application/json'
        req = Request(url, headers=headers)

        proxy = self.get_next_proxy()
        if proxy:
            opener = build_opener(ProxyHandler({'http': proxy, 'https': proxy}))
        else:
            opener = None

        try:
            if opener:
                resp = opener.open(req, timeout=timeout)
            else:
                resp = urlopen(req, timeout=timeout, context=self._ssl_ctx)
            raw = resp.read()
            enc = resp.headers.get('Content-Encoding', '')
            if 'gzip' in enc:
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            return json.loads(raw.decode('utf-8', errors='replace')), resp.status
        except HTTPError as e:
            return {}, e.code
        except Exception as e:
            logger.debug(f"HTTP GET JSON 失敗 {url}: {e}")
            return {}, 0

    # ── 延遲 ─────────────────────────────────────────────────

    def request_delay(self):
        """請求間隔"""
        time.sleep(random.uniform(self.request_delay_min, self.request_delay_max))

    def page_delay(self):
        """翻頁間隔"""
        time.sleep(random.uniform(self.page_delay_min, self.page_delay_max))

    def candidate_delay(self):
        """候選人間隔（長停頓）"""
        t = random.uniform(self.candidate_delay_min, self.candidate_delay_max)
        logger.debug(f"候選人間隔停頓 {t:.1f}s")
        time.sleep(t)

    def github_delay(self):
        """GitHub API 間隔"""
        time.sleep(random.uniform(self.github_delay_min, self.github_delay_max))

    def exponential_backoff(self, attempt: int):
        """指數退避（取代固定 sleep(15)）"""
        wait = min(self.backoff_initial * (self.backoff_multiplier ** attempt), self.backoff_max)
        jitter = random.uniform(0, wait * 0.1)
        total = wait + jitter
        logger.info(f"指數退避: 等待 {total:.1f}s (attempt={attempt})")
        time.sleep(total)

    # ── CAPTCHA ──────────────────────────────────────────────

    def is_captcha_page(self, text: str) -> bool:
        lower = text.lower()
        return any(ind.lower() in lower for ind in self.captcha_indicators)

    # ── Playwright 人類模擬 ──────────────────────────────────

    def human_delay(self, min_s: float = 1.5, max_s: float = 4.0):
        time.sleep(random.uniform(min_s, max_s))

    def human_scroll(self, page, total_distance: int = None):
        """模擬人類滾動"""
        if total_distance is None:
            total_distance = random.randint(600, 1800)
        scrolled = 0
        while scrolled < total_distance:
            chunk = random.randint(80, 350)
            if random.random() < 0.07 and scrolled > 200:
                chunk = -random.randint(50, 150)
            page.evaluate(f"window.scrollBy(0, {chunk})")
            scrolled += chunk
            time.sleep(random.uniform(0.08, 0.35))
        self.human_delay(0.5, 1.5)

    def random_mouse_wiggle(self, page):
        """滑鼠隨機晃動"""
        try:
            w = random.randint(400, 1200)
            h = random.randint(200, 600)
            page.mouse.move(w, h, steps=random.randint(5, 15))
            time.sleep(random.uniform(0.1, 0.3))
            page.mouse.move(
                w + random.randint(-80, 80),
                h + random.randint(-60, 60),
                steps=random.randint(3, 10),
            )
        except Exception:
            pass

    def apply_stealth(self, context):
        """對 Playwright browser context 注入 stealth 腳本"""
        context.add_init_script(STEALTH_JS)
        try:
            from playwright_stealth import stealth_sync
            # playwright-stealth 需要 page 而非 context
            # 我們在 context level 注入基本 JS，page level 再用 stealth
            logger.debug("playwright-stealth 可用")
        except ImportError:
            logger.debug("playwright-stealth 未安裝，使用基本 stealth JS")

    def apply_page_stealth(self, page):
        """對 Playwright page 套用 stealth"""
        try:
            from playwright_stealth import stealth_sync
            stealth_sync(page)
        except ImportError:
            pass
