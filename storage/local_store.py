"""
本地 JSON 儲存層 — 取代 Google Sheets，所有候選人資料存在 data/candidates.json
"""
import json
import logging
import os
import threading
from datetime import datetime
from typing import List, Optional

from storage.models import Candidate, ProcessedRecord

logger = logging.getLogger(__name__)


class LocalStore:
    """本地 JSON CRUD，多客戶分離 + 去重"""

    def __init__(self, data_dir: str = 'data'):
        self.data_dir = data_dir
        self.candidates_file = os.path.join(data_dir, 'candidates.json')
        self.processed_file = os.path.join(data_dir, 'processed.json')
        self._write_lock = threading.Lock()

        os.makedirs(data_dir, exist_ok=True)
        self._candidates = self._load_json(self.candidates_file) or {}
        # candidates: { "client_name": [ {candidate_dict}, ... ], ... }
        self._processed = self._load_json(self.processed_file) or []
        # processed: [ {processed_record_dict}, ... ]

        total = sum(len(v) for v in self._candidates.values())
        logger.info(f"LocalStore 已載入: {len(self._candidates)} 客戶, {total} 位候選人")

    # ── 工具方法 ─────────────────────────────────────────────

    def _load_json(self, filepath: str):
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"載入 {filepath} 失敗: {e}")
            return None

    def _save_candidates(self):
        try:
            with open(self.candidates_file, 'w', encoding='utf-8') as f:
                json.dump(self._candidates, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"儲存候選人資料失敗: {e}")

    def _save_processed(self):
        try:
            with open(self.processed_file, 'w', encoding='utf-8') as f:
                json.dump(self._processed, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"儲存已處理紀錄失敗: {e}")

    # ── 候選人 CRUD ──────────────────────────────────────────

    def write_candidates(self, client_name: str, candidates: List[Candidate]) -> dict:
        """批次寫入候選人（去重後新增）"""
        with self._write_lock:
            if client_name not in self._candidates:
                self._candidates[client_name] = []

            existing = self._candidates[client_name]
            new_count = 0
            skipped_count = 0

            for c in candidates:
                # 去重：查已有資料（LinkedIn URL 或 GitHub URL）
                if self._is_duplicate(c.linkedin_url, c.github_url):
                    skipped_count += 1
                    continue

                # Candidate → dict
                c_dict = c.to_dict() if hasattr(c, 'to_dict') else dict(c)

                # list 欄位特殊處理（skills 轉字串以便前端搜尋）
                if isinstance(c_dict.get('skills'), list):
                    c_dict['skills'] = ', '.join(c_dict['skills'])
                if isinstance(c_dict.get('top_repos'), list):
                    c_dict['top_repos'] = ', '.join(c_dict['top_repos'])
                # work_history / education_details 保持 list/dict
                if isinstance(c_dict.get('work_history'), list) and c_dict['work_history']:
                    c_dict['work_history'] = json.dumps(c_dict['work_history'], ensure_ascii=False)
                if isinstance(c_dict.get('education_details'), list) and c_dict['education_details']:
                    c_dict['education_details'] = json.dumps(c_dict['education_details'], ensure_ascii=False)

                c_dict['client_name'] = client_name
                existing.append(c_dict)
                new_count += 1

                # 加入已處理紀錄
                self._processed.append({
                    'linkedin_url': c.linkedin_url or '',
                    'github_url': c.github_url or '',
                    'name': c.name or '',
                    'client_name': client_name,
                    'job_title': c.job_title or '',
                    'imported_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'status': 'new',
                    'system_id': '',
                })

            self._save_candidates()
            self._save_processed()

            logger.info(f"寫入 {client_name}: 新增={new_count}, 跳過={skipped_count}")
            return {'new': new_count, 'skipped': skipped_count}

    def _is_duplicate(self, linkedin_url: str = None, github_url: str = None) -> bool:
        """檢查是否已存在（跨所有客戶）"""
        for client_data in self._candidates.values():
            for c in client_data:
                if linkedin_url and c.get('linkedin_url') == linkedin_url:
                    return True
                if github_url and c.get('github_url') == github_url:
                    return True
        return False

    def read_candidates(self, client_name: str = None, job_title: str = None,
                        status: str = None, limit: int = 100, offset: int = 0) -> dict:
        """讀取候選人"""
        results = []
        sheets = [client_name] if client_name else list(self._candidates.keys())

        for sheet_name in sheets:
            records = self._candidates.get(sheet_name, [])
            for r in records:
                if job_title and r.get('job_title') != job_title:
                    continue
                if status and r.get('status') != status:
                    continue
                # 確保有 client_name
                r_copy = dict(r)
                r_copy['client_name'] = sheet_name
                results.append(r_copy)

        total = len(results)
        results = results[offset:offset + limit]
        return {'data': results, 'total': total}

    def list_clients(self) -> list:
        """列出所有客戶"""
        return list(self._candidates.keys())

    def update_candidate_fields(self, client_name: str, candidate_name: str, fields: dict) -> bool:
        """根據姓名更新候選人的指定欄位（用於 enrichment 更新）"""
        with self._write_lock:
            records = self._candidates.get(client_name, [])
            for r in records:
                if (r.get('name', '').strip().lower() == candidate_name.strip().lower()):
                    for k, v in fields.items():
                        if v is not None and v != '' and v != [] and v != {}:
                            # work_history / education_details 特殊處理
                            if k in ('work_history', 'education_details') and isinstance(v, list):
                                r[k] = json.dumps(v, ensure_ascii=False)
                            else:
                                r[k] = v
                    self._save_candidates()
                    logger.info(f"更新 {candidate_name} enrichment 欄位: {list(fields.keys())}")
                    return True
            logger.warning(f"找不到候選人 {candidate_name} in {client_name}")
            return False

    def update_candidate_status(self, client_name: str, candidate_id: str, status: str):
        """更新候選人狀態"""
        with self._write_lock:
            records = self._candidates.get(client_name, [])
            for r in records:
                if r.get('id') == candidate_id:
                    r['status'] = status
                    self._save_candidates()
                    logger.info(f"更新 {candidate_id} 狀態 → {status}")
                    return
            logger.warning(f"找不到候選人 {candidate_id} in {client_name}")

    def update_candidate_score(self, client_name: str, candidate_id: str,
                               score: int, grade: str, score_detail: str):
        """更新候選人評分"""
        with self._write_lock:
            records = self._candidates.get(client_name, [])
            for r in records:
                if r.get('id') == candidate_id:
                    r['score'] = score
                    r['grade'] = grade
                    r['score_detail'] = score_detail
                    self._save_candidates()
                    logger.info(f"更新 {candidate_id} 評分 → {score} ({grade})")
                    return
            logger.warning(f"找不到候選人 {candidate_id} in {client_name}")

    # ── 已處理紀錄 ───────────────────────────────────────────

    def is_processed(self, linkedin_url: str = None, github_url: str = None) -> bool:
        """查已處理紀錄"""
        for rec in self._processed:
            if linkedin_url and rec.get('linkedin_url') == linkedin_url:
                return True
            if github_url and rec.get('github_url') == github_url:
                return True
        return False

    def get_processed_records(self, limit: int = 100) -> list:
        """取得已處理紀錄"""
        return self._processed[-limit:]

    def update_processed_status(self, url: str, status: str, system_id: int = None):
        """更新已處理紀錄狀態"""
        with self._write_lock:
            for rec in self._processed:
                if rec.get('linkedin_url') == url or rec.get('github_url') == url:
                    rec['status'] = status
                    if system_id is not None:
                        rec['system_id'] = system_id
                    self._save_processed()
                    return

    # ── 統計 ─────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """統計資料（Dashboard 用）"""
        stats = {
            'total_candidates': 0,
            'today_new': 0,
            'clients': {},
            'sources': {'linkedin': 0, 'github': 0, 'li+ocr': 0},
            'grades': {'A': 0, 'B': 0, 'C': 0, 'D': 0, '': 0},
        }
        today = datetime.now().strftime('%Y-%m-%d')

        for client_name, records in self._candidates.items():
            stats['clients'][client_name] = len(records)
            stats['total_candidates'] += len(records)

            for r in records:
                source = (r.get('source') or '').lower()
                if source in stats['sources']:
                    stats['sources'][source] += 1
                if (r.get('search_date') or '').startswith(today):
                    stats['today_new'] += 1
                grade = str(r.get('grade', '')).strip()
                if grade in stats['grades']:
                    stats['grades'][grade] += 1
                else:
                    stats['grades'][''] += 1

        return stats
