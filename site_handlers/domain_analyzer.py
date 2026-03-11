"""域名分析模块 - 从参考文献提取域名并分类统计"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reference_tool import ReferenceItem


@dataclass
class DomainInfo:
    """单个域名的统计信息"""
    domain: str                          # "link.springer.com"
    display_name: str                    # "Springer Link"
    ref_numbers: list[int] = field(default_factory=list)  # 参考文献编号列表
    count: int = 0                       # 引用数量
    requires_auth: bool = False          # 已知需要认证(付费墙)
    has_cookies: bool = False            # 是否已配置cookies
    cookies_path: str | None = None      # cookies文件路径


# 已知付费墙域名注册表
# display_name: 显示名称
# requires_subscription: 是否需要订阅
# open_access_indicators: HTML中的开放获取标识
PAYWALL_DOMAINS: dict[str, dict] = {
    # 学术出版商
    "link.springer.com": {
        "display_name": "Springer Link",
        "requires_subscription": True,
        "open_access_indicators": ["OpenAccess", "open-access", "free-access"],
    },
    "ieeexplore.ieee.org": {
        "display_name": "IEEE Xplore",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "sciencedirect.com": {
        "display_name": "ScienceDirect (Elsevier)",
        "requires_subscription": True,
        "open_access_indicators": ["Open access"],
    },
    "www.sciencedirect.com": {
        "display_name": "ScienceDirect (Elsevier)",
        "requires_subscription": True,
        "open_access_indicators": ["Open access"],
    },
    "dl.acm.org": {
        "display_name": "ACM Digital Library",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "jstor.org": {
        "display_name": "JSTOR",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "www.jstor.org": {
        "display_name": "JSTOR",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "tandfonline.com": {
        "display_name": "Taylor & Francis",
        "requires_subscription": True,
        "open_access_indicators": ["Open Access"],
    },
    "www.tandfonline.com": {
        "display_name": "Taylor & Francis",
        "requires_subscription": True,
        "open_access_indicators": ["Open Access"],
    },
    "onlinelibrary.wiley.com": {
        "display_name": "Wiley Online Library",
        "requires_subscription": True,
        "open_access_indicators": ["Open Access", "Free Access"],
    },
    "pubs.acs.org": {
        "display_name": "ACS Publications",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "aip.scitation.org": {
        "display_name": "AIP Scitation",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "pubs.aip.org": {
        "display_name": "AIP Publishing",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "journals.aps.org": {
        "display_name": "APS Journals",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "royalsocietypublishing.org": {
        "display_name": "Royal Society",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "pnas.org": {
        "display_name": "PNAS",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "www.pnas.org": {
        "display_name": "PNAS",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "nature.com": {
        "display_name": "Nature",
        "requires_subscription": True,
        "open_access_indicators": ["Open Access", "open-access"],
    },
    "www.nature.com": {
        "display_name": "Nature",
        "requires_subscription": True,
        "open_access_indicators": ["Open Access", "open-access"],
    },
    "science.org": {
        "display_name": "Science (AAAS)",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "www.science.org": {
        "display_name": "Science (AAAS)",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "cell.com": {
        "display_name": "Cell Press",
        "requires_subscription": True,
        "open_access_indicators": ["Open Access"],
    },
    "www.cell.com": {
        "display_name": "Cell Press",
        "requires_subscription": True,
        "open_access_indicators": ["Open Access"],
    },
    "cambridge.org": {
        "display_name": "Cambridge Core",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "www.cambridge.org": {
        "display_name": "Cambridge Core",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "academic.oup.com": {
        "display_name": "Oxford Academic",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    "spj.science.org": {
        "display_name": "Science Partner Journals",
        "requires_subscription": True,
        "open_access_indicators": [],
    },
    # 开放获取平台
    "arxiv.org": {
        "display_name": "arXiv",
        "requires_subscription": False,
        "open_access_indicators": [],
    },
    "www.arxiv.org": {
        "display_name": "arXiv",
        "requires_subscription": False,
        "open_access_indicators": [],
    },
    "plos.org": {
        "display_name": "PLOS",
        "requires_subscription": False,
        "open_access_indicators": [],
    },
    "journals.plos.org": {
        "display_name": "PLOS Journals",
        "requires_subscription": False,
        "open_access_indicators": [],
    },
    "biorxiv.org": {
        "display_name": "bioRxiv",
        "requires_subscription": False,
        "open_access_indicators": [],
    },
    "www.biorxiv.org": {
        "display_name": "bioRxiv",
        "requires_subscription": False,
        "open_access_indicators": [],
    },
    "mdpi.com": {
        "display_name": "MDPI",
        "requires_subscription": False,
        "open_access_indicators": [],
    },
    "www.mdpi.com": {
        "display_name": "MDPI",
        "requires_subscription": False,
        "open_access_indicators": [],
    },
    "frontiersin.org": {
        "display_name": "Frontiers",
        "requires_subscription": False,
        "open_access_indicators": [],
    },
    "www.frontiersin.org": {
        "display_name": "Frontiers",
        "requires_subscription": False,
        "open_access_indicators": [],
    },
    "scielo.org": {
        "display_name": "SciELO",
        "requires_subscription": False,
        "open_access_indicators": [],
    },
    # DOI解析服务
    "doi.org": {
        "display_name": "DOI (待解析)",
        "requires_subscription": None,  # 未知，需要解析后才知道
        "open_access_indicators": [],
    },
    "dx.doi.org": {
        "display_name": "DOI (待解析)",
        "requires_subscription": None,
        "open_access_indicators": [],
    },
}


def extract_domain_from_url(url: str) -> str:
    """从URL提取域名"""
    if not url:
        return ""
    url = url.strip()
    # 添加协议前缀（如果没有）
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        domain = parsed.hostname or ""
        return domain.lower()
    except Exception:
        return ""


def get_domain_display_name(domain: str) -> str:
    """获取域名的显示名称"""
    if domain in PAYWALL_DOMAINS:
        return PAYWALL_DOMAINS[domain].get("display_name", domain)
    return domain


def is_known_paywall_domain(domain: str) -> bool:
    """检查是否是已知付费墙域名"""
    if domain in PAYWALL_DOMAINS:
        return PAYWALL_DOMAINS[domain].get("requires_subscription", False) is True
    return False


def is_open_access_domain(domain: str) -> bool:
    """检查是否是已知开放获取域名"""
    if domain in PAYWALL_DOMAINS:
        return PAYWALL_DOMAINS[domain].get("requires_subscription", None) is False
    return False


def analyze_reference_domains(
    refs: list["ReferenceItem"],
    domain_cookies_config: dict[str, dict] | None = None,
) -> dict[str, DomainInfo]:
    """
    分析参考文献列表，统计各域名的引用数量

    Args:
        refs: 参考文献条目列表
        domain_cookies_config: 已配置的域名cookies {"domain": {"cookies_path": "..."}}

    Returns:
        域名到DomainInfo的映射字典
    """
    domain_cookies_config = domain_cookies_config or {}
    domain_refs: dict[str, list[int]] = defaultdict(list)

    for ref in refs:
        domains_for_ref = set()

        # 从DOI提取域名（DOI会解析到doi.org）
        for doi in ref.dois:
            if doi:
                domains_for_ref.add("doi.org")

        # 从URL提取域名
        for url in ref.urls:
            domain = extract_domain_from_url(url)
            if domain:
                domains_for_ref.add(domain)

        # 如果没有任何URL/DOI，标记为无来源
        if not domains_for_ref:
            domains_for_ref.add("no-url-doi")

        # 记录每个域名对应的参考文献编号
        for domain in domains_for_ref:
            domain_refs[domain].append(ref.number)

    # 构建DomainInfo字典
    result: dict[str, DomainInfo] = {}
    for domain, ref_numbers in domain_refs.items():
        paywall_info = PAYWALL_DOMAINS.get(domain, {})
        cookies_config = domain_cookies_config.get(domain, {})

        info = DomainInfo(
            domain=domain,
            display_name=paywall_info.get("display_name", domain),
            ref_numbers=ref_numbers,
            count=len(ref_numbers),
            requires_auth=paywall_info.get("requires_subscription", False) is True,
            has_cookies=bool(cookies_config.get("cookies_path")),
            cookies_path=cookies_config.get("cookies_path"),
        )
        result[domain] = info

    return result


def get_access_type(domain: str, domain_info: DomainInfo | None = None) -> str:
    """
    获取域名的访问类型标签

    Returns:
        "开放获取" | "需订阅*" | "待解析" | "未知"
    """
    if domain in ("no-url-doi", ""):
        return "无来源"

    if domain in PAYWALL_DOMAINS:
        requires_sub = PAYWALL_DOMAINS[domain].get("requires_subscription")
        if requires_sub is False:
            return "开放获取"
        elif requires_sub is True:
            return "需订阅*"
        else:
            return "待解析"

    return "未知"


def summarize_domains(domain_info: dict[str, DomainInfo]) -> str:
    """
    生成域名统计摘要文本

    Returns:
        格式化的文本摘要
    """
    lines = [
        "=" * 50,
        "参考文献域名分析",
        "=" * 50,
        f"{'域名':<30} {'数量':>6} {'访问类型':<10}",
        "-" * 50,
    ]

    # 按数量排序
    sorted_domains = sorted(
        domain_info.items(),
        key=lambda x: (-x[1].count, x[0])
    )

    open_count = 0
    paywall_count = 0
    unknown_count = 0

    for domain, info in sorted_domains:
        access_type = get_access_type(domain, info)
        display_name = info.display_name[:28] if len(info.display_name) > 28 else info.display_name

        lines.append(f"{display_name:<30} {info.count:>6} {access_type:<10}")

        if access_type == "开放获取":
            open_count += info.count
        elif access_type == "需订阅*":
            paywall_count += info.count
        else:
            unknown_count += info.count

    lines.extend([
        "-" * 50,
        f"* 标记的域名可能需要机构登录",
        f"开放获取: {open_count}篇, 需订阅: {paywall_count}篇, 其他: {unknown_count}篇",
        "=" * 50,
    ])

    return "\n".join(lines)


def get_domains_needing_cookies(
    domain_info: dict[str, DomainInfo]
) -> list[tuple[str, DomainInfo]]:
    """
    获取需要配置cookies的域名列表

    Returns:
        [(domain, DomainInfo), ...] 已知付费墙且未配置cookies的域名
    """
    result = []
    for domain, info in domain_info.items():
        if info.requires_auth and not info.has_cookies:
            result.append((domain, info))
    return result


def analyze_download_failures(
    refs: list["ReferenceItem"],
    domain_info: dict[str, DomainInfo],
) -> dict[str, dict]:
    """
    分析下载失败情况，按域名统计失败原因

    Returns:
        {
            "domain": {
                "failed_count": 5,
                "success_count": 2,
                "ref_numbers": [12, 15, ...],
                "likely_paywall": True,
            }
        }
    """
    result: dict[str, dict] = {}

    for domain, info in domain_info.items():
        if domain in ("no-url-doi", "doi.org"):
            continue

        failed_refs = []
        success_count = 0

        for ref_num in info.ref_numbers:
            # 找到对应的参考文献
            for ref in refs:
                if ref.number == ref_num:
                    if ref.download_status == "failed":
                        failed_refs.append(ref_num)
                    elif ref.download_status == "downloaded_pdf":
                        success_count += 1
                    break

        if failed_refs:
            # 根据失败比例和域名判断是否可能是付费墙
            total = len(info.ref_numbers)
            fail_ratio = len(failed_refs) / total if total > 0 else 0
            likely_paywall = fail_ratio > 0.5 or info.requires_auth

            result[domain] = {
                "failed_count": len(failed_refs),
                "success_count": success_count,
                "ref_numbers": failed_refs,
                "likely_paywall": likely_paywall,
                "display_name": info.display_name,
            }

    return result