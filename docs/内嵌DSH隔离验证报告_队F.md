# 内嵌DSH隔离与可移植性验证报告

- **验证方**：金丝雀蜜罐 队F（内嵌DSH隔离与换环境验证工程师）
- **验证日期**：2026-09-16
- **验证对象**：远程 Windows 192.168.10.110（免密 SSH）· `C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`
- **基线**：9200 主控运行（health 200、`/api/v1/system/health` ok）；pytest 317 passed；admin key 取自 `data\bootstrap_admin_key.txt`（X-API-Key 头）
- **约束遵守**：未改任何产品代码；未重启主控；未动全局 3080（远程机无 3080，3080 为本机 GUI）；未提交 git；未执行 `stop.bat`（默认端口 3091 有误杀风险，见 P1-4，仅源码分析取证）

---

## 一、结论总览（P1×4 / P2×5，验证点：4 项完整通过 / 1 项部分通过 / 1 项不通过）

| 验证项 | 结果 | 一句话结论 |
|---|---|---|
| 1. 隔离性（3092 监听 / 引擎 0.1.5-rc.1 / 空白 dsh-home / sessions 项目内） | ✅ 通过 | 全部符合预期 |
| 2. token 鉴权（裸路径 401 / 日志 ?token= URL / 带 token 访问） | ⚠️ 部分通过 | 401、URL 打印、303→200 均实测通过；但 **token_url 生命周期存在 P1-2 缺陷** |
| 3. 启停循环（stop→start.mjs→stop→start.bat，WMI 常驻两段 SSH） | ❌ 不通过 | **`node dsh/start.mjs` 无法启动（P1-1）**；stop / start.bat / WMI 常驻段全部通过 |
| 4. 端口隔离（3090/3091/3092 互不干扰）+ 不读全局 settings/凭据 | ✅ 通过 | 凭据 refs 空、匿名 ID 不同、DSH_HOME 显式覆盖 |
| 5. 换环境模拟（复制 dsh/ 到临时目录验证引擎独立性） | ✅ 通过（含 P1-3） | 引擎 100% 自包含（本地 node_modules/home/fallback）；但复制需处理 junction（P1-3） |
| 6. 主控集成（/api/v1/system/embedded-dsh + ConsoleView.vue + dist chunk） | ✅ 通过 | 全链路正确（port=3092 running=true；默认 3092；dist 含新 chunk） |

**缺陷清单速览**：P1-1 start.mjs 无法启动（wscript 拦截）；P1-2 start.bat 重启后 token_url 失效（日志不落盘）；P1-3 换环境复制 dsh/ 因 junction 损坏引擎拒绝启动；P1-4 stop.bat 默认端口 3091（误杀无关服务且停不掉 3092）。P2×5 见缺陷清单。

**最终状态**：内嵌 DSH 已恢复运行（`start.bat --no-browser` 启动，PID 13076，`"D:\nodejs\node.exe" node_modules\@deepseek-ai\dsh\lib\bin.js --profile web --no-open --host 127.0.0.1 --port 3092`，与初始实例启动签名完全一致）；3090/3091/9200 全程未受影响；无残留 wscript/临时进程；`C:\Windows\Temp\dsh-portable` 已清理。

---

## 二、逐项验证

### 验证项 1：隔离性 — ✅ 通过

| 检查点 | 操作 | 预期 | 实测 | 结果 |
|---|---|---|---|---|
| 端口监听 | `Get-NetTCPConnection -State Listen`（SSH） | 3092 由内嵌实例监听 | `127.0.0.1:3092 OwningProcess=11548`（启动签名：`"D:\nodejs\node.exe" node_modules\@deepseek-ai\dsh\lib\bin.js --profile web --no-open --host 127.0.0.1 --port 3092`，cwd=dsh/） | ✅ |
| 引擎版本 | 读 `dsh/node_modules/@deepseek-ai/dsh/package.json` | 0.1.5-rc.1 | `ENGINE_NAME=@deepseek-ai/dsh VERSION=0.1.5-rc.1`（全新 npm 安装，非全局 0.1.1-rc.2） | ✅ |
| dsh-home 空白 | 读 `.credentials.yaml` / `settings.yaml` / plugins / sessions | refs 空、plugins 0、无外部凭据 | `.credentials.yaml` = `version:1 / refs: {}` + `records: client-connection/browser-session`（仅本地浏览器会话授权，无任何 API Key）；`settings.yaml` 仅 `permission: defaultPreset: danger-full-access`（160B，对比全局 9829B）；`plugins/` 0 项；`sessions/` 0 项；`storages/workspace.json` 空工作区 | ✅ |
| sessions 位置 | 递归列 dsh-home | 项目内 | `dsh/dsh-home/{plugins,profiles,sessions,storages}.anonymous-user-id,.credentials.yaml,settings.yaml`，全部位于项目内 | ✅ |
| 匿名身份隔离 | 对比全局/内嵌 `.anonymous-user-id` | 不同 | 全局 `2626e65d-5630-4ff8-9e49-82f2a284730d` vs 内嵌 `05054bf5-2751-4de7-818d-eafa2206a451` | ✅ |

