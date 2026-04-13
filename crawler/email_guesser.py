"""
Email 猜測模組 — 從候選人姓名 + 公司推算可能的 email

技術: 企業 email 格式通常是固定模式 (firstname.lastname@company.com)
透過已知的公司 domain + 常見格式組合，生成可能的 email 清單。
可選搭配 SMTP 驗證確認 email 是否有效。
"""
import logging
import re
import smtplib
import socket
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── 台灣常見公司 email domain 對照表 ──
# 格式: company_keywords → (domain, email_pattern)
# email_pattern: f=firstname, l=lastname, fi=first_initial, li=last_initial
COMPANY_DOMAINS = {
    # 科技大廠
    '精誠': ('systex.com', ['f.l', 'fl', 'f_l']),
    'systex': ('systex.com', ['f.l', 'fl', 'f_l']),
    '三竹': ('mitake.com.tw', ['f.l', 'fl']),
    'mitake': ('mitake.com.tw', ['f.l', 'fl']),
    'appier': ('appier.com', ['f.l', 'fl']),
    'dcard': ('dcard.tw', ['f.l', 'fl']),
    'line': ('linecorp.com', ['f.l', 'fl']),
    'gogolook': ('gogolook.com', ['f.l', 'fl']),
    'trend': ('trendmicro.com', ['f.l', 'fl']),
    '趨勢': ('trendmicro.com', ['f.l', 'fl']),
    'kkbox': ('kkbox.com', ['f.l', 'fl']),
    'kkcompany': ('kkcompany.com', ['f.l', 'fl']),
    'pinkoi': ('pinkoi.com', ['f.l', 'fl']),
    'shopee': ('shopee.com', ['f.l', 'fl']),
    '蝦皮': ('shopee.com', ['f.l', 'fl']),
    '91app': ('91app.com', ['f.l', 'fl']),
    'synology': ('synology.com', ['f.l', 'fl']),
    '群暉': ('synology.com', ['f.l', 'fl']),
    'ikala': ('ikala.tv', ['f.l', 'fl']),
    'coolbitx': ('coolbitx.com', ['f.l', 'fl']),
    'picollage': ('cardinalblue.com', ['f.l', 'fl']),
    '17live': ('17.live', ['f.l', 'fl']),
    'garena': ('garena.com', ['f.l', 'fl']),
    'htc': ('htc.com', ['f.l', 'fl']),
    'asus': ('asus.com', ['f.l', 'fl', 'fi.l']),
    '華碩': ('asus.com', ['f.l', 'fl', 'fi.l']),
    'mediatek': ('mediatek.com', ['f.l', 'fl']),
    '聯發科': ('mediatek.com', ['f.l', 'fl']),
    'tsmc': ('tsmc.com', ['f.l', 'fl']),
    '台積電': ('tsmc.com', ['f.l', 'fl']),
    'foxconn': ('foxconn.com', ['f.l', 'fl']),
    '鴻海': ('foxconn.com', ['f.l', 'fl']),
    'wistron': ('wistron.com', ['f.l', 'fl']),
    '緯創': ('wistron.com', ['f.l', 'fl']),
    'acer': ('acer.com', ['f.l', 'fl']),
    '宏碁': ('acer.com', ['f.l', 'fl']),
    'delta': ('deltaww.com', ['f.l', 'fl']),
    '台達': ('deltaww.com', ['f.l', 'fl']),
    'cathay': ('cathayholdings.com.tw', ['f.l', 'fl']),
    '國泰': ('cathayholdings.com.tw', ['f.l', 'fl']),
    'fubon': ('fubon.com', ['f.l', 'fl']),
    '富邦': ('fubon.com', ['f.l', 'fl']),
    '中信': ('ctbcbank.com', ['f.l', 'fl']),
    'yahoo': ('yahooinc.com', ['f.l', 'fl']),
    'maicoin': ('maicoin.com', ['f.l', 'fl']),
    'cycraft': ('cycraft.com', ['f.l', 'fl']),
    'teamt5': ('teamt5.org', ['f.l', 'fl']),
    'hahow': ('hahow.in', ['f.l', 'fl']),
    'yourator': ('yourator.co', ['f.l', 'fl']),
    'cakeresume': ('cakeresume.com', ['f.l', 'fl']),
    # 日商
    'rakuten': ('rakuten.com', ['f.l', 'fl']),
    '樂天': ('rakuten.com', ['f.l', 'fl']),
}


