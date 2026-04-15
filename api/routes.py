"""
REST API — Flask Blueprint
"""
import json
import logging
import os
from datetime import datetime

from flask import Blueprint, request, jsonify, current_app

from storage.models import SearchTask, Candidate

logger = logging.getLogger(__name__)

api_bp = Blueprint('api', __name__)


def _get_task_manager():
    return current_app.config.get('TASK_MANAGER')


def _get_sheets_store():
    return current_app.config.get('SHEETS_STORE')


def _get_step1ne_client():
    return current_app.config.get('STEP1NE_CLIENT')


def _get_scoring_components():
    """取得評分系統元件（懶載入）"""
    if not hasattr(current_app, '_scoring_engine'):
        from scoring.normalizer import SkillNormalizer
        from scoring.engine import ScoringEngine
        from scoring.job_profile import JobProfileManager
        from scoring.keyword_generator import KeywordGenerator

        base_dir = os.path.dirname(os.path.dirname(__file__))
        synonyms_path = os.path.join(base_dir, 'config', 'skills_synonyms.yaml')

        normalizer = SkillNormalizer(synonyms_path)
        current_app._scoring_engine = ScoringEngine(normalizer)
        current_app._profile_manager = JobProfileManager(
            os.path.join(base_dir, 'config', 'job_profiles')
        )
        current_app._keyword_generator = KeywordGenerator()
        current_app._normalizer = normalizer

    return {
        'engine': current_app._scoring_engine,
        'profile_manager': current_app._profile_manager,
        'keyword_generator': current_app._keyword_generator,
        'normalizer': current_app._normalizer,
    }


# ── 健康檢查 ─────────────────────────────────────────────────