补充证据：3092 进程 cwd 为 dsh/（命令行相对路径），引擎从本地 `dsh/node_modules` 加载；`dsh-home/profiles/web/cordis.yml` 等为引擎在项目内 home 物化的产物。

### 验证项 2：token 鉴权 — ⚠️ 部分通过（核心链路通，生命周期有缺陷）

| 检查点 | 操作 | 预期 | 实测 | 结果 |
|---|---|---|---|---|
| 裸路径在线 | `curl http://127.0.0.1:3092/` | 401（0.1.5 token 鉴权） | `HTTP_CODE=401`，body：`dsh web authentication required; reopen the URL printed by dsh web.` | ✅ |
| 日志含 ?token= URL | 读 `dsh/dsh-web.out.log` | 有 `?token=` URL | 用显式 node.exe+重定向启动后，约 40–80s 落盘：`dsh web: http://127.0.0.1:3092/?token=5XwYcOi37KMImyGZwVukxDt7RuMjc8dLQC-dQtcG-d0` | ✅（时机有延迟，见 P2-2 提示） |
| 带 token 访问 | `curl -i "<token_url>"` | 200 或 302→登录 | `HTTP/1.1 303 See Other` + `location: /` + `set-cookie: dsh-auth-…(HttpOnly; SameSite=Strict; Max-Age=2592000)`；带 cookie 跟随 → `200 OK text/html` | ✅（等效"302→登录"） |
| 错误 token | `curl "<url>/?token=WRONGTOKEN123"` | 401 | `HTTP_CODE=401` | ✅ |
| 主控 token_url | `GET /api/v1/system/embedded-dsh` | token_url 与日志一致 | 同步为 `http://127.0.0.1:3092/?token=5XwY…` | ✅（有日志时） |

**缺陷触发点（P1-2）**：`start.bat`/`start-embedded.ps1` 启动路径不写日志；实测 `stop → start.bat --no-browser` 重启后，日志中的旧 token 立即失效（`STALE_TOKEN_HTTP=401`），且新 token 无处落盘 → 后端 token_url 缺失或指向失效 token。详见缺陷清单。

### 验证项 3：启停循环 — ❌ 不通过（start.mjs 故障）

| 步骤 | 操作 | 预期 | 实测 | 结果 |
|---|---|---|---|---|
| 1 | `node dsh/stop.mjs` | 3092 释放 | `[dsh-stop] 已停止内嵌 DSH（PID 7976）`；端口 FREE；curl 000；主控 running=false | ✅ |
| 2 | `node dsh/start.mjs --no-browser` | 就绪 | **失败**：WMI 内层 `cmd /c set DSH_HOME=…&& "…bin.js" --profile web…` 被 `.js→JSFile→wscript.exe` 拦截（实测 wscript PID 13752 挂起，JScript 解析 Node ESM 文件），3092 从未监听，start.mjs 90s 超时退出 1，out/err 日志为空文件 | ❌ P1-1 |
| 3 | `node dsh/stop.mjs`（再停） | 3092 释放 | 端口释放（偶发"停止失败"误报，见 P2-2） | ✅ |
| 4 | `start.bat --no-browser` | 就绪 | `[dsh] WMI 创建成功… 就绪：http://127.0.0.1:3092/`；3092 LISTEN（PID 15016→13076）；curl 401 | ✅ |
| 5 | WMI 常驻（两段 SSH） | 断开后仍监听 | 第一段启动后断开；第二段全新 SSH 查 `3092_STILL_LISTENING pid=15016`，401 正常 | ✅ |

