"""退款重算接口：批量录入凭据、执行日/周指标重算批次、读取看板实际金额。

这些是跨模块业务入口（凭据 -> 批次 -> 看板），不属于单一资源的增删改查，
因此单独挂在 ``/api/recalc`` 下；单条资源仍走通用资源路由。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import recalc
from app.services.refund_recalc import (
    batch_detail,
    bulk_import_vouchers,
    dashboard_actuals,
    execute_recalc_batch,
    voucher_list,
)

router = APIRouter(prefix="/api/recalc", tags=["退款指标重算"])


class VoucherBulkPayload(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    # 未传 items 时，允许直接传凭据字段数组的别名。
    vouchers: list[dict[str, Any]] = Field(default_factory=list)

    def entries(self) -> list[dict[str, Any]]:
        return self.items or self.vouchers


class BatchRunPayload(BaseModel):
    batch_code: str = ""
    metric_name: str = "实际订单金额"
    voucher_ids: list[str] | None = None
    publish_policy: str = recalc.POLICY_PARTIAL
    # 兼容中文字段名，前端或脚本直接按业务词提交。
    values: dict[str, Any] = Field(default_factory=dict)

    def resolved_code(self) -> str:
        return self.batch_code or str(self.values.get("批次编号") or "").strip()

    def resolved_policy(self) -> str:
        return str(self.values.get("发布策略") or self.publish_policy)


@router.get("/vouchers")
def list_vouchers() -> dict[str, Any]:
    rows = voucher_list()
    return {"items": rows, "total": len(rows)}


@router.post("/vouchers/bulk")
def bulk_vouchers(payload: VoucherBulkPayload) -> dict[str, Any]:
    entries = payload.entries()
    if not entries:
        raise HTTPException(status_code=400, detail="items 不能为空")
    return bulk_import_vouchers(entries)


@router.post("/batches")
def create_batch(payload: BatchRunPayload) -> dict[str, Any]:
    code = payload.resolved_code()
    if not code:
        raise HTTPException(status_code=400, detail="批次编号不能为空")
    metric_name = str(payload.values.get("指标名称") or payload.metric_name)
    result = execute_recalc_batch(
        code,
        metric_name=metric_name,
        voucher_ids=payload.voucher_ids,
        publish_policy=payload.resolved_policy(),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.get("/batches/{batch_code}")
def get_batch(batch_code: str) -> dict[str, Any]:
    detail = batch_detail(batch_code)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_code} 不存在")
    return detail


@router.get("/dashboard")
def get_dashboard(dashboard_id: str = "DASH-0001") -> dict[str, Any]:
    return dashboard_actuals(dashboard_id)


@router.get("/published")
def get_published() -> dict[str, Any]:
    rows = recalc.published_windows()
    return {"items": rows, "total": len(rows)}
