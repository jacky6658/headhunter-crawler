"""
排程管理 — APScheduler + multiprocessing
"""
import json
import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Dict, Optional


class TaskStoppedException(Exception):
    """任務被使用者手動停止"""
    pass

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

    def __init__(self, config: dict, store=None):
        self.config = config
        self.store = store  # 共用 LocalStore 實例（避免記憶體不同步）
        self.tasks: dict = {}  # task_id → SearchTask
        self._scheduler = None
        self._stop_events: Dict[str, threading.Event] = {}  # task_id → stop signal

        sched_cfg = config.get('scheduler', {})
        self.tasks_file = sched_cfg.get('tasks_file', 'data/tasks.json')
        self.checkpoint_file = sched_cfg.get('checkpoint_file', 'data/checkpoints.json')

        # Telegram 通知
        try:
            from notification.telegram import TelegramNotifier
            self.notifier = TelegramNotifier(config)
        except Exception as e:
            logger.warning(f"Telegram 通知初始化失敗: {e}")
            self.notifier = None

        self._load_tasks()

    def start(self):
        """啟動排程器"""
        if not SCHEDULER_AVAILABLE:
            logger.warning("APScheduler 未安裝，排程功能停用")
            return
        self._scheduler = BackgroundScheduler()
        self._scheduler.start()

        # 恢復已排程的任務
        scheduled_count = 0
        missed_tasks = []
        for task_id, task in self.tasks.items():
            if task.status in ('pending', 'paused') and task.schedule_type != 'once':
                self._schedule_task(task)
                scheduled_count += 1

                # v3: 檢查是否有「今天該跑但沒跑」的任務
                if task.schedule_type == 'daily' and task.last_run:
                    try:
                        last = datetime.strptime(task.last_run, '%Y-%m-%d %H:%M:%S')
                        now = datetime.now()
                        hours_since = (now - last).total_seconds() / 3600
                        if hours_since > 25:
                            missed_tasks.append((task_id, task.job_title, f"{hours_since:.0f}h ago"))
                    except (ValueError, TypeError):
                        pass

        logger.info(f"排程器已啟動，{len(self.tasks)} 個任務，{scheduled_count} 個定期排程已恢復")
        if missed_tasks:
            for tid, title, ago in missed_tasks:
                logger.warning(f"⚠️ 定期任務可能漏跑: [{tid}] {title} (上次執行: {ago})")

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

        stop_event = threading.Event()
        self._stop_events[task_id] = stop_event

        t = threading.Thread(target=self._execute_task, args=(task_id, stop_event), daemon=True)
        t.start()
        return True

    def stop_task(self, task_id: str) -> bool:
        """停止正在執行的任務"""
        task = self.tasks.get(task_id)
        if not task or task.status != 'running':
            return False

        stop_event = self._stop_events.get(task_id)
        if not stop_event:
            return False

        logger.info(f"停止任務: {task_id}")
        stop_event.set()
        return True

    def _execute_task(self, task_id: str, stop_event: threading.Event = None):
        """執行單個任務（在背景執行緒）"""
        task = self.tasks.get(task_id)
        if not task:
            return

        # 確保有 stop_event（排程器呼叫時可能沒有）
        if stop_event is None:
            stop_event = threading.Event()
            self._stop_events[task_id] = stop_event

        task.status = 'running'
        task.progress = 0
        task.progress_detail = '初始化...'
        task.error_message = ''
        self._save_tasks()
        self._save_checkpoint(task_id, 'started')

        try:
            from crawler.engine import SearchEngine

            # ── Phase 0: 拉取職缺畫像 + AI 生成搜尋關鍵字 ──
            job_context = None
            if task.step1ne_job_id:
                task.progress_detail = 'Phase 0: 拉取職缺畫像...'
                self._save_tasks()
                job_context = self._pull_job_context(task)

            engine = SearchEngine(self.config, task, job_context=job_context,
                                  stop_event=stop_event)

            def on_progress(current, total, found, source):
                # 每次回報進度時檢查停止信號
                if stop_event.is_set():
                    raise TaskStoppedException("使用者手動停止")
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

            # 寫入本地儲存
            try:
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

            # ── Telegram 回報: 任務完成 ──
            if self.notifier:
                try:
                    self.notifier.notify_task_completed(task, candidates)
                except Exception as e:
                    logger.warning(f"Telegram 完成通知失敗: {e}")

            # ── Auto-push: 任務完成後自動推送到 Step1ne 系統 ──
            self._auto_push_if_enabled(task, candidates)

        except TaskStoppedException:
            task.status = 'stopped'
            task.error_message = '使用者手動停止'
            logger.info(f"任務 {task_id} 已停止")
            # ── Telegram 回報: 任務停止 ──
            if self.notifier:
                try:
                    self.notifier.notify_task_stopped(task)
                except Exception:
                    pass

        except Exception as e:
            task.status = 'failed'
            task.error_message = str(e)
            self._save_checkpoint(task_id, 'failed', {'error': str(e)})
            logger.error(f"任務 {task_id} 失敗: {e}")
            # ── Telegram 回報: 任務失敗 ──
            if self.notifier:
                try:
                    self.notifier.notify_task_failed(task, str(e))
                except Exception:
                    pass

        finally:
            # 清除停止信號
            self._stop_events.pop(task_id, None)

        self._save_tasks()

    def _write_results(self, task: SearchTask, candidates: list):
        """寫入結果到本地 JSON 儲存（使用共用 store 實例）"""
        try:
            store = self.store
            if not store:
                # fallback: 建立新實例（不建議，會導致記憶體不同步）
                from storage.local_store import LocalStore
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                data_dir = os.path.join(base_dir, 'data')
                store = LocalStore(data_dir=data_dir)
                logger.warning("TaskManager 使用 fallback LocalStore（記憶體可能不同步）")
            result = store.write_candidates(task.client_name, candidates)
            logger.info(f"本地儲存寫入: {result}")
        except Exception as e:
            logger.error(f"本地儲存寫入失敗: {e}")

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

            # 序列化候選人（Candidate dataclass → dict）+ 附加 step1ne_job_id
            cand_dicts = []
            for c in candidates:
                d = c.to_dict() if hasattr(c, 'to_dict') else dict(c)
                # 把任務關聯的 step1ne_job_id 附加到每位候選人，用於設定目標職缺
                if task.step1ne_job_id and not d.get('step1ne_job_id'):
                    d['step1ne_job_id'] = task.step1ne_job_id
                # 爬蟲初篩狀態 — 等待 OpenClaw AI 複篩
                d['status'] = '爬蟲初篩'
                d['recruiter'] = '待指派'
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

            # ── Telegram 回報: Auto-push 結果 ──
            if self.notifier:
                try:
                    self.notifier.notify_auto_push_result(task, result)
                except Exception:
                    pass

        except Exception as e:
            # 靜默處理 — 不影響任務狀態
            logger.warning(f"Auto-push 例外（非阻塞）: {e}")

    # ── Phase 0: 職缺畫像 + AI 關鍵字 ──────────────────────────

    def _pull_job_context(self, task: SearchTask) -> dict:
        """
        Phase 0: 從 Step1ne 拉取完整職缺畫像，並用 AI 生成搜尋關鍵字
        Returns: job_context dict (含職缺完整資訊) 或 None
        """
        try:
            step1ne_cfg = self.config.get('step1ne', {})
            api_base = step1ne_cfg.get('api_base_url', '')
            if not api_base:
                logger.warning("Step1ne API 未設定，跳過 Phase 0")
                return None

            from integration.step1ne_client import Step1neClient
            client = Step1neClient(api_base)

            # 拉取職缺詳細資料
            job_data = client.fetch_job_detail(task.step1ne_job_id)
            if not job_data:
                logger.warning(f"Step1ne 職缺 {task.step1ne_job_id} 取得失敗")
                return None

            # 正規化: API 可能回 {data: {...}} 或直接 {...}
            if 'data' in job_data and isinstance(job_data['data'], dict):
                job_data = job_data['data']

            logger.info(
                f"Phase 0: 取得職缺 [{job_data.get('position_name', '')}] "
                f"from Step1ne (id={task.step1ne_job_id})"
            )

            # 如果任務沒有手動設定關鍵字，用規則式從職缺畫像生成
            if not task.primary_skills:
                task.progress_detail = 'Phase 0: 規則式關鍵字生成...'
                self._save_tasks()
                self._fallback_keywords(task, job_data)

            return job_data

        except Exception as e:
            logger.error(f"Phase 0 失敗: {e}")
            return None

    def _generate_ai_keywords(self, task: SearchTask, job_data: dict):
        """
        用 Perplexity AI 分析職缺畫像，生成最佳搜尋關鍵字
        失敗時 fallback 到規則式 KeywordGenerator
        """
        try:
            enrichment_cfg = self.config.get('enrichment', {})
            # perplexity config 可能在 enrichment.perplexity 或頂層 perplexity
            ppx_cfg = enrichment_cfg.get('perplexity', self.config.get('perplexity', {}))
            api_key = ppx_cfg.get('api_key', '')

            if not api_key:
                logger.info("Perplexity API key 未設定，使用規則式關鍵字")
                self._fallback_keywords(task, job_data)
                return

            from enrichment.prompts import KEYWORD_GENERATION_PROMPT
            import json as _json

            # 組裝 prompt
            prompt = KEYWORD_GENERATION_PROMPT.format(
                position_name=job_data.get('position_name', task.job_title),
                client_company=job_data.get('client_company', task.client_name),
                talent_profile=job_data.get('talent_profile', ''),
                job_description=job_data.get('job_description', ''),
                company_profile=job_data.get('company_profile', ''),
                key_skills=job_data.get('key_skills', ''),
                search_primary=', '.join(task.primary_skills) if task.primary_skills else '(未設定)',
                search_secondary=', '.join(task.secondary_skills) if task.secondary_skills else '(未設定)',
            )

            # 呼叫 Perplexity Sonar API
            import ssl
            from urllib.request import Request, urlopen

            payload = _json.dumps({
                'model': ppx_cfg.get('model', 'sonar'),
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.1,
            }).encode('utf-8')

            req = Request(
                ppx_cfg.get('api_url', 'https://api.perplexity.ai/chat/completions'),
                data=payload,
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json',
                },
            )

            ctx = ssl.create_default_context()
            resp = urlopen(req, timeout=30, context=ctx)
            result = _json.loads(resp.read().decode('utf-8'))
            content = result['choices'][0]['message']['content']

            # 提取 JSON（可能被 markdown code block 包裹）
            if '```json' in content:
                content = content.split('```json')[1].split('```')[0]
            elif '```' in content:
                content = content.split('```')[1].split('```')[0]

            keywords = _json.loads(content.strip())

            # 寫回 task
            if keywords.get('primary_skills'):
                task.primary_skills = keywords['primary_skills'][:5]
            if keywords.get('secondary_skills'):
                task.secondary_skills = keywords['secondary_skills'][:8]

            logger.info(
                f"AI 關鍵字生成成功: primary={task.primary_skills}, "
                f"secondary={task.secondary_skills}"
            )
            self._save_tasks()

        except Exception as e:
            logger.warning(f"AI 關鍵字生成失敗: {e}，fallback 規則式")
            self._fallback_keywords(task, job_data)

    def _fallback_keywords(self, task: SearchTask, job_data: dict):
        """規則式 fallback: 從職缺資料提取關鍵字"""
        try:
            from scoring.keyword_generator import KeywordGenerator
            gen = KeywordGenerator()

            job_title = job_data.get('position_name', task.job_title)
            key_skills = job_data.get('key_skills', '')
            jd = job_data.get('job_description', '')

            # 從 key_skills 提取
            if key_skills:
                skills = [s.strip() for s in key_skills.replace('、', ',').replace('；', ',').split(',') if s.strip()]
                task.primary_skills = skills[:5]
                task.secondary_skills = skills[5:10]
            elif jd:
                # 從 JD 用規則式提取
                result = gen.generate(job_title, jd)
                task.primary_skills = result.get('primary', [])[:5]
                task.secondary_skills = result.get('secondary', [])[:8]

            logger.info(f"Fallback 關鍵字: primary={task.primary_skills}")
            self._save_tasks()
        except Exception as e:
            logger.warning(f"Fallback 關鍵字也失敗: {e}")

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
                misfire_grace_time=3600,  # v3: 允許最多 1 小時的延遲（原本只有 1 秒）
                coalesce=True,           # v3: 錯過多次只補跑一次
                max_instances=1,         # 同一任務最多同時跑 1 個
            )
            logger.info(f"排程: {task.id} ({task.schedule_type}) misfire_grace=3600s")

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