结论：推荐的命令行入口 `node dsh/start.mjs`（README 与脚本 help 均标注）在当前机器**无法启动**；`start.bat`（WMI 常驻）路径可用。根因见 P1-1。

### 验证项 4：端口隔离 + 不读全局凭据 — ✅ 通过

| 检查点 | 操作 | 预期 | 实测 | 结果 |
|---|---|---|---|---|
| 端口归属 | `Get-CimInstance Win32_Process` | 3090/3091/3092 为互不相关进程 | 3090=`D:\dsh_3080\deepseek_harness\dsh-app\node_modules\@deepseek-ai\dsh\lib\bin.js web --no-open --port 3090`（另一套全局 harness 实例）；3091=`D:\agent-lightning\tools\agl-ctl\index.mjs`（无关程序）；3092=项目内嵌；9200=主控（python -m app.run）。全程互不干扰 | ✅ |
| 全局 3080 | — | 远程机无 3080 | 远程机 3080 无监听（全局 3080 在本机 GUI）；内嵌与 3080 物理隔离 | ✅ |
| 不读全局 DSH_HOME | 检查用户级环境变量 + 启动器源码 | 显式覆盖 | 用户级 `DSH_HOME=D:\dsh_3080\deepseek_harness\dsh-home`（含 5 个真实 API Key，524B 凭据）；`start.mjs` 用 `set DSH_HOME=<项目内 dsh-home>&&…`、`start-embedded.ps1` 用 `$env:DSH_HOME='<项目内 dsh-home>'` **显式覆盖**；内嵌 home refs 恒空（对比全局 5 个 key） | ✅（用户级 DSH_HOME 存在风险提示见 P2-4） |
| 引擎解析隔离 | 读 `start.mjs resolveBin()` | 本地优先 | `if (fs.existsSync(LOCAL_BIN)) return LOCAL_BIN;` → 仅当本地缺失才回退 `DSH_ENGINE_BIN`，**无 PATH 全局回退**；两者皆无则报错退出 | ✅ |
| 进程环境 | 实测进程 cmdline/行为 | 使用项目内 home | 3092 进程命令行全部指向项目内路径；内嵌 home 被引擎实时写入（browser-session grant、cordis.yml 物化），全局 home 零改动 | ✅ |

### 验证项 5：换环境模拟（便携复制） — ✅ 通过（含 P1-3 注意事项）

| 检查点 | 操作 | 预期 | 实测 | 结果 |
|---|---|---|---|---|
| 复制 dsh/ → C:\Windows\Temp\dsh-portable（411.5MB，50,833 文件） | robocopy /E | 完整副本 | RC=1（成功）；临时引擎 `0.1.5-rc.1`、settings.yaml/ps1/bin 均在 | ✅ |
| 临时副本独立启动（DSH_PORT=3094，显式 node.exe） | 第一次直接启动 | 就绪 | **失败**：err 报 `dsh: …\dsh-portable\dsh-home\profiles\node_modules\@deepseek-ai\dsh exists and is not a symlink or dsh-managed module proxy; remove it so dsh can manage the installation fallback` | 见 P1-3 |
| 按引擎提示删除该目录后重试 | 删除复制的真实目录 | 引擎自愈 | **PORTABLE_READY=True**：3094 LISTEN（PID 8340，cmdline 指向临时副本本地引擎）、`3094_HTTP=401` | ✅ |
| 自包含性判定 | 全过程观测 | 无全局依赖 | 引擎/home/module-fallback 全部解析在临时副本内；`dsh-home/profiles/node_modules/@deepseek-ai/dsh` 原为 **Junction → 项目内 dsh/node_modules/@deepseek-ai/dsh**（自引用本地，非全局）；未读取 `D:\dsh_3080` 与用户级 DSH_HOME | ✅ |
| 清理 | 杀 3094、删临时目录、恢复 3092 | 无残留 | 3094 已杀、temp 已删、3092 经 start.bat 恢复运行 | ✅ |

结论：**引擎独立性成立**（换目录、换端口、本地 node_modules 优先，`DSH_ROOT/DSH_ENGINE_BIN/PATH` 不指向全局）；但**"复制即用"需处理 junction**（P1-3），README 未说明。

### 验证项 6：主控集成 — ✅ 通过

