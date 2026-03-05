"""
GitHub 搜尋模組單元測試
"""
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from crawler.github import GitHubSearcher


class TestGitHubSearcher:
    def _make_searcher(self, tokens=None):
        config = {
            'api_keys': {'github_tokens': tokens or []},
            'crawler': {'github': {'languages': ['python', 'javascript', 'go']}},
        }
        ad = type('MockAD', (), {
            'github_delay': lambda self: None,
            'exponential_backoff': lambda self, n: None,
        })()
        return GitHubSearcher(config, ad)

    def test_no_tokens(self):
        s = self._make_searcher()
        assert s.current_token is None

    def test_single_token(self):
        s = self._make_searcher(['ghp_abc123'])
        assert s.current_token == 'ghp_abc123'

    def test_multi_token_rotation(self):
        s = self._make_searcher(['tok1', 'tok2', 'tok3'])
        assert s.current_token == 'tok1'
        s.rotate_token()
        assert s.current_token == 'tok2'
        s.rotate_token()
        assert s.current_token == 'tok3'
        s.rotate_token()
        assert s.current_token == 'tok1'  # wraps around

    def test_build_queries(self):
        s = self._make_searcher()
        queries = s.build_queries(['Python', 'Django'], 'Taiwan')
        assert len(queries) > 0
        # At least one query should have location
        combined = ' '.join(queries)
        assert 'Taiwan' in combined

    def test_build_queries_language_filter(self):
        s = self._make_searcher()
        queries = s.build_queries(['Python', 'React'], 'Taiwan')
        # Python is a valid GitHub language, should have language: filter
        has_lang = any('language:' in q.lower() for q in queries)
        assert has_lang

    def test_get_headers_with_token(self):
        s = self._make_searcher(['ghp_test'])
        headers = s.get_headers()
        assert headers['Authorization'] == 'token ghp_test'

    def test_get_headers_no_token(self):
        s = self._make_searcher()
        headers = s.get_headers()
        assert 'Authorization' not in headers