@api_bp.route('/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()})


# ── 候選人 ───────────────────────────────────────────────────

@api_bp.route('/candidates')
def list_candidates():
    store = _get_sheets_store()
    if not store:
        return jsonify({'error': '資料儲存未初始化'}), 503

    client = request.args.get('client')
    job_title = request.args.get('job_title')
    status = request.args.get('status')
    limit = int(request.args.get('limit', 50))
    offset = int(request.args.get('offset', 0))

    result = store.read_candidates(
        client_name=client, job_title=job_title,
        status=status, limit=limit, offset=offset,
    )
    return jsonify(result)


@api_bp.route('/candidates/<candidate_id>')
def get_candidate(candidate_id):
    store = _get_sheets_store()
    if not store:
        return jsonify({'error': '資料儲存未初始化'}), 503

    # 搜尋所有客戶工作表
    for client in store.list_clients():
        result = store.read_candidates(client_name=client)
        for r in result.get('data', []):
            if r.get('id') == candidate_id:
                return jsonify(r)
    return jsonify({'error': 'Not found'}), 404


@api_bp.route('/candidates/<candidate_id>', methods=['PATCH'])
def update_candidate(candidate_id):
    store = _get_sheets_store()
    if not store:
        return jsonify({'error': '資料儲存未初始化'}), 503

    data = request.get_json()
    status = data.get('status')
    client = data.get('client_name')
    if status and client:
        store.update_candidate_status(client, candidate_id, status)
        return jsonify({'success': True})
    return jsonify({'error': 'Missing client_name or status'}), 400


@api_bp.route('/candidates/recommend')
def recommend_candidates():
    """
    即時推薦 API — 從 DB 搜尋符合技能的 A+B 級候選人

    Query params:
      skills: 逗號分隔技能 (e.g. "golang,kubernetes")
      location: 地區 (default: "taiwan")
      limit: 最多回傳幾筆 (default: 10)
      min_grade: 最低等級 (default: "B", 只回 A+B)
    """
    store = _get_sheets_store()
    if not store:
        return jsonify({'error': '資料儲存未初始化'}), 503

    skills_str = request.args.get('skills', '')
    skills = [s.strip().lower() for s in skills_str.split(',') if s.strip()]
    location = request.args.get('location', 'taiwan').lower()
    limit = int(request.args.get('limit', 10))
    min_grade = request.args.get('min_grade', 'B').upper()

    # 等級過濾集合
    grade_filter = {'A'}
    if min_grade in ('B', 'C', 'D'):
        grade_filter.add('B')
    if min_grade in ('C', 'D'):
        grade_filter.add('C')
    if min_grade == 'D':
        grade_filter.add('D')

    # 搜尋所有客戶的候選人（跨客戶去重）
    all_candidates = []
    seen_keys = set()  # 用 linkedin_url 或 github_username 或 name 去重
    for client in store.list_clients():
        result = store.read_candidates(client_name=client, limit=9999)
        for c in result.get('data', []):
            grade = c.get('grade', '')
            if grade not in grade_filter:
                continue
            # 去重: 用 linkedin_url > github_username > name 作為唯一鍵
            dedup_key = (
                (c.get('linkedin_url') or '').lower().rstrip('/') or
                (c.get('github_username') or '').lower() or
                (c.get('name') or '').strip().lower()
            )
            if dedup_key and dedup_key in seen_keys:
                continue
            if dedup_key:
                seen_keys.add(dedup_key)
            all_candidates.append(c)

    # 技能匹配排序 — 嚴格模式：精確匹配 + 最少 2 個技能命中
    def match_score(candidate):
        c_skills = set()
        raw = candidate.get('skills', [])
        if isinstance(raw, str):
            c_skills = set(s.strip().lower() for s in raw.split(',') if s.strip())
        elif isinstance(raw, list):
            c_skills = set(s.lower() for s in raw if s)

        c_tech = candidate.get('tech_stack', [])
        if isinstance(c_tech, list):
            c_skills.update(t.lower() for t in c_tech if t)

        c_bio = (candidate.get('bio', '') or '').lower()
        c_title = (candidate.get('title', '') or '').lower()

        # 加入工作經歷的職稱
        work_history = candidate.get('work_history', [])
        if isinstance(work_history, str):
            c_bio += ' ' + work_history.lower()
        elif isinstance(work_history, list):
            for wh in work_history[:3]:
                if isinstance(wh, dict):
                    c_bio += ' ' + (wh.get('title', '') or '').lower()
                    c_bio += ' ' + (wh.get('description', '') or '').lower()

        # 精確匹配：技能必須完全匹配（不是子字串）
        # 例如 "mvc" 不能匹配 "spring-mvc"
        hits = 0
        for s in skills:
            s_lower = s.lower()
            # 精確匹配技能清單
            if s_lower in c_skills:
                hits += 1
            # 全詞匹配 bio/title（用 word boundary）
            elif f' {s_lower} ' in f' {c_bio} ' or f' {s_lower} ' in f' {c_title} ':
                hits += 1

        # 需要至少 2 個技能命中才推薦（避免只匹配到 1 個通用詞）
        min_hits = min(2, len(skills))  # 如果只搜 1 個技能，則 1 個就夠
        if hits < min_hits:
            return 0

        grade_bonus = {'A': 20, 'B': 10, 'C': 5, 'D': 0}.get(candidate.get('grade', ''), 0)
        return hits * 100 + grade_bonus + candidate.get('score', 0)

    if skills:
        all_candidates = [c for c in all_candidates if match_score(c) > 0]
        all_candidates.sort(key=match_score, reverse=True)

    # 本地人過濾（預設開啟，可用 local_only=false 關閉）
    local_only = request.args.get('local_only', 'true').lower() != 'false'
    if local_only:
        try:
            from crawler.locality_filter import filter_locals
            all_candidates = filter_locals(all_candidates, threshold=0.25)
        except Exception as e:
            logger.warning(f"本地人過濾失敗: {e}")

    return jsonify({
        'data': all_candidates[:limit],
        'total': len(all_candidates),
        'skills_searched': skills,
    })


@api_bp.route('/candidates/by-task/<task_id>')
def candidates_by_task(task_id):
    """
    按 task_id 篩選候選人 — 只回傳這次搜尋新找到的人

    Query params:
      limit: 最多回傳幾筆 (default: 20)
      sort_by: 排序 ('score' | 'grade' | 'has_email', default: 'score')
      only_new: 只顯示非重複的新候選人 (default: true)
    """
    store = _get_sheets_store()
    if not store:
        return jsonify({'error': '資料儲存未初始化'}), 503

    limit = int(request.args.get('limit', 20))
    sort_by = request.args.get('sort_by', 'score')
    only_new = request.args.get('only_new', 'true').lower() != 'false'

    results = []
    dup_count = 0
    for client in store.list_clients():
        for c in store.read_candidates(client_name=client, limit=9999).get('data', []):
            if c.get('task_id') == task_id:
                # 過濾重複：只要本次新找到的
                if only_new and c.get('is_duplicate'):
                    dup_count += 1
                    continue
                c['client_name'] = client
                results.append(c)

    # 排序
    if sort_by == 'has_email':
        results.sort(key=lambda c: (bool(c.get('email')), c.get('score', 0)), reverse=True)
    elif sort_by == 'grade':
        grade_order = {'A': 4, 'B': 3, 'C': 2, 'D': 1, '': 0}
        results.sort(key=lambda c: (grade_order.get(c.get('grade', ''), 0), c.get('score', 0)), reverse=True)
    else:  # score
        results.sort(key=lambda c: c.get('score', 0), reverse=True)

    return jsonify({
        'data': results[:limit],
        'total': len(results),
        'duplicates_filtered': dup_count,
    })


@api_bp.route('/candidates/search')
def search_candidates():
    """
    模糊搜尋 API — 從全部候選人中搜尋 bio/company/name/skills 含關鍵字的人

    Query params:
      q: 搜尋關鍵字（空格分隔，OR 邏輯）
      limit: 最多回傳幾筆 (default: 20)
    """
    store = _get_sheets_store()
    if not store:
        return jsonify({'error': '資料儲存未初始化'}), 503

    query = request.args.get('q', '').strip()
    limit = int(request.args.get('limit', 20))

    if not query:
        return jsonify({'data': [], 'total': 0})

    keywords = [k.strip().lower() for k in query.split() if k.strip()]

    results = []
    seen_keys = set()
    for client in store.list_clients():
        for c in store.read_candidates(client_name=client, limit=9999).get('data', []):
            # 去重
            dedup_key = (
                (c.get('linkedin_url') or '').lower().rstrip('/') or
                (c.get('github_username') or '').lower() or
                (c.get('name') or '').strip().lower()
            )
            if dedup_key and dedup_key in seen_keys:
                continue
            if dedup_key:
                seen_keys.add(dedup_key)

            # 模糊匹配
            text = ' '.join([
                str(c.get('name', '')),
                str(c.get('bio', '')),
                str(c.get('title', '')),
                str(c.get('company', '')),
                str(c.get('skills', '')),
                str(c.get('work_history', '')),
            ]).lower()

            if any(kw in text for kw in keywords):
                c['client_name'] = client
                results.append(c)

    # 排序：有 email 優先，再按 score
    results.sort(key=lambda c: (bool(c.get('email')), c.get('score', 0)), reverse=True)

    return jsonify({'data': results[:limit], 'total': len(results)})


@api_bp.route('/candidates/export', methods=['POST'])
def export_candidates():
    store = _get_sheets_store()
    if not store:
        return jsonify({'error': '資料儲存未初始化'}), 503

    data = request.get_json() or {}
    result = store.read_candidates(
        client_name=data.get('client'),
        job_title=data.get('job_title'),
        limit=data.get('limit', 1000),
    )
    return jsonify(result)


# ── 任務管理 ─────────────────────────────────────────────────

@api_bp.route('/tasks')
def list_tasks():
    tm = _get_task_manager()
    tasks = tm.get_all_tasks()
    return jsonify([t.to_dict() for t in tasks])


LOCATION_ZH_MAP = {
    'taiwan': '台灣', 'taipei, taiwan': '台北', 'singapore': '新加坡',
    'hong kong': '香港', 'japan': '日本', 'tokyo, japan': '東京',
    'united states': '美國', 'san francisco, usa': '舊金山',
    'new york, usa': '紐約', 'united kingdom': '英國', 'london, uk': '倫敦',
    'germany': '德國', 'berlin, germany': '柏林', 'canada': '加拿大',
    'australia': '澳洲', 'china': '中國', 'shanghai, china': '上海',
    'shenzhen, china': '深圳', 'korea': '韓國', 'vietnam': '越南',
    'india': '印度', 'southeast asia': '東南亞',
}


@api_bp.route('/tasks', methods=['POST'])
def create_task():
    tm = _get_task_manager()
    data = request.get_json()

    location = data.get('location', 'Taiwan')
    location_zh = data.get('location_zh') or LOCATION_ZH_MAP.get(location.lower(), location)

    task = SearchTask(
        client_name=data.get('client_name', ''),
        job_title=data.get('job_title', ''),
        primary_skills=data.get('primary_skills', []),
        secondary_skills=data.get('secondary_skills', []),
        location=location,
        location_zh=location_zh,
        pages=data.get('pages', 3),
        schedule_type=data.get('schedule_type', 'once'),
        schedule_time=data.get('schedule_time', ''),
        schedule_interval_hours=data.get('schedule_interval_hours', 6),
        schedule_weekdays=data.get('schedule_weekdays', []),
        step1ne_job_id=data.get('step1ne_job_id'),
        auto_push=data.get('auto_push', False),
        start_page=data.get('start_page', 0),
        custom_query=data.get('custom_query', ''),
        angle_id=data.get('angle_id', ''),
    )

    task_id = tm.add_task(task)

    # 立即執行（once 或空字串或 run_now）
    if task.schedule_type in ('once', '') or data.get('run_now'):
        tm.run_now(task_id)

    return jsonify({'id': task_id, 'task': task.to_dict()}), 201


@api_bp.route('/tasks/<task_id>', methods=['PATCH'])
def update_task(task_id):
    tm = _get_task_manager()
    data = request.get_json()
    if tm.update_task(task_id, data):
        return jsonify({'success': True})
    return jsonify({'error': 'Task not found'}), 404


@api_bp.route('/tasks/<task_id>', methods=['DELETE'])
def delete_task(task_id):
    tm = _get_task_manager()
    if tm.remove_task(task_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Task not found'}), 404


@api_bp.route('/tasks/<task_id>/run', methods=['POST'])
def run_task(task_id):
    tm = _get_task_manager()
    if tm.run_now(task_id):
        return jsonify({'success': True, 'message': '任務已開始執行'})
    return jsonify({'error': 'Task not found or already running'}), 400


@api_bp.route('/tasks/<task_id>/stop', methods=['POST'])
def stop_task(task_id):
    tm = _get_task_manager()
    if tm.stop_task(task_id):
        return jsonify({'success': True, 'message': '任務已停止'})
    return jsonify({'error': 'Task not found or not running'}), 400


@api_bp.route('/tasks/<task_id>/status')
def task_status(task_id):
    tm = _get_task_manager()
    status = tm.get_task_status(task_id)
    if status:
        return jsonify(status)
    return jsonify({'error': 'Task not found'}), 404


# ── 客戶 ─────────────────────────────────────────────────────

@api_bp.route('/clients')
def list_clients():
    store = _get_sheets_store()
    if not store:
        return jsonify([])
    return jsonify(store.list_clients())


# ── 已處理紀錄 ───────────────────────────────────────────────

@api_bp.route('/processed')
def list_processed():
    store = _get_sheets_store()
    if not store:
        return jsonify([])
    return jsonify(store.get_processed_records())


@api_bp.route('/processed/<record_id>', methods=['PATCH'])
def update_processed(record_id):
    store = _get_sheets_store()
    if not store:
        return jsonify({'error': '資料儲存未初始化'}), 503
    data = request.get_json()
    store.update_processed_status(
        record_id, data.get('status', 'imported'),
        system_id=data.get('system_id'),
    )
    return jsonify({'success': True})


# ── Step1ne 系統整合 ─────────────────────────────────────────

@api_bp.route('/system/jobs')
def system_jobs():
    client = _get_step1ne_client()
    if not client:
        return jsonify({'error': 'Step1ne 系統未連結', 'connected': False}), 503
    try:
        jobs = client.fetch_jobs(status='all')
        if not jobs:
            # 嘗試 test_connection 確認是否真的連線成功
            test = client.test_connection()
            if not test.get('connected'):
                return jsonify({
                    'connected': False,
                    'error': f"無法連線: {test.get('error', '未知錯誤')}",
                    'api_base': client.api_base,
                }), 503
        return jsonify({'connected': True, 'jobs': jobs})
    except Exception as e:
        return jsonify({
            'connected': False,
            'error': f'載入職缺失敗: {str(e)}',
            'api_base': client.api_base,
        }), 500


@api_bp.route('/system/test')
def system_test():
    client = _get_step1ne_client()
    if not client:
        return jsonify({'connected': False, 'error': '未設定'})
    return jsonify(client.test_connection())


@api_bp.route('/system/push', methods=['POST'])
def system_push():
    client = _get_step1ne_client()
    if not client:
        return jsonify({'error': 'Step1ne 系統未連結'}), 503

    data = request.get_json()
    candidates = data.get('candidates', [])

    # 評分門檻篩選（空值 = 全部推送，含 D 級）
    min_grade = data.get('min_grade', '')
    if min_grade:
        grade_order = {'A': 4, 'B': 3, 'C': 2, 'D': 1, '': 0}
        min_order = grade_order.get(min_grade, 0)
        candidates = [c for c in candidates
                      if grade_order.get(c.get('grade', ''), 0) >= min_order]
        if not candidates:
            return jsonify({'error': f'沒有候選人達到 {min_grade} 級以上'}), 400

    # 從任務查 step1ne_job_id，附加到每位候選人（用於設定目標職缺）
    tm = _get_task_manager()
    if tm:
        task_job_map = {}  # task_id → step1ne_job_id
        for t in tm.get_all_tasks():
            if t.step1ne_job_id:
                task_job_map[t.id] = t.step1ne_job_id
        for c in candidates:
            if not c.get('step1ne_job_id') and c.get('task_id'):
                job_id = task_job_map.get(c['task_id'])
                if job_id:
                    c['step1ne_job_id'] = job_id

    # 推送前資料處理
    for c in candidates:
        # status 設為 Step1ne 的初篩狀態（爬蟲內部 status 如 'new'/'imported' 不應洩漏）
        c['status'] = '爬蟲初篩'

        # work_history / education_details 如果是 JSON 字串，解析為物件
        # （LocalStore 以字串存放，但 Step1ne 的 crawlerImportService 預期物件）
        for field in ('work_history', 'education_details', 'ai_match_result'):
            val = c.get(field)
            if isinstance(val, str) and val.startswith('['):
                try:
                    c[field] = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    pass
            elif isinstance(val, str) and val.startswith('{'):
                try:
                    c[field] = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    pass

    # 使用新版匯入端點（v2），由 Step1ne 端做欄位映射
    result = client.push_candidates_v2(candidates, actor='Crawler-WebUI')

    created = result.get('created_count', 0)
    updated = result.get('updated_count', 0)
    failed = result.get('failed_count', 0)

    # 推送成功（至少有部分成功） → 標記候選人為「已匯入」
    if created > 0 or updated > 0 or result.get('success'):
        store = _get_sheets_store()
        if store:
            imported_count = 0
            for c in candidates:
                cid = c.get('id')
                cname = c.get('client_name')
                if cid and cname:
                    try:
                        store.update_candidate_status(cname, cid, 'imported')
                        imported_count += 1
                    except Exception as e:
                        logger.error(f"標記已匯入失敗 ({cid}): {e}")
            result['marked_imported'] = imported_count
            logger.info(f"已標記 {imported_count} 位候選人為「已匯入」")

    # 統一回傳格式：有部分成功就不算整體失敗
    response = {
        'created_count': created,
        'updated_count': updated,
        'failed_count': failed,
        'marked_imported': result.get('marked_imported', 0),
    }
    # 只有全部失敗（沒有任何成功）才帶 error
    if failed > 0 and created == 0 and updated == 0:
        response['error'] = result.get('error') or result.get('errors') or f'{failed} 位候選人匯入失敗'
    elif result.get('error') and created == 0 and updated == 0:
        response['error'] = result['error']

    return jsonify(response)


# ── 內部：Worker 回報結果 ────────────────────────────────────

@api_bp.route('/internal/results', methods=['POST'])
def internal_results():
    """Worker 回報搜尋結果，寫入本地儲存"""
    store = _get_sheets_store()
    data = request.get_json()

    task_id = data.get('task_id')
    client_name = data.get('client_name')
    candidates_data = data.get('candidates', [])

    if not store:
        logger.warning("儲存未初始化，結果僅記錄日誌")
        return jsonify({'success': True, 'written': 0})

    # 轉為 Candidate 物件
    candidates = []
    for c in candidates_data:
        candidate = Candidate(**{k: v for k, v in c.items() if hasattr(Candidate, k)})
        candidates.append(candidate)

    result = store.write_candidates(client_name, candidates)
    return jsonify({'success': True, **result})


# ── 設定 ─────────────────────────────────────────────────────

@api_bp.route('/settings', methods=['GET'])
def get_settings():
    config = current_app.config.get('CRAWLER_CONFIG', {})
    # 隱藏敏感資訊
    # 遮罩 API Keys（顯示前後幾位，讓用戶確認是哪把 key）
    brave_key = config.get('api_keys', {}).get('brave_api_key', '')
    github_tokens = config.get('api_keys', {}).get('github_tokens', [])

    def mask_key(key, prefix=4, suffix=4):
        if not key or len(key) < prefix + suffix + 3:
            return '***' if key else ''
        return key[:prefix] + '***' + key[-suffix:]

    perplexity_key = config.get('api_keys', {}).get('perplexity_api_key', '') or \
                     config.get('enrichment', {}).get('perplexity', {}).get('api_key', '')

    linkedin_cfg = config.get('enrichment', {}).get('linkedin', {})
    linkedin_username = linkedin_cfg.get('username', '')

    safe_config = {
        'step1ne': config.get('step1ne', {}),
        'crawler': config.get('crawler', {}),
        'anti_detect': config.get('anti_detect', {}),
        'enrichment': {
            'enabled': config.get('enrichment', {}).get('enabled', False),
            'provider': config.get('enrichment', {}).get('provider', 'perplexity'),
            'provider_priority': config.get('enrichment', {}).get('provider_priority', ['linkedin', 'perplexity', 'jina']),
            'linkedin': {
                'enabled': linkedin_cfg.get('enabled', False),
                'has_credentials': bool(linkedin_username and linkedin_cfg.get('password')),
                'username_masked': mask_key(linkedin_username, 3, 4) if linkedin_username else '',
            },
        },
        'google_sheets': {
            'spreadsheet_id': config.get('google_sheets', {}).get('spreadsheet_id', ''),
            'credentials_file': config.get('google_sheets', {}).get('credentials_file', 'credentials.json'),
            'has_credentials': bool(config.get('google_sheets', {}).get('credentials_file')),
        },
        'api_keys': {
            'github_token_count': len(github_tokens),
            'has_brave_key': bool(brave_key),
            'brave_key_masked': mask_key(brave_key, 4, 4),
            'github_tokens_masked': [mask_key(t, 4, 4) for t in github_tokens],
            'has_perplexity_key': bool(perplexity_key),
            'perplexity_key_masked': mask_key(perplexity_key, 4, 4),
        },
    }
    return jsonify(safe_config)


@api_bp.route('/settings', methods=['POST'])
def update_settings():
    """更新設定（寫入 config/default.yaml）"""
    import yaml
    import os

    data = request.get_json()
    config = current_app.config.get('CRAWLER_CONFIG', {})

    # 更新設定
    if 'step1ne' in data:
        config['step1ne'] = {**config.get('step1ne', {}), **data['step1ne']}
        # 重新初始化 Step1ne client
        if data['step1ne'].get('api_base_url'):
            from integration.step1ne_client import Step1neClient
            merged_s1 = {**config.get('step1ne', {}), **data['step1ne']}
            current_app.config['STEP1NE_CLIENT'] = Step1neClient(
                api_base_url=merged_s1['api_base_url'],
                api_key=merged_s1.get('api_key', '')
            )
        else:
            current_app.config['STEP1NE_CLIENT'] = None

    if 'api_keys' in data:
        existing_keys = config.get('api_keys', {})
        new_keys = data['api_keys']

        # Brave key: 只在有值時更新
        if new_keys.get('brave_api_key'):
            existing_keys['brave_api_key'] = new_keys['brave_api_key']

        # GitHub tokens: 智慧合併
        if 'github_tokens_new' in new_keys or 'github_tokens_keep_count' in new_keys:
            old_tokens = existing_keys.get('github_tokens', [])
            keep_count = new_keys.get('github_tokens_keep_count', len(old_tokens))
            kept = old_tokens[:keep_count]
            new_tokens = new_keys.get('github_tokens_new', [])
            existing_keys['github_tokens'] = kept + new_tokens
        elif 'github_tokens' in new_keys:
            # 直接覆蓋（向後相容）
            existing_keys['github_tokens'] = new_keys['github_tokens']

        # Perplexity key: 只在有值時更新
        if new_keys.get('perplexity_api_key'):
            existing_keys['perplexity_api_key'] = new_keys['perplexity_api_key']
            # 同步到 enrichment 區塊
            if 'enrichment' not in config:
                config['enrichment'] = {}
            if 'perplexity' not in config['enrichment']:
                config['enrichment']['perplexity'] = {}
            config['enrichment']['perplexity']['api_key'] = new_keys['perplexity_api_key']
            config['enrichment']['enabled'] = True
            # 清除快取的 enricher，下次會重新初始化
            if hasattr(current_app, '_profile_enricher'):
                del current_app._profile_enricher
            if hasattr(current_app, '_contextual_scorer'):
                del current_app._contextual_scorer

        config['api_keys'] = existing_keys

    if 'crawler' in data:
        config['crawler'] = {**config.get('crawler', {}), **data['crawler']}

    if 'anti_detect' in data:
        config['anti_detect'] = {**config.get('anti_detect', {}), **data['anti_detect']}

    if 'linkedin_credentials' in data:
        linkedin_data = data['linkedin_credentials']
        if 'enrichment' not in config:
            config['enrichment'] = {}
        if 'linkedin' not in config['enrichment']:
            config['enrichment']['linkedin'] = {}

        li_cfg = config['enrichment']['linkedin']

        if 'username' in linkedin_data and linkedin_data['username']:
            li_cfg['username'] = linkedin_data['username']
        if 'password' in linkedin_data and linkedin_data['password']:
            li_cfg['password'] = linkedin_data['password']
        if 'enabled' in linkedin_data:
            li_cfg['enabled'] = bool(linkedin_data['enabled'])

        config['enrichment']['linkedin'] = li_cfg

        # 清除快取的 enricher，下次會重新初始化
        if hasattr(current_app, '_profile_enricher'):
            del current_app._profile_enricher
        if hasattr(current_app, '_contextual_scorer'):
            del current_app._contextual_scorer

    if 'google_sheets' in data:
        config['google_sheets'] = {**config.get('google_sheets', {}), **data['google_sheets']}

    # 寫回 YAML
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'default.yaml')
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
        current_app.config['CRAWLER_CONFIG'] = config
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── 去重快取 ─────────────────────────────────────────────────

@api_bp.route('/dedup/stats')
def dedup_stats():
    from crawler.dedup import DedupCache
    config = current_app.config.get('CRAWLER_CONFIG', {})
    cache = DedupCache(config.get('dedup', {}).get('cache_file', 'data/dedup_cache.json'))
    return jsonify(cache.stats())


@api_bp.route('/dedup/clear', methods=['POST'])
def dedup_clear():
    from crawler.dedup import DedupCache
    config = current_app.config.get('CRAWLER_CONFIG', {})
    data = request.get_json() or {}
    cache = DedupCache(config.get('dedup', {}).get('cache_file', 'data/dedup_cache.json'))
    cache.clear(source=data.get('source'))
    return jsonify({'success': True, 'stats': cache.stats()})


# ── Dashboard 統計 ───────────────────────────────────────────

# ── 技能評分 API ─────────────────────────────────────────────

@api_bp.route('/score/candidates', methods=['POST'])
def score_candidates():
    """對指定候選人重新評分"""
    store = _get_sheets_store()
    if not store:
        return jsonify({'error': '資料儲存未初始化'}), 503

    data = request.get_json()
    client_name = data.get('client_name')
    job_title = data.get('job_title')
    candidate_ids = data.get('candidate_ids', [])

    if not client_name:
        return jsonify({'error': 'Missing client_name'}), 400

    scoring = _get_scoring_components()
    engine = scoring['engine']
    profile_mgr = scoring['profile_manager']

    # 嘗試從任務中取得 skills
    primary_skills = data.get('primary_skills', [])
    secondary_skills = data.get('secondary_skills', [])

    # 如果沒有指定 skills，從 TaskManager 查找相關任務
    if not primary_skills:
        tm = _get_task_manager()
        if tm:
            for t in tm.get_all_tasks():
                if t.client_name == client_name and (not job_title or t.job_title == job_title):
                    primary_skills = t.primary_skills
                    secondary_skills = t.secondary_skills
                    if not job_title:
                        job_title = t.job_title
                    break

    # 如果仍無 skills，嘗試用 KeywordGenerator 從 job_title 自動生成
    if not primary_skills and job_title:
        gen_result = scoring['keyword_generator'].generate(job_title)
        primary_skills = gen_result.get('primary_skills', [])
        secondary_skills = gen_result.get('secondary_skills', [])

    # 載入 Job Profile
    job_profile = profile_mgr.load_profile(
        client_name=client_name,
        job_title=job_title or '',
        primary_skills=primary_skills,
        secondary_skills=secondary_skills,
    )

    # 讀取候選人
    result = store.read_candidates(client_name=client_name, limit=500)
    candidates = result.get('data', [])

    if candidate_ids:
        candidates = [c for c in candidates if c.get('id') in candidate_ids]

    scored_results = []
    for c in candidates:
        try:
            score_result = engine.score_candidate(c, job_profile)
            score = score_result['total_score']
            grade = score_result['grade']
            detail_json = engine.score_to_detail_json(score_result)

            # 更新評分
            store.update_candidate_score(
                client_name, c['id'], score, grade, detail_json
            )

            scored_results.append({
                'id': c.get('id'),
                'name': c.get('name'),
                'score': score,
                'grade': grade,
                'matched_skills': score_result.get('matched_skills', []),
                'missing_critical': score_result.get('missing_critical', []),
            })
        except Exception as e:
            logger.error(f"重新評分失敗 ({c.get('name')}): {e}")

    return jsonify({
        'scored': len(scored_results),
        'results': scored_results,
    })


@api_bp.route('/score/profile/<client_name>/<job_title>')
def get_score_profile(client_name, job_title):
    """取得 Job Profile"""
    scoring = _get_scoring_components()
    profile = scoring['profile_manager'].load_profile(
        client_name=client_name,
        job_title=job_title,
    )
    return jsonify(profile)


@api_bp.route('/score/profile', methods=['POST'])
def save_score_profile():
    """儲存自訂 Job Profile"""
    data = request.get_json()
    client_name = data.get('client_name')
    job_title = data.get('job_title')
    profile = data.get('profile')

    if not client_name or not job_title or not profile:
        return jsonify({'error': 'Missing client_name, job_title, or profile'}), 400

    scoring = _get_scoring_components()
    scoring['profile_manager'].save_profile(client_name, job_title, profile)
    return jsonify({'success': True})


@api_bp.route('/score/detail/<candidate_id>')
def score_detail(candidate_id):
    """取得候選人的評分細項（展開顯示用）"""
    store = _get_sheets_store()
    if not store:
        return jsonify({'error': '資料儲存未初始化'}), 503

    from scoring.engine import ScoringEngine

    # 搜尋候選人
    for client in store.list_clients():
        result = store.read_candidates(client_name=client, limit=500)
        for r in result.get('data', []):
            if r.get('id') == candidate_id:
                detail_json = r.get('score_detail', '')
                display = ScoringEngine.detail_json_to_display(detail_json)
                display['score'] = r.get('score', 0)
                display['grade'] = r.get('grade', '')
                return jsonify(display)

    return jsonify({'error': 'Not found'}), 404


# ── 關鍵字生成 API ───────────────────────────────────────────

@api_bp.route('/keywords/generate', methods=['POST'])
def generate_keywords():
    """從職缺名稱自動生成搜尋關鍵字"""
    data = request.get_json()
    job_title = data.get('job_title', '')
    existing_skills = data.get('existing_skills', [])

    if not job_title:
        return jsonify({'error': 'Missing job_title'}), 400

    scoring = _get_scoring_components()
    result = scoring['keyword_generator'].generate(
        job_title=job_title,
        existing_skills=existing_skills,
    )
    return jsonify(result)


@api_bp.route('/keywords/suggestions')
def keyword_suggestions():
    """取得所有已知技能列表（供自動完成）"""
    scoring = _get_scoring_components()
    skills = scoring['normalizer'].get_all_canonical_skills()
    return jsonify(sorted(skills))


# ── LinkedIn OCR 深度分析 API ────────────────────────────────

@api_bp.route('/linkedin/ocr-analyze', methods=['POST'])
def linkedin_ocr_analyze():
    """對單一 LinkedIn 候選人做 OCR 深度分析"""
    store = _get_sheets_store()
    data = request.get_json()

    candidate_id = data.get('candidate_id')
    client_name = data.get('client_name')
    linkedin_url = data.get('linkedin_url')

    if not linkedin_url:
        return jsonify({'error': 'Missing linkedin_url'}), 400

    config = current_app.config.get('CRAWLER_CONFIG', {})

    from scoring.linkedin_ocr import LinkedInOCRAnalyzer
    from crawler.anti_detect import AntiDetect
    from crawler.ocr import CrawlerOCR

    # 初始化 OCR 分析器（或從 app 快取取得）
    if not hasattr(current_app, '_ocr_analyzer'):
        ad = AntiDetect(config)
        ocr = CrawlerOCR(config)
        current_app._ocr_analyzer = LinkedInOCRAnalyzer(config, ad, ocr)

    analyzer = current_app._ocr_analyzer

    # 檢查配額
    remaining = analyzer.get_quota_remaining()
    if remaining <= 0:
        return jsonify({
            'success': False,
            'error': 'OCR 配額已用完（每小時 10 次），請稍後再試',
            'quota_remaining': 0,
        }), 429

    # 執行 OCR 分析
    ocr_result = analyzer.analyze_sync(linkedin_url)

    if not ocr_result.get('success'):
        return jsonify(ocr_result), 500

    # 如果有提取到新技能，重新評分
    new_score = None
    new_grade = None
    previous_score = None

    if store and candidate_id and client_name:
        # 取得候選人目前資料
        result = store.read_candidates(client_name=client_name, limit=500)
        candidate_data = None
        for r in result.get('data', []):
            if r.get('id') == candidate_id:
                candidate_data = r
                break

        if candidate_data:
            previous_score = candidate_data.get('score', 0)

            # 合併新技能
            existing_skills = candidate_data.get('skills', '')
            if isinstance(existing_skills, str):
                existing_skills = [s.strip() for s in existing_skills.split(',') if s.strip()]

            extracted_skills = ocr_result.get('extracted_skills', [])
            combined = list(set(existing_skills + extracted_skills))
            candidate_data['skills'] = combined

            # 用新資料重新評分
            scoring = _get_scoring_components()
            ocr_job_title = candidate_data.get('job_title', '')

            # 從任務取得 skills 以建立 profile
            ocr_primary = []
            ocr_secondary = []
            tm = _get_task_manager()
            if tm:
                for t in tm.get_all_tasks():
                    if t.client_name == client_name and (not ocr_job_title or t.job_title == ocr_job_title):
                        ocr_primary = t.primary_skills
                        ocr_secondary = t.secondary_skills
                        break
            if not ocr_primary and ocr_job_title:
                gen = scoring['keyword_generator'].generate(ocr_job_title)
                ocr_primary = gen.get('primary_skills', [])
                ocr_secondary = gen.get('secondary_skills', [])

            job_profile = scoring['profile_manager'].load_profile(
                client_name=client_name,
                job_title=ocr_job_title,
                primary_skills=ocr_primary,
                secondary_skills=ocr_secondary,
            )

            score_result = scoring['engine'].score_candidate(candidate_data, job_profile)
            new_score = score_result['total_score']
            new_grade = score_result['grade']
            detail_json = scoring['engine'].score_to_detail_json(score_result)

            # 更新評分
            store.update_candidate_score(
                client_name, candidate_id, new_score, new_grade, detail_json
            )

    return jsonify({
        'success': True,
        'ocr_data': {
            'extracted_skills': ocr_result.get('extracted_skills', []),
            'headline': ocr_result.get('headline', ''),
            'experience': ocr_result.get('experience', ''),
            'raw_text': ocr_result.get('raw_text', '')[:500],
        },
        'new_score': new_score,
        'new_grade': new_grade,
        'previous_score': previous_score,
        'quota_remaining': ocr_result.get('quota_remaining', 0),
    })


@api_bp.route('/linkedin/ocr-quota')
def linkedin_ocr_quota():
    """查詢本小時 OCR 剩餘次數"""
    config = current_app.config.get('CRAWLER_CONFIG', {})

    from scoring.linkedin_ocr import LinkedInOCRAnalyzer

    if not hasattr(current_app, '_ocr_analyzer'):
        from crawler.anti_detect import AntiDetect
        from crawler.ocr import CrawlerOCR
        ad = AntiDetect(config)
        ocr = CrawlerOCR(config)
        current_app._ocr_analyzer = LinkedInOCRAnalyzer(config, ad, ocr)

    remaining = current_app._ocr_analyzer.get_quota_remaining()
    return jsonify({
        'quota_remaining': remaining,
        'quota_total': 10,
    })


# ── AI 深度分析 (Perplexity/Jina) ─────────────────────────────

def _get_enrichment_components():
    """取得 enrichment 系統元件（懶載入）"""
    if not hasattr(current_app, '_profile_enricher'):
        config = current_app.config.get('CRAWLER_CONFIG', {})
        enrichment_config = config.get('enrichment', {})

        # 合併 Perplexity API key（可能存在 api_keys 區塊）
        if not enrichment_config.get('perplexity', {}).get('api_key'):
            pplx_key = config.get('api_keys', {}).get('perplexity_api_key', '')
            if pplx_key:
                if 'perplexity' not in enrichment_config:
                    enrichment_config['perplexity'] = {}
                enrichment_config['perplexity']['api_key'] = pplx_key

        from enrichment.profile_enricher import ProfileEnricher
        from enrichment.contextual_scorer import ContextualScorer

        current_app._profile_enricher = ProfileEnricher(enrichment_config)

        step1ne = _get_step1ne_client()
        current_app._contextual_scorer = ContextualScorer(enrichment_config, step1ne)

    return {
        'enricher': current_app._profile_enricher,
        'scorer': current_app._contextual_scorer,
    }


@api_bp.route('/enrich/analyze', methods=['POST'])
def enrich_analyze():
    """
    單一候選人 AI 深度分析

    Body: {
        "candidate_id": "xxx" (optional, 用於關聯 Sheets 記錄),
        "linkedin_url": "https://linkedin.com/in/xxx",
        "candidate": { ... } (optional, 完整候選人資料),
        "job_id": 123 (optional, Step1ne 職缺 ID),
        "client_name": "xxx" (optional),
    }
    """
    data = request.get_json()
    linkedin_url = data.get('linkedin_url', '')
    candidate_data = data.get('candidate', {})
    job_id = data.get('job_id')

    # 允許無 LinkedIn URL 的候選人（用姓名搜尋）
    has_linkedin = linkedin_url or candidate_data.get('linkedin_url')
    has_name = candidate_data.get('name', '').strip()
    force = data.get('force', False)  # 強制重新分析（跳過快取）

    if not has_linkedin and not has_name:
        return jsonify({'error': '需要 linkedin_url 或候選人 name'}), 400

    # 組合候選人資料
    if not candidate_data:
        candidate_data = {'linkedin_url': linkedin_url} if linkedin_url else {}
    elif linkedin_url and not candidate_data.get('linkedin_url'):
        candidate_data['linkedin_url'] = linkedin_url

    # 如果有 candidate_id，從 Sheets 讀取完整資料
    store = _get_sheets_store()
    candidate_id = data.get('candidate_id')
    client_name = data.get('client_name')
    if store and candidate_id and client_name:
        result = store.read_candidates(client_name=client_name, limit=500)
        for r in result.get('data', []):
            if r.get('id') == candidate_id:
                candidate_data = {**r, **candidate_data}
                break

    try:
        components = _get_enrichment_components()
        enricher = components['enricher']
        scorer = components['scorer']

        # Step 1: 深度分析（LinkedIn 頁面 或 姓名搜尋）
        enriched = enricher.enrich_candidate(candidate_data, force=force)

        # Step 2: 職缺匹配評分（如果有 job_id）
        ai_match = None
        job_recommendations = []

        if job_id:
            score_result = scorer.score_with_job_context(enriched, int(job_id))
            ai_match = score_result.get('ai_match_result')
        else:
            # 嘗試從任務找 job_id
            task_id = candidate_data.get('task_id')
            if task_id:
                tm = _get_task_manager()
                if tm:
                    for t in tm.get_all_tasks():
                        if t.id == task_id and t.step1ne_job_id:
                            score_result = scorer.score_with_job_context(enriched, int(t.step1ne_job_id))
                            ai_match = score_result.get('ai_match_result')
                            break

            # 自動推薦 Top 3 職缺
            if not ai_match:
                job_recommendations = scorer.recommend_jobs(enriched, top_n=3)
                if job_recommendations:
                    ai_match = job_recommendations[0].get('ai_match_result')

        return jsonify({
            'success': enriched.get('success', False),
            'enriched': {k: v for k, v in enriched.items() if not k.startswith('_')},
            'ai_match_result': ai_match,
            'job_recommendations': job_recommendations,
            'usage': enricher.get_stats(),
            'source': enriched.get('_enrichment_source', ''),
        })

    except Exception as e:
        logger.error(f"AI 深度分析失敗: {e}", exc_info=True)
        return jsonify({'error': f'分析失敗: {str(e)}', 'success': False}), 500


@api_bp.route('/enrich/batch', methods=['POST'])
def enrich_batch():
    """
    批量 AI 深度分析

    Body: {
        "candidate_ids": ["id1", "id2", ...],
        "client_name": "xxx",
        "job_id": 123 (optional),
    }
    """
    data = request.get_json()
    candidate_ids = data.get('candidate_ids', [])
    candidates_direct = data.get('candidates', [])
    client_name = data.get('client_name')
    job_id = data.get('job_id')

    # 支援兩種模式：直接傳候選人陣列 或 傳 candidate_ids 從 Sheets 讀取
    if candidates_direct:
        candidates = candidates_direct
    elif candidate_ids:
        store = _get_sheets_store()
        if not store:
            return jsonify({'error': '資料儲存未初始化'}), 503
        result = store.read_candidates(client_name=client_name, limit=500)
        all_candidates = result.get('data', [])
        id_set = set(candidate_ids)
        candidates = [c for c in all_candidates if c.get('id') in id_set]
    else:
        return jsonify({'error': '需要 candidates 或 candidate_ids'}), 400

    if not candidates:
        return jsonify({'error': '找不到指定的候選人'}), 404

    try:
        components = _get_enrichment_components()
        enricher = components['enricher']
        scorer = components['scorer']

        # 批量深度分析
        enriched_list = enricher.enrich_batch(candidates)

        # 逐一評分
        results = []
        for enriched in enriched_list:
            ai_match = None
            if job_id and enriched.get('success'):
                score_result = scorer.score_with_job_context(enriched, int(job_id))
                ai_match = score_result.get('ai_match_result')

            results.append({
                'name': enriched.get('name', ''),
                'success': enriched.get('success', False),
                'enriched': {k: v for k, v in enriched.items() if not k.startswith('_')},
                'ai_match_result': ai_match,
                'source': enriched.get('_enrichment_source', ''),
            })

        success_count = sum(1 for r in results if r['success'])
        failed_count = len(results) - success_count

        return jsonify({
            'results': results,
            'total': len(results),
            'success_count': success_count,
            'failed_count': failed_count,
            'usage': enricher.get_stats(),
        })

    except Exception as e:
        logger.error(f"批量 AI 分析失敗: {e}", exc_info=True)
        return jsonify({'error': f'批量分析失敗: {str(e)}'}), 500


@api_bp.route('/enrich/stats')
def enrich_stats():
    """查詢 enrichment 使用統計"""
    try:
        components = _get_enrichment_components()
        enricher = components['enricher']
        return jsonify(enricher.get_stats())
    except Exception as e:
        return jsonify({
            'total_calls': 0,
            'error': str(e),
            'message': 'Enrichment 系統尚未初始化',
        })


@api_bp.route('/enrich/clear-cache', methods=['POST'])
def enrich_clear_cache():
    """清除空/失敗的 enrichment 快取，強制下次重新分析"""
    try:
        components = _get_enrichment_components()
        enricher = components['enricher']
        result = enricher.clear_stale_cache()
        return jsonify({
            'success': True,
            'message': f"已清除 {result['cleared']} 筆空/失敗快取，保留 {result['kept']} 筆有效",
            **result,
        })
    except Exception as e:
        logger.error(f"清除快取失敗: {e}", exc_info=True)
        return jsonify({'error': str(e), 'success': False}), 500


@api_bp.route('/enrich/re-enrich', methods=['POST'])
def enrich_re_enrich():
    """
    重新 enrich 指定客戶的所有候選人（清除快取 + 強制分析）

    Body: {
        "client_name": "xxx",           # 客戶名稱（從本地儲存讀取）
        "only_empty": true,             # 只處理缺少工作經歷/學歷的候選人（預設 true）
        "job_id": 123 (optional),       # Step1ne 職缺 ID（用於 AI 評分）
    }
    """
    data = request.get_json()
    client_name = data.get('client_name', '')
    only_empty = data.get('only_empty', True)
    job_id = data.get('job_id')

    if not client_name:
        return jsonify({'error': '需要 client_name'}), 400

    store = _get_sheets_store()
    if not store:
        return jsonify({'error': '本地儲存未初始化'}), 500

    try:
        # 讀取所有候選人
        result = store.read_candidates(client_name=client_name, limit=500)
        all_candidates = result.get('data', [])

        if not all_candidates:
            return jsonify({'error': f'找不到 {client_name} 的候選人'}), 404

        # 篩選需要重新 enrich 的候選人
        if only_empty:
            candidates = [
                c for c in all_candidates
                if not c.get('work_history') and not c.get('education_details')
            ]
        else:
            candidates = all_candidates

        if not candidates:
            return jsonify({
                'success': True,
                'message': '所有候選人已有完整資料，無需重新分析',
                'total': len(all_candidates),
                'enriched': 0,
            })

        # 清除空快取
        components = _get_enrichment_components()
        enricher = components['enricher']
        scorer = components['scorer']
        cache_result = enricher.clear_stale_cache()

        # 批量 enrich（強制模式）— 逐一處理並即時更新 store
        enriched_results = []
        success_count = 0
        for c in candidates:
            try:
                enriched = enricher.enrich_candidate(c, force=True)
                if enriched.get('success'):
                    success_count += 1
                    # 提取可更新欄位（排除內部欄位和空值）
                    update_fields = {
                        k: v for k, v in enriched.items()
                        if not k.startswith('_') and k != 'success' and v
                    }
                    # 即時更新 store 中的候選人
                    if update_fields:
                        store.update_candidate_fields(
                            client_name, c.get('name', ''), update_fields
                        )

                enriched_results.append({
                    'name': c.get('name', ''),
                    'success': enriched.get('success', False),
                    'source': enriched.get('_enrichment_source', ''),
                    'has_work_history': bool(enriched.get('work_history')),
                    'has_education': bool(enriched.get('education_details')),
                })
            except Exception as e:
                enriched_results.append({
                    'name': c.get('name', ''),
                    'success': False,
                    'error': str(e),
                })

        return jsonify({
            'success': True,
            'total': len(all_candidates),
            'processed': len(candidates),
            'enriched_success': success_count,
            'cache_cleared': cache_result.get('cleared', 0),
            'results': enriched_results,
            'usage': enricher.get_stats(),
        })

    except Exception as e:
        logger.error(f"重新 enrich 失敗: {e}", exc_info=True)
        return jsonify({'error': f'重新 enrich 失敗: {str(e)}', 'success': False}), 500


# ── LinkedIn API 狀態 ─────────────────────────────────────────

@api_bp.route('/linkedin/api-status')
def linkedin_api_status():
    """查詢 LinkedIn API 認證狀態"""
    try:
        components = _get_enrichment_components()
        enricher = components['enricher']

        if not enricher.linkedin_api:
            return jsonify({
                'available': False,
                'status': 'not_configured',
                'message': 'LinkedIn API 未設定',
            })

        api = enricher.linkedin_api
        stats = api.get_stats()

        return jsonify({
            'available': api.is_available(),
            'status': 'authenticated' if api._api else ('error' if api._auth_error else 'not_authenticated'),
            'authenticated': api._api is not None,
            'auth_error': api._auth_error,
            'stats': stats,
        })
    except Exception as e:
        return jsonify({
            'available': False,
            'status': 'error',
            'message': str(e),
        })


@api_bp.route('/linkedin/api-test', methods=['POST'])
def linkedin_api_test():
    """手動測試 LinkedIn API 認證"""
    try:
        components = _get_enrichment_components()
        enricher = components['enricher']

        if not enricher.linkedin_api:
            return jsonify({
                'success': False,
                'error': 'LinkedIn API 未設定，請先在設定頁面輸入帳密',
            }), 400

        api = enricher.linkedin_api
        if not api.is_available():
            return jsonify({
                'success': False,
                'error': '缺少 LinkedIn 帳號或密碼',
            }), 400

        # 嘗試認證
        api._api = None  # 清除舊連線
        api._auth_error = None
        api._ensure_authenticated()

        if api._api:
            return jsonify({
                'success': True,
                'message': 'LinkedIn API 認證成功！',
                'stats': api.get_stats(),
            })
        else:
            return jsonify({
                'success': False,
                'error': api._auth_error or '認證失敗（未知原因）',
            }), 401

    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'認證測試失敗: {str(e)}',
        }), 500


