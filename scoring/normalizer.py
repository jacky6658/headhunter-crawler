"""
技能正規化模組 — 載入 skills_synonyms.yaml 建立反向映射
"K8s" → "kubernetes", "Spring Boot" → "spring", "React.js" → "react"
"""
import logging
import re
from typing import List, Optional

import yaml

logger = logging.getLogger(__name__)


class SkillNormalizer:
    """
    技能名稱正規化 + 從自由文字中提取技能

    使用 skills_synonyms.yaml:
      kubernetes:
        - Kubernetes
        - K8s
        - Helm

    建立反向映射:
      "kubernetes" → "kubernetes"
      "k8s"        → "kubernetes"
      "helm"       → "kubernetes"
    """

    def __init__(self, synonyms_path: str = None):
        self._forward = {}   # canonical → [aliases]
        self._reverse = {}   # alias_lower → canonical
        self._all_names = [] # 所有可匹配的名稱（用於文字提取，按長度降序）

        if synonyms_path:
            self._load(synonyms_path)

    def _load(self, path: str):
        """載入 YAML 同義詞字典"""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}

            for canonical, aliases in data.items():
                canonical = str(canonical).lower().strip()
                self._forward[canonical] = []

                # 主名稱本身
                self._reverse[canonical] = canonical

                if not isinstance(aliases, list):
                    continue

                for alias in aliases:
                    alias_str = str(alias).strip()
                    alias_lower = alias_str.lower()
                    self._forward[canonical].append(alias_str)
                    self._reverse[alias_lower] = canonical

            # 建立所有可匹配的名稱（含主名稱 + 別名），按長度降序排列
            # 長名稱優先匹配，避免 "Spring Boot" 被 "Spring" 先匹配到
            all_names = set()
            for canonical, aliases in self._forward.items():
                all_names.add(canonical)
                for a in aliases:
                    all_names.add(a.lower())
            self._all_names = sorted(all_names, key=len, reverse=True)

            logger.info(f"技能正規化載入完成: {len(self._forward)} 主技能, "
                        f"{len(self._reverse)} 別名映射")
        except Exception as e:
            logger.error(f"載入技能同義詞失敗: {e}")

    def normalize(self, skill: str) -> str:
        """
        將技能名稱正規化為標準名稱

        例如:
          "K8s" → "kubernetes"
          "Spring Boot" → "spring"
          "React.js" → "react"
          "unknown_skill" → "unknown_skill" (不變)
        """
        if not skill:
            return ""
        key = skill.lower().strip()
        return self._reverse.get(key, key)

    def normalize_list(self, skills: list) -> List[str]:
        """批次正規化，去重"""
        if not skills:
            return []
        seen = set()
        result = []
        for s in skills:
            normalized = self.normalize(s)
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    def extract_skills_from_text(self, text: str) -> List[str]:
        """
        從自由文字（bio/headline/description）中提取技能關鍵字

        例如:
          "Senior Java Developer | Spring Boot | AWS" → ['java', 'spring', 'aws']
          "DevOps engineer, K8s lover, Terraform enthusiast" → ['devops', 'kubernetes', 'terraform']

        使用 word boundary 匹配，避免誤判:
          "JavaScript" 不會匹配 "Java"
          "React Native" 優先匹配完整詞

        返回: 正規化後的技能列表（去重）
        """
        if not text:
            return []

        text_lower = text.lower()
        found = set()
        found_positions = set()  # 避免重疊匹配

        for name in self._all_names:
            if len(name) < 2:
                continue

            # 用 word boundary 匹配
            # 特殊字元需要 escape（如 C++, C#, .NET）
            escaped = re.escape(name)
            pattern = r'(?<![a-zA-Z0-9])' + escaped + r'(?![a-zA-Z0-9])'

            for m in re.finditer(pattern, text_lower):
                start, end = m.start(), m.end()

                # 檢查是否已被更長的匹配覆蓋
                overlap = False
                for ps, pe in found_positions:
                    if start >= ps and end <= pe:
                        overlap = True
                        break
                if overlap:
                    continue

                canonical = self._reverse.get(name)
                if canonical:
                    found.add(canonical)
                    found_positions.add((start, end))

        return list(found)

    def get_all_canonical_skills(self) -> List[str]:
        """取得所有標準技能名稱"""
        return list(self._forward.keys())

    def get_aliases(self, canonical: str) -> List[str]:
        """取得某技能的所有別名"""
        return self._forward.get(canonical.lower(), [])

    def is_known_skill(self, skill: str) -> bool:
        """檢查是否為已知技能（含別名）"""
        return skill.lower().strip() in self._reverse
