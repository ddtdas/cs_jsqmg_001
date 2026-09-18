# 反钓鱼伪装验收报告（队C · 双盲交叉验证）

> 验收对象：蜜罐反钓鱼伪装（Cover Identity，P19）——A 队后端 + B 队前端的独立复核（双盲：本队未参与实现，仅依据设计文档 `PLAN-蜜罐反钓鱼伪装.md` 独立验证）
> 验收人：cover-tester（队C）
> 验证方式：只读验证 + 实测（不改产品代码、不重启主控、不提交 git）
> 验证环境：远程 192.168.10.110（项目根 `C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`），主控 9200
> 验证时间：2026-09-17

---

## 一、总体结论

| 项 | 结论 |
|---|---|
| 后端 /cover 8 端点 | ✅ 全部通过（53/53 断言 PASS，实时 API + 落库双查） |
| 前端契约 8 方法 ↔ 8 端点 | ✅ 一一对应（CoverView.vue + client.js） |
| 专项测试 test_p19_cover.py | ✅ 17 条独立运行全绿 |
| 全量回归 pytest | ✅ **479 passed**（基线 462 + 新增 17，≥462+14 达标） |
| 交付判定 | ✅ **可交付**（附 3 项改进建议：1 P1 + 2 P2，均不影响核心验收项） |

---

## 二、后端独立验证（实时 API + 落库双查，53/53 PASS）

### 2.1 创建伪装（layer 自动 +1 / name 唯一）

| 用例 | 结果 | 证据 |
|---|---|---|
| 顶层创建 → layer=1, enabled=1, parent_id=None | ✅ | POST /api/v1/cover/identities → `{"identity": {...,"layer":1,...}}` |
| 子伪装 parent_id=顶层 → layer=2 | ✅ | `layer == parent.layer+1 == 2` |
| 孙伪装 parent_id=子层 → layer=3（三层嵌套） | ✅ | `layer == 3` |
| name 重复 → 409 | ✅ | `HTTP 409 {"error":{"code":"identity_name_exists"}}` |
| 假信息字段（fake_phone 等）保留 | ✅ | 创建响应回显明文（用户自配，合规允许） |

### 2.2 列表 / 主动暴露

| 用例 | 结果 | 证据 |
|---|---|---|
| GET /cover/identities 嵌套层/被接触次数 | ✅ | 列表含 `layer` / `contact_count` / `enabled`；顶层被接触 0 次 |
| POST /identities/{id}/expose → 脱敏假信息 | ✅ | 返回 checklist：`fake_phone masked=13*********（首2尾2掩码）`，`value` 为原文（供 HITL 回复骗子）；写 `events(kind='cover_expose')` |

### 2.3 接触登记（脱敏 + IOC + 锁定范围）

| 用例 | 结果 | 证据 |
|---|---|---|
| POST /cover/contact（骗子手机号）→ 脱敏落库 | ✅ | 响应 `contact_value_mask=139********（3+4+4）`；SQLite 落库值即脱敏值，**明文不落库** |
| IOC 提取 | ✅ | 手机号命中 → `events(kind='cover_contact')`，payload 含 `iocs[{ioc_type, mask}]`（掩码） |
| layer_at 记录 | ✅ | 接触时伪装层记录（L1/L2 均验证） |
| 同号第 2 次 → 锁定 | ✅ | 第 2 次响应 `escalated=1`；`events(kind='cover_locked')` payload `{count:2, contact_value_mask}`；同值行全部 `escalated=1` |
| 微信接触（不同值） | ✅ | 不误锁（escalated=0），类型/来源正常 |

### 2.4 查询与过滤

| 用例 | 结果 | 证据 |
|---|---|---|
| GET /cover/contacts | ✅ | id 倒序；按 `identity_id` / `contact_type` / `escalated` 过滤均正确 |
| GET /cover/map | ✅ | `total_contacts`；`layers[{layer, contacts}]`（L1/L2 分布正确）；`tree` 嵌套（顶层→转介绍B）正确 |

### 2.5 安全闸控

| 用例 | 结果 | 证据 |
|---|---|---|
| DELETE 无 confirm → 403 | ✅ | `confirm_required` |
| DELETE confirm+reason≥20 → 成功 | ✅ | 删除成功后下层 parent 置空，接触事件保留（审计） |
| 无 key → 401（GET/POST/DELETE） | ✅ | `missing_api_key` |
| 错误 key → 401 | ✅ | `invalid_api_key` |
| toggle 无 confirm → 403；confirm+reason → 翻转 | ✅ | enabled 1→0；gate_logs 写 `before/after`（哈希链审计） |

### 2.6 数据模型 / 看板联动

