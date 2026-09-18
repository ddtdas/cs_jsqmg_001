"""聊天记录一键导入服务（ChatImportService，纯 Python 防御式）。

对应主方案 §4.1 collector/desensitize 衔接（CH-D 导入通道）：
  1) candidates()：探测微信/QQ 常见根路径（仅探测固定候选，不扫全盘），
     支持环境变量 AF_CHAT_ROOT 自定义根目录；
  2) scan(path)：识别平台（wechat/qq/generic）与消息库文件，探测 SQLCipher 加密
     （file:...?mode=ro 只读打开读 sqlite_master，异常即视为加密）；
  3) import_messages()：解析消息 → DesensitizeService 脱敏（正则层，确定性）→
     落库 detections(source='chat_import') → SpeechEngine().analyze_detection
     触发推理链 + 分级（LLM 不可用自动降级纯规则，D5）；
  4) parse_generic()：TXT 每行一条 / CSV / JSON 通用解析；
  5) clear_imported()：清空 chat_import 检测并写 gate_logs 哈希链审计
     （复用 app.services.audit_log.write_gate_log）。

设计原则：全程防御式——任何异常不外抛，回填 error/message 字段；
加密库给出明确提示，指引用户走微信/QQ 官方导出聊天记录后再导入。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .. import db as dbmod
from .audit_log import write_gate_log
from .desensitize import DesensitizeService
from .grading import GradingService
from .speech_engine import SpeechEngine

# 微信/QQ 常见根目录（相对当前用户主目录）
_WECHAT_ROOT_REL = os.path.join("Documents", "WeChat Files")
_QQ_ROOT_REL = os.path.join("Documents", "Tencent Files")

# 微信消息库文件名特征（Msg\Multi\MSG*.db）
_WECHAT_DB_PREFIX = "msg"
# QQ NT 消息库特征（nt_data 目录 / nt_msg*.db 命名）
_QQ_DB_MARKERS = ("nt_msg", "ntmsgb", "nt_msg_")

_GENERIC_SUFFIXES = (".txt", ".csv", ".json")
# 平台 → detections.platform（迁移后列，DEFAULT 'other'）
_PLATFORM_DB = {"wechat": "wechat", "qq": "qq", "generic": "other"}


def _fmt_time(value) -> str:
    """宽容格式化时间：unix 秒/毫秒 → 'YYYY-MM-DD HH:MM:SS'；其它原样字符串。"""
    if value in (None, ""):
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    if num <= 0 or num > 1e14:
        return str(value).strip()
    if num >= 1e12:  # 毫秒时间戳
        num /= 1000.0
    try:
        return datetime.fromtimestamp(num).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return str(value).strip()


def _db_uri(db_path: str) -> str:
    """Windows 路径 → SQLite URI（file:...?mode=ro，转义与中文/空格安全）。"""
    return Path(db_path).as_uri() + "?mode=ro"


def _db_encrypted(db_path: str) -> bool:
    """探测 SQLite 是否加密：只读打开读 sqlite_master，任何失败视为加密。"""
    try:
        con = sqlite3.connect(_db_uri(db_path), uri=True, timeout=3)
        try:
            con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return False
        finally:
            con.close()
    except Exception:
        return True


class ChatImportService:
    """聊天记录一键导入服务。"""

    def __init__(self) -> None:
        self._desensitize = DesensitizeService()
        self._engine = SpeechEngine()

    # ------------------------------------------------------------- 候选根路径
    def candidates(self) -> dict:
        """探测微信/QQ 常见根路径（不扫全盘）。返回 {wechat:[...], qq:[...], custom:''}。"""
        home = os.path.expanduser("~")
        wechat_roots = [os.path.join(home, _WECHAT_ROOT_REL)]
        qq_roots = [os.path.join(home, _QQ_ROOT_REL)]
        # OneDrive 文档重定向兜底（Win10/11 常见，仅当存在时补充）
        od = os.path.join(home, "OneDrive")
        if os.path.isdir(od):
            wechat_roots.append(os.path.join(od, _WECHAT_ROOT_REL))
            qq_roots.append(os.path.join(od, _QQ_ROOT_REL))
        custom = (os.environ.get("AF_CHAT_ROOT") or "").strip()
        return {
            "wechat": [p for p in wechat_roots if os.path.isdir(p)],
            "qq": [p for p in qq_roots if os.path.isdir(p)],
            "custom": custom,
        }

    # ------------------------------------------------------------- 平台识别
    def scan(self, path: str) -> dict:
        """识别平台与库文件。返回 {platform, db_files, encrypted}。"""
        p = Path(path)
        if not p.exists():
            return {"platform": "unknown", "db_files": [], "encrypted": False}
        lowered = str(p).lower()
        if "wechat files" in lowered:
            platform = "wechat"
            db_files = self._find_db_files(p, wechat=True)
        elif "tencent files" in lowered or "nt_qq" in lowered:
            platform = "qq"
            db_files = self._find_db_files(p, wechat=False)
        elif p.is_file() and p.suffix.lower() in _GENERIC_SUFFIXES:
            platform = "generic"
            db_files = []
        else:
            platform = "unknown"
            db_files = []
        encrypted = bool(db_files) and any(_db_encrypted(d) for d in db_files)
        return {"platform": platform, "db_files": db_files, "encrypted": encrypted}

    @staticmethod
    def _find_db_files(root: Path, wechat: bool) -> list[str]:
        """按平台特征收集消息库文件（去重保序）。"""
        found: list[str] = []
        if wechat:
            # 微信：Msg\Multi\MSG*.db（整树按文件名特征找，兼容大小写差异）
            for f in root.rglob("*"):
                if f.is_file() and f.suffix.lower() == ".db" \
                        and f.name.lower().startswith(_WECHAT_DB_PREFIX):
                    found.append(str(f))
        else:
            # QQ：nt_data 目录下的 nt_msg 消息库（含历史 Tencent Files 结构）
            for f in root.rglob("*.db"):
                if not f.is_file():
                    continue
                name = f.name.lower()
                parts = [part.lower() for part in f.parts]
                if any(m in name for m in _QQ_DB_MARKERS) or "nt_data" in parts:
                    found.append(str(f))
        return list(dict.fromkeys(found))

    # ------------------------------------------------------------- 通用解析
    def parse_generic(self, path: str) -> list[dict]:
        """通用格式解析：TXT 每行一条 / CSV（header content,sender,time 或默认第 1 列=content）
        / JSON（数组 [{sender,time,content}]）。返回 [{sender,time,content}, ...]。"""
        p = Path(path)
        suffix = p.suffix.lower()
        if suffix == ".txt":
            return self._parse_txt(p)
        if suffix == ".csv":
            return self._parse_csv(p)
        if suffix == ".json":
            return self._parse_json(p)
        raise ValueError(f"不支持的文件类型：{suffix or '(无后缀)'}")

    @staticmethod
    def _parse_txt(p: Path) -> list[dict]:
        out: list[dict] = []
        text = p.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if line:
                out.append({"sender": "", "time": "", "content": line})
        return out

    @staticmethod
    def _parse_csv(p: Path) -> list[dict]:
        out: list[dict] = []
        with p.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            rows = [r for r in csv.reader(fh) if r and any(c.strip() for c in r)]
        if not rows:
            return []
        header = [c.strip().lower() for c in rows[0]]
        if "content" in header:
            idx_c = header.index("content")
            idx_s = header.index("sender") if "sender" in header else None
            idx_t = header.index("time") if "time" in header else None
            data = rows[1:]
        else:
            idx_c, idx_s, idx_t = 0, None, None  # 默认第 1 列 = content
            data = rows
        for r in data:
            content = r[idx_c].strip() if idx_c < len(r) else ""
            if not content:
                continue
            sender = r[idx_s].strip() if idx_s is not None and idx_s < len(r) else ""
            t = r[idx_t].strip() if idx_t is not None and idx_t < len(r) else ""
            out.append({"sender": sender, "time": t, "content": content})
        return out

    @staticmethod
    def _parse_json(p: Path) -> list[dict]:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        if isinstance(data, dict):
            data = data.get("messages") or data.get("items") or data.get("list") or []
        if not isinstance(data, list):
            raise ValueError("JSON 结构不是消息数组")
        out: list[dict] = []
        for item in data:
            if isinstance(item, str):
                content, sender, t = item.strip(), "", ""
            elif isinstance(item, dict):
                content = str(item.get("content") or item.get("text") or "").strip()
                sender = str(item.get("sender") or item.get("from") or "").strip()
                t = str(item.get("time") or item.get("timestamp") or item.get("ts") or "").strip()
            else:
                continue
            if content:
                out.append({"sender": sender, "time": t, "content": content})
        return out

    # ------------------------------------------------------------- SQLite 消息库
    def _read_sqlite_messages(self, db_files: list[str], cap: int | None = None) -> list[dict]:
        """从明文 SQLite 消息库读取消息（跨库去重，cap 提前截断控内存）。"""
        messages: list[dict] = []
        seen: set[str] = set()
        for db_path in db_files:
            if _db_encrypted(db_path):
                continue
            try:
                rows = self._query_message_table(db_path)
            except Exception:
                continue  # 单个库异常不影响其它库
            for rec in rows:
                content = (rec.get("content") or "").strip()
                if not content or content.startswith("<"):
                    continue  # 跳过空内容与 XML 卡片消息（图片/链接等）
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                messages.append({
                    "sender": rec.get("sender") or "",
                    "receiver": rec.get("receiver") or "",
                    "time": rec.get("time") or "",
                    "content": content,
                })
                if cap and len(messages) >= cap:
                    return messages
        return messages

    @staticmethod
    def _pick_col(low: dict, *names: str) -> str | None:
        """按候选顺序取真实列名（low: 小写列名 → 真实列名）。"""
        for n in names:
            if n in low:
                return low[n]
        return None

    @staticmethod
    def _query_message_table(db_path: str) -> list[dict]:
        """读消息表：优先 message/MSG（微信）等常见表名，其次库内首张业务表。
        列名容错映射：content/strContent/msgContent/...、time/createTime/...、
        sender/strTalker/sendUin/...（返回 [{content, time, sender, receiver}]）。"""
        con = sqlite3.connect(_db_uri(db_path), uri=True, timeout=5)
        try:
            tables = [
                r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            if not tables:
                return []
            preferred = [t for t in tables if t.lower() in ("message", "msg", "msg_table", "msgtab")]
            table = preferred[0] if preferred else tables[0]
            quoted = '"' + table.replace('"', '""') + '"'
            cur = con.execute(f"SELECT * FROM {quoted}")
            names = [d[0] for d in cur.description]
            low = {n.lower(): n for n in names}
            c_col = ChatImportService._pick_col(
                low, "content", "strcontent", "msgcontent", "msgdata", "text", "strtext", "displaycontent")
            t_col = ChatImportService._pick_col(
                low, "time", "createtime", "msgtime", "sendtime", "strtime",
                "create_time", "msg_time", "send_time")
            s_col = ChatImportService._pick_col(
                low, "sender", "strtalker", "talker", "senduin", "senderuin",
                "fromuin", "senderid", "fromuser", "talkerid")
            r_col = ChatImportService._pick_col(
                low, "receiver", "receiveuin", "touin", "recvuser", "receiverid")
            if c_col is None:
                return []
            out: list[dict] = []
            while True:
                rows = cur.fetchmany(5000)
                if not rows:
                    break
                for row in rows:
                    rec = dict(zip(names, row))
                    content = str(rec.get(c_col) or "").strip()
                    if not content:
                        continue
                    out.append({
                        "content": content,
                        "time": _fmt_time(rec.get(t_col)) if t_col else "",
                        "sender": str(rec.get(s_col) or "") if s_col else "",
                        "receiver": str(rec.get(r_col) or "") if r_col else "",
                    })
            return out
        finally:
            con.close()

    # ------------------------------------------------------------- 导入主入口
    async def import_messages(self, path: str, limit: int = 2000, conn=None) -> dict:
        """解析 → 脱敏 → 落库 detections(chat_import) → SpeechEngine 推理链+分级。

        返回 {imported, suspicious, preview:[{sender,time,content_masked,grade}] 前 3 条}；
        加密库 → {imported:0, error:'encrypted_db', message:'...'}；
        任何异常捕获 → error 字段，不崩。
        """
        conn = conn or dbmod.get_conn()
        try:
            if not os.path.exists(path):
                return {"imported": 0, "error": "path_not_found",
                        "message": f"路径不存在：{path}"}
            info = self.scan(path)
            platform = info["platform"]
            if platform == "generic":
                messages = self.parse_generic(path)
            elif platform in ("wechat", "qq"):
                if not info["db_files"]:
                    return {"imported": 0, "error": "no_message_db",
                            "message": "未找到消息库文件"}
                plain = [d for d in info["db_files"] if not _db_encrypted(d)]
                if not plain:
                    return {"imported": 0, "error": "encrypted_db",
                            "message": "数据库加密，请用微信/QQ官方导出聊天记录后导入"}
                messages = self._read_sqlite_messages(plain, cap=limit)
            else:
                return {"imported": 0, "error": "unsupported_format",
                        "message": f"无法识别的聊天记录格式：{path}"}

            if not messages:
                return {"imported": 0, "suspicious": 0, "preview": []}

            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = 2000
            if limit <= 0:
                limit = 2000

            db_platform = _PLATFORM_DB.get(platform, "other")
            imported = 0
            suspicious = 0
            skipped = 0
            error_count = 0
            first_error = None
            preview: list[dict] = []

            for msg in messages[:limit]:
                raw = (msg.get("content") or "").strip()
                if not raw:
                    continue
                # 1) 脱敏（正则层，确定性，不依赖 LLM；LLM 复核由本服务按需触发）
                redacted = self._desensitize.regex_redact(raw)["redacted"].strip()
                if not redacted:
                    continue
                digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
                try:
                    # 2) 幂等：同文本（chat_import 来源）已存在则跳过，避免重复分析
                    dup = conn.execute(
                        "SELECT id FROM detections WHERE source='chat_import' AND text_hash=?",
                        (digest,),
                    ).fetchone()
                    if dup is not None:
                        skipped += 1
                        continue
                    cur = conn.execute(
                        "INSERT INTO detections (source, platform, text_hash, content, status) "
                        "VALUES ('chat_import', ?, ?, ?, 'pending')",
                        (db_platform, digest, redacted[:4000]),
                    )
                    conn.commit()
                    det_id = int(cur.lastrowid)
                    # 3) 推理链 + 分级（LLM 不可用内部自动降级纯规则，D5）
                    result = await self._engine.analyze_detection(det_id, conn)
                    imported += 1
                    if result.get("verdict") in ("suspicious", "fraud"):
                        suspicious += 1
                        # 与 scan 路径一致：调用 GradingService.finalize（含 P0 主动防御自动触发）
                        # 防御式：finalize 失败不影响导入
                        try:
                            GradingService().finalize(conn, detection_id=det_id)
                        except Exception:
                            pass
                    if len(preview) < 3:
                        preview.append({
                            "sender": str(msg.get("sender") or ""),
                            "time": str(msg.get("time") or ""),
                            "content_masked": redacted,
                            "grade": str(result.get("grade_hint") or result.get("severity") or ""),
                        })
                except Exception as exc:  # 单条失败不中断整体导入
                    error_count += 1
                    if first_error is None:
                        first_error = str(exc)
            conn.commit()
            out: dict = {"imported": imported, "suspicious": suspicious, "preview": preview}
            if skipped:
                out["skipped"] = skipped
            if error_count:
                out["error"] = "partial"
                out["message"] = f"{error_count} 条导入失败（首错：{first_error}）"
            return out
        except Exception as exc:  # 顶层兜底：任何异常不崩，回填 error
            return {"imported": 0, "error": "import_failed", "message": str(exc)}

    # ------------------------------------------------------------- 清空导入
    def clear_imported(self, conn, reason: str = "") -> int:
        """清空 chat_import 检测记录，写 gate_logs 审计（复用具审计函数）。返回删除条数。"""
        conn = conn or dbmod.get_conn()
        before_count = conn.execute(
            "SELECT COUNT(*) AS c FROM detections WHERE source='chat_import'"
        ).fetchone()["c"]
        cur = conn.execute("DELETE FROM detections WHERE source='chat_import'")
        deleted = int(cur.rowcount)
        write_gate_log(
            conn,
            action="chat_import.clear",
            payload_json=json.dumps({"source": "chat_import", "deleted": deleted}, ensure_ascii=False),
            before=json.dumps({"count": int(before_count)}, ensure_ascii=False),
            after=json.dumps({"count": 0}, ensure_ascii=False),
            reason=reason or "清空聊天记录一键导入产生的检测记录（chat_import）",
        )
        conn.commit()
        return deleted
