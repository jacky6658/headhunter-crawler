"""
去重快取單元測試
"""
import json
import os
import tempfile
import pytest
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from crawler.dedup import DedupCache


@pytest.fixture
def cache_file(tmp_path):
    return str(tmp_path / 'test_dedup.json')


class TestDedupCache:
    def test_mark_and_check_linkedin(self, cache_file):
        cache = DedupCache(cache_file)
        assert not cache.is_seen(linkedin_url='https://linkedin.com/in/john/')
        cache.mark_seen(linkedin_url='https://linkedin.com/in/john/')
        assert cache.is_seen(linkedin_url='https://linkedin.com/in/john/')

    def test_mark_and_check_github(self, cache_file):
        cache = DedupCache(cache_file)
        assert not cache.is_seen(github_username='johndoe')
        cache.mark_seen(github_username='johndoe')
        assert cache.is_seen(github_username='johndoe')

    def test_persistence(self, cache_file):
        cache1 = DedupCache(cache_file)
        cache1.mark_seen(linkedin_url='https://linkedin.com/in/test/')
        cache1.mark_seen(github_username='testuser')
        cache1.save()

        cache2 = DedupCache(cache_file)
        assert cache2.is_seen(linkedin_url='https://linkedin.com/in/test/')
        assert cache2.is_seen(github_username='testuser')

    def test_clear_linkedin(self, cache_file):
        cache = DedupCache(cache_file)
        cache.mark_seen(linkedin_url='https://linkedin.com/in/a/')
        cache.mark_seen(github_username='b')
        cache.clear(source='linkedin')
        assert not cache.is_seen(linkedin_url='https://linkedin.com/in/a/')
        assert cache.is_seen(github_username='b')

    def test_clear_github(self, cache_file):
        cache = DedupCache(cache_file)
        cache.mark_seen(linkedin_url='https://linkedin.com/in/a/')
        cache.mark_seen(github_username='b')
        cache.clear(source='github')
        assert cache.is_seen(linkedin_url='https://linkedin.com/in/a/')
        assert not cache.is_seen(github_username='b')

    def test_clear_all(self, cache_file):
        cache = DedupCache(cache_file)
        cache.mark_seen(linkedin_url='https://linkedin.com/in/a/')
        cache.mark_seen(github_username='b')
        cache.clear()
        assert not cache.is_seen(linkedin_url='https://linkedin.com/in/a/')
        assert not cache.is_seen(github_username='b')

    def test_stats(self, cache_file):
        cache = DedupCache(cache_file)
        cache.mark_seen(linkedin_url='https://linkedin.com/in/a/')
        cache.mark_seen(linkedin_url='https://linkedin.com/in/b/')
        cache.mark_seen(github_username='c')
        stats = cache.stats()
        assert stats['linkedin'] == 2
        assert stats['github'] == 1

    def test_empty_check(self, cache_file):
        cache = DedupCache(cache_file)
        assert not cache.is_seen()
        assert not cache.is_seen(linkedin_url=None)
        assert not cache.is_seen(github_username=None)

    def test_no_file(self, tmp_path):
        cache = DedupCache(str(tmp_path / 'nonexistent.json'))
        assert cache.stats() == {'linkedin': 0, 'github': 0}
