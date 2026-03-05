"""
資料模型單元測試
"""
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from storage.models import Candidate, SearchTask, ProcessedRecord


class TestCandidate:
    def test_defaults(self):
        c = Candidate()
        assert c.name == ''
        assert c.source == ''
        assert c.status == 'new'
        assert c.skills == []

    def test_to_dict(self):
        c = Candidate(name='John', source='github', skills=['Python'])
        d = c.to_dict()
        assert d['name'] == 'John'
        assert d['source'] == 'github'
        assert d['skills'] == ['Python']

    def test_sheets_header(self):
        header = Candidate.sheets_header()
        assert 'id' in header
        assert 'name' in header
        assert 'skills' in header
        assert len(header) == 18

    def test_to_sheets_row(self):
        c = Candidate(
            id='abc', name='John', source='github',
            skills=['Python', 'Django'], public_repos=10,
        )
        row = c.to_sheets_row()
        assert row[0] == 'abc'
        assert row[1] == 'John'
        assert row[2] == 'github'
        assert 'Python, Django' in row[10]

    def test_skills_as_string(self):
        c = Candidate(skills='Python, Django')
        row = c.to_sheets_row()
        assert row[10] == 'Python, Django'


class TestSearchTask:
    def test_defaults(self):
        t = SearchTask()
        assert t.location == 'Taiwan'
        assert t.pages == 3
        assert t.status == 'pending'

    def test_all_skills(self):
        t = SearchTask(
            primary_skills=['Java', 'Spring'],
            secondary_skills=['MySQL', 'Redis'],
        )
        assert t.all_skills == ['Java', 'Spring', 'MySQL', 'Redis']

    def test_from_dict(self):
        d = {
            'id': 'test1',
            'client_name': 'Client A',
            'job_title': 'Engineer',
            'primary_skills': ['Python'],
            'unknown_field': 'ignored',
        }
        t = SearchTask.from_dict(d)
        assert t.id == 'test1'
        assert t.client_name == 'Client A'
        assert t.primary_skills == ['Python']

    def test_to_dict_roundtrip(self):
        t = SearchTask(
            id='test2', client_name='B',
            primary_skills=['Go'], pages=5,
        )
        d = t.to_dict()
        t2 = SearchTask.from_dict(d)
        assert t2.id == t.id
        assert t2.client_name == t.client_name
        assert t2.primary_skills == t.primary_skills
        assert t2.pages == t.pages


class TestProcessedRecord:
    def test_sheets_header(self):
        header = ProcessedRecord.sheets_header()
        assert 'linkedin_url' in header
        assert 'status' in header
        assert len(header) == 8

    def test_to_sheets_row(self):
        r = ProcessedRecord(
            linkedin_url='https://linkedin.com/in/test/',
            name='Test', status='imported',
        )
        row = r.to_sheets_row()
        assert row[0] == 'https://linkedin.com/in/test/'
        assert row[2] == 'Test'
        assert row[6] == 'imported'