# ── GitHub 深度分析 API ──────────────────────────────────────

@api_bp.route('/github/analyze/<username>', methods=['POST'])
def github_analyze(username):
    """對單一 GitHub 用戶做深度分析"""
    config = current_app.config.get('CRAWLER_CONFIG', {})

    from crawler.github import GitHubSearcher
    from crawler.anti_detect import AntiDetect

    ad = AntiDetect(config)
    searcher = GitHubSearcher(config, ad)

    tokens = config.get('api_keys', {}).get('github_tokens', [])
    gh_headers = {}
    if tokens:
        gh_headers = {'Authorization': f'token {tokens[0]}'}

    result = searcher.deep_analyze(username, gh_headers)
    if result:
        return jsonify(result)
    return jsonify({'error': f'無法分析 {username}'}), 404


# ── Dashboard 統計 ───────────────────────────────────────────

@api_bp.route('/dashboard/stats')
def dashboard_stats():
    store = _get_sheets_store()
    tm = _get_task_manager()

    stats = {
        'total_candidates': 0,
        'today_new': 0,
        'running_tasks': 0,
        'scheduled_tasks': 0,
        'clients': {},
        'sources': {'linkedin': 0, 'github': 0, 'li+ocr': 0},
        'grades': {'A': 0, 'B': 0, 'C': 0, 'D': 0, '': 0},
        'recent_runs': [],
    }

    # 本地儲存統計
    if store:
        try:
            local_stats = store.get_stats()
            stats.update(local_stats)
        except Exception as e:
            logger.error(f"儲存統計失敗: {e}")

    # 任務統計
    if tm:
        tasks = tm.get_all_tasks()
        stats['running_tasks'] = sum(1 for t in tasks if t.status == 'running')
        stats['scheduled_tasks'] = sum(1 for t in tasks if t.schedule_type != 'once')
        stats['recent_runs'] = [
            {
                'task_id': t.id,
                'client_name': t.client_name,
                'job_title': t.job_title,
                'status': t.status,
                'last_run': t.last_run,
                'last_result_count': t.last_result_count,
                'progress': t.progress,
            }
            for t in sorted(tasks, key=lambda x: x.updated_at or '', reverse=True)[:10]
        ]

    return jsonify(stats)


