"""
排程管理 — APScheduler + multiprocessing
"""
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.date import DateTrigger
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False

from storage.models import SearchTask


class TaskManager:
    """排程任務管理"""

    def __init__(self, config: dict):
        self.config = config
        self.tasks: dict = {}  # task_id → SearchTask
        self._scheduler = None

        sched_cfg = config.get('scheduler', {})
        self.tasks_file = sched_cfg.get('tasks_file', 'data/tasks.json')
        self.checkpoint_file = sched_cfg.get('checkpoint_file', 'data/checkpoints.json')

        self._load_tasks()

    def start(self):
        """啟動排程器"""
        if not SCHEDULER_AVAILABLE:
            logger.warning("APScheduler 未安裝，排程功能停用")
            return
        self._scheduler = BackgroundScheduler()
        self._scheduler.start()

        # 恢復已排程的任務
        for task_id, task in self.tasks.items():
            if task.status in ('pending', 'paused') and task.schedule_type != 'once':
                self._schedule_task(task)

        logger.info(f"排程器已啟動，{len(self.tasks)} 個任務")

    def stop(self):
        if self._scheduler:
            self._scheduler.shutdown(wait=False)

    # ── 任務 CRUD ────────────────────────────────────────────

    def add_task(self, task: SearchTask) -> str:
        """新增任務"""
        if not task.id:
            task.id = str(uuid.uuid4())[:8]
        task.created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        task.updated_at = task.created_at
        self.tasks[task.id] = task
        self._save_tasks()

        if task.schedule_type != 'once':
            self._schedule_task(task)

        logger.info(f"新增任務: {task.id} ({task.client_name}/{task.job_title})")
        return task.id

    def remove_task(self, task_id: str) -> bool:
        if task_id not in self.tasks:
            return False
        # 取消排程
        if self._scheduler:
            try:
                self._scheduler.remove_job(task_id)
            except Exception:
                pass
        del self.tasks[task_id]
        self._save_tasks()
        logger.info(f"刪除任務: {task_id}")
        return True

    def update_task(self, task_id: str, updates: dict) -> bool:
        if task_id not in self.tasks:
            return False
        task = self.tasks[task_id]
        for key, value in updates.items():
            if hasattr(task, key):
                setattr(task, key, value)
        task.updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._save_tasks()

        # 重新排程
        if self._scheduler and task.schedule_type != 'once':
            try:
                self._scheduler.remove_job(task_id)
            except Exception:
                pass
            self._schedule_task(task)

        return True

    def get_task(self, task_id: str) -> Optional[SearchTask]:
        return self.tasks.get(task_id)

    def get_all_tasks(self) -> list:
        return list(self.tasks.values())

    # ── 執行 ─────────────────────────────────────────────────

    def run_now(self, task_id: str) -> bool:
        """立即執行任務（在背景執行緒）"""
        task = self.tasks.get(task_id)
        if not task:
            return False
        if task.status == 'running':
            logger.warning(f"任務 {task_id} 已在執行中")
            return False

        import threading
        t = threading.Thread(target=self._execute_task, args=(task_id,), daemon=True)
        t.start()
        return True

    def _execute_task(self, task_id: str):
        """執行單個任務（在背景執行緒）"""
        task = self.tasks.get(task_id)
        if not task:
            return

        task.status = 'running'
        task.progress = 0
        task.progress_detail = '初始化...'
        task.error_message = ''
        self._save_tasks()
        self._save_checkpoint(task_id, 'started')

        try:
            from crawler.engine import SearchEngine

            engine = SearchEngine(self.config, task)

            def on_progress(current, total, found, source):
                task.progress = int(current / total * 100) if total else 0
                task.progress_detail = f"{source}: {current}/{total} 頁, {found} 人"
                task.linkedin_count = found if 'linkedin' in source else task.linkedin_count
                task.github_count = found if 'github' in source else task.github_count
                self._save_checkpoint(task_id, 'running', {
                    'current_page': current, 'total_pages': total,
                    'found': found, 'source': source,
                })

            engine.on_progress = on_progress

            candidates = engine.execute()

            # 寫入 Google Sheets
            from flask import current_app
            try:
                # 嘗試透過 Flask app context 取得 SheetsStore
                # 如果在非 Flask context，直接初始化
                self._write_results(task, candidates)
            except Exception as e:
                logger.error(f"寫入結果失敗: {e}")

            task.status = 'completed'
            task.progress = 100
            task.last_run = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            task.last_result_count = len(candidates)
            task.linkedin_count = sum(1 for c in candidates if c.source in ('linkedin', 'li+ocr'))
            task.github_count = sum(1 for c in candidates if c.source == 'github')
            task.ocr_count = sum(1 for c in candidates if c.source == 'li+ocr')
            self._clear_checkpoint(task_id)
            logger.info(f"任務 {task_id} 完成: {len(candidates)} 位候選人")

            # ── Auto-push: 任務完成後自動推送到 Step1ne 系統 ──
            self._auto_push_if_enabled(task, candidates)

        except Exception as e:
            task.status = 'failed'
            task.error_message = str(e)
            self._save_checkpoint(task_id, 'failed', {'error': str(e)})
            logger.error(f"任務 {task_id} 失敗: {e}")

        self._save_tasks()

    def _write_results(self, task: SearchTask, candidates: list):
        """寫入結果到 Google Sheets"""
        sheets_cfg = self.config.get('google_sheets', {})
        if not sheets_cfg.get('spreadsheet_id'):
            logger.warning("Google Sheets 未設定，跳過寫入")
            return

        try:
            from storage.sheets_store import SheetsStore
            creds_file = sheets_cfg.get('credentials_file', 'credentials.json')
            # 相對路徑轉為基於專案根目錄的絕對路徑
            if not os.path.isabs(creds_file):
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                creds_file = os.path.join(base_dir, creds_file)
            store = SheetsStore(
                spreadsheet_id=sheets_cfg['spreadsheet_id'],
                credentials_file=creds_file,
            )
            result = store.write_candidates(task.client_name, candidates)
            logger.info(f"Sheets 寫入: {result}")
        except Exception as e:
            logger.error(f"Sheets 寫入失敗: {e}")

    # ── Auto-push ─────────────────────────────────────────────

    def _auto_push_if_enabled(self, task, candidates):
        """任務完成後自動推送候選人到 Step1ne 系統（靜默處理錯誤）"""
        try:
            # 檢查任務級別或全域開關
            step1ne_cfg = self.config.get('step1ne', {})
            global_auto_push = step1ne_cfg.get('auto_push', False)
            task_auto_push = getattr(task, 'auto_push', False)

            if not (global_auto_push or task_auto_push):
                return

            api_base = step1ne_cfg.get('api_base_url', '')
            if not api_base:
                logger.warning("auto_push 啟用但 Step1ne API 未設定，跳過推送")
                return

            if not candidates:
                return

            from integration.step1ne_client import Step1neClient
            client = Step1neClient(api_base)

            # 序列化候選人（Candidate dataclass → dict）
            cand_dicts = []
            for c in candidates:
                d = c.to_dict() if hasattr(c, 'to_dict') else c
                cand_dicts.append(d)

            result = client.push_candidates_v2(cand_dicts, actor='Crawler-AutoPush')

            if result.get('success'):
                logger.info(
                    f"Auto-push 成功: 任務 {task.id} → "
                    f"新增 {result.get('created_count', 0)}，"
                    f"更新 {result.get('updated_count', 0)}"
                )
            else:
                logger.warning(f"Auto-push 失敗: {result.get('error', 'unknown')}")

        except Exception as e:
            # 靜默處理 — 不影響任務狀態
            logger.warning(f"Auto-push 例外（非阻塞）: {e}")

    # ── 排程 ─────────────────────────────────────────────────

    def _schedule_task(self, task: SearchTask):
        """排程任務"""
        if not self._scheduler:
            return

        trigger = None
        if task.schedule_type == 'daily':
            parts = task.schedule_time.split(':')
            hour = int(parts[0]) if parts else 9
            minute = int(parts[1]) if len(parts) > 1 else 0
            trigger = CronTrigger(hour=hour, minute=minute)
        elif task.schedule_type == 'weekly':
            parts = task.schedule_time.split(':')
            hour = int(parts[0]) if parts else 9
            minute = int(parts[1]) if len(parts) > 1 else 0
            days = ','.join(str(d) for d in task.schedule_weekdays) if task.schedule_weekdays else '0-4'
            trigger = CronTrigger(day_of_week=days, hour=hour, minute=minute)
        elif task.schedule_type == 'interval':
            trigger = IntervalTrigger(hours=task.schedule_interval_hours)

        if trigger:
            self._scheduler.add_job(
                self._execute_task,
                trigger=trigger,
                id=task.id,
                args=[task.id],
                replace_existing=True,
            )
            logger.info(f"排程: {task.id} ({task.schedule_type})")

    # ── 持久化 ───────────────────────────────────────────────

    def _save_tasks(self):
        os.makedirs(os.path.dirname(self.tasks_file), exist_ok=True)
        try:
            data = {tid: t.to_dict() for tid, t in self.tasks.items()}
            with open(self.tasks_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"任務儲存失敗: {e}")

    def _load_tasks(self):
        if not os.path.exists(self.tasks_file):
            return
        try:
            with open(self.tasks_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for tid, d in data.items():
                self.tasks[tid] = SearchTask.from_dict(d)
                # 重設 running 狀態（上次異常中斷）
                if self.tasks[tid].status == 'running':
                    self.tasks[tid].status = 'pending'
            logger.info(f"載入 {len(self.tasks)} 個任務")
        except Exception as e:
            logger.warning(f"任務載入失敗: {e}")

    # ── Checkpoint ──────────────────────────────────────────

    def _load_checkpoints(self) -> dict:
        if not os.path.exists(self.checkpoint_file):
            return {}
        try:
            with open(self.checkpoint_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_checkpoint(self, task_id: str, phase: str, detail: dict = None):
        checkpoints = self._load_checkpoints()
        checkpoints[task_id] = {
            'phase': phase,
            'detail': detail or {},
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        os.makedirs(os.path.dirname(self.checkpoint_file), exist_ok=True)
        try:
            with open(self.checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump(checkpoints, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Checkpoint 儲存失敗: {e}")

    def _clear_checkpoint(self, task_id: str):
        checkpoints = self._load_checkpoints()
        checkpoints.pop(task_id, None)
        try:
            with open(self.checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump(checkpoints, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get_task_status(self, task_id: str) -> dict:
        """取得任務即時狀態"""
        task = self.tasks.get(task_id)
        if not task:
            return {}
        return {
            'id': task.id,
            'status': task.status,
            'progress': task.progress,
            'progress_detail': task.progress_detail,
            'linkedin_count': task.linkedin_count,
            'github_count': task.github_count,
            'ocr_count': task.ocr_count,
            'last_run': task.last_run,
            'last_result_count': task.last_result_count,
            'error_message': task.error_message,
        }
