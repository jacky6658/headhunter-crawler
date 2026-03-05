"""
LinkedIn 搜尋模組單元測試
"""
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from crawler.linkedin import LinkedInSearcher, _normalize_key


class TestNormalizeKey:
    def test_lowercase(self):
        assert _normalize_key('React') == 'react'

    def test_dot_removal(self):
        assert _normalize_key('Node.js') == 'nodejs'

    def test_space_removal(self):
        assert _normalize_key('Spring Boot') == 'springboot'

    def test_dash_removal(self):
        assert _normalize_key('Vue-Router') == 'vuerouter'

    def test_underscore_removal(self):
        assert _normalize_key('my_skill') == 'myskill'

    def test_combined(self):
        assert _normalize_key('ASP.NET Core') == 'aspnetcore'


class TestCleanUrl:
    def test_basic(self):
        url = 'https://www.linkedin.com/in/john-doe'
        assert LinkedInSearcher.clean_url(url) == 'https://www.linkedin.com/in/john-doe/'

    def test_with_query(self):
        url = 'https://www.linkedin.com/in/john-doe?trk=abc'
        assert LinkedInSearcher.clean_url(url) == 'https://www.linkedin.com/in/john-doe/'

    def test_localized(self):
        url = 'https://tw.linkedin.com/in/jane-wu'
        result = LinkedInSearcher.clean_url(url)
        assert result == 'https://www.linkedin.com/in/jane-wu/'

    def test_trailing_slash(self):
        url = 'https://www.linkedin.com/in/bob-chen/'
        assert LinkedInSearcher.clean_url(url) == 'https://www.linkedin.com/in/bob-chen/'

    def test_invalid_no_in(self):
        url = 'https://www.linkedin.com/company/google'
        assert LinkedInSearcher.clean_url(url) is None

    def test_invalid_empty(self):
        assert LinkedInSearcher.clean_url('') is None

    def test_encoded_query(self):
        url = 'https://www.linkedin.com/in/alice%3Ftrk=abc'
        result = LinkedInSearcher.clean_url(url)
        assert result == 'https://www.linkedin.com/in/alice/'


class TestParseTitleText:
    def test_name_dash_title(self):
        name, title = LinkedInSearcher._parse_title_text('John Doe - Senior Engineer')
        assert name == 'John Doe'
        assert title == 'Senior Engineer'

    def test_name_endash_title(self):
        name, title = LinkedInSearcher._parse_title_text('Jane Wu – Product Manager | LinkedIn')
        assert name == 'Jane Wu'
        assert title == 'Product Manager'

    def test_name_only(self):
        name, title = LinkedInSearcher._parse_title_text('Bob Chen')
        assert name == 'Bob Chen'
        assert title == ''

    def test_linkedin_suffix_stripped(self):
        name, title = LinkedInSearcher._parse_title_text('Alice Lin - Developer | LinkedIn')
        assert name == 'Alice Lin'
        assert title == 'Developer'


class TestExtractUrlsFromHtml:
    def test_direct_href(self):
        html = '<a href="https://www.linkedin.com/in/john-doe/">John Doe</a>'
        results = LinkedInSearcher.extract_urls_from_html(html)
        assert len(results) >= 1
        assert results[0]['linkedin_url'] == 'https://www.linkedin.com/in/john-doe/'

    def test_google_redirect(self):
        html = '<a href="/url?q=https://www.linkedin.com/in/jane-wu&sa=U">Jane Wu</a>'
        results = LinkedInSearcher.extract_urls_from_html(html)
        assert any(r['linkedin_url'] == 'https://www.linkedin.com/in/jane-wu/' for r in results)

    def test_plain_text(self):
        html = 'Check out linkedin.com/in/bob-chen for more info.'
        results = LinkedInSearcher.extract_urls_from_html(html)
        assert len(results) >= 1

    def test_dedup(self):
        html = '''
        <a href="https://www.linkedin.com/in/john-doe/">link1</a>
        <a href="https://www.linkedin.com/in/john-doe/">link2</a>
        text linkedin.com/in/john-doe more
        '''
        results = LinkedInSearcher.extract_urls_from_html(html)
        urls = [r['linkedin_url'] for r in results]
        assert len(set(urls)) == len(urls)

    def test_company_page_excluded(self):
        html = '<a href="https://www.linkedin.com/company/google/">Google</a>'
        results = LinkedInSearcher.extract_urls_from_html(html)
        assert len(results) == 0


class TestBuildQuery:
    """需要 config 和 anti_detect mock"""

    def _make_searcher(self):
        config = {'crawler': {'sample_per_page': 5, 'linkedin': {}}}
        ad = type('MockAD', (), {})()
        searcher = LinkedInSearcher(config, ad)
        # 直接設定空同義詞
        searcher.skill_synonyms = {}
        return searcher

    def test_basic_query(self):
        s = self._make_searcher()
        q = s.build_query(['Python', 'Django'], 'Taiwan')
        assert 'site:linkedin.com/in/' in q
        assert '"Python"' in q
        assert '"Django"' in q
        assert '"Taiwan"' in q

    def test_with_secondary(self):
        s = self._make_searcher()
        q = s.build_query(['Java', 'Spring', 'MySQL', 'Redis'], 'Taiwan')
        assert '"Java"' in q
        assert '"Spring"' in q
        # secondary in OR group
        assert 'OR' in q

    def test_synonym_expansion(self):
        s = self._make_searcher()
        s.skill_synonyms = {'react': ['React.js', 'ReactJS']}
        q = s.build_query(['React'], 'Taiwan')
        assert 'React.js' in q or 'ReactJS' in q
