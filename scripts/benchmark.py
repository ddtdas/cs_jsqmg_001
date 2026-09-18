#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P3-2 评估基准（benchmark）：话术 P/R + 踩饵对抗梯度（改 3/5/10 字、换说法）。

用法（项目根）:
    .venv\\Scripts\\python.exe scripts\\benchmark.py [--out var/benchmark.json]

输出: var/benchmark.json（结构化 JSON 报告）。
- 话术 P/R：41 条种子词库 + 10 条已知诈骗话术（正例）与 10 条正常语料（负例），
  纯规则模式（LLM 强制降级，保证确定性可复现）。
- 踩饵对抗梯度：对蜜饵原文做 3/5/10 字改写、标点/换说法，统计召回。
- 运行使用临时数据目录，不污染真实 data/。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCAM_TEXTS = [
    "稳赚不赔的理财项目，导师带你内幕消息，今天就能翻倍",
    "刷单返利，先垫付，解锁任务后日结佣金",
    "客服主动退款，点击理赔链接，双倍退款",
    "你涉嫌洗钱，尽快把资金转到安全账户配合调查",
    "请立即注销校园贷，否则影响你的征信记录",
    "网恋对象找我借钱，说家人急需用钱，让我转账",
    "包赔保本高息，投资虚拟货币翻倍收益",
    "冒充公检法的人让我交保证金到安全账户",
    "提现被冻结，交一笔解冻费才能提现，还有加密货币提现手续费",
    "足不出户打字员兼职，先缴纳保证金激活任务",
]

NORMAL_TEXTS = [
    "你好，明天下午三点开会，记得带笔记本",
    "这道数学题我解出来了，答案是42",
    "周末一起去爬山吧，天气不错",
    "项目进度报告已发到群里，请查收",
    "今天食堂的菜不错，我吃了两碗饭",
    "图书馆新到的书我借了一本，挺好看的",
    "天气预报说明天有雨，记得带伞",
    "新开的咖啡馆环境很好，适合写代码",
    "这周末有足球赛，一起去体育馆看吗",
    "我把会议纪要整理好了，发你邮箱了",
]

BAIT = "你好呀，我在网上看到一个很不错的兼职项目，感觉超适合你，我们加个V聊聊怎么样"


class _FakeFailProvider:  # 强制纯规则（D5 降级链），保证基准确定性
    async def complete(self, *, system, user, schema=None, temperature=0):
        from app.services.llm_provider import DegradedError

        raise DegradedError("benchmark: force rules-only mode")


def _run_benchmark() -> dict:
    from app import db as dbmod
    from app.config import get_settings
    from app.services import llm_provider
    from app.services.speech_engine import SpeechEngine
    from app.services.trap_engine import TrapEngine

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="af_benchmark_"))
    import os

    os.environ["AF_DATA_DIR"] = str(tmp)
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(_FakeFailProvider())
    dbmod.ensure_schema()
    conn = dbmod.get_conn()

    import asyncio

    engine = SpeechEngine()

    async def scan(text: str) -> dict:
        return await engine.scan_text(text, source="manual", conn=conn)

    # ---- 话术 P/R（纯规则）----
    tp = fp = fn = tn = 0
    scan_details: list[dict] = []
    for t in SCAM_TEXTS:
        r = asyncio.run(scan(t))
        hit = r["verdict"] in ("suspicious", "fraud")
        tp += int(hit)
        fn += int(not hit)
        scan_details.append({"text": t[:18], "verdict": r["verdict"], "rule_score": r["rule_score"]})
    for t in NORMAL_TEXTS:
        r = asyncio.run(scan(t))
        tn += int(r["verdict"] == "normal")
        fp += int(r["verdict"] != "normal")

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    # ---- 踩饵对抗梯度 ----
    trap = TrapEngine()
    variants = {
        "exact原文": BAIT,
        "改3字": BAIT.replace("很不错", "挺好").replace("感觉超适合", "挺适合"),
        "改5字": "你好呀，我看到一个很不错的兼职项目，感觉超适合你，我们加个V聊聊怎么呀",
        "改10字": "嗨，我最近在网上发现一个很不错的兼职项目，感觉挺适合你，加个V聊呗",
        "标点换说法": "你好呀，我在网上看到一个不错的兼职项目，感觉适合你，加V聊聊怎么样好不好？",
        "无关文本": "今天天气很好我们去公园散步吧",
    }
    gradient: dict[str, dict] = {}
    for name, cand in variants.items():
        t = trap.create_draft(conn, bait_text=BAIT)
        trap.deploy(conn, t["id"])
        trap.monitor(conn, t["id"])
        r = asyncio.run(trap.check_hit(conn, t["id"], cand))
        gradient[name] = {
            "hit": r["hit"],
            "matched_by": r.get("similarity", {}).get("matched_by", []),
            "hamming": r.get("similarity", {}).get("hamming"),
            "overlap": r.get("similarity", {}).get("overlap"),
        }

    dbmod.close_all()
    get_settings.cache_clear()
    llm_provider.set_provider(None)
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "speech_pr": {
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
            "per_case": scan_details,
        },
        "trap_gradient": gradient,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="benchmark.py", description="金丝雀蜜罐评估基准（P3-2）")
    parser.add_argument("--out", default=str(ROOT / "var" / "benchmark.json"))
    args = parser.parse_args()

    report = _run_benchmark()
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    pr = report["speech_pr"]
    print(f"[benchmark] 话术 P={pr['precision']:.2f} R={pr['recall']:.2f} F1={pr['f1']:.2f} "
          f"(tp={pr['tp']} fp={pr['fp']} fn={pr['fn']} tn={pr['tn']})")
    grad = report["trap_gradient"]
    print("[benchmark] 踩饵梯度召回: "
          + " ".join(f"{k}={'Y' if v['hit'] else 'N'}" for k, v in grad.items()))
    print(f"[benchmark] 报告 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())