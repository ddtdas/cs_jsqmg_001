"""P3 改进 + OSS 资产回归测试（审查报告 P3-1/P3-6/P3-7/P3-8/P3-9）。

覆盖：
  1) P3-6 蜜饵模板选择确定性（sha256，跨进程/重启可复现）
  2) P3-8 CookieVault 密钥独立路径（AF_ZHIHU_KEY_FILE）
  3) P3-9 话术近重复去重：改写文本不重复入库（ingest_manual 与 scan_text 两条路径）
  4) P3-7 Open Source Ready 资产就位（LICENSE / pyproject / CI / SECURITY.md / 版本上限）
  5) P3-1 README embedding 表述与实现一致

运行：pytest tests/test_p3_improvements.py -q（项目根）。
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from app import db as dbmod
from app.config import get_settings
from app.services import llm_provider, zhihu_bridge
from app.services.speech_engine import SpeechEngine
from app.services.trap_engine import TrapEngine


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.reset_provider()
    zhihu_bridge.reset_global_limits()
    dbmod.ensure_schema()
    yield dbmod.get_conn()
    dbmod.close_all()
    llm_provider.reset_provider()
    zhihu_bridge.reset_global_limits()
    get_settings.cache_clear()


# ===========================================================================
# P3-6 模板选择确定性
# ===========================================================================

def test_template_selection_deterministic(env):
    engine = TrapEngine()

    async def _pick(tid):
        return await engine._generate_bait(env, tid)

    # 未知 template_id → 走 sha256 确定性 fallback；两次调用结果一致
    t1a, g1a = asyncio.run(_pick("t_unknown_xyz"))
    t1b, g1b = asyncio.run(_pick("t_unknown_xyz"))
    assert (t1a, g1a) == (t1b, g1b), "同一 template_id 两次选择必须一致（P3-6）"
    assert g1a.startswith("template:")

    # 与独立复算的 sha256 索引一致（跨进程/重启可复现的根基）
    import hashlib

    idx = int.from_bytes(
        hashlib.sha256(b"t_unknown_xyz").digest()[:4], "big"
    ) % len(TrapEngine._generate_bait.__globals__["TRAP_TEMPLATES"])
    assert g1a == f"template:{TrapEngine._generate_bait.__globals__['TRAP_TEMPLATES'][idx]['id']}"

    # 不同 template_id 通常选中不同模板（确定性但不趋同）
    t2, g2 = asyncio.run(_pick("t_unknown_abc"))
    assert (t1a, g1a) != (t2, g2) or True  # 不强制不同（哈希碰撞概率极低），仅记录


# ===========================================================================
# P3-8 密钥独立路径
# ===========================================================================

def test_cookie_vault_key_file_override(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AF_ZHIHU_KEY_FILE", str(tmp_path / "secrets" / "zhihu.key"))
    monkeypatch.delenv("AF_ZHIHU_ENCRYPT_KEY", raising=False)
    get_settings.cache_clear()

    vault = zhihu_bridge.CookieVault(get_settings())
    # 密钥写到了独立路径，而不是 data 目录
    assert (tmp_path / "secrets" / "zhihu.key").exists()
    assert not (tmp_path / "data" / "secret.key").exists()
    # 往返可用且密钥为合法 Fernet key
    plain = "d_c0=abc; _zap=1"
    assert vault.decrypt(vault.encrypt(plain)) == plain
    key = (tmp_path / "secrets" / "zhihu.key").read_text(encoding="utf-8").strip()
    Fernet(key.encode())  # 不抛 = 合法

    # 相对路径基于项目根解析（不崩溃即可）
    monkeypatch.setenv("AF_ZHIHU_KEY_FILE", "var/test_zhihu.key")
    get_settings.cache_clear()
    vault2 = zhihu_bridge.CookieVault(get_settings())
    assert vault2.key_path.is_absolute()
    get_settings.cache_clear()


# ===========================================================================
# P3-9 近重复去重
# ===========================================================================

def test_ingest_manual_near_duplicate_semantics(env):
    """R7 L2-B 产品语义：ingest 近重复阈值默认 0.85 且可配置。

    - 近乎原样重发（overlap≥0.85）→ 判 near_duplicate 跳过（防库膨胀）；
    - 明显改写（overlap<0.85）→ 作为新证据入库（反诈取证：每次新消息都应被记录）；
    - zhihu.ingest_near_dup_threshold 热更新可调（config_reader 接线）。
    """
    import json

    def _set_cfg(key: str, value) -> None:
        env.execute(
            "INSERT OR REPLACE INTO configs (cfg_key, value, updated_at) VALUES (?, ?, datetime('now'))",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        env.commit()

    bridge = zhihu_bridge.ZhihuBridge(settings=get_settings(), conn=env)
    base = "稳赚不赔的理财项目，导师带你内幕消息"

    first = bridge.ingest_manual(base, source="import")
    assert first["ingested"] == 1

    # ① 近乎原样重发（追加 2 字 → overlap≈0.889 ≥ 默认 0.85）→ 判近重复跳过
    near_identical = base + "啊哈"
    second = bridge.ingest_manual(near_identical, source="import")
    assert second["ingested"] == 0, "近乎原样重发应判近重复跳过"
    assert second["duplicates"] == 1
    assert second["items"][0]["duplicate"] is True
    assert second["items"][0]["reason"] == "near_duplicate"
    assert second["items"][0]["detection_id"] == first["items"][0]["detection_id"]
    assert env.execute("SELECT COUNT(*) AS n FROM detections").fetchone()["n"] == 1

    # ② 明显改写（不同句式，overlap≈0.30 < 0.85）→ 作为新证据入库（取证完整性）
    rewrite = "有个稳赚不赔的内部理财渠道，导师带你高回报"
    third = bridge.ingest_manual(rewrite, source="import")
    assert third["ingested"] == 1, "明显改写的新消息必须入库（R7 产品语义）"
    assert third["items"][0]["detection_id"] != first["items"][0]["detection_id"]
    assert env.execute("SELECT COUNT(*) AS n FROM detections").fetchone()["n"] == 2

    # ③ 阈值配置化：0.95 → ① 的变体（0.889）不再被判近重复，作为新消息入库
    _set_cfg("zhihu.ingest_near_dup_threshold", 0.95)
    fourth = bridge.ingest_manual(near_identical, source="import")
    assert fourth["ingested"] == 1, "阈值调高后应作为新消息入库"
    assert env.execute("SELECT COUNT(*) AS n FROM detections").fetchone()["n"] == 3
    _set_cfg("zhihu.ingest_near_dup_threshold", zhihu_bridge.INGEST_NEAR_DUP_THRESHOLD)


async def _scan(conn, text: str) -> dict:
    return await SpeechEngine().scan_text(text, source="manual", conn=conn)


@pytest.mark.asyncio
async def test_scan_text_near_duplicate_reuses_detection(env):
    base = "刷单返利，先垫付，解锁任务后日结佣金"
    rewrite = "刷单返利，先垫付，解锁任务后日结佣金，今天就能结算"  # 改写

    r1 = await _scan(env, base)
    assert r1["verdict"] in ("suspicious", "fraud")

    r2 = await _scan(env, rewrite)
    assert r2["detection_id"] == r1["detection_id"], "改写扫描应复用原检测行"

    rows = env.execute("SELECT COUNT(*) AS n FROM detections").fetchone()["n"]
    assert rows == 1, "近重复改写不得新建检测行"

    # 完全不同的文本仍新建行
    r3 = await _scan(env, "今天天气很好我们去公园散步吧")
    assert r3["detection_id"] != r1["detection_id"]
    assert env.execute("SELECT COUNT(*) AS n FROM detections").fetchone()["n"] == 2


# ===========================================================================
# P3-7 OSS 资产就位
# ===========================================================================

def test_oss_assets_in_place():
    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("LICENSE", "pyproject.toml", "SECURITY.md",
                ".github/workflows/ci.yml", ".gitignore"):
        assert (root / rel).exists(), f"缺少 OSS 资产: {rel}"

    license_text = (root / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "falconeye" in license_text or "AGPL" in license_text, "LICENSE 应注明竞品思路借鉴声明"

    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "canaryguard-antifraud" in pyproject
    assert "mcp>=2.0,<3.0" in pyproject

    reqs = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "fastapi>=0.115.0,<1.0" in reqs, "requirements 应有版本上限"
    assert "mcp>=2.0.0,<3.0" in reqs

    ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "pytest" in ci and "3.12" in ci

    gi = (root / ".gitignore").read_text(encoding="utf-8")
    assert "scripts/_*.json" in gi


def test_readme_embedding_wording_accurate():
    readme = pathlib.Path(__file__).resolve().parents[1] / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert "SimHash 预筛 + 改写归一化" in text
    assert "embedding 精排为可选扩展" in text
    assert "SimHash→embedding" not in text, "README 不得再宣称 embedding 已实现（P3-1）"
