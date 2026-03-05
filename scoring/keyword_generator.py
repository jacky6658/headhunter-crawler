"""
關鍵字自動生成器 — 從職缺名稱自動產生搜尋技能
純規則式，不接外部 AI API
解決 Step1ne 系統 14/26 職缺無搜尋關鍵字的問題
"""
import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class KeywordGenerator:
    """
    從職缺名稱 + 職缺描述自動產生搜尋關鍵字和 Job Profile

    使用方式:
      gen = KeywordGenerator()
      result = gen.generate("資深 Java 後端工程師")
      # result = {
      #   'primary_skills': ['java', 'spring'],
      #   'secondary_skills': ['mysql', 'redis', 'docker'],
      #   'job_profile': { ... }
      # }
    """

    # ── 職缺名稱 → 技能映射表 ──────────────────────────────────
    TITLE_SKILL_MAP = {
        # 後端
        'backend': {
            'primary': ['python', 'java', 'nodejs'],
            'secondary': ['docker', 'postgresql', 'redis', 'rest'],
            'context': ['microservices'],
        },
        'java': {
            'primary': ['java', 'spring'],
            'secondary': ['mysql', 'redis', 'docker', 'microservices'],
            'context': [],
        },
        'python': {
            'primary': ['python'],
            'secondary': ['django', 'fastapi', 'flask', 'postgresql'],
            'context': [],
        },
        'golang': {
            'primary': ['golang'],
            'secondary': ['docker', 'kubernetes', 'postgresql', 'redis'],
            'context': ['microservices'],
        },
        'go': {
            'primary': ['golang'],
            'secondary': ['docker', 'kubernetes', 'postgresql'],
            'context': ['microservices'],
        },
        'nodejs': {
            'primary': ['nodejs', 'typescript'],
            'secondary': ['mongodb', 'redis', 'docker', 'rest'],
            'context': [],
        },
        'ruby': {
            'primary': ['ruby'],
            'secondary': ['postgresql', 'redis', 'docker'],
            'context': [],
        },
        'php': {
            'primary': ['php'],
            'secondary': ['mysql', 'redis', 'docker'],
            'context': [],
        },
        '.net': {
            'primary': ['csharp'],
            'secondary': ['docker', 'mysql', 'redis'],
            'context': [],
        },
        'c#': {
            'primary': ['csharp'],
            'secondary': ['docker', 'mysql'],
            'context': [],
        },

        # 前端
        'frontend': {
            'primary': ['react', 'typescript'],
            'secondary': ['vue', 'nextjs', 'javascript'],
            'context': [],
        },
        'react': {
            'primary': ['react', 'typescript'],
            'secondary': ['nextjs', 'javascript', 'graphql'],
            'context': [],
        },
        'vue': {
            'primary': ['vue', 'javascript'],
            'secondary': ['nuxt', 'typescript'],
            'context': [],
        },
        'angular': {
            'primary': ['angular', 'typescript'],
            'secondary': ['javascript', 'rest'],
            'context': [],
        },

        # 全端
        'fullstack': {
            'primary': ['javascript', 'nodejs'],
            'secondary': ['react', 'python', 'docker', 'postgresql'],
            'context': [],
        },
        '全端': {
            'primary': ['javascript', 'nodejs'],
            'secondary': ['react', 'python', 'docker'],
            'context': [],
        },

        # DevOps / SRE / 維運
        'devops': {
            'primary': ['docker', 'kubernetes', 'cicd'],
            'secondary': ['terraform', 'aws', 'linux', 'ansible'],
            'context': ['high-availability'],
        },
        'sre': {
            'primary': ['linux', 'docker', 'kubernetes'],
            'secondary': ['grafana', 'cicd', 'python', 'aws'],
            'context': ['high-availability', 'on-call'],
        },
        '系統維運': {
            'primary': ['linux', 'docker'],
            'secondary': ['cicd', 'grafana', 'aws', 'kubernetes'],
            'context': [],
        },
        '維運': {
            'primary': ['linux', 'docker'],
            'secondary': ['cicd', 'grafana', 'aws'],
            'context': [],
        },
        'infrastructure': {
            'primary': ['terraform', 'aws', 'docker'],
            'secondary': ['kubernetes', 'cicd', 'linux'],
            'context': ['high-availability'],
        },
        'cloud': {
            'primary': ['aws', 'docker', 'kubernetes'],
            'secondary': ['terraform', 'cicd', 'linux'],
            'context': [],
        },

        # Data
        'data engineer': {
            'primary': ['python', 'postgresql'],
            'secondary': ['kafka', 'elasticsearch', 'docker'],
            'context': [],
        },
        'data scientist': {
            'primary': ['python', 'machinelearning'],
            'secondary': ['pytorch', 'tensorflow'],
            'context': [],
        },
        'data analyst': {
            'primary': ['python', 'postgresql'],
            'secondary': ['elasticsearch'],
            'context': [],
        },
        'ml': {
            'primary': ['python', 'machinelearning'],
            'secondary': ['pytorch', 'tensorflow', 'docker'],
            'context': [],
        },
        'ai': {
            'primary': ['python', 'machinelearning'],
            'secondary': ['pytorch', 'tensorflow', 'llm'],
            'context': [],
        },

        # Mobile
        'ios': {
            'primary': ['swift'],
            'secondary': ['react'],
            'context': [],
        },
        'android': {
            'primary': ['kotlin', 'android'],
            'secondary': ['java'],
            'context': [],
        },
        'mobile': {
            'primary': ['react', 'swift', 'kotlin'],
            'secondary': ['android', 'javascript'],
            'context': [],
        },
        'flutter': {
            'primary': ['flutter'],
            'secondary': ['android', 'swift'],
            'context': [],
        },

        # QA
        'qa': {
            'primary': ['python'],
            'secondary': ['javascript', 'docker'],
            'context': [],
        },
        'test': {
            'primary': ['python'],
            'secondary': ['javascript'],
            'context': [],
        },
        'sdet': {
            'primary': ['python', 'java'],
            'secondary': ['docker', 'cicd'],
            'context': [],
        },

        # Security
        '資安': {
            'primary': ['security', 'linux'],
            'secondary': ['python', 'docker'],
            'context': [],
        },
        'security': {
            'primary': ['security', 'linux'],
            'secondary': ['python', 'docker', 'aws'],
            'context': [],
        },

        # Blockchain
        'blockchain': {
            'primary': ['solidity', 'web3'],
            'secondary': ['javascript', 'python'],
            'context': [],
        },
        'web3': {
            'primary': ['solidity', 'web3'],
            'secondary': ['react', 'typescript'],
            'context': [],
        },
    }

    # 年資關鍵字
    SENIORITY_KEYWORDS = {
        'senior': 5, '資深': 5, 'sr': 5, 'lead': 7, '主管': 7,
        'principal': 10, 'staff': 8, 'architect': 8, '架構師': 8,
        'junior': 1, '初級': 1, 'jr': 1, 'intern': 0, '實習': 0,
        'mid': 3, '中級': 3,
    }

    def generate(self, job_title: str, existing_skills: list = None,
                 job_description: str = '') -> Dict:
        """
        從職缺名稱生成關鍵字

        輸入:
          job_title: "資深 Java 後端工程師"
          existing_skills: 已有的技能（如果有就不覆蓋）
          job_description: 職缺描述（可選，用於補充）

        輸出:
        {
          'primary_skills': ['java', 'spring'],
          'secondary_skills': ['mysql', 'redis', 'docker', 'microservices'],
          'seniority_years': 5,
          'job_profile': { must_have: [...], core: [...], nice_to_have: [...] }
        }
        """
        # 如果已有技能就不覆蓋
        if existing_skills and len(existing_skills) >= 2:
            logger.info(f"職缺 '{job_title}' 已有 {len(existing_skills)} 個技能，跳過生成")
            return {
                'primary_skills': existing_skills[:3],
                'secondary_skills': existing_skills[3:],
                'seniority_years': self._detect_seniority(job_title),
                'job_profile': self._build_profile(
                    existing_skills[:3], existing_skills[3:], job_title
                ),
            }

        # 解析職缺名稱
        tokens = self._parse_title(job_title)
        logger.info(f"解析職缺 '{job_title}' → tokens: {tokens}")

        # 合併所有匹配的技能
        primary = []
        secondary = []
        context = []

        for token in tokens:
            if token in self.TITLE_SKILL_MAP:
                mapping = self.TITLE_SKILL_MAP[token]
                for s in mapping.get('primary', []):
                    if s not in primary:
                        primary.append(s)
                for s in mapping.get('secondary', []):
                    if s not in secondary and s not in primary:
                        secondary.append(s)
                for s in mapping.get('context', []):
                    if s not in context:
                        context.append(s)

        # 如果沒有匹配到任何技能，嘗試直接用 token 作為技能
        if not primary and not secondary:
            for token in tokens:
                if len(token) >= 2:
                    primary.append(token)
            logger.warning(f"職缺 '{job_title}' 無法映射技能，直接使用 tokens: {primary}")

        # 限制數量
        primary = primary[:4]
        secondary = secondary[:6]

        seniority = self._detect_seniority(job_title)

        result = {
            'primary_skills': primary,
            'secondary_skills': secondary,
            'seniority_years': seniority,
            'job_profile': self._build_profile(primary, secondary, job_title, context),
        }

        logger.info(f"生成關鍵字: primary={primary}, secondary={secondary}, "
                     f"seniority={seniority}yr")
        return result

    def _parse_title(self, job_title: str) -> List[str]:
        """
        解析職缺名稱，提取關鍵詞

        "資深 Java 後端工程師" → ['java', 'backend']
        "DevOps / SRE 工程師" → ['devops', 'sre']
        "Senior Python Developer" → ['python']
        "全端工程師 (React + Node.js)" → ['fullstack', 'react', 'nodejs']
        """
        title = job_title.lower().strip()

        # 移除常見後綴
        for suffix in ['工程師', 'engineer', 'developer', 'programmer',
                       '開發者', '工程人員', 'specialist', 'expert']:
            title = title.replace(suffix, '')

        # 移除常見前綴/修飾詞
        for prefix in ['senior', 'junior', 'lead', 'principal', 'staff',
                        '資深', '初級', '中級', '主管', 'sr.', 'jr.', 'sr', 'jr']:
            title = re.sub(r'\b' + re.escape(prefix) + r'\b', '', title)

        # 分割
        parts = re.split(r'[/\\|,\s+\-—·•()（）]+', title)
        parts = [p.strip() for p in parts if p.strip() and len(p.strip()) >= 2]

        # 中文特殊處理
        chinese_map = {
            '後端': 'backend', '前端': 'frontend', '全端': 'fullstack',
            '行動': 'mobile', '手機': 'mobile', '雲端': 'cloud',
            '資料': 'data engineer', '數據': 'data engineer',
            '機器學習': 'ml', '人工智慧': 'ai', '深度學習': 'ml',
            '區塊鏈': 'blockchain', '系統維運': '系統維運',
            '維運': '維運', '資安': '資安', '測試': 'qa',
        }
        result = []
        for p in parts:
            mapped = chinese_map.get(p, p)
            if mapped not in result:
                result.append(mapped)

        return result

    def _detect_seniority(self, job_title: str) -> int:
        """從職缺名稱推斷最低年資"""
        title_lower = job_title.lower()
        max_years = 0
        for keyword, years in self.SENIORITY_KEYWORDS.items():
            if keyword in title_lower:
                max_years = max(max_years, years)
        return max_years

    def _build_profile(self, primary: list, secondary: list,
                       job_title: str, context: list = None) -> Dict:
        """從技能列表生成 Job Profile"""
        must_have = []
        core = []
        nice_to_have = []

        # primary → must_have (前 2 個, weight 7-8) + core (其餘, weight 5-6)
        for i, skill in enumerate(primary):
            if i < 2:
                must_have.append({'skill': skill, 'weight': 8 - i})
            else:
                core.append({'skill': skill, 'weight': 6})

        # secondary → core (前 2 個, weight 5) + nice_to_have (其餘, weight 3)
        for i, skill in enumerate(secondary):
            if i < 2:
                core.append({'skill': skill, 'weight': 5})
            else:
                nice_to_have.append({'skill': skill, 'weight': 3})

        # context
        context_list = []
        if context:
            for tag in context:
                context_list.append({'tag': tag, 'weight': 2})

        seniority = self._detect_seniority(job_title)

        profile = {
            'job_profile': {
                'role_family': 'engineering',
                'role_name': job_title,
                'must_have': must_have,
                'core': core,
                'nice_to_have': nice_to_have,
                'context': context_list,
                'constraints': {
                    'seniority_min_years': seniority,
                },
            }
        }

        return profile
