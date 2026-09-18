#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""导出主控 OpenAPI 3.x 文档到 docs/openapi.json（P9 交付物）。

用法（项目根）：
    .venv\\Scripts\\python.exe scripts\\export_openapi.py [--out docs/openapi.json]

说明：
  - 直接 import app.main 并调用 app.openapi()，无需启动服务器；
  - 输出为 FastAPI 生成的 OpenAPI 3.1 JSON（版本来自 app.__init__.__version__）；
  - 带基本结构自检：openapi 版本、info、paths 非空、operational 端点统计。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(prog="export_openapi.py", description="导出金丝雀蜜罐主控 OpenAPI 文档")
    parser.add_argument("--out", default=str(ROOT / "docs" / "openapi.json"), help="输出路径（默认 docs/openapi.json）")
    args = parser.parse_args()

    from app.main import app  # noqa: E402  （需项目根在 sys.path）

    schema = app.openapi()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 基本结构自检（验证可被 Swagger UI / 导入器解析）----
    checks = {
        "openapi 版本": schema.get("openapi", "").startswith("3."),
        "info 存在": bool(schema.get("info", {}).get("title")),
        "paths 存在": isinstance(schema.get("paths"), dict) and len(schema["paths"]) > 0,
        "components/schemas 存在": isinstance(schema.get("components", {}).get("schemas"), dict),
    }
    n_paths = len(schema["paths"])
    operations = sum(
        1
        for p in schema["paths"].values()
        for m in ("get", "post", "put", "patch", "delete")
        if m in p
    )
    print(f"[openapi] 已导出 -> {out}")
    print(f"[openapi] title={schema['info'].get('title')}  version={schema['info'].get('version')}")
    print(f"[openapi] paths={n_paths}  operations={operations}  schemas={len(schema.get('components', {}).get('schemas', {}))}")
    for name, ok in checks.items():
        print(f"[openapi] 自检 {name}: {'OK' if ok else 'FAIL'}")
        if not ok:
            return 1
    print("[openapi] 结构校验通过，可导入 Swagger UI / OpenAPI 客户端。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())