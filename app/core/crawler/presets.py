"""Built-in Internet crawler source and keyword presets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class CrawlerPreset:
    id: str
    name: str
    description: str
    kind: str = "source"
    category_name: str | None = None
    site_urls: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    structured_sources: tuple[str, ...] = ()
    include_presets: tuple[str, ...] = ()
    source_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    domain_category: str | None = None
    kb_tier: str | None = None
    phases: tuple[str, ...] = ()
    topic_tags: tuple[str, ...] = ()
    priority: str | None = None
    review_criteria: str = ""


CRAWLER_SOURCE_PRESETS = (
    CrawlerPreset(
        id="international_security_news",
        name="国际安全新闻与博客",
        description="15 个国际安全新闻、研究与事件分析站点",
        site_urls=(
            "https://thehackernews.com/",
            "https://www.bleepingcomputer.com/",
            "https://krebsonsecurity.com/",
            "https://www.schneier.com/",
            "https://www.darkreading.com/",
            "https://threatpost.com/",
            "https://www.securityweek.com/",
            "https://www.infosecurity-magazine.com/",
            "https://www.csoonline.com/",
            "https://www.zdnet.com/topic/security/",
            "https://www.helpnetsecurity.com/",
            "https://cyberscoop.com/",
            "https://therecord.media/",
            "https://arstechnica.com/security/",
            "https://gbhackers.com/",
        ),
    ),
    CrawlerPreset(
        id="china_security_community",
        name="中国安全社区与公告",
        description="8 个国内安全社区、厂商公告与威胁情报站点",
        site_urls=(
            "https://www.freebuf.com/",
            "https://www.anquanke.com/",
            "https://www.4hou.com/",
            "https://www.sec-wiki.com/",
            "https://www.52pojie.cn/",
            "https://security.tencent.com/",
            "https://www.alibabacloud.com/help/zh/security-notices/",
            "https://ti.qianxin.com/",
        ),
    ),
    CrawlerPreset(
        id="vulnerability_databases",
        name="漏洞数据库与利用公告",
        description="Exploit-DB、Packet Storm 与 OpenCVE",
        site_urls=(
            "https://www.exploit-db.com/",
            "https://packetstormsecurity.com/",
            "https://www.opencve.io/cve",
        ),
    ),
    CrawlerPreset(
        id="government_security_agencies",
        name="政府与标准机构动态",
        description="CISA、NIST、NCSC、ACSC 与 ENISA 官方动态",
        site_urls=(
            "https://www.cisa.gov/news-events/news",
            "https://csrc.nist.gov/news",
            "https://www.ncsc.gov.uk/news",
            "https://www.cyber.gov.au/about-us/advisories",
            "https://www.enisa.europa.eu/news",
        ),
    ),
    CrawlerPreset(
        id="chinese_security_keywords",
        name="中文安全主题关键词",
        description="25 组漏洞、合规、AI、云、供应链与基础设施安全检索词",
        keywords=(
            "2025 2026 重大网络安全漏洞 通报",
            "零日漏洞 最新 2025 2026",
            "勒索软件 攻击事件 2025 2026",
            "供应链攻击 案例 分析 2025 2026",
            "APT 高级持续性威胁 最新动态 2025 2026",
            "数据泄露 事件 2025 2026 处罚",
            "个人信息保护 合规 案例 2025 2026",
            "数据跨境传输 安全评估 最新规定",
            "数据分类分级 实施指南",
            "大模型安全 攻击 防御 2025 2026",
            "AI 生成内容 安全风险 2025 2026",
            "提示词注入 防护 方案",
            "深度伪造 检测 技术",
            "云安全 最佳实践 2025 2026",
            "零信任 架构 实施 方案",
            "软件供应链安全 SBOM 2025 2026",
            "开源软件 安全治理",
            "网络安全等级保护 2.0 测评 要求",
            "关基 保护 条例 实施 细则",
            "数据安全法 执法案例",
            "个人信息出境 标准合同 备案",
            "量子计算 对密码学 威胁",
            "物联网安全 漏洞 2025 2026",
            "车联网 数据安全 规定",
            "工业控制系统 安全 事件 2025 2026",
        ),
    ),
    CrawlerPreset(
        id="english_security_keywords",
        name="英文安全主题关键词",
        description="20 组漏洞、事件、合规、AI 与基础设施安全检索词",
        keywords=(
            "critical CVE vulnerability 2025 2026 exploitation",
            "ransomware attack 2025 2026 analysis",
            "zero-day vulnerability 2025 2026 disclosure",
            "data breach incident 2025 2026 report",
            "supply chain attack 2025 2026 case study",
            "APT group threat intelligence 2025 2026",
            "cloud security best practices 2025 2026",
            "zero trust architecture implementation guide",
            "LLM AI security vulnerability OWASP 2025 2026",
            "prompt injection defense techniques",
            "software supply chain security SBOM 2025 2026",
            "NIST CSF 2.0 implementation guide",
            "cybersecurity regulation compliance 2025 2026",
            "IoT security vulnerability 2025 2026",
            "quantum computing cryptography threat",
            "CISA known exploited vulnerability 2025 2026",
            "cyber insurance requirements 2025 2026",
            "API security best practices 2025 2026",
            "container kubernetes security 2025 2026",
            "identity access management trends 2025 2026",
        ),
    ),
)


CRAWLER_CATEGORY_PRESETS = (
    CrawlerPreset(
        id="agent_01_asset_fingerprint",
        name="01 资产指纹与技术栈",
        description="产品特征、默认端口、服务版本、框架路径与 CPE 识别",
        kind="category",
        category_name="01_资产指纹与技术栈",
        keywords=(
            "web application fingerprint default port path header detection",
            "中间件 CMS 框架 指纹识别 默认路径 版本探测",
            "CPE product version fingerprint identification",
        ),
        site_urls=(
            "https://nmap.org/book/vscan.html",
            "https://docs.projectdiscovery.io/opensource/httpx/usage",
        ),
        domain_category="asset_fingerprint",
        kb_tier="manual",
        phases=("RECON", "THREAT_MODEL"),
        topic_tags=("fingerprint", "cpe", "service", "middleware", "framework"),
        priority="P0",
        review_criteria="""仅通过能够支持资产识别和技术栈判断的内容：应至少包含产品、厂商、协议、服务、组件或框架名称，并提供版本、CPE、默认端口、响应特征、路径、Header、Banner 等至少一种可验证指纹。拒绝纯营销内容、无技术特征的新闻、仅有产品介绍但无法形成识别规则的页面。内容应来源明确、正文完整，关键结论不能只有推测。""",
    ),
    CrawlerPreset(
        id="agent_02_vulnerability_weakness",
        name="02 漏洞与弱点知识",
        description="CVE、CWE、CVSS、影响版本、利用状态与补丁信息",
        kind="category",
        category_name="02_漏洞与弱点知识",
        keywords=(
            "critical CVE vulnerability 2025 2026 exploitation",
            "零日漏洞 最新 2025 2026",
        ),
        site_urls=("https://www.cisa.gov/known-exploited-vulnerabilities-catalog",),
        structured_sources=("nvd", "cisa_kev", "cwe", "cwe_views"),
        source_options={"nvd": {"days_back": 30}},
        domain_category="vulnerability_weakness",
        kb_tier="cve",
        phases=("THREAT_MODEL", "VULN_SCAN"),
        topic_tags=("cve", "cwe", "cvss", "cpe", "kev", "patch"),
        priority="P0",
        review_criteria="""仅通过可形成漏洞或弱点知识的内容：应明确包含 CVE/CWE/KEV 等标识，或给出漏洞成因、受影响产品与版本、CVSS/严重性、利用状态、补丁或缓解信息中的至少两项。拒绝只有编号列表、无漏洞细节、与网络安全无关或来源不可核验的内容。结构化记录必须包含足够描述，不能只有空字段。""",
    ),
    CrawlerPreset(
        id="agent_03_detection_exploit_validation",
        name="03 漏洞检测与利用验证",
        description="检测模板、PoC 前置条件、成功证据和误报排除方法",
        kind="category",
        category_name="03_漏洞检测与利用验证",
        keywords=(
            "vulnerability proof of concept verification false positive evidence",
            "nuclei template matcher exploit validation remediation",
            "漏洞 PoC 验证 成功证据 误报排除",
        ),
        site_urls=(
            "https://owasp.org/www-project-web-security-testing-guide/stable/",
            "https://docs.projectdiscovery.io/templates/introduction",
        ),
        structured_sources=("nvd", "cisa_kev", "capec"),
        source_options={"nvd": {"days_back": 90}},
        domain_category="detection_exploit_validation",
        kb_tier="manual",
        phases=("VULN_SCAN", "EXPLOIT"),
        topic_tags=("poc", "nuclei", "matcher", "evidence", "false_positive"),
        priority="P0",
        review_criteria="""仅通过能够指导漏洞检测或验证的内容：应包含检测前置条件、请求或测试步骤、匹配器/规则/模板、成功证据、影响判断、误报排除方法中的至少两项。拒绝只有漏洞概述而无检测方法、纯攻击宣传、不可验证的 PoC 转载，以及明显包含恶意诱导但无防护上下文的内容。""",
    ),
    CrawlerPreset(
        id="agent_04_remediation_closure",
        name="04 修复处置与闭环",
        description="官方补丁、升级路径、缓解配置、响应处置与回归验证",
        kind="category",
        category_name="04_修复处置与闭环",
        keywords=(
            "vulnerability remediation patch upgrade mitigation verification",
            "漏洞修复 补丁 升级 临时缓解 回归验证",
            "firewall block EDR isolate incident response playbook",
        ),
        site_urls=(
            "https://cheatsheetseries.owasp.org/",
            "https://www.cisa.gov/news-events/cybersecurity-advisories",
        ),
        structured_sources=("nvd", "cisa_kev", "cwe", "owasp", "nist"),
        domain_category="remediation_closure",
        kb_tier="manual",
        phases=("VULN_SCAN", "EXPLOIT", "REPORT"),
        topic_tags=("remediation", "patch", "mitigation", "containment", "verification"),
        priority="P0",
        review_criteria="""仅通过能够支持修复闭环的内容：应提供官方补丁或升级路径、配置缓解、隔离阻断、响应处置、回归验证、残余风险中的至少一项可执行措施，并明确适用对象。拒绝仅描述风险、不含处置建议的新闻，或来源不明且可能导致破坏性操作的建议。优先保留官方厂商、标准机构和可信安全组织内容。""",
    ),
    CrawlerPreset(
        id="agent_05_attack_chain",
        name="05 攻击技战术与攻击链",
        description="ATT&CK、CAPEC、攻击路径、横向移动、提权与持久化",
        kind="category",
        category_name="05_攻击技战术与攻击链",
        keywords=(
            "MITRE ATT&CK tactics techniques attack chain detection",
            "攻击技战术 攻击链 横向移动 权限提升 持久化",
            "APT threat hunting tactics techniques procedures",
        ),
        site_urls=(
            "https://attack.mitre.org/techniques/",
            "https://capec.mitre.org/data/index.html",
        ),
        structured_sources=("capec",),
        domain_category="attack_chain",
        kb_tier="manual",
        phases=("THREAT_MODEL", "EXPLOIT"),
        topic_tags=("attack", "capec", "ttp", "lateral_movement", "persistence"),
        priority="P1",
        review_criteria="""仅通过能够描述攻击技战术或攻击链的内容：应包含 ATT&CK/CAPEC 标识、攻击阶段、前置条件、具体技术、横向移动、提权、持久化、命令控制等可映射信息。拒绝只有事件标题、无行为细节的新闻，以及无法区分事实与猜测的攻击归因。内容应能支持威胁建模或攻击路径分析。""",
    ),
    CrawlerPreset(
        id="agent_06_xdr_detection",
        name="06 XDR 多源数据与检测知识",
        description="告警、事件、流量、DNS、端点和账号日志的关联研判",
        kind="category",
        category_name="06_XDR多源数据与检测知识",
        keywords=(
            "XDR alert event network endpoint DNS log correlation",
            "安全告警 流量 端点 进程 账号 日志 关联分析",
            "Sigma detection rule ATT&CK mapping incident triage",
        ),
        site_urls=(
            "https://sigmahq.io/docs/basics/rules.html",
            "https://attack.mitre.org/datasources/",
        ),
        structured_sources=("capec", "nist"),
        domain_category="xdr_detection",
        kb_tier="manual",
        phases=("RECON", "THREAT_MODEL", "REPORT"),
        topic_tags=("xdr", "alert", "event", "network", "endpoint", "dns", "correlation"),
        priority="P1",
        review_criteria="""仅通过能够支持 XDR 检测和关联研判的内容：应包含日志/遥测来源、字段或事件特征、检测逻辑、Sigma/查询规则、告警关联、调查步骤、误报条件中的至少一项。拒绝纯产品营销、只有能力口号而无数据字段或规则逻辑的内容。检测结论必须能追溯到端点、网络、DNS、身份或云等数据证据。""",
    ),
    CrawlerPreset(
        id="agent_07_tool_runbook",
        name="07 工具与技能运行手册",
        description="安全工具适用条件、参数选择、产物解释和失败处理",
        kind="category",
        category_name="07_工具与技能运行手册",
        keywords=(
            "nmap httpx katana nuclei sqlmap metasploit usage guide",
            "渗透测试工具 参数 结果解析 失败排查 安全边界",
            "nuclei template authoring matcher extractor guide",
        ),
        site_urls=(
            "https://nmap.org/book/man.html",
            "https://docs.projectdiscovery.io/opensource",
            "https://sqlmap.org/",
            "https://docs.metasploit.com/",
        ),
        structured_sources=("owasp",),
        domain_category="tool_runbook",
        kb_tier="manual",
        phases=("RECON", "THREAT_MODEL", "VULN_SCAN", "EXPLOIT", "REPORT"),
        topic_tags=("tool", "skill", "runbook", "parameters", "artifact", "troubleshooting"),
        priority="P1",
        review_criteria="""仅通过能够作为安全工具运行手册的内容：应明确工具适用场景、安装或前置条件、参数、命令示例、输出解释、失败排查、安全边界中的至少两项。拒绝只有下载链接、版本新闻、无参数说明的产品页面，以及鼓励未授权攻击且没有合规边界的内容。命令和参数应具有上下文，避免不可控破坏。""",
    ),
    CrawlerPreset(
        id="agent_08_threat_intelligence",
        name="08 威胁情报与实战案例",
        description="IOC、攻击组织、活跃利用、厂商通告与真实攻击案例",
        kind="category",
        category_name="08_威胁情报与实战案例",
        keywords=(
            "active exploitation threat intelligence IOC 2025 2026",
            "APT group campaign indicators compromise 2025 2026",
            "高危漏洞 在野利用 威胁情报 攻击案例",
        ),
        site_urls=(
            "https://www.microsoft.com/en-us/security/blog/topic/threat-intelligence/",
            "https://unit42.paloaltonetworks.com/",
            "https://securelist.com/",
        ),
        structured_sources=("cisa_kev",),
        domain_category="threat_intelligence",
        kb_tier="blogs",
        phases=("THREAT_MODEL", "VULN_SCAN", "REPORT"),
        topic_tags=("threat_intel", "ioc", "apt", "campaign", "active_exploitation"),
        priority="P1",
        review_criteria="""仅通过具有可操作威胁情报价值的内容：应包含 IOC、攻击组织、活动时间、目标行业、使用的漏洞/恶意软件/TTP、在野利用证据、检测或处置建议中的至少两项。拒绝无来源传闻、重复转载、只有宏观趋势没有实体或技术细节的内容。IOC 和归因信息应注明来源与时效。""",
    ),
    CrawlerPreset(
        id="agent_09_compliance_reporting",
        name="09 标准合规与报告依据",
        description="OWASP、PTES、NIST、等保、风险定级与报告规范",
        kind="category",
        category_name="09_标准合规与报告依据",
        keywords=(
            "penetration testing report risk rating remediation standard",
            "OWASP testing guide PTES NIST incident report",
            "等级保护 安全测试 风险定级 报告规范",
        ),
        site_urls=(
            "https://csrc.nist.gov/publications",
            "https://owasp.org/www-project-application-security-verification-standard/",
        ),
        structured_sources=("owasp", "nist", "china_standards"),
        domain_category="compliance_reporting",
        kb_tier="manual",
        phases=("REPORT",),
        topic_tags=("owasp", "ptes", "nist", "compliance", "risk_rating", "report"),
        priority="P2",
        review_criteria="""仅通过能够作为合规或报告依据的内容：应来自法律法规、国家/行业标准、NIST/OWASP/PTES 等可信规范，或清晰说明风险评级、测试范围、证据要求、报告结构、整改验收方法。拒绝无条款依据的二手解读、过期且未标明版本的要求、纯商业宣传。应保留标准名称、版本、发布日期或适用范围。""",
    ),
)

CRAWLER_PRESETS = (*CRAWLER_CATEGORY_PRESETS, *CRAWLER_SOURCE_PRESETS)

# Saved callers may still use the original 11 category IDs. Map them to the
# nearest Agent-oriented category instead of maintaining a second copy of every
# source, keyword and metadata definition.
_PRESET_ALIASES = {
    "category_01_compliance": "agent_09_compliance_reporting",
    "category_02_vulnerability": "agent_02_vulnerability_weakness",
    "category_03_attack": "agent_05_attack_chain",
    "category_04_cwe": "agent_02_vulnerability_weakness",
    "category_05_standards": "agent_09_compliance_reporting",
    "category_06_cloud": "agent_02_vulnerability_weakness",
    "category_07_ai": "agent_02_vulnerability_weakness",
    "category_08_data": "agent_09_compliance_reporting",
    "category_09_supply_chain": "agent_02_vulnerability_weakness",
    "category_10_emerging": "agent_08_threat_intelligence",
    "category_11_news": "agent_08_threat_intelligence",
}

_PRESET_INDEX = {preset.id: preset for preset in CRAWLER_PRESETS}


def get_crawler_preset(preset_id: str) -> CrawlerPreset:
    try:
        return _PRESET_INDEX[_PRESET_ALIASES.get(preset_id, preset_id)]
    except KeyError as error:
        raise ValueError(f"Unknown crawler preset: {preset_id}") from error


@dataclass(frozen=True, slots=True)
class ExpandedCrawlerPresets:
    site_urls: list[str]
    keywords: list[str]
    structured_sources: list[str]
    source_options: dict[str, dict[str, Any]]
    category_name: str | None
    domain_category: str | None
    kb_tier: str | None
    phases: list[str]
    topic_tags: list[str]
    priority: str | None
    review_criteria: str


def expand_crawler_presets(preset_ids: list[str]) -> ExpandedCrawlerPresets:
    site_urls: list[str] = []
    keywords: list[str] = []
    structured_sources: list[str] = []
    source_options: dict[str, dict[str, Any]] = {}
    category_names: list[str] = []
    domain_categories: list[str] = []
    kb_tiers: list[str] = []
    phases: list[str] = []
    topic_tags: list[str] = []
    priorities: list[str] = []
    review_criteria: list[str] = []
    visited: set[str] = set()

    def expand(preset_id: str) -> None:
        if preset_id in visited:
            return
        visited.add(preset_id)
        preset = get_crawler_preset(preset_id)
        site_urls.extend(preset.site_urls)
        keywords.extend(preset.keywords)
        structured_sources.extend(preset.structured_sources)
        for source_id, options in preset.source_options.items():
            source_options.setdefault(source_id, {}).update(options)
        if preset.category_name:
            category_names.append(preset.category_name)
        if preset.domain_category:
            domain_categories.append(preset.domain_category)
        if preset.kb_tier:
            kb_tiers.append(preset.kb_tier)
        phases.extend(preset.phases)
        topic_tags.extend(preset.topic_tags)
        if preset.priority:
            priorities.append(preset.priority)
        if preset.review_criteria:
            review_criteria.append(preset.review_criteria)
        for included_id in preset.include_presets:
            expand(included_id)

    for preset_id in preset_ids:
        expand(preset_id)
    categories = list(dict.fromkeys(category_names))
    if len(categories) > 1:
        raise ValueError("Only one crawler category preset can be selected per job")
    return ExpandedCrawlerPresets(
        site_urls=list(dict.fromkeys(site_urls)),
        keywords=list(dict.fromkeys(keywords)),
        structured_sources=list(dict.fromkeys(structured_sources)),
        source_options=source_options,
        category_name=categories[0] if categories else None,
        domain_category=next(iter(dict.fromkeys(domain_categories)), None),
        kb_tier=next(iter(dict.fromkeys(kb_tiers)), None),
        phases=list(dict.fromkeys(phases)),
        topic_tags=list(dict.fromkeys(topic_tags)),
        priority=next(iter(dict.fromkeys(priorities)), None),
        review_criteria="\n\n".join(dict.fromkeys(review_criteria)),
    )