# ── Logs API ──────────────────────────────────────────────────

@api_bp.route('/logs')
def get_logs():
    """讀取系統日誌"""
    lines = request.args.get('lines', 200, type=int)
    level = request.args.get('level', 'all')  # all / error / warning
    search = request.args.get('search', '')

    log_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs', 'crawler.log')

    if not os.path.exists(log_file):
        return jsonify({'logs': [], 'total': 0, 'file': log_file, 'error': '日誌檔案不存在'})

    try:
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()

        # 篩選等級
        if level == 'error':
            all_lines = [l for l in all_lines if 'ERROR' in l or 'CRITICAL' in l or 'Exception' in l or 'Traceback' in l]
        elif level == 'warning':
            all_lines = [l for l in all_lines if 'WARNING' in l or 'ERROR' in l or 'CRITICAL' in l]

        # 搜索過濾
        if search:
            search_lower = search.lower()
            all_lines = [l for l in all_lines if search_lower in l.lower()]

        # 取最後 N 行
        total = len(all_lines)
        result_lines = all_lines[-lines:]

        return jsonify({
            'logs': [l.rstrip('\n') for l in result_lines],
            'total': total,
            'showing': len(result_lines),
            'file': log_file,
        })
    except Exception as e:
        return jsonify({'logs': [], 'total': 0, 'error': str(e)}), 500


