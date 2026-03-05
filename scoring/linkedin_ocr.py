"""
LinkedIn OCR 深度分析 — 訪問 LinkedIn 個人頁面 + OCR 提取技能

手動觸發: 用戶在 Results 頁點擊「OCR 深度分析」按鈕
限制: 每小時最多 10 次（避免 LinkedIn 封鎖）
"""
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 每小時 OCR 配額
OCR_QUOTA_PER_HOUR = 10


class LinkedInOCRAnalyzer:
    """LinkedIn 個人頁面 OCR 分析（含配額管理）"""

    def __init__(self, config: dict, anti_detect=None, ocr=None):
        self.config = config
        self.ad = anti_detect
        self.ocr = ocr

        # 配額追蹤
        self._usage_log = []  # list of timestamps

    def get_quota_remaining(self) -> int:
        """取得本小時剩餘配額"""
        self._cleanup_usage_log()
        return max(0, OCR_QUOTA_PER_HOUR - len(self._usage_log))

    def _cleanup_usage_log(self):
        """清除超過 1 小時的記錄"""
        cutoff = datetime.now() - timedelta(hours=1)
        self._usage_log = [t for t in self._usage_log if t > cutoff]

    def _consume_quota(self) -> bool:
        """消費一次配額，返回是否成功"""
        self._cleanup_usage_log()
        if len(self._usage_log) >= OCR_QUOTA_PER_HOUR:
            return False
        self._usage_log.append(datetime.now())
        return True

    async def analyze(self, linkedin_url: str) -> Dict:
        """
        訪問 LinkedIn 個人頁面並用 OCR 提取資訊

        流程:
        1. 檢查配額
        2. Playwright 開啟頁面
        3. 模擬人類瀏覽（滾動、停頓）
        4. 截圖 + OCR
        5. 從 OCR 文字提取技能

        Returns:
        {
          'success': True/False,
          'extracted_skills': ['java', 'spring', ...],
          'headline': 'Senior Java Developer',
          'experience': 'Google, Amazon',
          'raw_text': '...',
          'error': '',
          'quota_remaining': 8,
        }
        """
        # 檢查配額
        if not self._consume_quota():
            remaining = self.get_quota_remaining()
            return {
                'success': False,
                'extracted_skills': [],
                'headline': '',
                'experience': '',
                'raw_text': '',
                'error': f'OCR 配額已用完（每小時 {OCR_QUOTA_PER_HOUR} 次），'
                         f'請 {60 - (datetime.now() - self._usage_log[0]).seconds // 60} 分鐘後再試',
                'quota_remaining': remaining,
            }

        try:
            result = await self._visit_and_ocr(linkedin_url)
            result['quota_remaining'] = self.get_quota_remaining()
            return result
        except Exception as e:
            logger.error(f"LinkedIn OCR 失敗: {e}")
            return {
                'success': False,
                'extracted_skills': [],
                'headline': '',
                'experience': '',
                'raw_text': '',
                'error': str(e),
                'quota_remaining': self.get_quota_remaining(),
            }

    async def _visit_and_ocr(self, linkedin_url: str) -> Dict:
        """實際訪問 LinkedIn 頁面並 OCR"""
        from playwright.async_api import async_playwright

        result = {
            'success': False,
            'extracted_skills': [],
            'headline': '',
            'experience': '',
            'raw_text': '',
            'error': '',
        }

        async with async_playwright() as p:
            # 啟動瀏覽器
            browser = await p.chromium.launch(
                headless=self.config.get('crawler', {}).get('headless', True)
            )

            try:
                # 建立 context（含反偵測）
                context = await browser.new_context(
                    viewport={'width': 1366, 'height': 768},
                    user_agent=self.ad.random_ua() if self.ad else
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    locale='en-US',
                )

                page = await context.new_page()

                # 注入 stealth script
                if self.ad and hasattr(self.ad, 'stealth_js'):
                    await page.add_init_script(self.ad.stealth_js)

                # 訪問頁面
                logger.info(f"訪問 LinkedIn: {linkedin_url}")
                await page.goto(linkedin_url, wait_until='domcontentloaded',
                                timeout=30000)

                # 等待載入
                await page.wait_for_timeout(3000)

                # 模擬人類滾動
                for _ in range(3):
                    await page.evaluate(
                        'window.scrollBy(0, Math.random() * 400 + 200)'
                    )
                    await page.wait_for_timeout(1000 + int(1000 * __import__('random').random()))

                # 嘗試從 DOM 提取文字（比 OCR 更準確）
                dom_text = await self._extract_from_dom(page)

                # 如果 DOM 提取失敗或不完整，截圖做 OCR
                if not dom_text or len(dom_text) < 50:
                    screenshot = await page.screenshot(full_page=False)
                    if self.ocr:
                        ocr_result = self.ocr.extract_from_screenshot(screenshot)
                        dom_text = ocr_result.get('raw_text', '')
                        result['headline'] = ocr_result.get('title', '')
                        result['experience'] = ocr_result.get('company', '')

                result['raw_text'] = dom_text[:2000]  # 限制長度
                result['success'] = True

                # 提取 headline（如果沒有從 OCR 取得）
                if not result['headline']:
                    result['headline'] = await self._extract_headline(page)

                # 提取經歷
                if not result['experience']:
                    result['experience'] = await self._extract_experience(page)

                await context.close()

            except Exception as e:
                logger.error(f"LinkedIn 頁面訪問失敗: {e}")
                result['error'] = str(e)
            finally:
                await browser.close()

        return result

    async def _extract_from_dom(self, page) -> str:
        """從 DOM 提取可見文字"""
        try:
            # LinkedIn 頁面的常見 selectors
            selectors = [
                'h1',  # 姓名
                '.text-body-medium',  # headline
                '.pv-about-section',  # About
                '.experience-section',  # 經歷
                '.pv-skill-categories-section',  # 技能
                '#profile-content',  # 主要內容區
                'main',  # 主區域
            ]

            texts = []
            for sel in selectors:
                try:
                    elements = await page.query_selector_all(sel)
                    for el in elements:
                        text = await el.inner_text()
                        if text and text.strip():
                            texts.append(text.strip())
                except Exception:
                    continue

            return '\n'.join(texts)
        except Exception:
            return ''

    async def _extract_headline(self, page) -> str:
        """提取 LinkedIn headline"""
        try:
            # 嘗試多個 selector
            for sel in ['h1 + .text-body-medium', '.top-card-layout__headline',
                        '[data-anonymize="headline"]']:
                el = await page.query_selector(sel)
                if el:
                    text = await el.inner_text()
                    if text and text.strip():
                        return text.strip()
        except Exception:
            pass
        return ''

    async def _extract_experience(self, page) -> str:
        """提取公司經歷"""
        try:
            companies = []
            for sel in ['.experience-section .pv-entity__company-summary-info h3',
                        '.top-card-layout__entity-info',
                        '[data-anonymize="company-name"]']:
                elements = await page.query_selector_all(sel)
                for el in elements:
                    text = await el.inner_text()
                    if text and text.strip():
                        companies.append(text.strip())
            return ', '.join(companies[:5])
        except Exception:
            return ''

    def analyze_sync(self, linkedin_url: str) -> Dict:
        """同步版本（用於 Flask route）"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 如果已經在 async context 中
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run, self.analyze(linkedin_url)
                    )
                    return future.result(timeout=60)
            else:
                return loop.run_until_complete(self.analyze(linkedin_url))
        except RuntimeError:
            return asyncio.run(self.analyze(linkedin_url))
