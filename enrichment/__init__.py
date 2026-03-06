"""
Enrichment 模組 — 候選人 LinkedIn 深度分析 + 綜合職缺匹配

組件:
- LinkedInApiClient: LinkedIn Voyager API 直連（免費，最完整）
- PerplexityClient: Perplexity Sonar API 封裝（付費，AI 分析）
- JinaReader: Jina Reader 免費 URL 讀取（備援）
- ProfileEnricher: 統一充實入口（LinkedIn → Perplexity → Jina）
- ContextualScorer: 5 維度綜合評分（JD + 企業畫像 + 人才畫像）
"""

from .linkedin_client import LinkedInApiClient
from .perplexity_client import PerplexityClient
from .jina_reader import JinaReader
from .profile_enricher import ProfileEnricher
from .contextual_scorer import ContextualScorer

__all__ = [
    'LinkedInApiClient',
    'PerplexityClient',
    'JinaReader',
    'ProfileEnricher',
    'ContextualScorer',
]
