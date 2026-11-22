"""审计分发接口：按订阅范围生成复核数据包、权限核对、双订阅对照与重发。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.audit_distribution import audit_distribution

router = APIRouter(prefix="/api/audit-distribution", tags=["审计分发"])


class DistributePayload(BaseModel):
    audit_id: int
    subscription_ids: list[int] | None = None
    dry_run: bool = False


class ComparePayload(BaseModel):
    request_no: str
    subscription_ids: list[int] = Field(min_length=2)


@router.get("/audits/{audit_id}/permissions")
def preview_permissions(audit_id: int) -> dict[str, Any]:
    """分发前核对：逐个列出该审计明细涉及的订阅权限与范围检查结果。"""
    result = audit_distribution.preview_permissions(audit_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@router.post("/packages")
def distribute(payload: DistributePayload) -> dict[str, Any]:
    """生成审计分发数据包；未指定订阅时覆盖该API下所有订阅。"""
    result = audit_distribution.distribute(
        payload.audit_id, payload.subscription_ids, dry_run=payload.dry_run)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.get("/packages/{request_no}/{subscription_id}")
def read_package(request_no: str, subscription_id: int) -> dict[str, Any]:
    """按请求号读取已发放给指定订阅的数据包。"""
    result = audit_distribution.read_package(request_no, subscription_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@router.post("/packages/{request_no}/{subscription_id}/reissue")
def reissue(request_no: str, subscription_id: int) -> dict[str, Any]:
    """重发：按请求号回到原始审计明细，沿用首次发放的关联与字段范围。"""
    result = audit_distribution.reissue(request_no, subscription_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.post("/compare")
def compare(payload: ComparePayload) -> dict[str, Any]:
    """用两份订阅取同一条请求的复核材料，对照可用列、行数与专有字段隔离情况。"""
    result = audit_distribution.compare(payload.request_no, payload.subscription_ids)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result
