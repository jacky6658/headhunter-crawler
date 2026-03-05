"""
爬蟲 Worker 進程
每個 worker 自帶 Playwright browser，執行搜尋任務後 POST 結果到 Flask
"""
import json
import logging
from typing import Optional
from urllib.request import Request, urlopen

from storage.models import SearchTask
from crawler.engine import SearchEngine
from crawler.browser_pool import BrowserPool

logger = logging.getLogger(__name__)

# 進程級別的 browser（初始化一次，重複使用）
_browser_pool: Optional[BrowserPool] = None


def crawler_worker_init(headless: bool = True):
    """Worker 進程初始化：建立 Playwright browser"""
    global _browser_pool
    _browser_pool = BrowserPool(headless=headless)
    _browser_pool.start()
    logger.info("Worker browser 初始化完成")


def crawler_worker_cleanup():
    """進程結束時關閉 browser"""
    global _browser_pool
    if _browser_pool:
        _browser_pool.stop()
        _browser_pool = None


def execute_search_task(task_config: dict, app_config: dict,
                        flask_url: str = 'http://localhost:5000') -> dict:
    """
    在 worker 內執行爬蟲任務。
    完成後 POST 結果到 Flask /api/internal/results。
    """
    task = SearchTask.from_dict(task_config)
    engine = SearchEngine(app_config, task)

    try:
        candidates = engine.execute()

        # POST 結果到 Flask
        payload = json.dumps({
            'task_id': task.id,
            'candidates': [c.to_dict() for c in candidates],
            'client_name': task.client_name,
        }).encode('utf-8')

        req = Request(
            f"{flask_url}/api/internal/results",
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            urlopen(req, timeout=30)
        except Exception as e:
            logger.error(f"POST 結果到 Flask 失敗: {e}")

        return {
            'success': True,
            'task_id': task.id,
            'count': len(candidates),
        }

    except Exception as e:
        logger.error(f"Worker 執行失敗: {e}")
        return {
            'success': False,
            'task_id': task.id,
            'error': str(e),
        }
