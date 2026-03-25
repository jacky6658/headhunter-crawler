"""
反偵測工具 — UA 輪換、延遲、CAPTCHA 偵測、代理、瀏覽器 stealth
來源: search-plan-executor.py L34-106 + profile-reader.py L40-114
v2 (2026-03-25): 強化隨機化 + 指紋一致性 + 真人行為模擬
"""
import gzip
import json
import logging
import math
import os
import random
import ssl
import time
from typing import Optional
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen, ProxyHandler, build_opener

logger = logging.getLogger(__name__)

# ── 指紋配置表：UA 平台 → 對應的 locale/timezone ──────────────
_FINGERPRINT_PROFILES = [
    {"platform": "mac", "languages": ["zh-TW", "zh", "en-US", "en"], "timezone": "Asia/Taipei", "accept_lang": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"},
    {"platform": "mac", "languages": ["en-US", "en"], "timezone": "America/Los_Angeles", "accept_lang": "en-US,en;q=0.9"},
    {"platform": "mac", "languages": ["ja", "en-US", "en"], "timezone": "Asia/Tokyo", "accept_lang": "ja,en-US;q=0.9,en;q=0.8"},
    {"platform": "win", "languages": ["zh-TW", "zh", "en-US", "en"], "timezone": "Asia/Taipei", "accept_lang": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"},
    {"platform": "win", "languages": ["en-US", "en"], "timezone": "America/New_York", "accept_lang": "en-US,en;q=0.9"},
    {"platform": "win", "languages": ["ja", "en-US", "en"], "timezone": "Asia/Tokyo", "accept_lang": "ja,en-US;q=0.9,en;q=0.8"},
    {"platform": "linux", "languages": ["en-US", "en"], "timezone": "America/Chicago", "accept_lang": "en-US,en;q=0.9"},
    {"platform": "linux", "languages": ["zh-TW", "zh", "en-US", "en"], "timezone": "Asia/Taipei", "accept_lang": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"},
]


def _build_stealth_js(languages: list) -> str:
    """根據指紋 profile 動態生成 stealth JS"""
    lang_js = json.dumps(languages)
    return f"""
    Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
    Object.defineProperty(navigator, 'plugins', {{
        get: () => {{
            const arr = [1, 2, 3, 4, 5];
            arr.__proto__ = PluginArray.prototype;
            return arr;
        }}
    }});
    Object.defineProperty(navigator, 'languages', {{
        get: () => {lang_js}
    }});
    window.chrome = {{ runtime: {{}}, loadTimes: () => {{}}, csi: () => {{}}, app: {{}} }};
    delete window.__playwright;
    delete window.__pwInitScripts;
    // 隱藏 Playwright 特徵
    Object.defineProperty(navigator, 'maxTouchPoints', {{ get: () => 0 }});
    Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {random.choice([4, 8, 12, 16])} }});
    Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {random.choice([4, 8, 16])} }});
    // 模糊 performance.now() 精度（防高精度計時偵測）
    const _perfNow = performance.now.bind(performance);
    performance.now = () => _perfNow() + Math.random() * 0.1;
    """

# 保留舊變數名相容性
STEALTH_JS = _build_stealth_js(["zh-TW", "zh", "en-US", "en"])


class AntiDetect:
    """反偵測工具集 v2"""

    def __init__(self, config: dict):
        self.config = config
        ad_cfg = config.get('anti_detect', {})
        timeout_cfg = config.get('timeouts', {})
        captcha_cfg = config.get('captcha', {})

        # 載入 UA（按平台分類）
        self.user_agents = self._load_user_agents()
        self._ua_by_platform = self._classify_user_agents()

        # 當前指紋 session（每次 rotate 時換）
        self._current_fingerprint = None
        self._current_ua = None
        self.rotate_fingerprint()

        # 代理
        proxy_cfg = ad_cfg.get('proxy', {})
        self.proxy_enabled = proxy_cfg.get('enabled', False)
        self.proxy_list = proxy_cfg.get('list', [])
        self.proxy_strategy = proxy_cfg.get('strategy', 'round_robin')
        self._proxy_index = 0

        # SSL
        self.ssl_verify = ad_cfg.get('ssl_verify', True)
        self._ssl_ctx = self._create_ssl_context()

        # 延遲（加寬範圍 v2）
        req_delay = ad_cfg.get('request_delay', {})
        self.request_delay_min = req_delay.get('min', 2.0)
        self.request_delay_max = req_delay.get('max', 8.0)

        page_delay = ad_cfg.get('page_delay', {})
        self.page_delay_min = page_delay.get('min', 3.0)
        self.page_delay_max = page_delay.get('max', 12.0)

        cand_delay = ad_cfg.get('candidate_delay', {})
        self.candidate_delay_min = cand_delay.get('min', 15.0)
        self.candidate_delay_max = cand_delay.get('max', 45.0)

        gh_delay = ad_cfg.get('github_delay', {})
        self.github_delay_min = gh_delay.get('min', 0.3)
        self.github_delay_max = gh_delay.get('max', 0.8)

        # 批次設定（隨機化 v2）
        batch_cfg = ad_cfg.get('batch', {})
        self.batch_size_min = batch_cfg.get('size_min', 3)
        self.batch_size_max = batch_cfg.get('size_max', 7)
        self.batch_break_min = batch_cfg.get('break_min', 180.0)
        self.batch_break_max = batch_cfg.get('break_max', 420.0)

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

    # ── 指紋管理（v2 新增）────────────────────────────────────

    def _classify_user_agents(self) -> dict:
        """將 UA 按平台分類"""
        result = {"mac": [], "win": [], "linux": []}
        for ua in self.user_agents:
            ua_lower = ua.lower()
            if "macintosh" in ua_lower or "mac os" in ua_lower:
                result["mac"].append(ua)
            elif "windows" in ua_lower:
                result["win"].append(ua)
            elif "linux" in ua_lower or "x11" in ua_lower:
                result["linux"].append(ua)
            else:
                result["win"].append(ua)  # 預設歸 Windows
        return result

    def rotate_fingerprint(self):
        """切換一組一致的指紋（UA + locale + timezone）"""
        fp = random.choice(_FINGERPRINT_PROFILES)
        platform = fp["platform"]
        candidates = self._ua_by_platform.get(platform, self.user_agents)
        if not candidates:
            candidates = self.user_agents
        self._current_ua = random.choice(candidates)
        self._current_fingerprint = fp
        logger.debug(f"指紋切換: platform={platform}, tz={fp['timezone']}, lang={fp['languages'][0]}")

    def get_current_fingerprint(self) -> dict:
        return self._current_fingerprint or _FINGERPRINT_PROFILES[0]

    def get_random_batch_size(self) -> int:
        """每批次處理的候選人數（隨機化避免固定模式）"""
        return random.randint(self.batch_size_min, self.batch_size_max)

    def get_batch_break_duration(self) -> float:
        """批次間休息時間（大幅隨機化）"""
        base = random.uniform(self.batch_break_min, self.batch_break_max)
        # 額外 jitter ±30%
        jitter = base * random.uniform(-0.3, 0.3)
        return max(60.0, base + jitter)

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

    # ── CDP Session 管理（v2 新增）────────────────────────────

    async def rotate_cdp_session(self, browser):
        """批次間切換 session：清除 cookies + 切換指紋
        在每個批次結束後呼叫，降低 LinkedIn 追蹤風險。

        Args:
            browser: Playwright CDP browser instance
        Returns:
            new page (舊 page 需由呼叫方關閉)
        """
        self.rotate_fingerprint()
        fp = self.get_current_fingerprint()

        context = browser.contexts[0]

        # 清除所有 cookies（LinkedIn session cookies 會被重設）
        await context.clear_cookies()
        logger.info("CDP session rotated: cookies cleared, fingerprint switched")

        # 建立新 page（取代舊的）
        page = await context.new_page()

        # 注入新指紋的 stealth JS
        js = _build_stealth_js(fp["languages"])
        await page.add_init_script(js)

        return page

    async def cleanup_cdp_pages(self, browser, keep_count: int = 1):
        """清理多餘的 CDP pages（防止記憶體洩漏）"""
        context = browser.contexts[0]
        pages = context.pages
        if len(pages) > keep_count:
            for p in pages[:-keep_count]:
                try:
                    await p.close()
                except Exception:
                    pass
            logger.debug(f"Cleaned up {len(pages) - keep_count} CDP pages")

    # ── UA & Headers ─────────────────────────────────────────

    def get_random_ua(self) -> str:
        """回傳當前 session 的 UA（與指紋一致）"""
        return self._current_ua or random.choice(self.user_agents)

    def get_browser_headers(self, extra: dict = None) -> dict:
        """產生跟指紋一致的 headers"""
        fp = self.get_current_fingerprint()
        h = {
            'User-Agent': self.get_random_ua(),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': fp.get('accept_lang', 'en-US,en;q=0.9'),
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

    # ── 延遲（v2: 加大 jitter 避免固定節奏）─────────────────

    @staticmethod
    def _jittered_delay(base_min: float, base_max: float, jitter_factor: float = 0.5) -> float:
        """產生帶大幅 jitter 的延遲，避免規律模式
        jitter_factor=0.5 表示結果在 base*0.5 ~ base*1.5 之間"""
        base = random.uniform(base_min, base_max)
        multiplier = random.uniform(1.0 - jitter_factor, 1.0 + jitter_factor)
        # 偶爾（8%）來一個特別長的停頓（模擬人去喝水/看手機）
        if random.random() < 0.08:
            multiplier *= random.uniform(2.0, 4.0)
        return max(0.5, base * multiplier)

    def request_delay(self):
        """請求間隔（2-8s, ±50% jitter, 8% 機率長停頓）"""
        t = self._jittered_delay(self.request_delay_min, self.request_delay_max, 0.5)
        time.sleep(t)

    def page_delay(self):
        """翻頁間隔（3-12s, ±50% jitter）"""
        t = self._jittered_delay(self.page_delay_min, self.page_delay_max, 0.5)
        time.sleep(t)

    def candidate_delay(self):
        """候選人間隔（15-45s, ±50% jitter, 8% 機率長停頓）"""
        t = self._jittered_delay(self.candidate_delay_min, self.candidate_delay_max, 0.5)
        logger.debug(f"候選人間隔停頓 {t:.1f}s")
        time.sleep(t)

    def github_delay(self):
        """GitHub API 間隔"""
        time.sleep(random.uniform(self.github_delay_min, self.github_delay_max))

    def exponential_backoff(self, attempt: int):
        """指數退避 + 隨機 jitter（±30%）"""
        wait = min(self.backoff_initial * (self.backoff_multiplier ** attempt), self.backoff_max)
        jitter = random.uniform(-wait * 0.3, wait * 0.3)
        total = max(1.0, wait + jitter)
        logger.info(f"指數退避: 等待 {total:.1f}s (attempt={attempt})")
        time.sleep(total)

    # ── CAPTCHA ──────────────────────────────────────────────

    def is_captcha_page(self, text: str) -> bool:
        lower = text.lower()
        return any(ind.lower() in lower for ind in self.captcha_indicators)

    # ── Playwright 人類模擬（v2: 強化真人行為）─────────────────

    def human_delay(self, min_s: float = 1.5, max_s: float = 4.0):
        time.sleep(random.uniform(min_s, max_s))

    def human_scroll(self, page, total_distance: int = None):
        """模擬人類滾動（v2: 更自然的節奏變化）"""
        if total_distance is None:
            total_distance = random.randint(600, 1800)
        scrolled = 0
        while scrolled < total_distance:
            chunk = random.randint(80, 350)
            # 10% 往回滾（人會回頭看）
            if random.random() < 0.10 and scrolled > 200:
                chunk = -random.randint(50, 200)
            # 5% 停下來「閱讀」一段
            if random.random() < 0.05:
                time.sleep(random.uniform(1.5, 4.0))
            page.evaluate(f"window.scrollBy(0, {chunk})")
            scrolled += abs(chunk)
            time.sleep(random.uniform(0.08, 0.45))
        self.human_delay(0.5, 1.5)

    def random_mouse_wiggle(self, page):
        """滑鼠隨機晃動（v2: 更像真人軌跡）"""
        try:
            # 起點
            x = random.randint(300, 1100)
            y = random.randint(150, 650)
            page.mouse.move(x, y, steps=random.randint(8, 20))
            time.sleep(random.uniform(0.1, 0.4))

            # 2-4 段隨機移動
            for _ in range(random.randint(2, 4)):
                dx = random.randint(-120, 120)
                dy = random.randint(-80, 80)
                page.mouse.move(
                    max(50, min(1300, x + dx)),
                    max(50, min(700, y + dy)),
                    steps=random.randint(5, 15),
                )
                x += dx
                y += dy
                time.sleep(random.uniform(0.05, 0.25))
        except Exception:
            pass

    def simulate_reading(self, page, min_s: float = 8.0, max_s: float = 25.0):
        """模擬閱讀 profile（v2 新增）：滾動 + 停頓 + 偶爾 hover"""
        read_time = random.uniform(min_s, max_s)
        start = time.time()
        while time.time() - start < read_time:
            action = random.choices(
                ["scroll", "pause", "wiggle", "nothing"],
                weights=[0.35, 0.30, 0.15, 0.20],
            )[0]
            if action == "scroll":
                dist = random.randint(100, 400)
                if random.random() < 0.15:
                    dist = -dist  # 往回滾
                page.evaluate(f"window.scrollBy(0, {dist})")
                time.sleep(random.uniform(0.3, 1.2))
            elif action == "pause":
                time.sleep(random.uniform(1.0, 3.5))
            elif action == "wiggle":
                self.random_mouse_wiggle(page)
            else:
                time.sleep(random.uniform(0.5, 1.5))
        logger.debug(f"模擬閱讀 {time.time()-start:.1f}s")

    async def simulate_reading_async(self, page, min_s: float = 8.0, max_s: float = 25.0):
        """非同步版模擬閱讀（給 async playwright 用）"""
        import asyncio
        read_time = random.uniform(min_s, max_s)
        start = time.time()
        while time.time() - start < read_time:
            action = random.choices(
                ["scroll", "pause", "wiggle", "nothing"],
                weights=[0.35, 0.30, 0.15, 0.20],
            )[0]
            if action == "scroll":
                dist = random.randint(100, 400)
                if random.random() < 0.15:
                    dist = -dist
                await page.evaluate(f"window.scrollBy(0, {dist})")
                await asyncio.sleep(random.uniform(0.3, 1.2))
            elif action == "pause":
                await asyncio.sleep(random.uniform(1.0, 3.5))
            elif action == "wiggle":
                try:
                    x = random.randint(300, 1100)
                    y = random.randint(150, 650)
                    await page.mouse.move(x, y, steps=random.randint(5, 12))
                    await asyncio.sleep(random.uniform(0.1, 0.3))
                except Exception:
                    pass
            else:
                await asyncio.sleep(random.uniform(0.5, 1.5))

    async def maybe_browse_feed_async(self, page, probability: float = 0.20):
        """v2 新增：有 probability 機率瀏覽 LinkedIn feed（模擬正常使用者）"""
        import asyncio
        if random.random() > probability:
            return False
        logger.debug("模擬瀏覽 LinkedIn feed...")
        try:
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(random.uniform(3, 8))
            # 隨機滾動 feed
            for _ in range(random.randint(2, 5)):
                await page.mouse.wheel(0, random.randint(200, 600))
                await asyncio.sleep(random.uniform(1.0, 3.0))
            await asyncio.sleep(random.uniform(2, 5))
            return True
        except Exception as e:
            logger.debug(f"Feed 瀏覽失敗（無礙）: {e}")
            return False

    def apply_stealth(self, context):
        """對 Playwright browser context 注入指紋一致的 stealth 腳本"""
        fp = self.get_current_fingerprint()
        js = _build_stealth_js(fp["languages"])
        context.add_init_script(js)
        try:
            from playwright_stealth import stealth_sync
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

    def get_playwright_context_options(self) -> dict:
        """v2 新增：取得跟指紋一致的 Playwright context 設定"""
        fp = self.get_current_fingerprint()
        return {
            "locale": fp["languages"][0].replace("-", "_") if fp["languages"] else "en_US",
            "timezone_id": fp.get("timezone", "Asia/Taipei"),
            "viewport": {
                "width": random.randint(1280, 1440),
                "height": random.randint(700, 900),
            },
            "user_agent": self.get_random_ua(),
        }
