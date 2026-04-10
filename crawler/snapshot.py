"""
數據快照 + 異常截圖模組

1. Snapshot: 進入候選人頁面後，先存 raw HTML → 即使解析失敗也能離線重新解析
2. Error Screen: 異常時自動截圖 → 一眼看出是被擋還是找不到元素
"""
import logging
import os
import re
from datetime import datetime

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_HTML_DIR = os.path.join(BASE_DIR, 'data', 'raw_html')
ERROR_SCREEN_DIR = os.path.join(BASE_DIR, 'data', 'error_screens')

# 確保目錄存在
os.makedirs(RAW_HTML_DIR, exist_ok=True)
os.makedirs(ERROR_SCREEN_DIR, exist_ok=True)


def _safe_filename(name: str, max_len: int = 80) -> str:
    """把名字轉成安全的檔名"""
    safe = re.sub(r'[^\w\u4e00-\u9fff\-.]', '_', name)
    return safe[:max_len]


def save_snapshot(html: str, candidate_name: str, source: str = '', url: str = '') -> str:
    """
    存候選人頁面的 raw HTML

    Args:
        html: 網頁原始碼
        candidate_name: 候選人名字
        source: 來源 (linkedin/cakeresume/github)
        url: 原始 URL

    Returns:
        存檔路徑
    """
    if not html:
        return ''

    today = datetime.now().strftime('%Y%m%d')
    safe_name = _safe_filename(candidate_name)
    filename = f"{today}_{source}_{safe_name}.html"
    filepath = os.path.join(RAW_HTML_DIR, filename)

    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            # 寫入 metadata header
            f.write(f"<!-- Snapshot: {candidate_name} -->\n")
            f.write(f"<!-- Source: {source} -->\n")
            f.write(f"<!-- URL: {url} -->\n")
            f.write(f"<!-- Date: {datetime.now().isoformat()} -->\n\n")
            f.write(html)
        logger.debug(f"Snapshot saved: {filename}")
        return filepath
    except Exception as e:
        logger.warning(f"Snapshot save failed ({candidate_name}): {e}")
        return ''


def save_error_screen(html: str = None, screenshot_bytes: bytes = None,
                       error_msg: str = '', context: str = '', url: str = '') -> str:
    """
    異常時存 HTML + 截圖

    Args:
        html: 當時的頁面 HTML（可選）
        screenshot_bytes: Playwright 截圖的 bytes（可選）
        error_msg: 錯誤訊息
        context: 上下文（如 "linkedin_search_page3"）
        url: 當時的 URL

    Returns:
        存檔路徑
    """
    now = datetime.now().strftime('%Y%m%d_%H%M%S')
    safe_ctx = _safe_filename(context or 'unknown')
    base_name = f"{now}_{safe_ctx}"

    saved = []

    # 存 HTML
    if html:
        html_path = os.path.join(ERROR_SCREEN_DIR, f"{base_name}.html")
        try:
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(f"<!-- Error: {error_msg} -->\n")
                f.write(f"<!-- URL: {url} -->\n")
                f.write(f"<!-- Context: {context} -->\n")
                f.write(f"<!-- Time: {datetime.now().isoformat()} -->\n\n")
                f.write(html)
            saved.append(html_path)
        except Exception:
            pass

    # 存截圖
    if screenshot_bytes:
        img_path = os.path.join(ERROR_SCREEN_DIR, f"{base_name}.png")
        try:
            with open(img_path, 'wb') as f:
                f.write(screenshot_bytes)
            saved.append(img_path)
        except Exception:
            pass

    # 存錯誤描述
    meta_path = os.path.join(ERROR_SCREEN_DIR, f"{base_name}.txt")
    try:
        with open(meta_path, 'w', encoding='utf-8') as f:
            f.write(f"Time: {datetime.now().isoformat()}\n")
            f.write(f"URL: {url}\n")
            f.write(f"Context: {context}\n")
            f.write(f"Error: {error_msg}\n")
            if saved:
                f.write(f"Files: {saved}\n")
    except Exception:
        pass

    if saved:
        logger.info(f"Error screen saved: {base_name} ({len(saved)} files)")
    return saved[0] if saved else ''


