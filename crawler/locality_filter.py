"""
本地人判斷模組 — 判斷候選人是否為目標地區的本地人

用於過濾非台灣本地人（外國人），提升搜尋精準度。
多信號加權判斷，不是非黑即白。
"""
import re
import logging

logger = logging.getLogger(__name__)

# 中文字元範圍
_CJK_PATTERN = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')

# 常見台灣城市（中英文）
_TW_CITIES = {
    'taipei', 'new taipei', 'taichung', 'kaohsiung', 'tainan',
    'hsinchu', 'taoyuan', 'keelung', 'chiayi', 'changhua',
    'pingtung', 'yilan', 'nantou', 'hualien', 'miaoli',
    '台北', '新北', '台中', '高雄', '台南', '新竹', '桃園',
    '基隆', '嘉義', '彰化', '屏東', '宜蘭', '南投', '花蓮', '苗栗',
    '台灣', 'taiwan', 'r.o.c',
}

# 常見中文姓氏（前 100 大）
_COMMON_CHINESE_SURNAMES = {
    '陳', '林', '黃', '張', '李', '王', '吳', '劉', '蔡', '楊',
    '許', '鄭', '謝', '洪', '郭', '邱', '曾', '廖', '賴', '徐',
    '周', '葉', '蘇', '莊', '呂', '江', '何', '蕭', '羅', '高',
    '潘', '簡', '朱', '鍾', '游', '彭', '詹', '胡', '施', '沈',
    '余', '盧', '梁', '趙', '顏', '柯', '翁', '魏', '孫', '戴',
    '范', '方', '宋', '鄧', '杜', '傅', '侯', '曹', '薛', '丁',
    '卓', '馬', '阮', '董', '温', '唐', '藍', '石', '蔣', '田',
    '康', '鄒', '白', '涂', '尤', '巫', '韓', '龔', '嚴', '袁',
}

# 台灣常見姓氏拼音（Wade-Giles + 通用拼音 + 漢語拼音）
_TW_SURNAME_PINYIN = {
    'chen', 'lin', 'huang', 'chang', 'zhang', 'li', 'lee', 'wang', 'wu',
    'liu', 'lau', 'tsai', 'cai', 'yang', 'hsu', 'xu', 'cheng', 'zheng',
    'hsieh', 'xie', 'hung', 'hong', 'kuo', 'guo', 'chiu', 'qiu', 'tseng',
    'zeng', 'liao', 'lai', 'chou', 'zhou', 'yeh', 'ye', 'su', 'chuang',
    'zhuang', 'lu', 'lv', 'chiang', 'jiang', 'ho', 'he', 'hsiao', 'xiao',
    'lo', 'luo', 'kao', 'gao', 'pan', 'chien', 'jian', 'chu', 'zhu',
    'chung', 'zhong', 'yu', 'peng', 'chan', 'zhan', 'hu', 'shih', 'shi',
    'shen', 'fan', 'fang', 'sung', 'song', 'teng', 'deng', 'tu', 'du',
    'fu', 'hou', 'tsao', 'cao', 'hsueh', 'xue', 'ting', 'ding',
    'cho', 'zhuo', 'ma', 'yuan', 'ruan', 'tung', 'dong', 'wen', 'tang',
    'lan', 'shih', 'chiang', 'tien', 'tian', 'kang', 'tsou', 'zou',
    'pai', 'bai', 'wei', 'han', 'kung', 'gong', 'yen', 'yan',
}

# 常見非台灣本地人的名字模式（印度、中東、非洲等）
_FOREIGN_NAME_PATTERNS = [
    r'\b(Kumar|Singh|Sharma|Patel|Gupta|Reddy|Rao|Das|Mishra|Joshi)\b',
    r'\b(Arun|Sachin|Mahesh|Rajesh|Suresh|Ramesh|Ganesh|Mukesh)\b',
    r'\b(Mohammad|Mohammed|Ahmed|Ali|Hassan|Hussein|Abdul|Md\.?)\b',
    r'\b(Adetunji|Oluwaseun|Chukwu|Okonkwo|Babatunde)\b',
    r'\b(Sourabh|Harshwardhan|Chaitanya|Thanish|Muthu|Umair)\b',
]


