"""供应链反查端点：POST /api/v1/supply-chain/query（轻量本地起链 + 图谱骨架）、
GET /api/v1/supply-chain/results、GET /api/v1/supply-chain/results/{name}（读取 agent workflow 报告）。

顺藤摸瓜方法论见 skills/supply-chain/SKILL.md：
  L1 DNS 解析 → L2 IP 情报 → L3 WHOIS → L4 CT 证书 → L5 子域名 → L6 相似域 → L7 账号/社交/资金。
本端点提供「本地可直接执行的轻量反查」（DNS 解析 + 相似域名 + 基础信号），
完整的多维 OSINT（WHOIS/CT/被动子域/资金链）由 agent 按 skill 用 workflow 深化，
结果落盘 var/supply-chain/*.json，前端可在此读取展示图谱。
"""

from __future__ import annotations

import json
import re
import socket
from pathlib import Path

from fastapi import APIRouter, Depends

from ..config import get_settings
from ..db import get_db
from ..deps import require_admin
from ..utils import ApiError, ok

router = APIRouter(prefix="/supply-chain", tags=["supply-chain"])

_URL_RE = re.compile(r"https?://([^/]+)")
_DOMAIN_RE = re.compile(r"^([a-z0-9][a-z0-9-]{0,62}\.)+[a-z]{2,24}$", re.IGNORECASE)


def _extract_domain(seed: str) -> str:
    """从网址/概念中提取域名（网址取 host；裸域直接用；概念词返回空串交给 agent workflow）。"""
    seed = (seed or "").strip()
    m = _URL_RE.search(seed)
    if m:
        return m.group(1).lower()
    if _DOMAIN_RE.match(seed):
        return seed.lower()
    return ""


def _resolve(domain: str) -> dict:
    """轻量本地解析：A 记录 / CNAME。失败返回空（不代表结论，完整反查交 workflow）。

    P1-B/C 修复：超长域名单标签（如 https://<70个a>.com/x）触发 socket.getaddrinfo
    内部 idna 编码的 UnicodeError（非 OSError 子类），原实现漏捕 → 500。
    这里统一按解析失败兜底（ips=[]）；域名是否合法由 _validate_domain 在入口 422 拦下。
    """
    out: dict = {}
    try:
        addrs = socket.getaddrinfo(domain, None, socket.AF_INET)
        ips = sorted({a[4][0] for a in addrs})
        out["ips"] = ips
    except (OSError, UnicodeError):
        out["ips"] = []
    return out


_MAX_DOMAIN_LEN = 253   # RFC 1035 域名总长上限
_MAX_LABEL_LEN = 63     # 单标签长度上限


def _validate_domain(domain: str) -> None:
    """域名长度/标签合法性校验：超限 → 422 seed_invalid（不落 500）。

    P1-B/C：socket.getaddrinfo 对超长标签/超长域名抛 UnicodeError（原 500），
    入口先校验拒绝；仅对提取出的 host 做校验，概念词（无域名可提取）不受影响。
    """
    if len(domain) > _MAX_DOMAIN_LEN:
        raise ApiError("seed_invalid", f"域名总长超过 {_MAX_DOMAIN_LEN} 字符上限，无法解析", 422)
    for label in domain.split("."):
        if len(label) > _MAX_LABEL_LEN:
            raise ApiError("seed_invalid",
                           f"域名标签超过 {_MAX_LABEL_LEN} 字符上限（{label[:16]}…），无法解析", 422)


def _similar_domains(domain: str) -> list[str]:
    """相似域名族（换 TLD + 常见变体），供 L6 层起链。"""
    name, _, tld = domain.rpartition(".")
    if not name:
        return []
    variants = []
    for t in ("com", "net", "org", "top", "cc", "xyz", "vip", "site", "cn", "info"):
        if t != tld:
            variants.append(f"{name}.{t}")
    # 加连字符/串联变体
    variants += [f"{name}pro.com", f"{name}-{tld}", f"{name}{tld}.com"[:40]]
    return list(dict.fromkeys(variants))[:12]


