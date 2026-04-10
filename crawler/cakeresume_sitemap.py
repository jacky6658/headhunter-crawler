"""
CakeResume Sitemap 探索器 — 從 sitemap 批量發現 profile URLs，再篩選技能匹配的

策略:
  1. 下載 sitemap XML → 提取所有 /me/{username} URLs
  2. 批量 GET profile 頁面 → 解析 __NEXT_DATA__ 取得 description
  3. 用技能關鍵字匹配 description → 只保留相關的

這個模組用於離線批量建立人才庫索引，不適合即時搜尋（太慢）。
CakeResumeSearcher 用於即時搜尋（Brave API），此模組用於補充。
"""
import gzip
import json
import logging
import re
import time
from typing import List
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)

SITEMAP_INDEX = "https://sitemaps.cake.me/www/sitemap.xml.gz"
PROFILE_PATTERN = re.compile(r'https://www\.cake\.me/me/([\w\-.]+)(?:\?|$)')


def fetch_sitemap_urls(sitemap_url: str, ua: str = "Mozilla/5.0") -> List[str]:
    """下載並解析單一 sitemap XML，提取 /me/ profile URLs"""
    try:
        req = Request(sitemap_url, headers={"User-Agent": ua})
        resp = urlopen(req, timeout=30)
        data = resp.read()
        if sitemap_url.endswith('.gz'):
            data = gzip.decompress(data)
        xml = data.decode('utf-8')

        urls = set()
        for match in PROFILE_PATTERN.finditer(xml):
            username = match.group(1)
            urls.add(f"https://www.cake.me/me/{username}")
        return list(urls)

    except Exception as e:
        logger.warning(f"Sitemap fetch failed ({sitemap_url}): {e}")
        return []


def discover_profiles(sitemap_range: range = None, max_profiles: int = 500) -> List[str]:
    """
    從 CakeResume sitemap 批量發現 profile URLs

    Args:
        sitemap_range: sitemap 編號範圍 (e.g. range(70, 120))，預設 70-120
        max_profiles: 最多回傳幾個 URL

    Returns:
        List of profile URLs
    """
    if sitemap_range is None:
        sitemap_range = range(70, 120)

    all_urls = []
    for i in sitemap_range:
        if len(all_urls) >= max_profiles:
            break
        sitemap_url = f"https://sitemaps.cake.me/www/sitemap{i}.xml.gz"
        urls = fetch_sitemap_urls(sitemap_url)
        all_urls.extend(urls)
        logger.info(f"Sitemap {i}: {len(urls)} profiles (total: {len(all_urls)})")
        time.sleep(0.5)  # 不要太快

    return all_urls[:max_profiles]