def has_chinese(text: str) -> bool:
    """文字中是否包含中文字元"""
    return bool(_CJK_PATTERN.search(text or ''))


def locality_score(candidate: dict, target_location: str = 'taiwan') -> float:
    """
    計算候選人的本地人信心分數 (0.0 ~ 1.0)

    > 0.6 = 很可能是本地人
    > 0.3 = 可能是本地人
    < 0.3 = 很可能不是本地人

    信號:
    +0.35  中文名字（漢字）
    +0.20  bio/title 含中文
    +0.15  location 含台灣城市
    +0.10  公司是台灣公司 or 中文公司名
    +0.10  有 CakeResume profile
    +0.10  GitHub bio 含中文
    -0.30  名字匹配外國人模式（印度/中東/非洲常見名）
    -0.20  location 明確在其他國家
    """
    score = 0.0
    name = candidate.get('name', '') or ''
    bio = candidate.get('bio', '') or ''
    title = candidate.get('title', '') or ''
    location = (candidate.get('location', '') or '').lower()
    company = candidate.get('company', '') or ''
    cake_url = candidate.get('cakeresume_url', '') or ''

    all_text = f"{name} {bio} {title} {company} {location}"

    # ── 正面信號 ──

    # 中文名字 (+0.35)
    if has_chinese(name):
        score += 0.35
    # 名字的第一個字是常見中文姓氏 (+0.35)
    elif name and name[0] in _COMMON_CHINESE_SURNAMES:
        score += 0.35
    else:
        # 拼音姓氏偵測 (+0.25) — "HungWei Chiu" → surname "Chiu" in pinyin set
        name_parts = name.replace('-', ' ').split()
        if name_parts:
            # 姓可能在最前或最後
            first_lower = name_parts[0].lower()
            last_lower = name_parts[-1].lower()
            if first_lower in _TW_SURNAME_PINYIN or last_lower in _TW_SURNAME_PINYIN:
                score += 0.25

    # Bio/Title 含中文 (+0.20)
    if has_chinese(bio) or has_chinese(title):
        score += 0.20

    # Location 含台灣城市 (+0.15)
    if any(city in location for city in _TW_CITIES):
        score += 0.15
    elif 'taiwan' in all_text.lower():
        score += 0.10

    # 公司是中文名 or 含台灣關鍵字 (+0.10)
    if has_chinese(company):
        score += 0.10
    elif any(kw in company.lower() for kw in ['台', 'taiwan', 'taipei']):
        score += 0.10

    # 有 CakeResume profile (+0.10) — CakeResume 主要是台灣平台
    if cake_url:
        score += 0.10

    # Bio 含中文 (+0.10)
    if has_chinese(candidate.get('bio', '')):
        score += 0.10

    # ── 負面信號 ──

    # 名字匹配外國人模式 (-0.30)
    for pattern in _FOREIGN_NAME_PATTERNS:
        if re.search(pattern, name, re.IGNORECASE):
            score -= 0.30
            break

    # Location 明確在其他國家 (-0.20)
    foreign_locs = ['india', 'bangalore', 'mumbai', 'delhi', 'hyderabad',
                    'nigeria', 'lagos', 'pakistan', 'karachi', 'lahore',
                    'bangladesh', 'dhaka', 'dallas', 'california', 'london',
                    'berlin', 'new york', 'san francisco', 'seattle']
    if any(fl in location for fl in foreign_locs):
        score -= 0.20

    return max(0.0, min(1.0, score))


def is_likely_local(candidate: dict, threshold: float = 0.25) -> bool:
    """候選人是否可能是台灣本地人"""
    return locality_score(candidate) >= threshold


def filter_locals(candidates: list, threshold: float = 0.25) -> list:
    """過濾出可能是本地人的候選人"""
    kept = []
    filtered = 0
    for c in candidates:
        ls = locality_score(c)
        if ls >= threshold:
            c['_locality_score'] = round(ls, 2)
            kept.append(c)
        else:
            filtered += 1

    if filtered:
        logger.info(f"本地人過濾: {len(candidates)} → {len(kept)} (過濾 {filtered} 位非本地候選人)")

    return kept