def list_snapshots(source: str = None, limit: int = 20) -> list:
    """列出最近的快照"""
    files = []
    for f in os.listdir(RAW_HTML_DIR):
        if f.endswith('.html'):
            if source and f"_{source}_" not in f:
                continue
            path = os.path.join(RAW_HTML_DIR, f)
            files.append({
                'filename': f,
                'path': path,
                'size': os.path.getsize(path),
                'modified': os.path.getmtime(path),
            })
    files.sort(key=lambda x: x['modified'], reverse=True)
    return files[:limit]


def list_error_screens(limit: int = 20) -> list:
    """列出最近的異常截圖"""
    files = []
    for f in os.listdir(ERROR_SCREEN_DIR):
        if f.endswith('.txt'):
            path = os.path.join(ERROR_SCREEN_DIR, f)
            files.append({
                'filename': f,
                'path': path,
                'modified': os.path.getmtime(path),
            })
    files.sort(key=lambda x: x['modified'], reverse=True)
    return files[:limit]


def reparse_snapshot(filepath: str) -> dict:
    """
    從快照重新解析候選人資料（離線解析）

    Returns:
        解析後的 candidate dict
    """
    import json

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        # 提取 metadata
        source = ''
        url = ''
        for line in content.split('\n')[:5]:
            if '<!-- Source:' in line:
                source = line.replace('<!-- Source:', '').replace('-->', '').strip()
            if '<!-- URL:' in line:
                url = line.replace('<!-- URL:', '').replace('-->', '').strip()

        # 根據來源分流解析
        if 'cakeresume' in source or 'cake.me' in url:
            return _reparse_cakeresume(content, url)
        elif 'linkedin' in source or 'linkedin.com' in url:
            return _reparse_linkedin(content, url)
        elif 'github' in source or 'github.com' in url:
            return _reparse_github(content, url)
        else:
            return {'raw_html_path': filepath, 'source': source, 'url': url}

    except Exception as e:
        logger.error(f"Reparse failed ({filepath}): {e}")
        return {}


def _reparse_cakeresume(html: str, url: str) -> dict:
    """離線解析 CakeResume 快照"""
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if match:
        import json
        data = json.loads(match.group(1))
        profile = data.get('props', {}).get('pageProps', {}).get('ssr', {}).get('profile', {})
        return {
            'name': profile.get('name', ''),
            'description': profile.get('description', ''),
            'meta': profile.get('meta_tags', {}),
            'source': 'cakeresume',
            'url': url,
        }
    return {'source': 'cakeresume', 'url': url}


def _reparse_linkedin(html: str, url: str) -> dict:
    """離線解析 LinkedIn 快照"""
    name = ''
    title = ''
    name_match = re.search(r'<title>([^<]+)</title>', html)
    if name_match:
        raw = name_match.group(1)
        parts = re.match(r'^(.+?)\s*[-–]\s*(.+?)(?:\s*\||\s*-\s*LinkedIn)', raw)
        if parts:
            name = parts.group(1).strip()
            title = parts.group(2).strip()
    return {'name': name, 'title': title, 'source': 'linkedin', 'url': url}


def _reparse_github(html: str, url: str) -> dict:
    """離線解析 GitHub 快照"""
    name_match = re.search(r'<span[^>]*itemprop="name"[^>]*>([^<]+)</span>', html)
    bio_match = re.search(r'<div[^>]*class="[^"]*user-profile-bio[^"]*"[^>]*>([^<]+)</div>', html)
    return {
        'name': name_match.group(1).strip() if name_match else '',
        'bio': bio_match.group(1).strip() if bio_match else '',
        'source': 'github',
        'url': url,
    }
