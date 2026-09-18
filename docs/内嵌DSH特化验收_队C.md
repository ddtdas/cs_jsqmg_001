# 内嵌 DSH 特化验收报告（队C · 验收-3）

- **验收对象**：金丝雀蜜罐 内嵌自包含 DSH 实例（dsh/ 目录，端口 3092）
- **验收人**：tester-c（队C，独立复核）
- **验收时间**：2026-09-17 13:15 – 13:40
- **环境**：远程 192.168.10.110（根 `C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`）；主控 9200 health 200；内嵌 DSH 3092 运行中
- **方法**：只读检查 dsh/ 配置与 dsh-home、调 /system/embedded-dsh、检查前端 ConsoleView 集成、远程本机探测 3092；**未改任何文件、未重启内嵌实例、未提交 git**

---

## 一、总体结论

**内嵌 DSH 是特化给本项目使用的独立实例**（非通用裸壳、非连接全局 3080）：

| 维度 | 结论 |
|---|---|
| 特化配置层（端口/patch/启动器/README） | ✅ 项目专属二次开发层完整 |
| 隔离性（DSH_HOME/引擎/插件/junction/凭据） | ✅ 与全局 3080 完全隔离 |
| ConsoleView 集成（默认 3092 + 消费 token_url） | ✅ 前端指向本项目实例 |
| 主控联动（system/embedded-dsh 状态/Token 互通） | ✅ 只对接 3092 |
| 残留项 | ⚠️ 仅 .credentials.yaml 运行 grant（P2，可清理，不影响特化） |

**P0/P1：无。P2：2 项（见三）。可交付/特化成立。**

---

## 二、逐项验收

### 1. 特化配置（项目专属二次开发层）✅

| 文件 | 实测 | 判定 |
|---|---|---|
| `dsh/cordis.patch.yml` | 仅一条 patch：`webserver host=127.0.0.1 port=3092`（绑定独立端口） | ✅ 专属端口 |
| `dsh/start.mjs`（143 行） | 引擎=`<proj>/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js`（0.1.5-rc.1 全新干净、自包含）；DSH_HOME=`<proj>/dsh/dsh-home`（显式 set，避免读全局 DSH_HOME=D:\dsh_3080）；patch=cordis.patch.yml；端口默认 3092 且 `CANARY_DSH_PORT` 可覆盖；WMI (Win32_Process.Create) 分离进程；**显式 node.exe** 启动（P1-1 修复 .js→wscript 拦截）；stdout/stderr 重定向 UTF-8 日志 | ✅ 二次开发层完整 |
| `dsh/start-embedded.ps1`（84 行） | 同端口口径 3092/CANARY_DSH_PORT；WMI + 显式 node；**junction 自动修复**（P1-3：profiles/node_modules/@deepseek-ai/dsh 若被复制展开成真实目录则重建 junction）；UTF-8 日志强制（Out-File:Encoding=utf8） | ✅ |
| `dsh/start.bat` / `stop.bat` / `stop.mjs` | 启停一致：默认端口 3092，`CANARY_DSH_PORT` 可覆盖；stop 按端口找 PID → taskkill /T /F | ✅ |
| `dsh/README.md`（70 行） | 明确"本项目自带的干净独立 DSH（DeepSeek Harness），**绝不连接设备全局 3080**"；换环境即用说明、junction 修复说明、文件说明表 | ✅ 特化文档完整 |

### 2. 隔离确认 ✅

| 检查项 | 实测 | 判定 |
|---|---|---|
| dsh-home 空白 | `settings.yaml` 仅 `permission.defaultPreset: danger-full-access` + onboarding 版本号（**无任何模型/API Key 配置**）；`storages/workspace.json` 空表（无会话） | ✅ |
| plugins | **plugins 目录不存在（0 个插件）**，无全局插件 | ✅ |
| junction 检查 | `dsh-home/profiles/node_modules/@deepseek-ai/dsh` → **LinkType=Junction，Target=`<proj>\dsh\node_modules\@deepseek-ai\dsh`**（指向项目内引擎，**非 D:\dsh_3080**） | ✅ |
| node_modules 无污染 | `dsh/node_modules` 下**无任何指向 D:\dsh_3080 的 junction**（扫描无 LINK 项） | ✅ |
| 运行实例 | 3092 监听 PID 4188：`"D:\nodejs\node.exe" node_modules\@deepseek-ai\dsh\lib\bin.js --profile web --no-open --host 127.0.0.1 --port 3092`（cwd=项目 dsh/）；对比全局 3080 PID 8540：`D:\dsh_3080\deepseek_harness\dsh-app\node_modules\...\bin.js --trusted-host 192.168.10.110 --port 3080` —— **两个完全独立进程** | ✅ |
| /system/embedded-dsh | 实测返回 `{"port":3092,"running":true,"url":"http://127.0.0.1:3092/","token_url":"http://127.0.0.1:3092/?token=bDZpsyLoILKAl0Q5fUoofJrShiyAwpv9dFy6j8Ov_Ls","log_path":"...\dsh\dsh-web.out.log"}` —— **链接的是本项目 3092 而非全局 3080** | ✅ |
| token_url 有效性 | 远程本机 `http://127.0.0.1:3092/?token=...` → **200**（27660 字节页面）；裸路径 → 401（0.1.5 token 鉴权在线正常） | ✅ |