@router.post("/query", dependencies=[Depends(require_admin)])
def supply_chain_query(payload: dict, db=Depends(get_db)) -> dict:
    """轻量供应链反查起链：输入网址/概念 → 提取域名 → 本地 DNS 解析 + 相似域名族 + 基础信号。

    返回图谱骨架（graph + evidence + signals），完整多维 OSINT 由 agent workflow 深化
    （按 skills/supply-chain/SKILL.md 逐层反查后落盘 var/supply-chain/）。
    """
    seed = str((payload or {}).get("seed") or "").strip()
    if not seed:
        raise ApiError("seed_required", "请输入要反查的网址/域名/概念", 422)
    # P1-B/C：超大输入（概念词路径）直接 422，避免超长字符串进入正则/解析链路
    if len(seed) > 512:
        raise ApiError("seed_invalid", "输入过长（>512 字符），请精简后重试", 422)
    domain = _extract_domain(seed)
    if domain:
        # P1-B/C：超长域名/单标签 → 422 seed_invalid（原 500 UnicodeError）
        _validate_domain(domain)
    nodes: list[dict] = []
    edges: list[dict] = []
    evidence: list[dict] = []
    signals: list[str] = []

    if not domain:
        # 概念词：无域名可解析，交 agent workflow 全网反查
        nodes.append({"id": "concept", "name": seed, "type": "concept", "layer": "seed", "risk": "medium"})
        return ok({
            "seed": seed, "domain": None, "mode": "concept_only",
            "graph": {"nodes": nodes, "edges": edges},
            "evidence": [{
                "layer": "L0_概念", "type": "concept_seed", "value": seed,
                "note": "概念词（非网址）无域名可本地解析，已存为图谱根节点；请在命令输入/agent 面板用 supply-chain skill + workflow 做全网检索反查",
                "source": "input",
            }],
            "signals": ["概念类输入：需 agent workflow 按 skill 全网检索（搜索/社交/资金链）"],
            "hint": "agent_workflow",
        })

    # L1/L2：DNS 解析
    resolved = _resolve(domain)
    nodes.append({"id": domain, "name": domain, "type": "domain", "layer": "L1", "risk": "high" if not resolved.get("ips") else "medium"})
    if resolved.get("ips"):
        for ip in resolved["ips"][:4]:
            nodes.append({"id": f"ip:{ip}", "name": ip, "type": "ip", "layer": "L2", "risk": "medium"})
            edges.append({"source": domain, "target": f"ip:{ip}", "rel": "resolve"})
        evidence.append({
            "layer": "L1/L2_DNS-IP", "type": "a_record", "value": ", ".join(resolved["ips"]),
            "note": "本地 DNS 解析命中", "source": "socket.getaddrinfo",
        })
    else:
        evidence.append({
            "layer": "L1_DNS", "type": "nxdomain", "value": domain,
            "note": "本地解析无 A 记录（NXDOMAIN 或暂不可达）——需 agent workflow 用 DoH/被动源复核，'查无此站'本身是诈骗短期域名特征信号",
            "source": "socket.getaddrinfo",
        })
        signals.append("域名本地解析无记录：若为真实投放链接，符合'打一枪换域名'的诈骗生命周期特征；需多源复核")

    # L6：相似域名族
    sims = _similar_domains(domain)
    for s in sims:
        nodes.append({"id": s, "name": s, "type": "domain", "layer": "L6", "risk": "low"})
        edges.append({"source": domain, "target": s, "rel": "similar"})
    if sims:
        evidence.append({
            "layer": "L6_相似域名", "type": "similar_family", "value": ", ".join(sims),
            "note": "相似域名族（换 TLD + 变体）供批量注册核查；同注册人/同 IP 批量挂 = 团伙聚合点",
            "source": "pattern",
        })

    # 记录到 events（供应链反查动作）
    db.execute(
        "INSERT INTO events (kind, payload) VALUES ('supply_chain_query', ?)",
        (json.dumps({"seed": seed, "domain": domain, "resolved": bool(resolved.get("ips"))}, ensure_ascii=False),),
    )
    db.commit()

    return ok({
        "seed": seed, "domain": domain, "mode": "local_recon",
        "graph": {"nodes": nodes, "edges": edges},
        "evidence": evidence,
        "signals": signals,
        "hint": "agent_workflow",
    })


@router.get("/results", dependencies=[Depends(require_admin)])
def supply_chain_results(settings=Depends(get_settings)) -> dict:
    """列出 var/supply-chain/ 下的完整反查报告（agent workflow 产物）。"""
    d = Path(settings.data_dir).parent / "var" / "supply-chain"
    if not d.is_dir():
        return ok([])
    items = []
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            items.append({
                "name": f.stem,
                "seed": data.get("seed", f.stem),
                "summary": (data.get("summary") or "")[:200],
                "evidence_total": data.get("evidence_counts", {}).get("total", 0),
                "nodes": len(data.get("graph", {}).get("nodes", [])),
                "edges": len(data.get("graph", {}).get("edges", [])),
                "updated_at": data.get("run_at", ""),
            })
        except (json.JSONDecodeError, OSError):
            continue
    return ok(items)


@router.get("/results/{name}", dependencies=[Depends(require_admin)])
def supply_chain_result(name: str, settings=Depends(get_settings)) -> dict:
    """读取单份完整反查报告。"""
    name = Path(name).name  # 防路径穿越
    p = Path(settings.data_dir).parent / "var" / "supply-chain" / f"{name}.json"
    if not p.is_file():
        raise ApiError("report_not_found", f"反查报告不存在: {name}", 404)
    try:
        return ok(json.loads(p.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        raise ApiError("report_invalid", f"反查报告损坏: {name}", 500)