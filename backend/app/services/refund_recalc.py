"""退款重算领域服务：批量录入退款凭据、执行指标重算批次、读取看板实际金额。

列表、登记、动作仍走通用资源路由；退款重算的业务入口放在这里，
路由层只负责把参数转进来。
"""
from __future__ import annotations

from typing import Any

from app import recalc
from app.services.base import BaseModuleService


class RefundVoucherService(BaseModuleService):
    MODULE = "refund_voucher"
    ENTITY = "退款凭据"
    LIST_FIELDS = [
        "凭据编号", "原订单编号", "订单日期", "退款金额", "凭据来源",
        "核验状态", "关联日窗口", "关联周窗口", "资格校验",
    ]
    REQUIRED_FIELDS = ["凭据编号", "原订单编号", "退款金额"]
    STATUS_ORDER = ["待核验", "已核验", "已作废"]
    ACTION_RULES = {"核验凭据": "已核验", "退回待核": "待核验", "作废凭据": "已作废"}
    NEGATIVE_ACTIONS = ["作废凭据"]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """单条登记复用批量录入：同样做订单、金额、状态校验并补全日/周窗口。"""
        result = recalc.bulk_upsert_vouchers([values])
        if result["errors"] or result["conflicts"]:
            reasons: list[str] = []
            for item in result["errors"]:
                reasons.extend(item["reasons"])
            for item in result["conflicts"]:
                reasons.extend(item["reasons"])
            return None, reasons
        if result["created"]:
            row_id = result["created"][0]["id"]
        else:
            row_id = result["unchanged"][0]["id"]
        return recalc.store.find(self.MODULE, int(row_id)), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        row = recalc.store.find(self.MODULE, entry_id)
        if row is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        voucher_id = str(row.get("凭据编号"))
        updated = recalc.set_voucher_evidence(voucher_id, target)
        if updated is None:
            return None, f"{self.ENTITY} {voucher_id} 的核验状态无法更新"
        return recalc.store.find(self.MODULE, entry_id), f"{self.ENTITY}{voucher_id}已{action}"


class MetricRecalcBatchService(BaseModuleService):
    MODULE = "metric_recalc_batch"
    ENTITY = "指标重算批次"
    LIST_FIELDS = [
        "批次编号", "指标名称", "涉及窗口", "发布策略", "批次轮次", "日额", "周额", "批次状态",
    ]
    REQUIRED_FIELDS = ["批次编号", "指标名称"]
    STATUS_ORDER = ["待补证", "部分发布", "已完整发布", "凭据不合格"]
    ACTION_RULES = {
        "执行批次": "已完整发布",
        "补齐后重算": "已完整发布",
        "标记待补证": "待补证",
    }
    NEGATIVE_ACTIONS = ["标记待补证"]

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """详情在列表字段基础上追加窗口明细，方便核对日、周各自的完整事实。"""
        row = recalc.store.find(self.MODULE, entry_id)
        if row is None:
            return None
        detail = recalc.BATCH_DETAILS.get(str(row.get("批次编号")), {})
        return {
            **row,
            "window_results": [
                {
                    "window": item["window"],
                    "status": item["status"],
                    "gross": item["gross"],
                    "verified_refund": item["verified_refund"],
                    "actual": item["actual"],
                    "provisional_actual": item["provisional_actual"],
                    "published": item["published"],
                    "effective_round": item["effective_round"],
                    "effective_batch": item["effective_batch"],
                    "basis": item["basis"],
                }
                for item in detail.get("window_results", [])
            ],
            "errors": detail.get("errors", []),
            "duplicates": detail.get("duplicates", []),
        }

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        batch_code = str(values.get("批次编号") or "").strip()
        if not batch_code:
            return None, ["批次编号"]
        voucher_ids = values.get("凭据编号") or values.get("voucher_ids")
        if isinstance(voucher_ids, str):
            voucher_ids = [item.strip() for item in voucher_ids.split(",") if item.strip()]
        result = recalc.run_batch(
            batch_code,
            metric_name=str(values.get("指标名称") or "实际订单金额"),
            voucher_ids=voucher_ids or None,
            publish_policy=values.get("发布策略") or recalc.POLICY_PARTIAL,
        )
        if not result.get("ok"):
            return None, [result.get("message", "批次执行失败")]
        return result["batch"], []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        row = recalc.store.find(self.MODULE, entry_id)
        if row is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        batch_code = str(row.get("批次编号"))
        detail = recalc.BATCH_DETAILS.get(batch_code, {})
        if action == "标记待补证":
            row["status"] = target
            row["pending"] = True
            row["abnormal"] = True
            return row, f"{self.ENTITY}{batch_code}已标记待补证"

        # 执行/补齐：按批次当前事实再跑一次，事实变化时进入下一轮并原地刷新批次行。
        rerun = recalc.run_batch(
            batch_code,
            metric_name=str(row.get("指标名称") or "实际订单金额"),
            publish_policy=detail.get("publish_policy") or recalc.POLICY_PARTIAL,
        )
        if not rerun.get("ok"):
            return row, rerun.get("message", f"{self.ENTITY}未能执行")
        latest = rerun.get("batch") or row
        for field in ("status", "pending", "abnormal", *self.LIST_FIELDS):
            if field in latest:
                row[field] = latest[field]
        return row, rerun.get("message", f"{self.ENTITY}已{action}")


def bulk_import_vouchers(items: list[dict[str, Any]]) -> dict[str, Any]:
    """批量录入多条退款凭据，按原订单关联日、周两个窗口。"""
    return recalc.bulk_upsert_vouchers(items)


def execute_recalc_batch(
    batch_code: str,
    *,
    metric_name: str = "实际订单金额",
    voucher_ids: list[str] | None = None,
    publish_policy: str = recalc.POLICY_PARTIAL,
) -> dict[str, Any]:
    return recalc.run_batch(
        batch_code,
        metric_name=metric_name,
        voucher_ids=voucher_ids,
        publish_policy=publish_policy,
    )


def dashboard_actuals(dashboard_id: str = "DASH-0001") -> dict[str, Any]:
    return recalc.dashboard_actuals(dashboard_id)


def batch_detail(batch_code: str) -> dict[str, Any] | None:
    return recalc.batch_windows(batch_code)


def voucher_list() -> list[dict[str, Any]]:
    return recalc.list_voucher_rows()
