"""
Job Profile 管理 — 載入、儲存、自動生成職缺評分模板

優先順序:
1. 手動建立的 profile（config/job_profiles/{client}_{job_title}.yaml）
2. AI 自動生成的 profile（config/job_profiles/auto_generated/）
3. 從 SearchTask 的 skills 自動轉換為基本 profile
"""
import logging
import os
import re
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


class JobProfileManager:
    """Job Profile 載入與管理"""

    def __init__(self, profiles_dir: str = None):
        self.profiles_dir = profiles_dir or os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'config', 'job_profiles'
        )
        self.auto_dir = os.path.join(self.profiles_dir, 'auto_generated')
        os.makedirs(self.auto_dir, exist_ok=True)

    def _sanitize_filename(self, name: str) -> str:
        """將名稱轉為安全檔名"""
        # 移除特殊字元，只保留字母數字和底線
        safe = re.sub(r'[^\w\u4e00-\u9fff]', '_', name)
        safe = re.sub(r'_+', '_', safe).strip('_')
        return safe[:50]  # 限制長度

    def _profile_path(self, client_name: str, job_title: str) -> str:
        """手動 profile 路徑"""
        filename = f"{self._sanitize_filename(client_name)}_{self._sanitize_filename(job_title)}.yaml"
        return os.path.join(self.profiles_dir, filename)

    def _auto_profile_path(self, client_name: str, job_title: str) -> str:
        """自動生成 profile 路徑"""
        filename = f"{self._sanitize_filename(client_name)}_{self._sanitize_filename(job_title)}.yaml"
        return os.path.join(self.auto_dir, filename)

    def load_profile(self, client_name: str, job_title: str,
                     primary_skills: list = None,
                     secondary_skills: list = None,
                     location: str = '') -> Dict:
        """
        載入 Job Profile

        優先順序:
        1. 手動建立的 profile
        2. 自動生成的 profile
        3. 從 skills 自動轉換
        """
        # 1. 查找手動 profile
        manual_path = self._profile_path(client_name, job_title)
        if os.path.exists(manual_path):
            logger.info(f"載入手動 profile: {manual_path}")
            return self._load_yaml(manual_path)

        # 2. 查找自動生成 profile
        auto_path = self._auto_profile_path(client_name, job_title)
        if os.path.exists(auto_path):
            logger.info(f"載入自動 profile: {auto_path}")
            return self._load_yaml(auto_path)

        # 3. 從 skills 自動轉換
        if primary_skills or secondary_skills:
            logger.info(f"從技能列表生成 profile: primary={primary_skills}, "
                        f"secondary={secondary_skills}")
            profile = self.generate_from_skills(
                primary_skills or [], secondary_skills or [],
                job_title, location
            )
            # 儲存供下次使用
            self.save_profile(client_name, job_title, profile, auto=True)
            return profile

        # 4. 空 profile（不評分）
        logger.warning(f"找不到 profile: {client_name}/{job_title}，使用空 profile")
        return self._empty_profile(job_title)

    def save_profile(self, client_name: str, job_title: str,
                     profile: Dict, auto: bool = False):
        """儲存 Job Profile YAML"""
        path = self._auto_profile_path(client_name, job_title) if auto \
            else self._profile_path(client_name, job_title)

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                yaml.dump(profile, f, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            logger.info(f"儲存 profile: {path}")
        except Exception as e:
            logger.error(f"儲存 profile 失敗: {e}")

    def generate_from_skills(self, primary_skills: list,
                             secondary_skills: list,
                             job_title: str = '',
                             location: str = '') -> Dict:
        """
        從 SearchTask 的 primary_skills + secondary_skills 自動轉換

        primary_skills → must_have (weight=7-8) + core (weight=5-6)
        secondary_skills → nice_to_have (weight=3)
        location → constraints.location
        """
        must_have = []
        core = []
        nice_to_have = []

        # primary → must_have (前 2 個) + core (其餘)
        for i, skill in enumerate(primary_skills):
            skill_lower = skill.lower().strip()
            if i < 2:
                must_have.append({'skill': skill_lower, 'weight': 8 - i})
            else:
                core.append({'skill': skill_lower, 'weight': 6 - min(i - 2, 2)})

        # secondary → nice_to_have
        for i, skill in enumerate(secondary_skills):
            skill_lower = skill.lower().strip()
            nice_to_have.append({'skill': skill_lower, 'weight': max(2, 4 - i)})

        # constraints
        constraints = {}
        if location:
            loc_lower = location.lower().strip()
            constraints['location'] = [loc_lower]
            # 加入常見變體
            loc_variants = {
                'taiwan': ['taipei', '台灣', '台北'],
                'singapore': ['sg'],
                'hong kong': ['hk', '香港'],
                'japan': ['tokyo', '日本'],
            }
            if loc_lower in loc_variants:
                constraints['location'].extend(loc_variants[loc_lower])

        profile = {
            'job_profile': {
                'role_family': 'engineering',
                'role_name': job_title or 'Unknown',
                'must_have': must_have,
                'core': core,
                'nice_to_have': nice_to_have,
                'context': [],
                'constraints': constraints,
            }
        }

        return profile

    def list_profiles(self) -> List[Dict]:
        """列出所有已存的 profile"""
        profiles = []

        # 手動 profiles
        if os.path.exists(self.profiles_dir):
            for f in os.listdir(self.profiles_dir):
                if f.endswith('.yaml') and f != '_template.yaml':
                    path = os.path.join(self.profiles_dir, f)
                    if os.path.isfile(path):
                        profiles.append({
                            'filename': f,
                            'path': path,
                            'type': 'manual',
                            'name': f.replace('.yaml', ''),
                        })

        # 自動 profiles
        if os.path.exists(self.auto_dir):
            for f in os.listdir(self.auto_dir):
                if f.endswith('.yaml'):
                    profiles.append({
                        'filename': f,
                        'path': os.path.join(self.auto_dir, f),
                        'type': 'auto',
                        'name': f.replace('.yaml', ''),
                    })

        return profiles

    def delete_profile(self, client_name: str, job_title: str) -> bool:
        """刪除 profile"""
        for path in [self._profile_path(client_name, job_title),
                     self._auto_profile_path(client_name, job_title)]:
            if os.path.exists(path):
                os.remove(path)
                return True
        return False

    def _load_yaml(self, path: str) -> Dict:
        """載入 YAML 檔案"""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"載入 YAML 失敗 {path}: {e}")
            return self._empty_profile('')

    def _empty_profile(self, job_title: str) -> Dict:
        """空 profile（不評分）"""
        return {
            'job_profile': {
                'role_family': 'engineering',
                'role_name': job_title or 'Unknown',
                'must_have': [],
                'core': [],
                'nice_to_have': [],
                'context': [],
                'constraints': {},
            }
        }