| 检查点 | 操作 | 预期 | 实测 | 结果 |
|---|---|---|---|---|
| 主控健康 | `GET /api/v1/system/health`（X-API-Key） | 200 ok | `{"ok":true,"data":{"status":"ok","version":"1.0.0","db":true,…}}` | ✅ |
| embedded-dsh 接口 | `GET /api/v1/system/embedded-dsh` | port=3092 running=true | `{"port":3092,"running":true,"url":"http://127.0.0.1:3092/","token_url":"…?token=…","log_path":"…dsh-web.out.log"}`；无 key → 401；停止后 running=false（实时反映） | ✅ |
| 后端实现 | 读 `app/api/system.py:87-135` | 探 3092 + 解析日志 | `port=3092`（硬编码，见 P2-5）；socket 探测 127.0.0.1:3092；正则 `http://127\.0\.0\.1:\d+/\?token=[A-Za-z0-9_-]+` 解析 out.log → token_url | ✅ |
| ConsoleView 默认 | 读 `webui/src/views/ConsoleView.vue` | 默认 3092 | `EMBEDDED_PORT=3092`、`DEFAULT_DSH=http://127.0.0.1:3092/`、`api.embeddedDsh()` 拉取 token_url（无手动地址时自动用 token 入口）、401 视为"实例在线" | ✅ |
| dist 产物 | grep dist/assets | 含新 chunk | `ConsoleView-D1vYK94l.js` 含 `w=3092`、`token_url`、`af_dsh_console_url`（构建时间 2026-09-16） | ✅ |
| 前端 API 客户端 | `webui/src/api/client.js:118` | `embeddedDsh()` | `embeddedDsh: () => request('/system/embedded-dsh')` | ✅ |

---

## 三、缺陷清单

### P1-1（阻断级）：`node dsh/start.mjs` 无法启动内嵌实例（.js 文件关联被 wscript 拦截）
- **位置**：`dsh/start.mjs` WMI 内层命令 `set DSH_HOME=…&& "<bin>" --profile web …`（bin = `…\node_modules\@deepseek-ai\dsh\lib\bin.js`）
- **现象**：Windows 上 `.js=JSFile`（PATHEXT 含 .JS）且 ftype 指向 WScript；`cmd /c "…bin.js"` 由 **wscript.exe**（JScript 引擎）执行 Node ESM 文件 → 语法错误挂起（桌面弹出错误对话框）；3092 永不监听；`start.mjs` 轮询 90s 后退出码 1；`dsh-web.out.log/err.log` 创建为空文件；残留 wscript 进程。
- **复现**：`assoc .js` → `JSFile`；`cmd /c ""…\bin.js" --version"` 挂死（实测 120s）；`node dsh/start.mjs --no-browser` 30s 内可见 wscript（PID 13752）挂起、curl 000。
- **修复建议**：内层命令显式调用 node：`set DSH_HOME=…&& "<process.execPath>" "<bin>" …`（`start-embedded.ps1` 已用 `& 'D:\nodejs\node.exe' …` 模式，实测有效）；并让 `waitPort` 超时分支回读 `dsh-web.err.log` 输出诊断信息。

### P1-2（阻断级）：`start.bat`/`start-embedded.ps1` 不写日志 → 重启后 token_url 缺失/失效，ConsoleView iframe 401
- **位置**：`dsh/start-embedded.ps1` WMI inner（无 `> dsh-web.out.log` 重定向，对比 `start.mjs` 有）
- **现象**：0.1.5 引擎**每次重启轮换** browser-session token（实测内嵌 `.credentials.yaml` 的 browser-session secret 在重启后由 `W0T1y-…` 变为 `y30fI7…`）；ps1 启动路径不落盘 → 无 out.log（初始部署即无，`token_url=""`）或残留旧日志（后端返回的 token 实测 `401`）。ConsoleView 默认 iframe 用后端 token_url → 401 墙；引擎 401 响应为纯文本（`reopen the URL printed by dsh web`），无交互登录页 → 用户无入口。
- **复现**：`stop → start.bat --no-browser` 重启后，`curl "<旧token_url>"` → 401；`GET /api/v1/system/embedded-dsh` 仍返回旧 token。
- **修复建议**：① start-embedded.ps1 inner 追加 `> "<dsh>\dsh-web.out.log" 2> "<dsh>\dsh-web.err.log"`（与 start.mjs 对齐）；② 或后端改读 `.credentials.yaml` 的 `client-connection/browser-session` grant 生成/校验 token；③ 或启动器支持 `--token` 固定 token 并持久化。

