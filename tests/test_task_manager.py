"""
任務管理模組單元測試
"""
import json
import os
import pytest
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from scheduler.task_manager import TaskManager
from storage.models import SearchTask


@pytest.fixture
def tm(tmp_path):
    config = {
        'scheduler': {
            'tasks_file': str(tmp_path / 'tasks.json'),
            'checkpoint_file': str(tmp_path / 'checkpoints.json'),
        },
    }
    return TaskManager(config)


class TestTaskManager:
    def test_add_task(self, tm):
        task = SearchTask(
            client_name='TestClient',
            job_title='Dev',
            primary_skills=['Python'],
        )
        tid = tm.add_task(task)
        assert tid
        assert tid in tm.tasks

    def test_get_task(self, tm):
        task = SearchTask(client_name='A', job_title='B')
        tid = tm.add_task(task)
        retrieved = tm.get_task(tid)
        assert retrieved.client_name == 'A'
        assert retrieved.job_title == 'B'

    def test_get_all_tasks(self, tm):
        tm.add_task(SearchTask(client_name='A', job_title='1'))
        tm.add_task(SearchTask(client_name='B', job_title='2'))
        all_tasks = tm.get_all_tasks()
        assert len(all_tasks) == 2

    def test_remove_task(self, tm):
        tid = tm.add_task(SearchTask(client_name='Del', job_title='X'))
        assert tm.remove_task(tid)
        assert tm.get_task(tid) is None

    def test_remove_nonexistent(self, tm):
        assert not tm.remove_task('fake_id')

    def test_update_task(self, tm):
        tid = tm.add_task(SearchTask(client_name='Up', job_title='Y', pages=3))
        assert tm.update_task(tid, {'pages': 5})
        assert tm.get_task(tid).pages == 5

    def test_update_nonexistent(self, tm):
        assert not tm.update_task('fake', {'pages': 1})

    def test_persistence(self, tmp_path):
        config = {
            'scheduler': {
                'tasks_file': str(tmp_path / 'tasks.json'),
                'checkpoint_file': str(tmp_path / 'checkpoints.json'),
            },
        }
        tm1 = TaskManager(config)
        tid = tm1.add_task(SearchTask(client_name='P', job_title='Q', pages=7))

        tm2 = TaskManager(config)
        t = tm2.get_task(tid)
        assert t is not None
        assert t.client_name == 'P'
        assert t.pages == 7

    def test_running_reset_on_load(self, tmp_path):
        config = {
            'scheduler': {
                'tasks_file': str(tmp_path / 'tasks.json'),
                'checkpoint_file': str(tmp_path / 'checkpoints.json'),
            },
        }
        tm1 = TaskManager(config)
        tid = tm1.add_task(SearchTask(client_name='R', job_title='S'))
        tm1.tasks[tid].status = 'running'
        tm1._save_tasks()

        tm2 = TaskManager(config)
        assert tm2.get_task(tid).status == 'pending'

    def test_task_status(self, tm):
        tid = tm.add_task(SearchTask(client_name='St', job_title='T'))
        status = tm.get_task_status(tid)
        assert status['id'] == tid
        assert status['status'] == 'pending'
        assert status['progress'] == 0

    def test_task_status_nonexistent(self, tm):
        assert tm.get_task_status('fake') == {}

    def test_checkpoint_save_and_clear(self, tm):
        tid = tm.add_task(SearchTask(client_name='C', job_title='K'))
        tm._save_checkpoint(tid, 'running', {'page': 3})
        data = tm._load_checkpoints()
        assert tid in data
        assert data[tid]['phase'] == 'running'

        tm._clear_checkpoint(tid)
        data2 = tm._load_checkpoints()
        assert tid not in data2
