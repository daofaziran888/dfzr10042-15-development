"""退款重算领域核心：退款凭据 -> 日/周窗口 -> 指标重算批次 -> 看板实际金额。

业务约束（与产品口径一一对应）：

* 一条退款凭据按「原订单」同时关联**日窗口**和**周窗口**，窗口归属只认订单日期，
  不认凭据提交时自带的日期，避免凭据把金额挪到别的周期。
* 重算批次一次处理日、周两个窗口的**完整事实**：每个窗口都用窗口内全部订单金额
  减去窗口内全部合格凭据金额重新计算，而不是在上一次结果上继续扣减。
  因此重传同一条退款、同批次里并发确认同一条退款，都只记一次。
* 未核验的凭据会把所在窗口标记为「待补证」，该窗口不出数（actual 为 None），
  看板继续展示窗口的原始订单金额；只有凭据补齐后才发布实际金额。
* 发布策略有两种：
  - ``partial``（局部发布）：完整窗口先发布，其余窗口保持待补证；
  - ``atomic``（两周期一起发布）：所有受影响窗口都完整时才一起发布。
* 每个窗口记录「哪一轮生效」：数值首次发布或发生变化的轮次才更新生效轮次，
  重复计算但数值不变的窗口保留最早生效轮次。

本模块只依赖标准库和内存 store，所有计算都是确定性的，方便直接单测。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from app.store import store

# ============================================================
# 常量与示例订单事实
# ============================================================

VERIFIED = "已核验"
PENDING = "待核验"
VOID = "已作废"
VALID_EVIDENCE = {VERIFIED, PENDING, VOID}

DAY = "日"
WEEK = "周"

POLICY_PARTIAL = "partial"
POLICY_ATOMIC = "atomic"
POLICY_LABELS = {
    POLICY_PARTIAL: "局部发布",
    POLICY_ATOMIC: "两周期一起发布",
}
_POLICY_ALIASES = {
    "": POLICY_PARTIAL,
    "partial": POLICY_PARTIAL,
    "局部发布": POLICY_PARTIAL,
    "部分发布": POLICY_PARTIAL,
    "atomic": POLICY_ATOMIC,
    "一起发布": POLICY_ATOMIC,
    "两周期一起发布": POLICY_ATOMIC,
    "两个周期一起发布": POLICY_ATOMIC,
}

# 订单事实：昨天、今天各 100，本周原始订单金额合计 200。
# 真实项目里换成订单明细表，这里保持只读常量以便确定性计算。
ORDERS: dict[str, dict[str, Any]] = {
    "ORD-20261123-001": {
        "order_id": "ORD-20261123-001",
        "order_date": "2026-11-23",
        "amount": 100.0,
        "channel": "小程序",
    },
    "ORD-20261124-001": {
        "order_id": "ORD-20261124-001",
        "order_date": "2026-11-24",
        "amount": 100.0,
        "channel": "小程序",
    },
}

# 已发布窗口：window_key -> 发布记录。批次重算时读写，进程内有效。
PUBLISHED_WINDOWS: dict[str, dict[str, Any]] = {}

# 批次明细单独保存，避免把结构化结果塞进列表行里。
BATCH_DETAILS: dict[str, dict[str, Any]] = {}

# 单进程内存仓库的写锁：并发确认同一批时只允许一个批次落库。
_BATCH_LOCK = threading.Lock()


# ============================================================
# 基础工具
# ============================================================

def _to_float(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def _round(value: float) -> float:
    return round(value + 0.0, 2)


def parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def iso_week_key(value: date) -> str:
    year, week, _ = value.isocalendar()
    return f"{year}-W{week:02d}"


def day_window(value: date) -> str:
    return f"{DAY}:{value.isoformat()}"


def week_window(value: date) -> str:
    return f"{WEEK}:{iso_week_key(value)}"


def windows_for_date(value: date) -> list[str]:
    """一条订单（或凭据）关联的日、周两个窗口。"""
    return [day_window(value), week_window(value)]


def _split_window(window_key: str) -> tuple[str, str]:
    kind, _, key = window_key.partition(":")
    return kind, key


def _order_in_window(order: dict[str, Any], window_key: str) -> bool:
    order_date = parse_date(order.get("order_date"))
    if order_date is None:
        return False
    kind, key = _split_window(window_key)
    if kind == DAY:
        return order_date.isoformat() == key
    if kind == WEEK:
        return iso_week_key(order_date) == key
    return False


def reference_date() -> date:
    """场景参考日：取订单事实中的最大日期（即「今天」）。"""
    dates = [parse_date(order.get("order_date")) for order in ORDERS.values()]
    valid = [item for item in dates if item is not None]
    return max(valid) if valid else date.today()


def normalize_policy(value: Any) -> str:
    return _POLICY_ALIASES.get(str(value or "").strip(), POLICY_PARTIAL)


def reset_state() -> None:
    """重置批次、发布窗口与凭据表，回到初始示例状态（供测试与演示使用）。"""
    from app.seed import SEED_ROWS

    PUBLISHED_WINDOWS.clear()
    BATCH_DETAILS.clear()
    store.rows("metric_recalc_batch").clear()
    seed_vouchers = SEED_ROWS.get("refund_voucher", [])
    store.rows("refund_voucher").clear()
    store.rows("refund_voucher").extend(dict(row) for row in seed_vouchers)


# ============================================================
# 凭据规范化与资格校验
# ============================================================

def normalize_voucher(raw: dict[str, Any]) -> dict[str, Any]:
    """把接口/表格里的中英文字段统一成内部凭据结构。"""
    voucher_id = raw.get("凭据编号", raw.get("voucher_id", raw.get("voucherId", "")))
    order_id = raw.get("原订单编号", raw.get("order_id", raw.get("orderId", "")))
    amount = raw.get("退款金额", raw.get("amount", 0))
    source = raw.get("凭据来源", raw.get("source", ""))
    evidence = raw.get(
        "核验状态",
        raw.get("evidence", raw.get("status", PENDING)),
    )
    evidence = str(evidence or PENDING).strip()
    return {
        "voucher_id": str(voucher_id or "").strip(),
        "order_id": str(order_id or "").strip(),
        "amount": _to_float(amount),
        "source": str(source or "").strip(),
        "evidence": evidence,
    }


def _voucher_row_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "voucher_id": str(row.get("凭据编号", "")).strip(),
        "order_id": str(row.get("原订单编号", "")).strip(),
        "amount": _to_float(row.get("退款金额")),
        "source": str(row.get("凭据来源", "")).strip(),
        "evidence": str(row.get("核验状态") or row.get("status") or PENDING).strip(),
        "row_id": row.get("id"),
    }


@dataclass
class VoucherCheck:
    """一条凭据的资格校验结果。"""

    voucher_id: str
    order_id: str
    amount: float
    evidence: str
    order_date: date | None
    eligible: bool = True
    reasons: list[str] = field(default_factory=list)
    windows: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.evidence == VERIFIED

    @property
    def voided(self) -> bool:
        return self.evidence == VOID

    def as_dict(self) -> dict[str, Any]:
        return {
            "voucher_id": self.voucher_id,
            "order_id": self.order_id,
            "amount": self.amount,
            "evidence": self.evidence,
            "order_date": self.order_date.isoformat() if self.order_date else "",
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "windows": list(self.windows),
        }


def dedupe_vouchers(vouchers: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """同一批次内按凭据编号去重；重复提交只保留第一条。"""
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    for voucher in vouchers:
        voucher_id = voucher["voucher_id"]
        if voucher_id in seen:
            duplicates.append(voucher_id)
            continue
        seen.add(voucher_id)
        unique.append(voucher)
    return unique, duplicates


def check_vouchers(
    vouchers: list[dict[str, Any]],
    orders: dict[str, dict[str, Any]] | None = None,
) -> list[VoucherCheck]:
    """逐条校验凭据资格，并做原订单维度的累计退款上限校验。"""
    order_book = orders or ORDERS
    checks: list[VoucherCheck] = []

    for voucher in vouchers:
        order = order_book.get(voucher["order_id"])
        order_date = parse_date(order.get("order_date")) if order else None
        check = VoucherCheck(
            voucher_id=voucher["voucher_id"],
            order_id=voucher["order_id"],
            amount=voucher["amount"],
            evidence=voucher["evidence"],
            order_date=order_date,
        )
        if not check.voucher_id:
            check.eligible = False
            check.reasons.append("凭据编号为空")
        if check.amount <= 0:
            check.eligible = False
            check.reasons.append("退款金额必须大于 0")
        if order is None:
            check.eligible = False
            check.reasons.append(f"原订单 {check.order_id or '(空)'} 不存在")
        if check.evidence not in VALID_EVIDENCE:
            check.eligible = False
            check.reasons.append(f"核验状态「{check.evidence}」无法识别")
        if order_date is not None:
            check.windows = windows_for_date(order_date)
        checks.append(check)

    # 累计退款不得超过原订单金额：按凭据编号稳定排序，超额的那条标记不合格。
    by_order: dict[str, list[VoucherCheck]] = {}
    for check in checks:
        if check.order_id and check.order_id in order_book and not check.voided and check.amount > 0:
            by_order.setdefault(check.order_id, []).append(check)
    for order_id, group in by_order.items():
        capacity = _to_float(order_book[order_id].get("amount"))
        used = 0.0
        for check in sorted(group, key=lambda item: item.voucher_id):
            used += check.amount
            if used > capacity + 1e-9:
                check.eligible = False
                check.reasons.append(
                    f"累计退款 {_fmt(used)} 超过原订单金额 {_fmt(capacity)}"
                )
    return checks


# ============================================================
# 窗口计算
# ============================================================

def _window_basis(
    window_key: str,
    gross: float,
    orders: list[dict[str, Any]],
    verified: list[VoucherCheck],
    pending: list[VoucherCheck],
    actual: float | None,
    provisional: float,
) -> dict[str, Any]:
    verified_total = sum(item.amount for item in verified)
    pending_total = sum(item.amount for item in pending)
    parts = [_fmt(gross)]
    formula_parts: list[str] = []
    for item in verified:
        formula_parts.append(f"- {_fmt(item.amount)}({item.voucher_id} 已核验)")
    for item in pending:
        formula_parts.append(f"- {_fmt(item.amount)}({item.voucher_id} 待核验)")
    formula = " ".join([_fmt(gross), *formula_parts])
    if actual is not None:
        formula = f"{formula} = {_fmt(actual)}"
    else:
        formula = f"{formula} = {_fmt(provisional)}(暂算)"
    return {
        "window": window_key,
        "gross": _round(gross),
        "orders": [
            {"order_id": order["order_id"], "order_date": order["order_date"], "amount": order["amount"]}
            for order in orders
        ],
        "verified_refunds": [
            {"voucher_id": item.voucher_id, "amount": item.amount} for item in verified
        ],
        "pending_refunds": [
            {"voucher_id": item.voucher_id, "amount": item.amount} for item in pending
        ],
        "verified_total": _round(verified_total),
        "pending_total": _round(pending_total),
        "formula": formula,
    }


def evaluate_windows(
    checks: list[VoucherCheck],
    orders: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """按窗口汇总完整事实，分别计算日、周实际金额。

    每个窗口的结果都独立地从窗口内订单与窗口内凭据重算，窗口之间不共享扣减量。
    """
    order_book = orders or ORDERS
    buckets: dict[str, list[VoucherCheck]] = {}

    for check in checks:
        if check.voided or not check.windows:
            continue
        for window_key in check.windows:
            buckets.setdefault(window_key, []).append(check)

    results: list[dict[str, Any]] = []
    for window_key in sorted(buckets, key=lambda key: (_split_window(key)[0] != DAY, key)):
        window_checks = buckets[window_key]
        orders_in_window = [
            order for order in order_book.values()
            if _order_in_window(order, window_key)
        ]
        gross = sum(_to_float(order.get("amount")) for order in orders_in_window)

        eligible = [item for item in window_checks if item.eligible and not item.voided]
        verified = [item for item in eligible if item.verified]
        pending = [item for item in eligible if item.evidence == PENDING]
        invalid = [item for item in window_checks if not item.eligible]

        verified_total = sum(item.amount for item in verified)
        pending_total = sum(item.amount for item in pending)
        # 不合格凭据不参与扣减，但其所在窗口不能被当作完整事实发布；
        # 暂算值同样只扣已核验与待核验凭据，不合格部分留给人工处理。
        provisional = _round(gross - verified_total - pending_total)
        invalid_blocked = bool(invalid)
        pending_blocked = bool(pending)

        if invalid_blocked:
            status = "凭据不合格"
        elif pending_blocked:
            status = "待补证"
        else:
            status = "完整"

        actual = _round(gross - verified_total) if status == "完整" else None
        basis = _window_basis(
            window_key, gross, orders_in_window, verified, pending, actual, provisional
        )
        results.append({
            "window": window_key,
            "kind": _split_window(window_key)[0],
            "label": "日窗" if _split_window(window_key)[0] == DAY else "周窗",
            "gross": _round(gross),
            "verified_refund": _round(verified_total),
            "provisional_actual": provisional,
            "actual": actual,
            "status": status,
            "voucher_ids": sorted({item.voucher_id for item in eligible}),
            "blockers": [
                {
                    "voucher_id": item.voucher_id,
                    "reason": "待补证" if item.eligible else "、".join(item.reasons) or "凭据不合格",
                }
                for item in [*pending, *invalid]
            ],
            "basis": basis,
            "published": False,
            "effective_round": None,
            "effective_batch": "",
        })
    return results


# ============================================================
# 批次执行与发布
# ============================================================

def _find_batch(batch_code: str) -> dict[str, Any] | None:
    for row in store.rows("metric_recalc_batch"):
        if str(row.get("批次编号")) == batch_code:
            return row
    return None


def _batch_signature(
    voucher_views: list[dict[str, Any]],
    policy: str,
    metric_name: str,
) -> str:
    """批次事实指纹：凭据集合、金额与核验状态都参与，防止同编号改事实重跑。"""
    facts = ",".join(
        f"{item['voucher_id']}:{_fmt(item['amount'])}:{item['evidence']}"
        for item in sorted(voucher_views, key=lambda item: item["voucher_id"])
    )
    return f"{metric_name}|{policy}|{facts}"


def _publish(
    window_results: list[dict[str, Any]],
    *,
    batch_code: str,
    round_no: int,
    policy: str,
    reference: date,
) -> tuple[list[str], bool]:
    """按发布策略发布窗口，返回（已发布窗口列表, 是否因策略被整体拦下）。"""
    complete = [item for item in window_results if item["status"] == "完整"]
    all_complete = bool(window_results) and len(complete) == len(window_results)

    if policy == POLICY_ATOMIC and not all_complete:
        for item in window_results:
            previous = PUBLISHED_WINDOWS.get(item["window"])
            if previous:
                item["effective_round"] = previous["round_no"]
                item["effective_batch"] = previous["batch_code"]
        return [], True

    published: list[str] = []
    for item in complete:
        window_key = item["window"]
        previous = PUBLISHED_WINDOWS.get(window_key)
        if previous is None or abs(previous["value"] - item["actual"]) > 1e-9:
            effective_round = round_no
            effective_batch = batch_code
        else:
            # 数值与上一轮一致：保留最早生效的轮次与批次，便于核对「哪次生效」。
            effective_round = previous["round_no"]
            effective_batch = previous["source_batch"]
        PUBLISHED_WINDOWS[window_key] = {
            "value": item["actual"],
            "batch_code": batch_code,
            "round_no": effective_round,
            "source_batch": effective_batch,
            "voucher_ids": list(item["voucher_ids"]),
            "basis": item["basis"],
            "published_at": f"{reference.isoformat()}T00:00:00",
        }
        item["published"] = True
        item["effective_round"] = effective_round
        item["effective_batch"] = effective_batch
        published.append(window_key)

    for item in window_results:
        if item["published"]:
            continue
        previous = PUBLISHED_WINDOWS.get(item["window"])
        if previous:
            item["effective_round"] = previous["round_no"]
            item["effective_batch"] = previous["batch_code"]
    return published, False


def _display_amount(result: dict[str, Any] | None) -> str:
    if result is None:
        return "—"
    if result["published"] and result["actual"] is not None:
        return _fmt(result["actual"])
    if result["status"] == "凭据不合格":
        return "凭据不合格"
    if result["status"] == "完整":
        suffix = _fmt(result["actual"]) if result["actual"] is not None else "—"
        return f"待发布（{suffix}）"
    return f"待补证（暂算{_fmt(result['provisional_actual'])}）"


def run_batch(
    batch_code: str,
    *,
    metric_name: str = "实际订单金额",
    voucher_ids: list[str] | None = None,
    publish_policy: Any = POLICY_PARTIAL,
    reference: date | None = None,
) -> dict[str, Any]:
    """执行一次指标重算批次，一次处理日、周两个窗口。

    ``voucher_ids`` 为 None 时取凭据表里全部未作废凭据，确保每个窗口读到完整事实。
    同一批次编号重复提交且事实一致时幂等返回，不重复扣减、不新增轮次。
    """
    with _BATCH_LOCK:
        return _run_batch_locked(
            batch_code,
            metric_name=metric_name,
            voucher_ids=voucher_ids,
            publish_policy=publish_policy,
            reference=reference,
        )


def _run_batch_locked(
    batch_code: str,
    *,
    metric_name: str,
    voucher_ids: list[str] | None,
    publish_policy: Any,
    reference: date | None,
) -> dict[str, Any]:
    policy = normalize_policy(publish_policy)
    reference_day = reference or reference_date()
    batch_code = str(batch_code or "").strip()
    if not batch_code:
        return {"ok": False, "message": "批次编号不能为空"}

    if voucher_ids is None:
        selected_rows = [
            row for row in store.rows("refund_voucher")
            if str(row.get("核验状态") or row.get("status") or PENDING) != VOID
        ]
    else:
        selected_rows = []
        missing: list[str] = []
        for voucher_id in voucher_ids:
            found = None
            for row in store.rows("refund_voucher"):
                if str(row.get("凭据编号")) == str(voucher_id):
                    found = row
                    break
            if found is None:
                missing.append(str(voucher_id))
            else:
                selected_rows.append(found)
        if missing:
            return {
                "ok": False,
                "message": f"以下退款凭据不存在：{'、'.join(sorted(set(missing)))}",
                "missing_vouchers": sorted(set(missing)),
            }

    vouchers = [_voucher_row_view(row) for row in selected_rows]
    vouchers, duplicates = dedupe_vouchers(vouchers)
    ids = [item["voucher_id"] for item in vouchers]
    if not vouchers:
        return {"ok": False, "message": "没有可参与重算的退款凭据"}

    signature = _batch_signature(vouchers, policy, metric_name)
    existing = _find_batch(batch_code)
    if existing is not None:
        detail = BATCH_DETAILS.get(batch_code, {})
        if detail.get("signature") == signature:
            # 事实未变：幂等返回，不重复扣减、不新增轮次。
            message = (
                f"批次 {batch_code} 已执行过，幂等返回，不重复扣减"
                if existing.get("批次状态") == detail.get("status")
                else f"批次 {batch_code} 事实未变化，无需重算"
            )
            return {
                "ok": True,
                "message": message,
                "batch": existing,
                "window_results": detail.get("window_results", []),
                "idempotent": True,
            }
        # 事实变化（例如缺证凭据补齐后重算）：同一批次编号进入下一轮，
        # 本轮以新事实重算并原地刷新批次行，不覆盖历史轮次的生效记录。

    checks = check_vouchers(vouchers)
    window_results = evaluate_windows(checks)

    if not window_results:
        return {"ok": False, "message": "凭据没有关联到有效窗口，批次未生成"}

    # 轮次是该指标下的全局序号：同一批次编号重算也进入下一轮，不与其他批次撞号。
    round_no = max(
        (
            int(detail.get("round_no") or 0)
            for detail in BATCH_DETAILS.values()
            if detail.get("metric_name") == metric_name
        ),
        default=0,
    ) + 1
    published_windows, withheld = _publish(
        window_results,
        batch_code=batch_code,
        round_no=round_no,
        policy=policy,
        reference=reference_day,
    )

    invalid_checks = [check for check in checks if not check.eligible]
    invalid_blocked = any(item["status"] == "凭据不合格" for item in window_results)

    if invalid_blocked:
        batch_status = "凭据不合格"
    elif all(item["status"] == "完整" for item in window_results):
        batch_status = "已完整发布"
    elif published_windows:
        batch_status = "部分发布"
    else:
        batch_status = "待补证"

    day_result = next(
        (item for item in window_results if item["window"] == day_window(reference_day)),
        None,
    )
    week_result = next(
        (item for item in window_results if item["window"] == week_window(reference_day)),
        None,
    )

    rows = store.rows("metric_recalc_batch")
    batch = existing if existing is not None else {
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1
    }
    batch.update({
        "status": batch_status,
        "pending": batch_status != "已完整发布",
        "abnormal": batch_status in {"待补证", "凭据不合格"},
        "批次编号": batch_code,
        "指标名称": metric_name,
        "涉及窗口": "、".join(item["window"] for item in window_results),
        "发布策略": POLICY_LABELS[policy],
        "批次轮次": f"第{round_no}轮",
        "日额": _display_amount(day_result),
        "周额": _display_amount(week_result),
        "批次状态": batch_status,
    })
    if existing is None:
        rows.append(batch)
    BATCH_DETAILS[batch_code] = {
        "batch_code": batch_code,
        "metric_name": metric_name,
        "round_no": round_no,
        "publish_policy": policy,
        "publish_policy_label": POLICY_LABELS[policy],
        "reference_date": reference_day.isoformat(),
        "voucher_ids": ids,
        "duplicates": duplicates,
        "signature": signature,
        "window_results": window_results,
        "published_windows": published_windows,
        "withheld": withheld,
        "checks": [check.as_dict() for check in checks],
        "invalid_vouchers": [check.as_dict() for check in invalid_checks],
        "errors": [
            f"{check.voucher_id}：{'、'.join(check.reasons)}"
            for check in invalid_checks
        ],
        "status": batch_status,
    }
    # 把录入选出的凭据回填资格校验摘要，便于列表页看出凭据的日/周窗口。
    eligible_ids = {check.voucher_id for check in checks if check.eligible}
    for row in store.rows("refund_voucher"):
        voucher_id = str(row.get("凭据编号"))
        if voucher_id in ids:
            row["资格校验"] = "通过" if voucher_id in eligible_ids else "未通过，见批次明细"
    return {
        "ok": True,
        "message": (
            f"批次 {batch_code} 已执行（第{round_no}轮，{POLICY_LABELS[policy]}）"
            + ("，部分窗口待补证" if batch_status in {"待补证", "部分发布"} else "")
        ),
        "batch": batch,
        "window_results": window_results,
        "published_windows": published_windows,
        "duplicates": duplicates,
        "round_no": round_no,
    }


# ============================================================
# 看板聚合
# ============================================================

def _pending_vouchers_in_window(window_key: str) -> list[dict[str, Any]]:
    pending = []
    for row in store.rows("refund_voucher"):
        view = _voucher_row_view(row)
        if view["evidence"] != PENDING:
            continue
        order = ORDERS.get(view["order_id"])
        if order and _order_in_window(order, window_key):
            pending.append({
                "voucher_id": view["voucher_id"],
                "order_id": view["order_id"],
                "amount": view["amount"],
            })
    return pending


def _dashboard_metric(label: str, window_key: str) -> dict[str, Any]:
    orders_in_window = [
        order for order in ORDERS.values() if _order_in_window(order, window_key)
    ]
    gross = _round(sum(_to_float(order.get("amount")) for order in orders_in_window))
    published = PUBLISHED_WINDOWS.get(window_key)
    pending = _pending_vouchers_in_window(window_key)
    all_refunds = []
    for row in store.rows("refund_voucher"):
        view = _voucher_row_view(row)
        if view["evidence"] == VOID:
            continue
        order = ORDERS.get(view["order_id"])
        if order and _order_in_window(order, window_key):
            all_refunds.append({"voucher_id": view["voucher_id"], "amount": view["amount"]})
    provisional = _round(gross - sum(item["amount"] for item in all_refunds))

    if published:
        actual = published["value"]
        source = "指标重算批次"
    else:
        # 没有已发布批次时，看板仍展示原始订单金额，不提前出实际金额。
        actual = gross
        source = "原始订单金额"

    return {
        "period": label,
        "window": window_key,
        "gross": gross,
        "actual": actual,
        "provisional_actual": provisional,
        "value_source": source,
        "published": published is not None,
        "batch_code": published["source_batch"] if published else "",
        "effective_round": published["round_no"] if published else None,
        "voucher_ids": published["voucher_ids"] if published else [],
        "basis": published["basis"] if published else {
            "window": window_key,
            "gross": gross,
            "orders": [
                {"order_id": order["order_id"], "order_date": order["order_date"], "amount": order["amount"]}
                for order in orders_in_window
            ],
            "verified_refunds": [],
            "pending_refunds": [],
            "formula": f"{_fmt(gross)}(原始订单金额)",
        },
        "pending_vouchers": pending,
        "can_publish": not pending,
    }


def dashboard_actuals(
    dashboard_id: str = "DASH-0001",
    reference: date | None = None,
) -> dict[str, Any]:
    """看板实际指标金额：分别读取日、周两个窗口的重算事实。

    已发布窗口读批次里该窗口的完整事实结果；未发布窗口继续展示原始订单金额，
    并把待补证凭据列出来，保证「未获证据的周数据不能提前出数」。
    """
    reference_day = reference or reference_date()
    daily = _dashboard_metric("日", day_window(reference_day))
    weekly = _dashboard_metric("周", week_window(reference_day))
    return {
        "dashboard_id": dashboard_id,
        "reference_date": reference_day.isoformat(),
        "metrics": [daily, weekly],
        "daily": daily,
        "weekly": weekly,
        "published_windows": sorted(PUBLISHED_WINDOWS),
        "source": "指标重算批次" if PUBLISHED_WINDOWS else "原始订单金额",
    }


def published_windows() -> list[dict[str, Any]]:
    """列出已生效窗口，标明各自哪一轮、哪个批次生效及计算依据。"""
    results: list[dict[str, Any]] = []
    for window_key in sorted(
        PUBLISHED_WINDOWS, key=lambda key: (_split_window(key)[0] != DAY, key)
    ):
        published = PUBLISHED_WINDOWS[window_key]
        results.append({
            "window": window_key,
            "kind": _split_window(window_key)[0],
            "actual": published["value"],
            "gross": published["basis"]["gross"],
            "verified_refund": published["basis"]["verified_total"],
            "effective_round": published["round_no"],
            "effective_batch": published["source_batch"],
            "last_recalc_batch": published["batch_code"],
            "voucher_ids": published["voucher_ids"],
            "formula": published["basis"]["formula"],
            "published_at": published["published_at"],
        })
    return results


def batch_windows(batch_code: str) -> dict[str, Any] | None:
    batch = _find_batch(batch_code)
    if batch is None:
        return None
    detail = BATCH_DETAILS.get(batch_code, {})
    return {
        "batch_code": batch_code,
        "batch": batch,
        "batch_status": detail.get("status"),
        "round_no": detail.get("round_no"),
        "publish_policy": detail.get("publish_policy"),
        "publish_policy_label": detail.get("publish_policy_label"),
        "window_results": detail.get("window_results", []),
        "published_windows": detail.get("published_windows", []),
        "withheld": detail.get("withheld", False),
        "checks": detail.get("checks", []),
        "errors": detail.get("errors", []),
        "duplicates": detail.get("duplicates", []),
    }


# ============================================================
# 批量录入退款凭据
# ============================================================

def _voucher_core(values: dict[str, Any]) -> tuple[str, str, float, str, str]:
    return (
        values["voucher_id"],
        values["order_id"],
        _round(values["amount"]),
        values["source"],
        values["evidence"],
    )


def bulk_upsert_vouchers(items: list[dict[str, Any]]) -> dict[str, Any]:
    """批量录入退款凭据，按凭据编号幂等：重传同一条只保留一份。

    录入时完成订单存在性、金额、核验状态校验，并按原订单补全日、周窗口字段；
    累计退款是否超额留到批次执行时统一校验（因为要拿到同订单的全部凭据）。
    """
    created: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    duplicates: list[str] = []
    seen: dict[str, tuple[str, str, float, str, str]] = {}

    for index, raw in enumerate(items or []):
        values = normalize_voucher(raw)
        voucher_id = values["voucher_id"]
        if not voucher_id:
            errors.append({"index": index, "voucher_id": "", "reasons": ["凭据编号为空"]})
            continue
        core = _voucher_core(values)
        if voucher_id in seen:
            if seen[voucher_id] == core:
                duplicates.append(voucher_id)
            else:
                conflicts.append({
                    "index": index,
                    "voucher_id": voucher_id,
                    "reasons": ["同批次内凭据编号重复且内容不一致"],
                })
            continue
        seen[voucher_id] = core

        reasons: list[str] = []
        if values["amount"] <= 0:
            reasons.append("退款金额必须大于 0")
        order = ORDERS.get(values["order_id"])
        if order is None:
            reasons.append(f"原订单 {values['order_id'] or '(空)'} 不存在")
        if values["evidence"] not in VALID_EVIDENCE:
            reasons.append(f"核验状态「{values['evidence']}」无法识别")
        if reasons:
            errors.append({"index": index, "voucher_id": voucher_id, "reasons": reasons})
            continue

        order_date = parse_date(order["order_date"])
        assert order_date is not None
        windows = windows_for_date(order_date)
        row_values = {
            "凭据编号": voucher_id,
            "原订单编号": values["order_id"],
            "订单日期": order_date.isoformat(),
            "退款金额": _fmt(values["amount"]),
            "凭据来源": values["source"],
            "核验状态": values["evidence"],
            "关联日窗口": windows[0],
            "关联周窗口": windows[1],
            "资格校验": "录入待重算校验",
        }

        existing = None
        for row in store.rows("refund_voucher"):
            if str(row.get("凭据编号")) == voucher_id:
                existing = row
                break
        if existing is not None:
            existing_core = (
                str(existing.get("凭据编号", "")),
                str(existing.get("原订单编号", "")),
                _round(_to_float(existing.get("退款金额"))),
                str(existing.get("凭据来源", "")),
                str(existing.get("核验状态") or existing.get("status") or PENDING),
            )
            if existing_core == core:
                unchanged.append({"voucher_id": voucher_id, "id": existing.get("id")})
            else:
                conflicts.append({
                    "index": index,
                    "voucher_id": voucher_id,
                    "reasons": ["凭据编号已存在且内容不一致，未覆盖"],
                })
            continue

        rows = store.rows("refund_voucher")
        row = {
            "id": max((int(item.get("id", 0)) for item in rows), default=0) + 1,
            "status": values["evidence"],
            "pending": values["evidence"] == PENDING,
            "abnormal": values["evidence"] == VOID,
            **row_values,
        }
        rows.append(row)
        created.append({"voucher_id": voucher_id, "id": row["id"], "windows": windows})

    return {
        "ok": not conflicts and not errors,
        "created": created,
        "unchanged": unchanged,
        "conflicts": conflicts,
        "errors": errors,
        "duplicates": duplicates,
        "message": (
            f"新增 {len(created)} 条，已存在 {len(unchanged)} 条，"
            f"重复 {len(duplicates)} 条，冲突 {len(conflicts)} 条，失败 {len(errors)} 条"
        ),
    }


def set_voucher_evidence(voucher_id: str, evidence: str) -> dict[str, Any] | None:
    """更新凭据核验状态；返回更新后的凭据视图。"""
    if evidence not in VALID_EVIDENCE:
        return None
    for row in store.rows("refund_voucher"):
        if str(row.get("凭据编号")) == voucher_id:
            row["核验状态"] = evidence
            row["status"] = evidence
            row["pending"] = evidence == PENDING
            row["abnormal"] = evidence == VOID
            return _voucher_row_view(row)
    return None


def list_voucher_rows() -> list[dict[str, Any]]:
    return [_voucher_row_view(row) for row in store.rows("refund_voucher")]