### P1-3（阻断级，仅换环境场景）：复制 dsh/ 目录后引擎拒绝启动（junction 被复制成真实目录）
- **位置**：`dsh-home/profiles/node_modules/@deepseek-ai/dsh`（原为 **Junction → 项目内 `dsh/node_modules/@deepseek-ai/dsh`**，引擎 `healProfilesModuleFallback` 管理）
- **现象**：普通复制（robocopy/copy）把 junction 复制成真实目录 → 引擎启动即退出：`Error: dsh: …\dsh-home\profiles\node_modules\@deepseek-ai\dsh exists and is not a symlink or dsh-managed module proxy; remove it so dsh can manage the installation fallback`。**实测**：按提示删除该副本目录后重试，引擎自愈重建 junction 并成功启动（3094 独立运行）→ 引擎本身自包含无碍，问题在复制方式。
- **复现**：`robocopy dsh → C:\Windows\Temp\dsh-portable /E` 后启动 → 上述错误。
- **修复建议**：README「换环境使用」补充复制注意事项（保留 junction 复制 / `/SL`；或复制后删除 `dsh-home\profiles\node_modules\@deepseek-ai\dsh` 让引擎自愈）；更稳做法是在 `start.mjs`/`start-embedded.ps1` 启动前检测"存在非 symlink 的 module proxy 目录"并自动删除触发 heal。

### P1-4（操作破坏级）：`dsh/stop.bat` 默认端口 3091，会误杀无关服务且停不掉 3092
- **位置**：`dsh/stop.bat`（`if "%DSH_PORT%"=="" set "DSH_PORT=3091"` → `node stop.mjs` 按 `DSH_PORT||3092` 取 3091）
- **现象**：直接运行 `stop.bat`（未设 DSH_PORT）会向 `stop.mjs` 传 3091 → taskkill 3091 端口进程（当前为 `D:\agent-lightning\tools\agl-ctl\index.mjs`，PID 13052，无关服务）；内嵌实例（3092）反而停不掉。start 默认 3092 与 stop 默认 3091 不一致（历史遗留，`cordis.patch.yml` 亦残留 3091，见 P2-1）。
- **复现**：源码路径分析 + 端口归属证据（**未实际执行**，避免误杀 3091 的 agl-ctl）。
- **修复建议**：`stop.bat` 默认改为 3092（与 start/README 一致），并统一清理 3091 残留（cordis.patch.yml）。

### P2-1：`dsh/cordis.patch.yml` 端口注释与值仍为 3091（与运行 3092 不符）
- **位置**：`dsh/cordis.patch.yml`（`webserver: host 127.0.0.1 port 3091`，注释"绑定独立端口 3091"）；README 表格写"绑定 127.0.0.1:3092"
- **现象**：当前 CLI `--port 3092` 覆盖 patch 生效（3092 监听）；若未来引擎改为优先读取 patch 端口且不带 `--port` 启动，将与 agl-ctl 的 3091 冲突。
- **修复建议**：patch 值与注释更新为 3092。

### P2-2：`stop.mjs` 竞态误报"停止失败"
- **位置**：`dsh/stop.mjs` taskkill 分支
- **现象**：实测一次报 `[dsh-stop] 停止失败：Command failed: taskkill /PID …`，但端口实际已释放（netstat 发现 PID 与 taskkill 之间进程已退出）。另一次正常。
- **修复建议**：taskkill 失败时复查端口，已释放则视为成功（返回 0）。

### P2-3：`start-embedded.ps1` 硬编码 `D:\nodejs\node.exe`（可移植性）
- **位置**：`dsh/start-embedded.ps1` inner：`& 'D:\nodejs\node.exe' …`
- **现象**：换机器后若 node 不在 `D:\nodejs`，`start.bat` 路径失效（README 已注明"可自行调整"，但未自动化）。
- **修复建议**：探测 `(Get-Command node).Source` 或 `$env:ProgramFiles\nodejs\node.exe` 兜底。

### P2-4：用户级环境变量 `DSH_HOME=D:\dsh_3080\deepseek_harness\dsh-home` 指向全局（含真实 API Key）
- **位置**：注册表用户环境变量（`USER_DSH_HOME`）；全局 home 含 `NEWAPI_API_KEY` 等 5 个真实 key + grants
- **现象**：内嵌启动器显式覆盖 DSH_HOME（已验证隔离生效）；但任何绕过启动器的直启（如直接 `node node_modules\…\bin.js`）会命中全局凭据/插件。
- **修复建议**：README 强调"必须经 `dsh\start.bat` 启动"；启动器在启动时校验/打印实际 DSH_HOME 来源便于审计。

