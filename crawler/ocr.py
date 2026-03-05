"""
OCR 圖像辨識模組 — 截圖提取、履歷圖片、CAPTCHA
使用 pytesseract + Pillow，需系統安裝 Tesseract-OCR
"""
import io
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import pytesseract
    from PIL import Image, ImageFilter, ImageOps
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False
    logger.warning("pytesseract 或 Pillow 未安裝，OCR 功能停用")


class CrawlerOCR:
    """OCR 圖像辨識"""

    def __init__(self, config: dict):
        self.enabled = config.get('crawler', {}).get('ocr_enabled', True) and OCR_AVAILABLE
        if not self.enabled:
            logger.info("OCR 停用")

    def extract_from_screenshot(self, screenshot_bytes: bytes) -> dict:
        """
        場景 1: LinkedIn 頁面截圖 → 提取姓名/職稱/公司/地區
        被登入牆擋住時，頁面仍可能顯示部分資訊
        """
        result = {
            'name': '', 'title': '', 'company': '',
            'location': '', 'raw_text': '', 'success': False,
        }
        if not self.enabled:
            return result

        try:
            img = Image.open(io.BytesIO(screenshot_bytes))
            # 中英文混合辨識
            text = pytesseract.image_to_string(img, lang='chi_tra+eng')
            result['raw_text'] = text.strip()

            if not text.strip():
                return result

            lines = [l.strip() for l in text.split('\n') if l.strip()]

            # 嘗試提取結構化資料
            result['name'] = self._extract_name(lines)
            result['title'] = self._extract_title(lines)
            result['company'] = self._extract_company(lines)
            result['location'] = self._extract_location(lines, text)
            result['success'] = bool(result['name'] or result['title'])

            logger.info(f"OCR 截圖提取: name={result['name']}, title={result['title'][:30]}")

        except Exception as e:
            logger.error(f"OCR 截圖提取失敗: {e}")

        return result

    def extract_from_resume_image(self, image_path: str) -> dict:
        """
        場景 2: 履歷圖片/掃描 PDF → 提取文字
        """
        result = {
            'raw_text': '', 'detected_skills': [],
            'detected_name': '', 'detected_company': '',
            'success': False,
        }
        if not self.enabled:
            return result

        try:
            img = Image.open(image_path)
            text = pytesseract.image_to_string(img, lang='chi_tra+eng')
            result['raw_text'] = text.strip()

            if text.strip():
                result['detected_skills'] = self._detect_skills_in_text(text)
                result['detected_name'] = self._extract_name(
                    [l.strip() for l in text.split('\n') if l.strip()])
                result['success'] = True

        except Exception as e:
            logger.error(f"OCR 履歷圖片失敗: {e}")

        return result

    def solve_simple_captcha(self, captcha_image_bytes: bytes) -> Optional[str]:
        """
        場景 3: 簡單文字 CAPTCHA → 辨識
        reCAPTCHA / hCaptcha 不支援
        """
        if not self.enabled:
            return None

        try:
            img = Image.open(io.BytesIO(captcha_image_bytes))

            # 前處理：灰階 + 二值化 + 降噪
            img = ImageOps.grayscale(img)
            img = img.point(lambda x: 0 if x < 128 else 255)
            img = img.filter(ImageFilter.MedianFilter(size=3))

            # OCR（單行文字模式）
            custom_config = r'--oem 3 --psm 7'
            text = pytesseract.image_to_string(img, config=custom_config).strip()

            # 清理非英數字元
            text = re.sub(r'[^A-Za-z0-9]', '', text)

            if text:
                logger.info(f"CAPTCHA OCR 辨識: {text}")
                return text
            return None

        except Exception as e:
            logger.error(f"CAPTCHA OCR 失敗: {e}")
            return None

    # ── 內部提取方法 ─────────────────────────────────────────

    @staticmethod
    def _extract_name(lines: list) -> str:
        """嘗試從 OCR 文字行中提取姓名"""
        for line in lines[:5]:
            # 跳過太長或太短的行
            if len(line) < 2 or len(line) > 30:
                continue
            # 跳過明顯非姓名的行
            if any(kw in line.lower() for kw in [
                'linkedin', 'experience', 'education', 'about', 'sign in',
                'join now', 'connections', 'followers', 'following',
            ]):
                continue
            # 中文姓名（2-4 字）或英文姓名
            if re.match(r'^[\u4e00-\u9fff]{2,4}$', line):
                return line
            if re.match(r'^[A-Z][a-z]+ [A-Z][a-z]+', line):
                return line
        return ''

    @staticmethod
    def _extract_title(lines: list) -> str:
        """提取職稱"""
        title_keywords = [
            'engineer', 'developer', 'manager', 'designer', 'architect',
            'lead', 'senior', 'director', 'analyst', 'consultant',
            '工程師', '經理', '設計師', '架構師', '開發者', '主管',
        ]
        for line in lines[:10]:
            if any(kw in line.lower() for kw in title_keywords):
                return line[:100]
        return ''

    @staticmethod
    def _extract_company(lines: list) -> str:
        """提取公司名"""
        company_keywords = ['at ', '@ ', '在 ', 'inc', 'ltd', 'corp', '公司', '股份', '科技']
        for line in lines[:10]:
            if any(kw in line.lower() for kw in company_keywords):
                # 清理前綴
                for prefix in ['at ', '@ ', '在 ']:
                    if line.lower().startswith(prefix):
                        return line[len(prefix):].strip()
                return line[:60]
        return ''

    @staticmethod
    def _extract_location(lines: list, full_text: str) -> str:
        """提取地區"""
        location_keywords = [
            'taiwan', '台灣', '台北', '新竹', '台中', '高雄', '桃園',
            'taipei', 'hsinchu', 'taichung', 'kaohsiung',
            'singapore', 'hong kong', 'tokyo', 'shanghai',
        ]
        lower = full_text.lower()
        for kw in location_keywords:
            if kw in lower:
                return kw.title()
        return ''

    @staticmethod
    def _detect_skills_in_text(text: str) -> list:
        """從文字中偵測技能關鍵字"""
        known_skills = [
            'Python', 'Java', 'JavaScript', 'TypeScript', 'React', 'Vue',
            'Angular', 'Node.js', 'Go', 'Rust', 'C++', 'C#', 'Swift',
            'Kotlin', 'Docker', 'Kubernetes', 'AWS', 'GCP', 'Azure',
            'PostgreSQL', 'MySQL', 'MongoDB', 'Redis', 'Elasticsearch',
            'Spring', 'Django', 'Flask', 'FastAPI', 'TensorFlow', 'PyTorch',
        ]
        found = []
        text_lower = text.lower()
        for skill in known_skills:
            if skill.lower() in text_lower:
                found.append(skill)
        return found