### 3. ConsoleView 集成 ✅

`webui/src/views/ConsoleView.vue`：
- `const EMBEDDED_PORT = 3092`；`const DEFAULT_DSH = http://127.0.0.1:${EMBEDDED_PORT}/`（默认 iframe 指向 **3092，非 3080**）
- 挂载时 `await api.embeddedDsh()` → `state.embedded.running/tokenUrl`，若用户未自定义 URL 则自动用 `d.token_url` 作为 iframe src
- 界面提示："默认内嵌**项目自包含独立 DSH**（dsh/，端口 3092，全新 0.1.5 引擎 + 空白 dsh-home，与设备全局 3080 完全隔离）"
- `webui/src/api/client.js`：`embeddedDsh: () => request('/system/embedded-dsh')` —— 端点被 ConsoleView 消费 ✅

### 4. 主控联动 ✅

`app/api/system.py` `GET /api/v1/system/embedded-dsh`（require_admin）：
- 端口解析口径与 dsh 启动脚本一致：**优先 `CANARY_DSH_PORT` → 读 `dsh/cordis.patch.yml` 的 webserver port → 默认 3092**；**明确不读全局 `DSH_PORT`/`GLOBAL_DSH_PORT`**（防根脚本环境变量污染误报 3080）
- `running` 按上述端口探测监听；`token_url` 从 `dsh/dsh-web.out.log` 解析（0.1.5 token 轮换 URL 落盘）
- 实测：200，port=3092 running=true token_url 指向 3092 —— **主控↔内嵌 DSH 状态/Token 互通正常** ✅

### 5. 清理残留（只报告，未改动）⚠️

| 残留项 | 位置 | 说明 | 判定 |
|---|---|---|---|
| `.credentials.yaml`（161B） | dsh-home/ | 仅 `client-connection/browser-session` grant（引擎运行自动生成的本地浏览器会话授权，含 secret，**无任何 API Key**） | ⚠️ 可清理项：停止实例后删除不影响特化；属运行期凭据，随实例重置自动再生 |
| `profiles/node_modules/` | dsh-home/profiles/ | 引擎 profile 依赖（zod/express/openai/@deepseek-ai/dsh-* 等，junction 目标指向引擎目录） | ✅ 正常（引擎模块布局，非污染） |
| `.anonymous-user-id`（37B） | dsh-home/ | 匿名访问标识 | ✅ 无害 |
| `dsh-web.err.log` | dsh/ | **空**（无错误） | ✅ |
| `dsh-web.pid` | dsh/ | 运行标记（wmi-managed） | ✅ 正常 |

---

## 三、问题清单

### P0（阻断）：无
### P1（高）：无
### P2（低，不阻塞特化用途）

| # | 项 | 说明 | 建议 |
|---|---|---|---|
| P2-1 | dsh-home/.credentials.yaml 运行残留 | 含 `browser-session` grant secret（161B），为引擎运行自动生成，不含 API Key | 停止实例后可清理（仅报告，未改动）；若要求交付时"绝对空白"，在最终打包前删除即可 |
| P2-2 | 3092 仅绑定 127.0.0.1 | 内嵌实例只监听本机回环（特化隔离正确）；远程机器无法直接浏览器访问 3092 | 符合设计（隔离）；如需远程访问可经 SSH 隧道或主控代理，不建议放开绑定 |

---

## 四、验收结论

1. **特化成立**：内嵌 DSH = 本项目专属二次开发层（专属端口 3092 + 专属 DSH_HOME + 全新 0.1.5-rc.1 引擎 + cordis.patch.yml + WMI 启动器 + junction 自修复 + UTF-8 日志 + 完整 README），**不是通用裸壳**。
2. **完全隔离**：与设备全局 3080（D:\dsh_3080 实例，PID 8540）无共享 DSH_HOME/引擎/插件/凭据/端口；dsh/node_modules 无指向 D:\dsh_3080 的 junction。
3. **集成闭环**：主控 `/system/embedded-dsh` 与前端 ConsoleView（默认 3092 + 消费 token_url）构成"命令输入页内嵌本项目 DSH"的完整链路，实测 token_url 有效（200）。
4. **残留**：仅 .credentials.yaml 运行 grant（P2-1，可清理），不影响特化用途。
5. **交付判定**：**通过（可交付）**。P0/P1 无，P2 项不影响特化结论。

（完）