### P2-5：后端 `embedded-dsh` 硬编码 `port=3092`，与 `DSH_PORT` 覆盖不一致
- **位置**：`app/api/system.py:101`（`port = 3092`）
- **现象**：README 支持 `set DSH_PORT=3093 && dsh\start.bat`，但后端始终上报 3092 → 前端 ConsoleView 指向错误端口。
- **修复建议**：后端读取环境变量 `DSH_PORT`（默认 3092）或扫描探测。

---

## 四、对标差异（ScamIntelli 11 层引擎）

> 说明：队F 未持有 ScamIntelli 11 层引擎的正式文档，以下按框架层面对照其"引擎隔离/运行时隔离"相关维度；精确校准需队长提供 ScamIntelli 文档后复核。

| 维度 | ScamIntelli 11 层引擎（待校准） | 金丝雀蜜罐内嵌 DSH | 差异/差距 |
|---|---|---|---|
| 运行时隔离 | （待文档） | 独立端口 3092 + 独立进程（WMI 常驻）+ 独立 DSH_HOME | 满足"进程/端口/配置"三层隔离；缺 scoped 凭据绑定层 |
| 凭据隔离 | （待文档） | `.credentials.yaml` refs 空（默认零凭据），依赖用户显式配置 | 强（零凭据启动）；但依赖"必须走启动器"这一软约束（P2-4） |
| 引擎自包含 | （待文档） | 本地 node_modules（0.1.5-rc.1）+ LOCAL_BIN 优先 + 无 PATH 全局回退 | 强；复制场景需处理 junction（P1-3） |
| 会话隔离 | （待文档） | sessions/storages/profile 全在 `dsh/dsh-home` 项目内 | 满足；匿名身份与全局不同（05054bf5 vs 2626e65d） |
| 可移植性 | （待文档） | 硬编码 `D:\nodejs\node.exe`（P2-3）、复制 junction 需处理（P1-3）、start.mjs 不可用（P1-1） | 弱于"即拷即用"预期，需修复 |
| token 生命周期 | （待文档） | 0.1.5 token 每次重启轮换，日志驱动 token_url（P1-2） | 明显弱：无持久/固定 token 机制 |

---

## 五、更新/修复方案建议（按优先级）

1. **P1-1**（30 分钟内可修）：`start.mjs` 内层命令改用 `process.execPath`（node.exe）显式执行 bin.js；`waitPort` 超时分支回读 err.log。
2. **P1-2**：`start-embedded.ps1` inner 追加 `> dsh-web.out.log 2> dsh-web.err.log`；后端 token_url 解析逻辑不变即可恢复（start.bat 路径从此也能落盘 token）。
3. **P1-4**：`stop.bat` 默认端口改 3092（一行）；同步清理 `cordis.patch.yml` 的 3091 残留（P2-1）。
4. **P1-3**：README 换环境章节补 junction 说明；启动器加"非法 module proxy 自动删除触发 heal"。
5. **P2 批量**：stop.mjs 竞态复查端口；ps1 node 路径探测；后端 port 读 DSH_PORT；README 强调必须走启动器。
6. 修复后回归：重跑本报告验证项 3（启停循环应全绿）与验证项 2（start.bat 重启后 token_url 应有效）。

---

### 附：本次验证执行痕迹（供队长核验）
- 3092 恢复命令（与 start-embedded.ps1 等效，仅加日志重定向）：`cmd /c set DSH_HOME=<项目>\dsh\dsh-home&& "D:\nodejs\node.exe" "<项目>\dsh\node_modules\@deepseek-ai\dsh\lib\bin.js" --profile web --no-open --host 127.0.0.1 --port 3092 > dsh-web.out.log 2> dsh-web.err.log`（WMI 创建）
- 便携验证：`robocopy dsh C:\Windows\Temp\dsh-portable /E` → 删除 `dsh-home\profiles\node_modules\@deepseek-ai\dsh` 副本 → 3094 启动（401）→ 停止 → 清理 → 恢复 3092
- 全程未改动任何产品代码文件；临时文件均已清除。
