"""
Telegram 爬蟲回報通知模組
任務完成 / 失敗 / 停止時自動發送摘要到指定 TG 群組
"""
import json
import logging
import ssl
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """爬蟲任務完成後發送 Telegram 通知"""

    def __init__(self, config: dict):
        tg_cfg = config.get('telegram', {})
        self.enabled = tg_cfg.get('enabled', False)
        self.bot_token = tg_cfg.get('bot_token', '')
        self.chat_id = tg_cfg.get('chat_id', '')
        self.thread_id = tg_cfg.get('thread_id', None)

        if self.enabled and not (self.bot_token and self.chat_id):
            logger.warning("Telegram 已啟用但缺少 bot_token 或 chat_id，通知停用")
            self.enabled = False

        if self.enabled:
            logger.info("Telegram 通知已啟用")

    # ── 公開方法 ─────────────────────────────────────────────

    def notify_task_completed(self, task, candidates: list):
        """任務成功完成 — 發送摘要報告"""
        if not self.enabled:
            return

        # 統計資料
        total = len(candidates)
        li_count = sum(1 for c in candidates if getattr(c, 'source', '') in ('linkedin', 'li+ocr'))
        gh_count = sum(1 for c in candidates if getattr(c, 'source', '') == 'github')

        # 評分分佈
        grades = {}
        for c in candidates:
            g = getattr(c, 'grade', None) or getattr(c, 'talent_level', '-')
            grades[g] = grades.get(g, 0) + 1
        grade_text = ' | '.join(f"{k}: {v}" for k, v in sorted(grades.items()))

        # 有 LinkedIn URL 的比例
        has_linkedin = sum(1 for c in candidates if getattr(c, 'linkedin_url', ''))
        has_email = sum(1 for c in candidates if getattr(c, 'email', '') and getattr(c, 'email', '') != 'unknown@github.com')

        # 候選人名單（按評分排序）
        sorted_candidates = sorted(candidates, key=lambda c: getattr(c, 'score', 0) or 0, reverse=True)
        candidate_lines = []
        for c in sorted_candidates[:30]:  # 最多列 30 人避免訊息過長
            name = self._esc(getattr(c, 'name', '') or getattr(c, 'full_name', '未知'))
            title = self._esc(getattr(c, 'current_title', '') or getattr(c, 'headline', '') or '')
            li_url = getattr(c, 'linkedin_url', '') or ''
            email = getattr(c, 'email', '') or ''
            grade = getattr(c, 'grade', '') or getattr(c, 'talent_level', '')

            contact = ''
            if li_url:
                contact = f" [LinkedIn]({li_url})"
            elif email and email != 'unknown@github.com':
                contact = f" 📧 {self._esc(email)}"

            grade_icon = {'S': '🟣', 'A': '🔵', 'B': '🟢', 'C': '🟡', 'D': '🔴'}.get(grade, '⚪')
            line = f"{grade_icon} {name}"
            if title:
                line += f" — {title[:30]}"
            if contact:
                line += contact
            candidate_lines.append(line)

        name_list = '\n'.join(candidate_lines) if candidate_lines else '（無候選人）'
        remaining = max(0, total - 30)
        if remaining > 0:
            name_list += f"\n...還有 {remaining} 人"

        msg = (
            f"✅ *爬蟲任務完成*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📋 *{self._esc(task.client_name)}* — {self._esc(task.job_title)}\n"
            f"🕐 完成時間: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"\n"
            f"👥 *候選人: {total} 位*\n"
            f"  💼 LinkedIn: {li_count} | 🐙 GitHub: {gh_count}\n"
            f"  📊 {grade_text or '尚未評分'}\n"
            f"\n"
            f"📋 *名單*\n"
            f"{name_list}\n"
        )

        # Auto-push 狀態
        auto_push = getattr(task, 'auto_push', False)
        if auto_push:
            msg += f"\n🚀 已自動推送到 Step1ne 系統"

        self._send(msg)

    def notify_task_failed(self, task, error: str):
        """任務失敗 — 發送錯誤通知"""
        if not self.enabled:
            return

        msg = (
            f"❌ *爬蟲任務失敗*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📋 *{self._esc(task.client_name)}* — {self._esc(task.job_title)}\n"
            f"🕐 時間: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"\n"
            f"⚠️ *錯誤訊息:*\n"
            f"`{self._esc(error[:500])}`\n"
        )
        self._send(msg)

    def notify_task_stopped(self, task):
        """任務被手動停止"""
        if not self.enabled:
            return

        found = getattr(task, 'last_result_count', 0) or 0
        msg = (
            f"⏹️ *爬蟲任務已停止*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📋 *{self._esc(task.client_name)}* — {self._esc(task.job_title)}\n"
            f"🕐 時間: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"👥 已找到: {found} 位候選人\n"
        )
        self._send(msg)

    def notify_auto_push_result(self, task, result: dict):
        """Auto-push 結果通知 — 包含新增候選人名單"""
        if not self.enabled:
            return

        created = result.get('created_count', 0)
        updated = result.get('updated_count', 0)
        skipped = result.get('skipped_count', 0)
        success = result.get('success', False)

        if success:
            job_id = getattr(task, 'step1ne_job_id', None) or ''

            msg = (
                f"🚀 *新候選人已匯入系統*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📋 {self._esc(task.client_name)} — {self._esc(task.job_title)}\n"
                f"✨ 新增 *{created}* 位 | 更新 {updated} 位\n"
            )

            if created > 0:
                # 前端系統連結 — 直接帶篩選參數
                system_url = "https://hrsystem.step1ne.com"
                msg += (
                    f"\n👉 *請立即到系統處理：*\n"
                    f"[打開系統 → 今日新增]({system_url})\n"
                    f"點「*今日新增*」→ 職缺篩選「*{self._esc(task.job_title[:25])}*」\n"
                    f"狀態為「*未開始*」的就是剛匯入的新人選\n"
                )
        else:
            msg = (
                f"⚠️ *Auto-Push 失敗*\n"
                f"📋 {self._esc(task.client_name)} — {self._esc(task.job_title)}\n"
                f"錯誤: `{self._esc(str(result.get('error', 'unknown'))[:200])}`\n"
            )
        self._send(msg)

    def send_custom(self, text: str):
        """發送自訂訊息"""
        if not self.enabled:
            return
        self._send(text)

    # ── 內部方法 ─────────────────────────────────────────────

    def _send(self, text: str):
        """發送 Telegram 訊息"""
        try:
            url = TELEGRAM_API.format(token=self.bot_token)
            payload = {
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': 'Markdown',
            }
            if self.thread_id:
                payload['message_thread_id'] = int(self.thread_id)

            data = json.dumps(payload).encode('utf-8')
            req = Request(url, data=data, headers={'Content-Type': 'application/json'})
            ctx = ssl.create_default_context()
            resp = urlopen(req, timeout=10, context=ctx)
            result = json.loads(resp.read().decode('utf-8'))

            if result.get('ok'):
                logger.info("Telegram 通知發送成功")
            else:
                logger.warning(f"Telegram 通知失敗: {result}")

        except URLError as e:
            logger.warning(f"Telegram 通知網路錯誤: {e}")
        except Exception as e:
            logger.warning(f"Telegram 通知例外: {e}")

    @staticmethod
    def _esc(text: str) -> str:
        """Escape Markdown 特殊字元"""
        if not text:
            return ''
        for ch in ('_', '*', '`', '['):
            text = text.replace(ch, '\\' + ch)
        return text