- 表迁移：`cover_identities` + `contact_events` + 3 个索引（identity/value/escalated）✅，`TABLE_NAMES=24`（22→24）✅
- 看板联动：`GET /api/v1/system/stats` 返回 `cover_identities` / `contact_events` 真实计数 ✅

---

## 三、前端契约核对（CoverView.vue + client.js ↔ 后端 8 端点）

### 3.1 方法 ↔ 端点一一对应（8↔8 全对上）

| client.js 方法 | 后端端点 | CoverView.vue 使用 |
|---|---|---|
| `coverIdentities()` | GET /api/v1/cover/identities | loadIdentities |
| `coverCreate(p)` | POST /api/v1/cover/identities | submitCreate |
| `coverToggle(id)` | POST /api/v1/cover/identities/{id}/toggle | onToggle（启用开关） |
| `coverDelete(id,p)` | DELETE /api/v1/cover/identities/{id} | submitDelete（confirm+reason 弹窗） |
| `coverExpose(id)` | POST /api/v1/cover/identities/{id}/expose | openExpose（脱敏清单弹窗） |
| `coverContact(p)` | POST /api/v1/cover/contact | submitContact（登记接触弹窗） |
| `coverContacts(q)` | GET /api/v1/cover/contacts | loadContacts |
| `coverMap()` | GET /api/v1/cover/map | loadMap |

- 路由：`webui/src/router/index.js` 注册 `/cover` → CoverView.vue ✅
- 构建产物：`webui/dist/assets/CoverView-*.js` 含 cover 代码 ✅；主控 SPA 静态托管 + `/ui/cover` 返回 200 ✅
- 认证/封包/错误处理：与 client.js 统一协议一致（X-API-Key、401 清凭据、`{ok,data}` 解包）✅
- 删除弹窗实现了 confirm+reason≥20 审计；暴露弹窗 HITL 提示合规；加载/错误/空态齐全 ✅

### 3.2 发现项（前端侧，不影响后端验收结论）

- **P1-01 启用开关（toggle）无法从 UI 生效**：`api.coverToggle(id)` 不发送 `confirm+reason` body，而后端 toggle 按设计强制双确认（无 body → 403 `confirm_required`）。UI 开关点击后必然报错回滚。**合入前请修复**：client.js `coverToggle` 传 `{confirm:true, reason}` 或在 CoverView 增加与删除一致的确认弹窗（建议做法：开关点击走确认弹窗，reason≥20）。
- **P2-01 接触记录「锁定状态」过滤参数不匹配**：前端 `contactQuery()` 发 `locked=true/false`，后端参数名为 `escalated`，过滤静默失效（返回全量）。改前端参数或后端收 `locked` 别名。
- **P2-02 暴露清单解包路径与后端不一致**：后端返回 `{exposure:{checklist:[{field,label,value,masked}]}}`；CoverView `normalizeExposed` 解的是 `d.masked/d.exposures/d.items`，未命中嵌套 `d.exposure.checklist`，实际回退到按行内 fake_* 客户端掩码兜底展示（脱敏展示可用、复制按钮复制的是掩码）。建议解包 `res.exposure.checklist`，展示 masked、复制 value（用户自配假号原文，HITL 场景需要）。

---

## 四、回归

| 项 | 结果 |
|---|---|
| `pytest tests/test_p19_cover.py -q`（独立运行） | ✅ **17 passed**（3.68s） |
| 全量 `pytest -q`（独立运行，远程 venv） | ✅ **479 passed**（76.69s，1 warning 为 anyio 弃用提示，非失败） |
| 基线要求 | 462 基线 + new ≥14 → **479 ≥ 476 达标** |

## 五、工作方式合规

- 只读验证 + 实测：未修改任何产品代码；未重启主控；未 git 提交
- 验收期间产生的本队测试数据（伪装身份/接触事件/事件/审计日志）已按本队前缀自清理；实时库中另有其他队/流程（如 E2E）的数据未触碰
- 关键证据：本目录 `c_verify_live.py`（53 断言脚本）+ `c_live_verify_out.txt`（PASS 明细）+ `pytest_cover_out.txt` / `pytest_full_verify.txt`（回归输出）

## 六、结论

**反钓鱼伪装（Cover Identity / P19）达到可交付状态。**

后端全部 8 端点行为、脱敏落库（明文不落库）、IOC 提取、同号锁定升级（escalated + cover_locked）、嵌套伪装地图、敏感操作双确认审计、看板 stats 联动均独立验证通过；前端 8 方法 ↔ 8 端点契约完整对应且已部署。回归全绿（专项 17 + 全量 479）。合入前建议修复 P1-01（toggle 前端确认）并择机处理 P2-01/P2-02 三个前端体验项。

— cover-tester, 队C