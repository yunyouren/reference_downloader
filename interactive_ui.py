"""交互式终端UI模块 - 提供用户友好的配置界面"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from site_handlers.domain_analyzer import DomainInfo


def should_run_interactive(interactive_setting: str) -> bool:
    """
    检测是否应该运行交互模式

    Args:
        interactive_setting: "auto" | "true" | "false"

    Returns:
        是否应该进入交互模式
    """
    if interactive_setting == "true":
        return True
    elif interactive_setting == "false":
        return False
    else:  # "auto"
        # 检测是否在TTY终端运行
        try:
            return sys.stdin.isatty() and sys.stdout.isatty()
        except Exception:
            return False


def display_domain_summary(domain_info: dict[str, "DomainInfo"]) -> None:
    """显示域名统计摘要"""
    from site_handlers.domain_analyzer import summarize_domains
    print(summarize_domains(domain_info))


def prompt_cookie_configuration(
    domain: str,
    info: "DomainInfo",
) -> dict | None:
    """
    提示用户为单个域名配置cookies

    Returns:
        配置字典 {"cookies_path": "...", "description": "..."} 或 None
    """
    print(f"\n配置 {info.display_name}:")
    print("-" * 40)

    try:
        cookies_path = input("Cookies文件路径 (回车跳过): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n跳过")
        return None

    if not cookies_path:
        return None

    # 验证文件是否存在
    path = Path(cookies_path)
    if not path.exists():
        print(f"警告: 文件不存在: {cookies_path}")
        try:
            confirm = input("仍要使用此路径? [y/N]: ").strip().lower()
            if confirm != 'y':
                return None
        except (EOFError, KeyboardInterrupt):
            return None

    try:
        description = input("描述 (可选): ").strip()
    except (EOFError, KeyboardInterrupt):
        description = ""

    return {
        "cookies_path": cookies_path,
        "description": description,
    }


def configure_cookies_interactively(
    domain_info: dict[str, "DomainInfo"],
    existing_config: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """
    交互式配置cookies

    Args:
        domain_info: 域名分析结果
        existing_config: 已有的配置

    Returns:
        更新后的域名cookies配置 {"domain": {"cookies_path": ..., "description": ...}}
    """
    from site_handlers.domain_analyzer import get_domains_needing_cookies

    existing_config = existing_config or {}
    result = dict(existing_config)

    # 找出需要配置cookies的域名
    needs_cookies = get_domains_needing_cookies(domain_info)

    if not needs_cookies:
        print("\n所有已知付费墙域名都已配置cookies")
        return result

    print("\n" + "=" * 50)
    print("以下域名可能需要机构登录才能下载:")
    print("=" * 50)

    # 显示需要配置的域名列表
    for i, (domain, info) in enumerate(needs_cookies, 1):
        status = "已配置" if info.has_cookies else "未配置"
        print(f"[{i}] {info.display_name} ({info.count}篇) - {status}")

    print("-" * 50)

    try:
        selection = input("\n请选择要配置的域名 (输入编号, 'all', 或 'skip'): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n跳过配置")
        return result

    if selection == "skip":
        return result

    # 解析选择
    indices = []
    if selection == "all":
        indices = list(range(len(needs_cookies)))
    else:
        try:
            # 支持逗号分隔或空格分隔
            for part in selection.replace(",", " ").split():
                idx = int(part) - 1
                if 0 <= idx < len(needs_cookies):
                    indices.append(idx)
        except ValueError:
            print("无效输入，跳过配置")
            return result

    # 逐个配置
    for idx in indices:
        domain, info = needs_cookies[idx]
        config = prompt_cookie_configuration(domain, info)
        if config:
            result[domain] = config
            print(f"已配置 {info.display_name}")

    return result


def confirm_continue_without_cookies(domains: list[str]) -> bool:
    """
    确认是否在未配置cookies的情况下继续

    Returns:
        True表示继续，False表示中止
    """
    if not domains:
        return True

    print("\n警告: 以下域名未配置cookies，可能无法下载:")
    for domain in domains[:5]:  # 最多显示5个
        print(f"  - {domain}")
    if len(domains) > 5:
        print(f"  ... 还有 {len(domains) - 5} 个")

    try:
        answer = input("是否继续? [Y/n]: ").strip().lower()
        return answer != 'n'
    except (EOFError, KeyboardInterrupt):
        return True


def prompt_for_additional_cookies(
    failed_domains: dict[str, dict],
) -> dict[str, dict] | None:
    """
    下载失败后提示用户补充cookies

    Args:
        failed_domains: 失败域名统计 {"domain": {"failed_count": ..., "display_name": ...}}

    Returns:
        新的cookies配置或None
    """
    if not failed_domains:
        return None

    print("\n" + "=" * 50)
    print("下载失败分析")
    print("=" * 50)

    # 按失败数量排序
    sorted_domains = sorted(
        failed_domains.items(),
        key=lambda x: -x[1]["failed_count"]
    )

    likely_paywall_domains = []
    for domain, stats in sorted_domains:
        if stats.get("likely_paywall"):
            likely_paywall_domains.append((domain, stats))
            print(f"{stats['display_name']}: {stats['failed_count']}篇失败 (可能需要登录)")

    if not likely_paywall_domains:
        print("未检测到明确的付费墙问题")
        return None

    print("-" * 50)

    try:
        answer = input("\n是否为这些域名配置cookies? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None

    if answer == 'n':
        return None

    # 构建简化的DomainInfo用于配置
    from site_handlers.domain_analyzer import DomainInfo

    fake_domain_info = {}
    for domain, stats in likely_paywall_domains:
        fake_domain_info[domain] = DomainInfo(
            domain=domain,
            display_name=stats["display_name"],
            ref_numbers=stats["ref_numbers"],
            count=stats["failed_count"],
            requires_auth=True,
            has_cookies=False,
        )

    return configure_cookies_interactively(fake_domain_info)


def display_download_summary(
    refs: list,
    domain_info: dict[str, "DomainInfo"] | None = None,
) -> None:
    """
    显示下载结果摘要

    Args:
        refs: 参考文献列表
        domain_info: 域名分析结果（可选）
    """
    total = len(refs)
    downloaded = sum(1 for r in refs if r.download_status == "downloaded_pdf")
    landing = sum(1 for r in refs if r.download_status == "saved_landing_url")
    failed = sum(1 for r in refs if r.download_status == "failed")

    print("\n" + "=" * 50)
    print("下载结果摘要")
    print("=" * 50)
    print(f"总计: {total}篇参考文献")
    print(f"  PDF下载成功: {downloaded}篇 ({downloaded/total*100:.1f}%)")
    print(f"  落地页保存: {landing}篇")
    print(f"  下载失败: {failed}篇")

    if domain_info and failed > 0:
        from site_handlers.domain_analyzer import analyze_download_failures
        failures = analyze_download_failures(refs, domain_info)

        if failures:
            print("\n失败域名分析:")
            for domain, stats in sorted(failures.items(), key=lambda x: -x[1]["failed_count"])[:5]:
                print(f"  {stats['display_name']}: {stats['failed_count']}篇失败")

    print("=" * 50)