@api_bp.route('/logs/clear', methods=['POST'])
def clear_logs():
    """清空日誌檔案"""
    log_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs', 'crawler.log')
    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write('')
        return jsonify({'success': True, 'message': '日誌已清空'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── 快照 + 異常截圖 API ─────────────────────────────────────

@api_bp.route('/snapshots')
def list_snapshots_api():
    """列出最近的候選人頁面快照"""
    try:
        from crawler.snapshot import list_snapshots
        source = request.args.get('source')
        limit = int(request.args.get('limit', 20))
        return jsonify({'data': list_snapshots(source=source, limit=limit)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api_bp.route('/snapshots/reparse', methods=['POST'])
def reparse_snapshot_api():
    """從快照離線重新解析候選人"""
    try:
        from crawler.snapshot import reparse_snapshot
        data = request.get_json() or {}
        filepath = data.get('filepath', '')
        if not filepath:
            return jsonify({'error': 'Missing filepath'}), 400
        result = reparse_snapshot(filepath)
        return jsonify({'data': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api_bp.route('/error-screens')
def list_error_screens_api():
    """列出最近的異常截圖"""
    try:
        from crawler.snapshot import list_error_screens
        limit = int(request.args.get('limit', 20))
        return jsonify({'data': list_error_screens(limit=limit)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
