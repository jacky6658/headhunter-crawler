"""
Google Cache + Wayback Machine 讀取器 — 繞過 LinkedIn authwall

技術:
  1. Google Cache: webcache.googleusercontent.com → 曾被 Google 快取的頁面
  2. Wayback Machine: web.archive.org → 歷史快照
  3. Bing Cache: cc.bingj.com → Bing 的快取版本

用於讀取 LinkedIn profile 的完整資料，即使現在被 authwall 擋住。
"""
import json
import logging
import re
import time
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)


def read_linkedin_cache(linkedin_url: str, ad) -> Optional[dict]:
    """
    嘗試從快取讀取 LinkedIn profile

    依序嘗試: Google Cache → Wayback Machine → Bing Cache

    Args:
        linkedin_url: LinkedIn profile URL
        ad: AntiDetect instance (用 http_get)

    Returns:
        {'name': str, 'title': str, 'company': str, 'skills': list, 'source': str} 或 None
    """
    if not linkedin_url or 'linkedin.com/in/' not in linkedin_url:
        return None

    # 1. Google Cache
    result = _try_google_cache(linkedin_url, ad)
    if result:
        result['_cache_source'] = 'google_cache'
        return result

    # 2. Wayback Machine
    result = _try_wayback(linkedin_url, ad)
    if result:
        result['_cache_source'] = 'wayback'
        return result

    return None


def _try_google_cache(url: str, ad) -> Optional[dict]:
    """Google Cache 讀取"""
    cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{quote(url)}"
    try:
        html, status = ad.http_get(cache_url, timeout=10)
        if not html or status != 200:
            return None
        if 'authwall' in html.lower() or 'sign in' in html.lower()[:500]:
            return None
        return _parse_linkedin_html(html, 'google_cache')
    except Exception as e:
        logger.debug(f"Google Cache failed ({url}): {e}")
        return None


def _try_wayback(url: str, ad) -> Optional[dict]:
    """Wayback Machine 讀取 — 先查有沒有快照，再讀最新的"""
    try:
        # 查詢可用快照
        check_url = f"https://archive.org/wayback/available?url={quote(url)}"
        data, status = ad.http_get_json(check_url, timeout=10)
        if status != 200:
            return None

        snapshots = data.get('archived_snapshots', {})
        closest = snapshots.get('closest', {})
        if not closest.get('available'):
            return None

        snapshot_url = closest.get('url', '')
        if not snapshot_url:
            return None

        # 讀取快照
        html, s2 = ad.http_get(snapshot_url, timeout=15)
        if not html:
            return None

        return _parse_linkedin_html(html, 'wayback')

    except Exception as e:
        logger.debug(f"Wayback failed ({url}): {e}")
        return None


def _parse_linkedin_html(html: str, source: str) -> Optional[dict]:
    """
    從 LinkedIn HTML（快取版）解析 profile 資料

    LinkedIn 的 HTML 結構即使在快取中也包含:
    - <title> 裡有名字和職稱
    - meta description 有完整摘要
    - 經驗區塊有公司名和職稱
    """
    if not html:
        return None

    result = {
        'name': '',
        'title': '',
        'company': '',
        'location': '',
        'skills': [],
        'experience': [],
        'education': [],
        'bio': '',
    }

    # 1. 從 <title> 提取名字和職稱
    title_match = re.search(r'<title>([^<]+)</title>', html)
    if title_match:
        raw_title = title_match.group(1).strip()
        # 格式: "名字 - 職稱 - 公司 | LinkedIn"
        raw_title = re.sub(r'\s*[\|｜]\s*LinkedIn.*$', '', raw_title)
        parts = re.split(r'\s*[-–]\s*', raw_title, maxsplit=2)
        if len(parts) >= 1:
            result['name'] = parts[0].strip()
        if len(parts) >= 2:
            result['title'] = parts[1].strip()
        if len(parts) >= 3:
            result['company'] = parts[2].strip()

    # 2. 從 meta description 提取更多資訊
    desc_match = re.search(
        r'<meta[^>]*(?:name|property)="(?:og:)?description"[^>]*content="([^"]*)"', html)
    if desc_match:
        desc = desc_match.group(1)
        result['bio'] = desc[:300]

        # 提取 "Experience: X at Company" 模式
        exp_matches = re.findall(r'Experience:\s*([^·]+)', desc)
        for exp in exp_matches:
            result['experience'].append(exp.strip())

        # 提取 "Skills: X, Y, Z"
        skill_match = re.search(r'Skills?:\s*([^·]+)', desc)
        if skill_match:
            skills = [s.strip() for s in skill_match.group(1).split(',') if s.strip()]
            result['skills'] = skills

        # 提取 "Education: X"
        edu_match = re.search(r'Education:\s*([^·]+)', desc)
        if edu_match:
            result['education'].append(edu_match.group(1).strip())

        # 提取地區
        loc_match = re.search(r'Location:\s*([^·]+)', desc)
        if loc_match:
            result['location'] = loc_match.group(1).strip()

    # 3. 從 HTML body 提取更多技能（如果有）
    # LinkedIn 有時在 body 裡有 skill endorsement
    skill_patterns = re.findall(
        r'(?:skill|endorsement)[^>]*>([^<]{2,30})</(?:span|div|li)', html, re.IGNORECASE)
    for sp in skill_patterns:
        skill = sp.strip()
        if skill and skill not in result['skills'] and len(skill) < 30:
            result['skills'].append(skill)

    # 確保至少有名字才回傳
    if not result['name']:
        return None

    logger.info(f"Cache 解析成功 ({source}): {result['name']} | "
                f"title={result['title']} | skills={len(result['skills'])}")
    return result


def batch_read_cache(linkedin_urls: list, ad, delay: float = 1.0) -> dict:
    """
    批量讀取 LinkedIn profile 快取

    Args:
        linkedin_urls: LinkedIn URL 列表
        ad: AntiDetect instance
        delay: 每次請求間隔

    Returns:
        {url: result_dict} 成功的結果
    """
    results = {}
    for url in linkedin_urls:
        result = read_linkedin_cache(url, ad)
        if result:
            results[url] = result
        time.sleep(delay)

    logger.info(f"Cache 批量讀取: {len(linkedin_urls)} URLs → {len(results)} 成功")
    return results
