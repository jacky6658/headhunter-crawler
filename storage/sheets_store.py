"""
Google Sheets 儲存層 — gspread 實現多客戶分離 CRUD
"""
import logging
import threading
from datetime import datetime
from typing import List, Optional

from storage.models import Candidate, ProcessedRecord

logger = logging.getLogger(__name__)

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False
    logger.warning("gspread 未安裝，Google Sheets 功能停用")


SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]


class SheetsStore:
    """Google Sheets CRUD，多客戶工作表 + 已處理紀錄去重"""

    def __init__(self, spreadsheet_id: str, credentials_file: str,
                 processed_sheet_name: str = '去重'):
        if not GSPREAD_AVAILABLE:
            raise RuntimeError("gspread 未安裝")

        creds = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.spreadsheet = self.gc.open_by_key(spreadsheet_id)
        self.processed_sheet_name = processed_sheet_name
        self._write_lock = threading.Lock()
        self._header_checked = set()  # 已升級過 header 的工作表（避免重複讀取）

        # 確保「已處理紀錄」分頁存在
        self._ensure_processed_sheet()
        logger.info(f"Google Sheets 已連線: {self.spreadsheet.title}")

    def _ensure_processed_sheet(self):
        """確保已處理紀錄分頁存在"""
        try:
            self.spreadsheet.worksheet(self.processed_sheet_name)
        except gspread.WorksheetNotFound:
            ws = self.spreadsheet.add_worksheet(
                title=self.processed_sheet_name, rows=1000, cols=10)
            ws.append_row(ProcessedRecord.sheets_header())
            logger.info(f"建立「{self.processed_sheet_name}」分頁")

    # ── 客戶工作表 ───────────────────────────────────────────

    def get_or_create_client_sheet(self, client_name: str):
        """取得或建立客戶工作表"""
        try:
            ws = self.spreadsheet.worksheet(client_name)
            # 確保有新的評分欄位 header
            self._ensure_score_headers(ws)
            return ws
        except gspread.WorksheetNotFound:
            ws = self.spreadsheet.add_worksheet(
                title=client_name, rows=1000, cols=25)
            ws.append_row(Candidate.sheets_header())
            logger.info(f"建立客戶工作表: {client_name}")
            return ws

    def _ensure_score_headers(self, ws):
        """確保工作表有 score/grade/score_detail 欄位（舊表升級）"""
        # 每個工作表只檢查一次，避免大量 API 讀取導致 429
        if ws.title in self._header_checked:
            return
        self._header_checked.add(ws.title)
        try:
            header_row = ws.row_values(1)
            expected = Candidate.sheets_header()
            if len(header_row) < len(expected):
                # 先擴充欄位數（舊表可能只有 20 欄）
                needed_cols = len(expected)
                if ws.col_count < needed_cols:
                    ws.resize(cols=needed_cols + 5)
                    logger.info(f"擴充工作表欄位: {ws.col_count} → {needed_cols + 5}")

                # 缺少欄位，補上
                missing = expected[len(header_row):]
                start_col = len(header_row) + 1
                for i, col_name in enumerate(missing):
                    ws.update_cell(1, start_col + i, col_name)
                logger.info(f"升級工作表 header: 新增 {missing}")
        except Exception as e:
            logger.error(f"升級 header 失敗: {e}")

    def list_clients(self) -> list:
        """列出所有客戶（= 工作表名稱，排除「已處理紀錄」）"""
        return [
            ws.title for ws in self.spreadsheet.worksheets()
            if ws.title != self.processed_sheet_name
        ]

    # ── 候選人 CRUD ──────────────────────────────────────────

    def write_candidates(self, client_name: str, candidates: List[Candidate]) -> dict:
        """批次寫入候選人到客戶工作表 + 已處理紀錄"""
        with self._write_lock:
            ws = self.get_or_create_client_sheet(client_name)
            processed_ws = self.spreadsheet.worksheet(self.processed_sheet_name)

            new_count = 0
            skipped_count = 0
            candidate_rows = []
            processed_rows = []

            for c in candidates:
                # 去重：查已處理紀錄
                if self._is_processed_internal(processed_ws, c.linkedin_url, c.github_url):
                    skipped_count += 1
                    continue

                candidate_rows.append(c.to_sheets_row())
                processed_rows.append(ProcessedRecord(
                    linkedin_url=c.linkedin_url,
                    github_url=c.github_url,
                    name=c.name,
                    client_name=client_name,
                    job_title=c.job_title,
                    imported_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    status='new',
                ).to_sheets_row())
                new_count += 1

            # 批次寫入
            if candidate_rows:
                ws.append_rows(candidate_rows, value_input_option='USER_ENTERED')
            if processed_rows:
                processed_ws.append_rows(processed_rows, value_input_option='USER_ENTERED')

            logger.info(f"寫入 {client_name}: 新增={new_count}, 跳過={skipped_count}")
            return {'new': new_count, 'skipped': skipped_count}

    def read_candidates(self, client_name: str = None, job_title: str = None,
                        status: str = None, limit: int = 100, offset: int = 0) -> list:
        """讀取候選人"""
        results = []
        sheets = [client_name] if client_name else self.list_clients()

        for sheet_name in sheets:
            try:
                ws = self.spreadsheet.worksheet(sheet_name)
                self._ensure_score_headers(ws)
                records = ws.get_all_records()
                for r in records:
                    if job_title and r.get('job_title') != job_title:
                        continue
                    if status and r.get('status') != status:
                        continue
                    r['client_name'] = sheet_name
                    results.append(r)
            except gspread.WorksheetNotFound:
                continue

        # 分頁
        total = len(results)
        results = results[offset:offset + limit]
        return {'data': results, 'total': total}

    def update_candidate_status(self, client_name: str, candidate_id: str, status: str):
        """更新候選人狀態"""
        with self._write_lock:
            try:
                ws = self.spreadsheet.worksheet(client_name)
                cell = ws.find(candidate_id)
                if cell:
                    # status 在第 17 欄 (index 0-based = 16)
                    status_col = Candidate.sheets_header().index('status') + 1
                    ws.update_cell(cell.row, status_col, status)
                    logger.info(f"更新 {candidate_id} 狀態 → {status}")
            except Exception as e:
                logger.error(f"更新候選人狀態失敗: {e}")

    def update_candidate_score(self, client_name: str, candidate_id: str,
                                score: int, grade: str, score_detail: str):
        """更新候選人評分（OCR 重新評分後使用）"""
        with self._write_lock:
            try:
                ws = self.spreadsheet.worksheet(client_name)
                cell = ws.find(candidate_id)
                if cell:
                    header = Candidate.sheets_header()
                    score_col = header.index('score') + 1
                    grade_col = header.index('grade') + 1
                    detail_col = header.index('score_detail') + 1
                    ws.update_cell(cell.row, score_col, score)
                    ws.update_cell(cell.row, grade_col, grade)
                    ws.update_cell(cell.row, detail_col, score_detail)
                    logger.info(f"更新 {candidate_id} 評分 → {score} ({grade})")
            except Exception as e:
                logger.error(f"更新候選人評分失敗: {e}")

    # ── 已處理紀錄 ───────────────────────────────────────────

    def _is_processed_internal(self, ws, linkedin_url: str = None,
                               github_url: str = None) -> bool:
        """內部方法：查已處理紀錄（已持有 ws）"""
        try:
            if linkedin_url:
                cell = ws.find(linkedin_url)
                if cell:
                    return True
            if github_url:
                cell = ws.find(github_url)
                if cell:
                    return True
        except Exception:
            pass
        return False

    def is_processed(self, linkedin_url: str = None, github_url: str = None) -> bool:
        """查已處理紀錄"""
        try:
            ws = self.spreadsheet.worksheet(self.processed_sheet_name)
            return self._is_processed_internal(ws, linkedin_url, github_url)
        except Exception:
            return False

    def get_processed_records(self, limit: int = 100) -> list:
        """取得已處理紀錄"""
        try:
            ws = self.spreadsheet.worksheet(self.processed_sheet_name)
            return ws.get_all_records()[-limit:]
        except Exception:
            return []

    def update_processed_status(self, url: str, status: str, system_id: int = None):
        """更新已處理紀錄狀態"""
        with self._write_lock:
            try:
                ws = self.spreadsheet.worksheet(self.processed_sheet_name)
                cell = ws.find(url)
                if cell:
                    header = ProcessedRecord.sheets_header()
                    status_col = header.index('status') + 1
                    ws.update_cell(cell.row, status_col, status)
                    if system_id is not None:
                        sid_col = header.index('system_id') + 1
                        ws.update_cell(cell.row, sid_col, system_id)
            except Exception as e:
                logger.error(f"更新已處理紀錄失敗: {e}")

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

        for client_name in self.list_clients():
            try:
                ws = self.spreadsheet.worksheet(client_name)
                records = ws.get_all_records()
                stats['clients'][client_name] = len(records)
                stats['total_candidates'] += len(records)

                for r in records:
                    source = r.get('source', '').lower()
                    if source in stats['sources']:
                        stats['sources'][source] += 1
                    if r.get('search_date', '').startswith(today):
                        stats['today_new'] += 1
                    # 評分統計
                    grade = str(r.get('grade', '')).strip()
                    if grade in stats['grades']:
                        stats['grades'][grade] += 1
                    else:
                        stats['grades'][''] += 1
            except Exception:
                continue

        return stats