def _extract_english_name(full_name: str) -> tuple:
    """
    從候選人名字提取英文 first/last name
    例: "Yu Chun, Kao" → ("yu-chun", "kao")
    例: "陳柏源 (Bob Chen)" → ("bob", "chen")
    例: "HungWei Chiu" → ("hungwei", "chiu")
    """
    if not full_name:
        return ('', '')

    # 嘗試提取括號裡的英文名
    paren = re.search(r'[（(]([A-Za-z\s]+)[）)]', full_name)
    if paren:
        full_name = paren.group(1).strip()

    # 移除中文字元
    eng_only = re.sub(r'[\u4e00-\u9fff]+', ' ', full_name).strip()
    if not eng_only:
        return ('', '')

    # 清理多餘符號
    eng_only = re.sub(r'[,;|/]+', ' ', eng_only).strip()
    parts = [p.strip() for p in eng_only.split() if p.strip() and len(p.strip()) > 1]

    if not parts:
        return ('', '')
    if len(parts) == 1:
        return (parts[0].lower(), '')

    # 常見格式: "FirstName LastName" 或 "LastName, FirstName"
    first = parts[0].lower()
    last = parts[-1].lower()
    return (first, last)


def guess_emails(name: str, company: str) -> List[dict]:
    """
    從候選人姓名 + 公司猜測可能的 email

    Returns:
        [{'email': 'bob.chen@systex.com', 'confidence': 0.6, 'pattern': 'f.l'}]
    """
    if not name or not company:
        return []

    first, last = _extract_english_name(name)
    if not first or not last:
        return []

    # 找到公司 domain
    company_lower = company.lower().strip()
    domain = None
    patterns = ['f.l', 'fl']

    for keyword, (d, p) in COMPANY_DOMAINS.items():
        if keyword in company_lower:
            domain = d
            patterns = p
            break

    if not domain:
        # 嘗試從公司名推算 domain（中文公司可能無法推算）
        # 英文公司名: "Appier Inc." → appier.com
        eng_company = re.sub(r'[^\w]', '', re.sub(r'[\u4e00-\u9fff]+', '', company)).strip().lower()
        if eng_company and len(eng_company) >= 3:
            domain = f"{eng_company}.com"
            patterns = ['f.l', 'fl']

    if not domain:
        return []

    results = []
    fi = first[0]  # first initial
    li = last[0] if last else ''

    for pattern in patterns:
        if pattern == 'f.l':
            email = f"{first}.{last}@{domain}"
        elif pattern == 'fl':
            email = f"{first}{last}@{domain}"
        elif pattern == 'f_l':
            email = f"{first}_{last}@{domain}"
        elif pattern == 'fi.l':
            email = f"{fi}.{last}@{domain}"
        elif pattern == 'l.f':
            email = f"{last}.{first}@{domain}"
        else:
            continue

        # 清理
        email = re.sub(r'[^a-z0-9@._-]', '', email.lower())
        if '@' in email:
            results.append({
                'email': email,
                'confidence': 0.5 if pattern in ('f.l', 'fl') else 0.3,
                'pattern': pattern,
                'domain': domain,
            })

    return results


def verify_email_smtp(email: str, timeout: int = 5) -> Optional[bool]:
    """
    用 SMTP 驗證 email 是否存在（不發送郵件）

    Returns:
        True = 確認存在
        False = 確認不存在
        None = 無法判斷（server 拒絕驗證）
    """
    domain = email.split('@')[1] if '@' in email else ''
    if not domain:
        return False

    try:
        # 查 MX record
        import dns.resolver
        mx_records = dns.resolver.resolve(domain, 'MX')
        mx_host = str(mx_records[0].exchange).rstrip('.')
    except Exception:
        # 沒有 dnspython，用 domain 直接連
        mx_host = f"mail.{domain}"

    try:
        with smtplib.SMTP(mx_host, 25, timeout=timeout) as smtp:
            smtp.helo('verify.local')
            smtp.mail('verify@verify.local')
            code, _ = smtp.rcpt(email)
            return code == 250
    except (smtplib.SMTPServerDisconnected, socket.timeout, ConnectionRefusedError, OSError):
        return None


def enrich_candidate_emails(candidate: dict) -> List[dict]:
    """
    為候選人猜測可能的 email 並加入 contact_methods

    Args:
        candidate: 候選人 dict

    Returns:
        猜測到的 email 列表 [{email, confidence, pattern}]
    """
    name = candidate.get('name', '')
    company = candidate.get('company', '')

    # 也從 bio/title 提取公司名
    if not company:
        bio = candidate.get('bio', '') or candidate.get('title', '') or ''
        # 嘗試匹配已知公司關鍵字
        for keyword in COMPANY_DOMAINS:
            if keyword in bio.lower():
                company = keyword
                break

    guessed = guess_emails(name, company)

    if guessed:
        logger.info(f"Email 猜測: {name} @ {company} → {[g['email'] for g in guessed]}")

    return guessed
