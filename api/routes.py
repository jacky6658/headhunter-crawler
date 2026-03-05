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
        return jsonify({'error': 'Google Sheets 未設定'}), 503

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
        return jsonify({'error': 'Google Sheets 未設定'}), 503

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
        return jsonify({'error': 'Google Sheets 未設定'}), 503

    data = request.get_json()
    status = data.get('status')
    client = data.get('client_name')
    if status and client:
        store.update_candidate_status(client, candidate_id, status)
        return jsonify({'success': True})
    return jsonify({'error': 'Missing client_name or status'}), 400


@api_bp.route('/candidates/export', methods=['POST'])
def export_candidates():
    store = _get_sheets_store()
    if not store:
        return jsonify({'error': 'Google Sheets 未設定'}), 503

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
    )

    task_id = tm.add_task(task)

    # 如果是 once，立即執行
    if task.schedule_type == 'once' or data.get('run_now'):
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
        return jsonify({'error': 'Google Sheets 未設定'}), 503
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
    jobs = client.fetch_jobs()
    return jsonify({'connected': True, 'jobs': jobs})


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

    # 使用新版匯入端點（v2），由 Step1ne 端做欄位映射
    result = client.push_candidates_v2(candidates, actor='Crawler-WebUI')
    return jsonify(result)


# ── 內部：Worker 回報結果 ────────────────────────────────────

@api_bp.route('/internal/results', methods=['POST'])
def internal_results():
    """Worker 回報搜尋結果，Flask 統一寫入 Sheets"""
    store = _get_sheets_store()
    data = request.get_json()

    task_id = data.get('task_id')
    client_name = data.get('client_name')
    candidates_data = data.get('candidates', [])

    if not store:
        logger.warning("Sheets 未設定，結果僅記錄日誌")
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
    safe_config = {
        'step1ne': config.get('step1ne', {}),
        'crawler': config.get('crawler', {}),
        'anti_detect': config.get('anti_detect', {}),
        'google_sheets': {
            'spreadsheet_id': config.get('google_sheets', {}).get('spreadsheet_id', ''),
            'has_credentials': bool(config.get('google_sheets', {}).get('credentials_file')),
        },
        'api_keys': {
            'github_token_count': len(config.get('api_keys', {}).get('github_tokens', [])),
            'has_brave_key': bool(config.get('api_keys', {}).get('brave_api_key')),
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
            current_app.config['STEP1NE_CLIENT'] = Step1neClient(data['step1ne']['api_base_url'])
        else:
            current_app.config['STEP1NE_CLIENT'] = None

    if 'api_keys' in data:
        config['api_keys'] = {**config.get('api_keys', {}), **data['api_keys']}

    if 'crawler' in data:
        config['crawler'] = {**config.get('crawler', {}), **data['crawler']}

    if 'anti_detect' in data:
        config['anti_detect'] = {**config.get('anti_detect', {}), **data['anti_detect']}

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
        return jsonify({'error': 'Google Sheets 未設定'}), 503

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

            # 更新 Sheets
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
        return jsonify({'error': 'Google Sheets 未設定'}), 503

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

            # 更新 Sheets
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

    # Sheets 統計
    if store:
        try:
            sheet_stats = store.get_stats()
            stats.update(sheet_stats)
        except Exception as e:
            logger.error(f"Sheets 統計失敗: {e}")

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
