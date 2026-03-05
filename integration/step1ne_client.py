"""
Step1ne Headhunter System API 客戶端
完全可選 — 不設定 API 位址時不影響爬蟲獨立運作
"""
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class Step1neClient:
    """連結 Step1ne Headhunter System API"""

    def __init__(self, api_base_url: str = None):
        self.api_base = (api_base_url or '').rstrip('/')

    def is_connected(self) -> bool:
        """檢查是否已設定且可連線"""
        if not self.api_base:
            return False
        try:
            req = Request(f"{self.api_base}/api/health",
                          headers={'Accept': 'application/json'})
            resp = urlopen(req, timeout=5)
            return resp.status == 200
        except Exception:
            # health endpoint 可能不存在，嘗試 /api/jobs
            try:
                req = Request(f"{self.api_base}/api/jobs",
                              headers={'Accept': 'application/json'})
                resp = urlopen(req, timeout=5)
                return resp.status == 200
            except Exception:
                return False

    # 活躍的職缺狀態（排除已暫停、暫不開放等）
    ACTIVE_STATUSES = {'招募中', '開放中'}

    def fetch_jobs(self, status: str = None) -> list:
        """
        GET /api/jobs → 返回職缺列表

        Args:
            status: 篩選特定狀態。None = 返回所有活躍狀態（招募中 + 開放中）
                    傳入 'all' = 不篩選
        """
        if not self.api_base:
            return []
        try:
            req = Request(f"{self.api_base}/api/jobs",
                          headers={'Accept': 'application/json'})
            resp = urlopen(req, timeout=10)
            data = json.loads(resp.read().decode('utf-8'))

            # API 可能返回 {data: [...]} 或直接 [...]
            jobs = data if isinstance(data, list) else data.get('data', data.get('jobs', []))

            # 篩選狀態
            if status == 'all':
                pass  # 不篩選
            elif status:
                jobs = [j for j in jobs if j.get('job_status') == status]
            else:
                # 預設: 活躍狀態
                jobs = [j for j in jobs
                        if j.get('job_status') in self.ACTIVE_STATUSES]

            logger.info(f"Step1ne API: 取得 {len(jobs)} 個職缺 (status={status or 'active'})")
            return jobs

        except Exception as e:
            logger.error(f"Step1ne fetch_jobs 失敗: {e}")
            return []

    def fetch_job_detail(self, job_id: int) -> dict:
        """GET /api/jobs/:id → 單一職缺完整資料"""
        if not self.api_base:
            return {}
        try:
            req = Request(f"{self.api_base}/api/jobs/{job_id}",
                          headers={'Accept': 'application/json'})
            resp = urlopen(req, timeout=10)
            data = json.loads(resp.read().decode('utf-8'))
            return data if isinstance(data, dict) else data.get('data', {})
        except Exception as e:
            logger.error(f"Step1ne fetch_job_detail 失敗: {e}")
            return {}

    def push_candidates(self, candidates: list, actor: str = 'Crawler') -> dict:
        """
        POST /api/candidates/bulk → 批次回寫候選人到系統
        """
        if not self.api_base:
            return {'success': False, 'error': 'API 未設定'}
        try:
            payload = json.dumps({
                'candidates': candidates,
                'actor': actor,
            }).encode('utf-8')

            req = Request(
                f"{self.api_base}/api/candidates/bulk",
                data=payload,
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
                method='POST',
            )
            resp = urlopen(req, timeout=30)
            result = json.loads(resp.read().decode('utf-8'))
            logger.info(f"Step1ne push_candidates: {result}")
            return {'success': True, 'data': result}
        except HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            logger.error(f"Step1ne push_candidates HTTP {e.code}: {body}")
            return {'success': False, 'error': f'HTTP {e.code}', 'detail': body}
        except Exception as e:
            logger.error(f"Step1ne push_candidates 失敗: {e}")
            return {'success': False, 'error': str(e)}

    def push_candidates_v2(self, candidates: list, actor: str = 'Crawler') -> dict:
        """
        POST /api/crawler/import → 使用新版匯入端點
        直接傳送爬蟲格式資料，由 Step1ne 端進行欄位映射。
        支援所有等級（含 D 級）。
        """
        if not self.api_base:
            return {'success': False, 'error': 'API 未設定'}
        if not candidates:
            return {'success': True, 'message': '無候選人需要推送', 'created_count': 0, 'updated_count': 0}
        try:
            # 直接傳爬蟲原始格式，Step1ne 端會映射
            payload = json.dumps({
                'candidates': candidates,
                'actor': actor,
            }).encode('utf-8')

            req = Request(
                f"{self.api_base}/api/crawler/import",
                data=payload,
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
                method='POST',
            )
            resp = urlopen(req, timeout=60)
            result = json.loads(resp.read().decode('utf-8'))
            logger.info(
                f"Step1ne push_candidates_v2: "
                f"新增 {result.get('created_count', 0)}，"
                f"更新 {result.get('updated_count', 0)}，"
                f"失敗 {result.get('failed_count', 0)}"
            )
            return result
        except HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            logger.error(f"Step1ne push_candidates_v2 HTTP {e.code}: {body}")
            return {'success': False, 'error': f'HTTP {e.code}', 'detail': body}
        except Exception as e:
            logger.error(f"Step1ne push_candidates_v2 失敗: {e}")
            return {'success': False, 'error': str(e)}

    def test_connection(self) -> dict:
        """測試連線，回傳狀態資訊"""
        if not self.api_base:
            return {'connected': False, 'error': 'API 位址未設定'}

        connected = self.is_connected()
        if not connected:
            return {'connected': False, 'error': '無法連線到 Step1ne 系統'}

        jobs = self.fetch_jobs()
        return {
            'connected': True,
            'api_base': self.api_base,
            'job_count': len(jobs),
        }
