"""
Playwright 瀏覽器池管理
每個 worker 進程各自初始化 browser（Playwright 不能跨進程共享）
"""
import atexit
import logging
import random

logger = logging.getLogger(__name__)

# 全域追蹤所有 pool 實例，以便 atexit 清理
_active_pools: list = []

try:
    from playwright.sync_api import sync_playwright, Playwright, Browser, BrowserContext
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class BrowserPool:
    """
    管理 Playwright 瀏覽器。
    在 worker 進程內使用，每個 worker 自帶一個 browser。
    """

    BROWSER_ARGS = [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--disable-blink-features=AutomationControlled',
        '--disable-infobars',
        '--disable-extensions',
    ]

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw: 'Playwright' = None
        self._browser: 'Browser' = None

    @property
    def available(self) -> bool:
        return PLAYWRIGHT_AVAILABLE

    def start(self) -> 'Browser':
        """啟動 Playwright + Chromium browser"""
        if not PLAYWRIGHT_AVAILABLE:
            logger.error("Playwright 未安裝")
            return None

        self._pw = sync_playwright().start()
        w = random.randint(1280, 1440)
        h = random.randint(760, 900)
        args = self.BROWSER_ARGS + [f'--window-size={w},{h}']

        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=args,
        )
        _active_pools.append(self)
        logger.info(f"Browser 已啟動 (headless={self.headless})")
        return self._browser

    def new_context(self, user_agent: str = None, locale: str = 'zh-TW') -> 'BrowserContext':
        """建立新的 browser context"""
        if not self._browser:
            return None

        ctx = self._browser.new_context(
            user_agent=user_agent or '',
            viewport={
                'width': random.randint(1280, 1440),
                'height': random.randint(700, 900),
            },
            locale=locale,
            timezone_id='Asia/Taipei',
            extra_http_headers={
                'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'DNT': '1',
            },
        )
        return ctx

    def stop(self):
        """關閉 browser + Playwright"""
        try:
            if self._browser:
                self._browser.close()
                self._browser = None
            if self._pw:
                self._pw.stop()
                self._pw = None
            if self in _active_pools:
                _active_pools.remove(self)
            logger.info("Browser 已關閉")
        except Exception as e:
            logger.warning(f"Browser 關閉失敗: {e}")

    @property
    def browser(self) -> 'Browser':
        return self._browser


def _atexit_cleanup():
    """進程結束時清理所有未關閉的 browser"""
    for pool in list(_active_pools):
        try:
            pool.stop()
        except Exception:
            pass

atexit.register(_atexit_cleanup)
