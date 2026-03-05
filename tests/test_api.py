"""
REST API 端點單元測試
"""
import json
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app


@pytest.fixture
def client(tmp_path):
    """建立 Flask 測試客戶端"""
    os.environ.setdefault('CRAWLER_DATA_DIR', str(tmp_path))
    app = create_app({
        'TESTING': True,
        'scheduler': {'tasks_file': str(tmp_path / 'tasks.json')},
    })
    with app.test_client() as c:
        yield c


class TestHealthEndpoint:
    def test_health(self, client):
        resp = client.get('/api/health')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'ok'
        assert 'timestamp' in data


class TestTasksEndpoints:
    def test_list_empty(self, client):
        resp = client.get('/api/tasks')
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_create_task(self, client):
        resp = client.post('/api/tasks', json={
            'client_name': 'TestClient',
            'job_title': 'Python Dev',
            'primary_skills': ['Python', 'Django'],
            'schedule_type': 'once',
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert 'id' in data
        assert data['task']['client_name'] == 'TestClient'

    def test_list_after_create(self, client):
        client.post('/api/tasks', json={
            'client_name': 'A', 'job_title': 'Dev',
            'primary_skills': ['Go'], 'schedule_type': 'daily',
            'schedule_time': '09:00',
        })
        resp = client.get('/api/tasks')
        tasks = resp.get_json()
        assert len(tasks) >= 1

    def test_delete_task(self, client):
        resp = client.post('/api/tasks', json={
            'client_name': 'Del', 'job_title': 'X',
            'primary_skills': ['Rust'], 'schedule_type': 'daily',
            'schedule_time': '10:00',
        })
        task_id = resp.get_json()['id']
        del_resp = client.delete(f'/api/tasks/{task_id}')
        assert del_resp.status_code == 200

    def test_delete_nonexistent(self, client):
        resp = client.delete('/api/tasks/nonexistent')
        assert resp.status_code == 404

    def test_update_task(self, client):
        resp = client.post('/api/tasks', json={
            'client_name': 'Up', 'job_title': 'Y',
            'primary_skills': ['Java'], 'schedule_type': 'daily',
            'schedule_time': '08:00',
        })
        task_id = resp.get_json()['id']
        patch_resp = client.patch(f'/api/tasks/{task_id}', json={
            'pages': 5,
        })
        assert patch_resp.status_code == 200

    def test_task_status_not_found(self, client):
        resp = client.get('/api/tasks/nonexistent/status')
        assert resp.status_code == 404


class TestSettingsEndpoints:
    def test_get_settings(self, client):
        resp = client.get('/api/settings')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'crawler' in data or 'step1ne' in data

    def test_update_settings(self, client):
        resp = client.post('/api/settings', json={
            'step1ne': {'api_base_url': 'https://test.example.com'},
        })
        assert resp.status_code == 200


class TestDedupEndpoints:
    def test_dedup_stats(self, client):
        resp = client.get('/api/dedup/stats')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'linkedin' in data
        assert 'github' in data

    def test_dedup_clear(self, client):
        resp = client.post('/api/dedup/clear', json={})
        assert resp.status_code == 200


class TestDashboardStats:
    def test_dashboard(self, client):
        resp = client.get('/api/dashboard/stats')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'total_candidates' in data
        assert 'running_tasks' in data


class TestSystemEndpoints:
    def test_system_test_no_client(self, client):
        resp = client.get('/api/system/test')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['connected'] is False

    def test_system_jobs_no_client(self, client):
        resp = client.get('/api/system/jobs')
        # 503 when no client or error when client can't connect
        assert resp.status_code in (503, 200)


class TestCandidatesNoSheets:
    def test_list_no_sheets(self, client):
        resp = client.get('/api/candidates')
        # 503 when Sheets not configured
        assert resp.status_code == 503
