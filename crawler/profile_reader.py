"""
個人檔案讀取 — GitHub / LinkedIn 頁面
來源: profile-reader.py L126-430
移除: enrich_candidate_for_scoring (不接 AI)
"""
import logging
import random
import re
from typing import Dict, Optional

logger = logging.getLogger(__name__)

try:
    from playwright.sync_api import Page, BrowserContext
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class ProfileReader:
    """
    讀取候選人 GitHub / LinkedIn 個人頁面。
    接受外部 browser context（從瀏覽器池取用）。
    """

    def __init__(self, anti_detect, ocr=None, context_rotation: int = 5):
        self.ad = anti_detect
        self.ocr = ocr
        self._context: Optional['BrowserContext'] = None
        self._browser = None
        self._candidate_count = 0
        self._context_rotation = context_rotation

    def set_browser(self, browser):
        """設定外部 browser（從 BrowserPool 取得）"""
        self._browser = browser
        self._new_context()

    def _new_context(self):
        """建立新的 browser context"""
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
        if not self._browser:
            return

        self._context = self._browser.new_context(
            user_agent=self.ad.get_random_ua(),
            viewport={
                'width': random.randint(1280, 1440),
                'height': random.randint(700, 900),
            },
            locale='zh-TW',
            timezone_id='Asia/Taipei',
            extra_http_headers={
                'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'DNT': '1',
            },
        )
        self.ad.apply_stealth(self._context)

    def close(self):
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass

    def _open_page(self, url: str, timeout_ms: int = None) -> Optional['Page']:
        """開啟頁面"""
        timeout_ms = timeout_ms or self.ad.profile_read_timeout
        if not PLAYWRIGHT_AVAILABLE or not self._context:
            return None

        self._candidate_count += 1
        if self._candidate_count % self._context_rotation == 0:
            logger.debug("輪換 browser context")
            self._new_context()

        page = self._context.new_page()
        self.ad.apply_page_stealth(page)
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
            self.ad.human_delay(1.5, 3.5)
            self.ad.random_mouse_wiggle(page)
            self.ad.human_scroll(page)
            return page
        except Exception as e:
            logger.warning(f"開啟頁面失敗 {url}: {e}")
            try:
                page.close()
            except Exception:
                pass
            return None

    # ── GitHub ───────────────────────────────────────────────

    def read_github_profile(self, github_url: str) -> Dict:
        """讀取 GitHub 個人頁"""
        result = {
            'url': github_url,
            'name': '', 'bio': '', 'company': '', 'location': '',
            'followers': 0,
            'pinned_repos': [],
            'languages': [],
            'readme_text': '',
            'is_active': False,
            'read_success': False,
        }
        if not PLAYWRIGHT_AVAILABLE or not self._context:
            return result

        logger.info(f"讀取 GitHub: {github_url}")
        page = self._open_page(github_url)
        if not page:
            return result

        try:
            for sel, field in [
                ('[itemprop="name"], .p-name', 'name'),
                ('[data-bio-text], .p-note', 'bio'),
                ('[itemprop="worksFor"], .p-org', 'company'),
                ('[itemprop="homeLocation"], .p-label', 'location'),
            ]:
                el = page.query_selector(sel)
                if el:
                    result[field] = el.inner_text().strip()

            followers_el = page.query_selector('a[href$="?tab=followers"] .text-bold')
            if followers_el:
                try:
                    result['followers'] = int(
                        followers_el.inner_text().replace(',', '').replace('k', '000').strip()
                    )
                except ValueError:
                    pass

            for item in page.query_selector_all('.pinned-item-list-item')[:6]:
                name_el = item.query_selector('.repo')
                desc_el = item.query_selector('p.pinned-item-desc')
                lang_el = item.query_selector('[itemprop="programmingLanguage"]')
                stars_el = item.query_selector('.pinned-item-meta svg.octicon-star + span')
                if name_el:
                    lang = lang_el.inner_text().strip() if lang_el else ''
                    result['pinned_repos'].append({
                        'name': name_el.inner_text().strip(),
                        'description': desc_el.inner_text().strip() if desc_el else '',
                        'language': lang,
                        'stars': stars_el.inner_text().strip() if stars_el else '0',
                    })
                    if lang and lang not in result['languages']:
                        result['languages'].append(lang)

            readme_el = page.query_selector('.markdown-body')
            if readme_el:
                result['readme_text'] = readme_el.inner_text().strip()[:3000]

            contrib_svg = page.query_selector('.js-calendar-graph-svg')
            if contrib_svg:
                contrib_html = contrib_svg.inner_html()
                active_days = len(re.findall(r'data-count="([1-9]\d*)"', contrib_html))
                result['is_active'] = active_days > 15

            result['read_success'] = True
            logger.info(f"  GitHub OK: {result['name']} | langs={result['languages']}")

        except Exception as e:
            result['error'] = str(e)
            logger.error(f"  GitHub 讀取例外: {e}")
        finally:
            page.close()

        return result

    # ── LinkedIn ─────────────────────────────────────────────

    def read_linkedin_profile(self, linkedin_url: str) -> Dict:
        """讀取 LinkedIn 個人頁（未登入狀態）"""
        result = {
            'url': linkedin_url,
            'name': '', 'headline': '', 'location': '',
            'summary': '',
            'current_position': '', 'current_company': '',
            'read_success': False,
            'login_required': False,
            'ocr_used': False,
        }
        if not PLAYWRIGHT_AVAILABLE or not self._context:
            return result

        logger.info(f"讀取 LinkedIn: {linkedin_url}")
        page = self._open_page(linkedin_url, timeout_ms=self.ad.page_load_timeout)
        if not page:
            return result

        try:
            current_url = page.url

            # 偵測登入牆
            if any(kw in current_url for kw in [
                'linkedin.com/login', 'linkedin.com/checkpoint',
                'linkedin.com/authwall',
            ]):
                result['login_required'] = True
                logger.warning("LinkedIn 要求登入")

                # OCR 嘗試從截圖提取可見資訊
                if self.ocr and self.ocr.enabled:
                    screenshot = page.screenshot()
                    ocr_result = self.ocr.extract_from_screenshot(screenshot)
                    if ocr_result.get('success'):
                        result['name'] = ocr_result.get('name', '')
                        result['headline'] = ocr_result.get('title', '')
                        result['current_company'] = ocr_result.get('company', '')
                        result['location'] = ocr_result.get('location', '')
                        result['ocr_used'] = True
                        result['read_success'] = True
                        logger.info(f"  OCR 補充: {result['name']}")

                page.close()
                return result

            # 姓名
            for sel in [
                'h1.top-card-layout__title', 'h1[class*="name"]',
                '.top-card__title', 'h1',
            ]:
                el = page.query_selector(sel)
                if el:
                    text = el.inner_text().strip()
                    if text and len(text) < 80:
                        result['name'] = text
                        break

            # Headline
            for sel in [
                '.top-card-layout__headline', '.top-card-layout__second-subline',
                '[class*="headline"]',
                '.top-card__sublines .top-card__subline-item:first-child',
            ]:
                el = page.query_selector(sel)
                if el:
                    text = el.inner_text().strip()
                    if text:
                        result['headline'] = text
                        break

            # 地點
            for sel in [
                '.top-card__subline-item', '[class*="location"]',
                '.profile-info-subheader .not-first-middot',
            ]:
                el = page.query_selector(sel)
                if el:
                    text = el.inner_text().strip()
                    if text and len(text) < 60:
                        result['location'] = text
                        break

            # Summary
            for sel in [
                '.core-section-container__content .show-more-less-html__markup',
                '.about-section p', '[class*="about"] p', '.summary',
            ]:
                el = page.query_selector(sel)
                if el:
                    result['summary'] = el.inner_text().strip()[:1000]
                    break

            # 目前職位
            for pos_sel, comp_sel in [
                ('.experience-item h3', '.experience-item h4'),
                ('.experience__list-item h3', '.experience__list-item h4'),
            ]:
                pos_el = page.query_selector(pos_sel)
                comp_el = page.query_selector(comp_sel)
                if pos_el:
                    result['current_position'] = pos_el.inner_text().strip()
                if comp_el:
                    result['current_company'] = comp_el.inner_text().strip()
                if pos_el:
                    break

            result['read_success'] = bool(result['name'] or result['headline'])

            # 讀取不完整時，用 OCR 補充
            if not result['read_success'] and self.ocr and self.ocr.enabled:
                screenshot = page.screenshot()
                ocr_result = self.ocr.extract_from_screenshot(screenshot)
                if ocr_result.get('success'):
                    if not result['name']:
                        result['name'] = ocr_result.get('name', '')
                    if not result['headline']:
                        result['headline'] = ocr_result.get('title', '')
                    result['ocr_used'] = True
                    result['read_success'] = True

            logger.info(f"  LinkedIn: {result['name']} | {result['headline'][:40] if result['headline'] else '(無)'}")

        except Exception as e:
            result['error'] = str(e)
            logger.error(f"  LinkedIn 讀取例外: {e}")
        finally:
            page.close()

        return result
