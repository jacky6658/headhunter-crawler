"""
已知好公司策略 — 根據技術棧推薦搜尋目標公司

透過 known_companies.yaml 維護「技術 → 使用該技術的台灣公司」對照表，
為搜尋器產生額外的公司維度 query，大幅提升搜尋精準度。
"""
import logging
import os

import yaml

logger = logging.getLogger(__name__)


class CompanyStrategy:
    """已知好公司策略"""

    def __init__(self, config: dict):
        self.enabled = config.get('crawler', {}).get('company_strategy', {}).get('enabled', True)
        self.companies = {}
        if self.enabled:
            self._load()

    def _load(self):
        """載入 known_companies.yaml"""
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'known_companies.yaml')
        try:
            with open(path, 'r', encoding='utf-8') as f:
                self.companies = yaml.safe_load(f) or {}
            total = sum(len(v) for v in self.companies.values())
            logger.info(f"已知好公司載入: {len(self.companies)} 技術, {total} 家公司")
        except FileNotFoundError:
            logger.info("known_companies.yaml 不存在，跳過公司策略")
        except Exception as e:
            logger.warning(f"known_companies.yaml 載入失敗: {e}")

    def get_target_companies(self, skills: list) -> list:
        """根據技能列表，回傳所有可能使用這些技術的公司（去重）"""
        if not self.enabled:
            return []

        companies = set()
        for skill in skills:
            key = skill.lower().strip()
            if key in self.companies:
                companies.update(self.companies[key])
        return list(companies)

    def get_company_queries(self, skills: list) -> list:
        """
        產生公司維度的搜尋 query
        例: skills=['Golang'] → ['Dcard Backend', 'LINE Golang', 'Appier Engineer']
        """
        if not self.enabled:
            return []

        companies = self.get_target_companies(skills)
        if not companies:
            return []

        queries = []
        primary = skills[0] if skills else ''
        for company in companies[:10]:  # 最多 10 家公司
            if primary:
                queries.append(f'{company} {primary}')
            else:
                queries.append(f'{company} Engineer')

        return queries
