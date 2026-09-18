"""社工库情报 API：query / analyze / status。

依赖服务层 app/services/soc_lib.py（SocLibService：HIBP + 本地 leak_records，
任何源不可用一律降级 {source:'none', found:false}，绝不抛异常）。
三个端点全部 require_admin（D6）；统一 {ok,data}/{ok:false,error:{code,message}}
封包（MCP 透传协议 §7.1）。合规边界（D7 最小化）：只查嫌疑对象、结果仅掩码 IOC、
明文不落库。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..db import get_db
from ..deps import require_admin
from ..services.soc_lib import SocLibService
from ..utils import ApiError, ok

router = APIRouter(prefix="/soc-lib", tags=["soc-lib"])


class SocLibQueryRequest(BaseModel):
    """社工库查询入参（三选一）：{email} / {phone} / {ioc_type, ioc_value}。"""

    email: str | None = None
    phone: str | None = None
    ioc_type: str | None = None
    ioc_value: str | None = None


@router.post("/query", dependencies=[Depends(require_admin)])
def soc_lib_query(payload: SocLibQueryRequest) -> dict:
    """社工库情报查询：HIBP（email）→ 本地 leak_records（sha256 匹配 ioc_value_hash）。

    返回 {result}；body 三选一：{email} / {phone} / {ioc_type, ioc_value}
    （ioc_type 非 email 时按 phone 走本地库 sha256 匹配）。
    """
    email = (payload.email or "").strip()
    phone = (payload.phone or "").strip()
    ioc_type = (payload.ioc_type or "").strip().lower()
    ioc_value = (payload.ioc_value or "").strip()
    if email:
        result = SocLibService().query(email=email)
    elif phone:
        result = SocLibService().query(phone=phone)
    elif ioc_type and ioc_value:
        if ioc_type == "email":
            result = SocLibService().query(email=ioc_value)
        else:
            result = SocLibService().query(phone=ioc_value)
    else:
        raise ApiError(
            "invalid_query",
            "缺少查询参数：body 需提供 email、phone 或 (ioc_type, ioc_value)",
            400,
        )
    return ok({"result": result})


@router.post("/analyze/{det_id}", dependencies=[Depends(require_admin)])
def soc_lib_analyze(det_id: int, db=Depends(get_db)) -> dict:
    """对检测项做社工库分析：读 detections.content → extract_iocs → analyze(det_id, iocs, conn)。

    命中写 events(kind='soc_lib_hit')（payload 仅掩码 IOC）；返回 {result}。
    """
    row = db.execute(
        "SELECT id, content FROM detections WHERE id=?", (det_id,)
    ).fetchone()
    if row is None:
        raise ApiError("detection_not_found", f"检测不存在: id={det_id}", 404)
    svc = SocLibService()
    iocs = svc.extract_iocs(row["content"] or "")
    result = svc.analyze(det_id, iocs, db)
    return ok({"result": result})


@router.get("/status", dependencies=[Depends(require_admin)])
def soc_lib_status(db=Depends(get_db)) -> dict:
    """社工库服务状态：{hibp_configured, local_records}（leak_records 表缺失时 local_records=0）。

    local_records 为本地泄露库记录数：3 条演示种子 + 7 条真实知名泄露事件索引
    （仅存公开元数据：事件名/ioc 类型/sha256(事件名)，不引入任何真实个人数据）。
    """
    return ok({"status": SocLibService().status(db)})
