"""退款重算测试：退款凭据 -> 日/周窗口 -> 指标重算批次 -> 看板实际金额。

覆盖产品口径中的关键约束：
1. 一条批次同时算日、周两个窗口；
2. 重传退款、同批次并发确认都只扣一次；
3. 未获证据的窗口不提前出数；
4. 局部发布与两周期一起发布的责任边界；
5. 每个窗口的最终金额对得上自己的真实窗口与生效轮次。
"""
from __future__ import annotations

import threading

import pytest

from app import recalc
from app.store import store


@pytest.fixture(autouse=True)
def clean_state():
    recalc.reset_state()
    # 每个用例从空凭据表开始，避免示例凭据影响金额；示例数据另有专门的用例覆盖。
    store.rows("refund_voucher").clear()
    yield
    recalc.reset_state()


def _dashboard():
    return {item["period"]: item for item in recalc.dashboard_actuals()["metrics"]}


def _window(batch: dict, window: str) -> dict:
    for item in batch["window_results"]:
        if item["window"] == window:
            return item
    raise AssertionError(f"批次结果里没有窗口 {window}")


# ============================================================
# 凭据录入与窗口归属
# ============================================================

class TestVoucherBulkImport:
    def test_bulk_import_links_day_and_week_windows(self):
        result = recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-A", "原订单编号": "ORD-20261124-001", "退款金额": 40, "核验状态": "已核验"},
        ])
        assert result["ok"] is True
        assert len(result["created"]) == 1
        assert result["created"][0]["windows"] == ["日:2026-11-24", "周:2026-W48"]

    def test_duplicate_rows_in_one_submission_only_store_once(self):
        result = recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-A", "原订单编号": "ORD-20261124-001", "退款金额": 40, "核验状态": "已核验"},
            {"凭据编号": "RV-A", "原订单编号": "ORD-20261124-001", "退款金额": 40, "核验状态": "已核验"},
        ])
        assert result["duplicates"] == ["RV-A"]
        assert len(result["created"]) == 1
        rows = [row for row in store.rows("refund_voucher") if row["凭据编号"] == "RV-A"]
        assert len(rows) == 1

    def test_resubmitting_same_voucher_is_idempotent(self):
        payload = [{"凭据编号": "RV-A", "原订单编号": "ORD-20261124-001", "退款金额": 40, "核验状态": "已核验"}]
        recalc.bulk_upsert_vouchers(payload)
        again = recalc.bulk_upsert_vouchers(payload)
        assert again["created"] == []
        assert len(again["unchanged"]) == 1
        rows = [row for row in store.rows("refund_voucher") if row["凭据编号"] == "RV-A"]
        assert len(rows) == 1

    def test_changed_voucher_content_is_rejected_not_overwritten(self):
        recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-A", "原订单编号": "ORD-20261124-001", "退款金额": 40, "核验状态": "已核验"},
        ])
        conflict = recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-A", "原订单编号": "ORD-20261124-001", "退款金额": 99, "核验状态": "已核验"},
        ])
        assert conflict["ok"] is False
        assert conflict["conflicts"]
        amount = [row["退款金额"] for row in store.rows("refund_voucher") if row["凭据编号"] == "RV-A"]
        assert amount == ["40.00"]

    def test_unknown_order_is_rejected(self):
        result = recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-X", "原订单编号": "ORD-NOPE", "退款金额": 10, "核验状态": "已核验"},
        ])
        assert result["ok"] is False
        assert result["errors"]
        assert not [row for row in store.rows("refund_voucher") if row["凭据编号"] == "RV-X"]

    def test_refund_total_cannot_exceed_order(self):
        recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-A", "原订单编号": "ORD-20261124-001", "退款金额": 70, "核验状态": "已核验"},
            {"凭据编号": "RV-B", "原订单编号": "ORD-20261124-001", "退款金额": 50, "核验状态": "已核验"},
        ])
        checks = recalc.check_vouchers([recalc._voucher_row_view(row) for row in store.rows("refund_voucher")])
        by_id = {check.voucher_id: check for check in checks}
        assert by_id["RV-A"].eligible is True
        assert by_id["RV-B"].eligible is False
        assert any("超过原订单金额" in reason for reason in by_id["RV-B"].reasons)


# ============================================================
# 批次执行：日、周一起算
# ============================================================

class TestBatchRecompute:
    def _seed_verified_40(self):
        recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-40", "原订单编号": "ORD-20261124-001", "退款金额": 40, "核验状态": "已核验"},
        ])

    def test_one_batch_computes_both_day_and_week(self):
        self._seed_verified_40()
        batch = recalc.run_batch("B1", voucher_ids=["RV-40"])
        assert batch["ok"] is True
        day = _window(batch, "日:2026-11-24")
        week = _window(batch, "周:2026-W48")
        assert day["gross"] == 100.0 and day["actual"] == 60.0
        assert week["gross"] == 200.0 and week["actual"] == 160.0
        assert day["actual"] == 60.0
        assert week["actual"] == 160.0

    def test_batch_result_is_deterministic(self):
        self._seed_verified_40()
        first = recalc.run_batch("B1", voucher_ids=["RV-40"])
        again = recalc.run_batch("B1", voucher_ids=["RV-40"])
        assert again["idempotent"] is True
        assert again["window_results"] == first["window_results"]

    def test_resending_refund_in_new_batch_does_not_double_deduct(self):
        self._seed_verified_40()
        recalc.run_batch("B1", voucher_ids=["RV-40"])
        second = recalc.run_batch("B2", voucher_ids=["RV-40"])
        assert _window(second, "日:2026-11-24")["actual"] == 60.0
        assert _window(second, "周:2026-W48")["actual"] == 160.0

    def test_duplicate_voucher_ids_in_same_batch_dedupe(self):
        self._seed_verified_40()
        batch = recalc.run_batch("B1", voucher_ids=["RV-40", "RV-40"])
        assert batch["duplicates"] == ["RV-40"]
        assert _window(batch, "日:2026-11-24")["actual"] == 60.0

    def test_concurrent_same_batch_only_creates_one_round(self):
        self._seed_verified_40()
        results: list[dict] = []

        def worker() -> None:
            results.append(recalc.run_batch("B-CONCURRENT", voucher_ids=["RV-40"]))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert all(result["ok"] for result in results)
        assert sum(1 for result in results if not result.get("idempotent")) == 1
        assert len(store.rows("metric_recalc_batch")) == 1
        assert len(recalc.BATCH_DETAILS["B-CONCURRENT"]["window_results"]) == 2
        assert _dashboard()["日"]["actual"] == 60.0
        assert _dashboard()["周"]["actual"] == 160.0

    def test_pending_refund_blocks_its_day_and_week(self):
        recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-40", "原订单编号": "ORD-20261124-001", "退款金额": 40, "核验状态": "已核验"},
            {"凭据编号": "RV-10", "原订单编号": "ORD-20261123-001", "退款金额": 10, "核验状态": "待核验"},
        ])
        batch = recalc.run_batch("B1", voucher_ids=["RV-40", "RV-10"])
        assert _window(batch, "日:2026-11-24")["actual"] == 60.0
        assert _window(batch, "日:2026-11-23")["status"] == "待补证"
        assert _window(batch, "日:2026-11-23")["actual"] is None
        assert _window(batch, "周:2026-W48")["status"] == "待补证"
        assert _window(batch, "周:2026-W48")["actual"] is None
        # 暂算值把待补证凭据也扣掉，仅供核对，不发布。
        assert _window(batch, "周:2026-W48")["provisional_actual"] == 150.0


# ============================================================
# 看板：未获证据不提前出数
# ============================================================

class TestDashboardAggregation:
    def test_unpublished_week_keeps_gross_amount(self):
        recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-40", "原订单编号": "ORD-20261124-001", "退款金额": 40, "核验状态": "已核验"},
            {"凭据编号": "RV-10", "原订单编号": "ORD-20261123-001", "退款金额": 10, "核验状态": "待核验"},
        ])
        recalc.run_batch("B1", voucher_ids=["RV-40", "RV-10"])
        board = _dashboard()
        assert board["日"]["actual"] == 60.0
        assert board["日"]["batch_code"] == "B1"
        assert board["周"]["actual"] == 200.0  # 仍是原始订单金额
        assert board["周"]["published"] is False
        assert board["周"]["value_source"] == "原始订单金额"
        assert [item["voucher_id"] for item in board["周"]["pending_vouchers"]] == ["RV-10"]

    def test_dashboard_before_any_batch_is_gross(self):
        board = _dashboard()
        assert board["日"]["actual"] == 100.0
        assert board["周"]["actual"] == 200.0
        assert board["日"]["published"] is False


# ============================================================
# 发布策略：局部发布 vs 两周期一起发布
# ============================================================

class TestPublishPolicy:
    def _seed_two(self):
        recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-40", "原订单编号": "ORD-20261124-001", "退款金额": 40, "核验状态": "已核验"},
            {"凭据编号": "RV-10", "原订单编号": "ORD-20261123-001", "退款金额": 10, "核验状态": "待核验"},
        ])

    def test_partial_policy_publishes_complete_day_early(self):
        self._seed_two()
        batch = recalc.run_batch("B1", publish_policy="局部发布")
        assert _window(batch, "日:2026-11-24")["published"] is True
        assert _window(batch, "周:2026-W48")["published"] is False
        assert batch["batch"]["批次状态"] == "部分发布"
        assert _dashboard()["日"]["actual"] == 60.0

    def test_atomic_policy_holds_day_until_week_is_ready(self):
        self._seed_two()
        batch = recalc.run_batch("B1", publish_policy="两周期一起发布")
        assert all(item["published"] is False for item in batch["window_results"])
        assert batch["batch"]["批次状态"] == "待补证"
        board = _dashboard()
        assert board["日"]["actual"] == 100.0  # 日额也先保持原始金额
        assert board["周"]["actual"] == 200.0

    def test_atomic_policy_publishes_both_after_evidence(self):
        self._seed_two()
        recalc.run_batch("B1", publish_policy="两周期一起发布")
        recalc.set_voucher_evidence("RV-10", "已核验")
        batch = recalc.run_batch("B2", publish_policy="两周期一起发布")
        assert _window(batch, "日:2026-11-24")["published"] is True
        assert _window(batch, "日:2026-11-23")["published"] is True
        assert _window(batch, "周:2026-W48")["published"] is True
        board = _dashboard()
        assert board["日"]["actual"] == 60.0
        assert board["周"]["actual"] == 150.0


# ============================================================
# 最终结果与生效轮次
# ============================================================

class TestFinalResultsAndRounds:
    def test_final_amounts_are_60_and_150_with_correct_rounds(self):
        recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-40", "原订单编号": "ORD-20261124-001", "退款金额": 40, "核验状态": "已核验"},
            {"凭据编号": "RV-10", "原订单编号": "ORD-20261123-001", "退款金额": 10, "核验状态": "待核验"},
        ])
        first = recalc.run_batch("B1", publish_policy="局部发布")
        assert _window(first, "日:2026-11-24")["effective_round"] == 1
        recalc.set_voucher_evidence("RV-10", "已核验")
        second = recalc.run_batch("B2", publish_policy="局部发布")

        day = _window(second, "日:2026-11-24")
        week = _window(second, "周:2026-W48")
        assert day["actual"] == 60.0
        assert day["effective_round"] == 1  # 数值没变，沿用第 1 轮生效
        assert day["effective_batch"] == "B1"
        assert week["actual"] == 150.0
        assert week["effective_round"] == 2
        assert week["effective_batch"] == "B2"

        board = _dashboard()
        assert board["日"]["actual"] == 60.0
        assert board["周"]["actual"] == 150.0
        assert "40.00" in board["日"]["basis"]["formula"]
        assert "10.00" in board["周"]["basis"]["formula"]

    def test_rerun_same_batch_code_after_new_evidence_advances_round(self):
        recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-40", "原订单编号": "ORD-20261124-001", "退款金额": 40, "核验状态": "已核验"},
            {"凭据编号": "RV-10", "原订单编号": "ORD-20261123-001", "退款金额": 10, "核验状态": "待核验"},
        ])
        recalc.run_batch("B1", publish_policy="局部发布")
        recalc.set_voucher_evidence("RV-10", "已核验")
        rerun = recalc.run_batch("B1", publish_policy="局部发布")
        assert rerun["batch"]["批次轮次"] == "第2轮"
        assert rerun["batch"]["批次状态"] == "已完整发布"
        assert len(store.rows("metric_recalc_batch")) == 1  # 原地刷新，不新增行
        assert _dashboard()["周"]["actual"] == 150.0

    def test_same_batch_code_with_changed_facts_does_not_overwrite_history(self):
        recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-40", "原订单编号": "ORD-20261124-001", "退款金额": 40, "核验状态": "已核验"},
        ])
        recalc.run_batch("B1", voucher_ids=["RV-40"], publish_policy="局部发布")
        recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-20", "原订单编号": "ORD-20261124-001", "退款金额": 20, "核验状态": "已核验"},
        ])
        # 用不同凭据集合重跑同编号：进入新一轮而不是覆盖旧结果。
        rerun = recalc.run_batch("B1", voucher_ids=["RV-40", "RV-20"], publish_policy="局部发布")
        assert rerun["batch"]["批次轮次"] == "第2轮"
        assert _dashboard()["日"]["actual"] == 40.0

    def test_batch_detail_exposes_each_window_basis(self):
        recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-40", "原订单编号": "ORD-20261124-001", "退款金额": 40, "核验状态": "已核验"},
        ])
        recalc.run_batch("B1", voucher_ids=["RV-40"])
        detail = recalc.batch_windows("B1")
        assert detail is not None
        day = _window(detail, "日:2026-11-24")
        assert day["basis"]["verified_refunds"] == [{"voucher_id": "RV-40", "amount": 40.0}]
        assert [order["order_id"] for order in day["basis"]["orders"]] == ["ORD-20261124-001"]


# ============================================================
# 服务层
# ============================================================

class TestServiceLayer:
    def test_voucher_service_rejects_unknown_action(self):
        from app.services.refund_recalc import RefundVoucherService

        service = RefundVoucherService()
        _, message = service.run_action(1, "不存在的动作")
        assert "不属于" in message

    def test_voucher_service_action_updates_evidence(self):
        from app.services.refund_recalc import RefundVoucherService

        recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-10", "原订单编号": "ORD-20261123-001", "退款金额": 10, "核验状态": "待核验"},
        ])
        service = RefundVoucherService()
        row_id = store.rows("refund_voucher")[0]["id"]
        row, message = service.run_action(int(row_id), "核验凭据")
        assert row is not None
        assert row["核验状态"] == "已核验"
        assert "已核验凭据" in message

    def test_batch_service_create_runs_batch(self):
        from app.services.refund_recalc import MetricRecalcBatchService

        recalc.bulk_upsert_vouchers([
            {"凭据编号": "RV-40", "原订单编号": "ORD-20261124-001", "退款金额": 40, "核验状态": "已核验"},
        ])
        service = MetricRecalcBatchService()
        entry, missing = service.create_entry({
            "批次编号": "SVC-1",
            "指标名称": "实际订单金额",
            "发布策略": "局部发布",
        })
        assert missing == []
        assert entry["日额"] == "60.00"


# ============================================================
# 示例数据：未补证前先出日额、补证后周额补齐
# ============================================================

class TestSeedScenario:
    def test_seed_scenario_first_round_then_evidence(self):
        # reset_state 会恢复示例凭据：今天 40 已核验、昨天 10 待核验。
        recalc.reset_state()
        first = recalc.run_batch("SEED-1", publish_policy="局部发布")
        try:
            assert _window(first, "日:2026-11-24")["actual"] == 60.0
            assert _window(first, "周:2026-W48")["status"] == "待补证"
            assert _window(first, "周:2026-W48")["provisional_actual"] == 150.0
            assert _dashboard()["周"]["actual"] == 200.0

            recalc.set_voucher_evidence("RV-20261123-0010", "已核验")
            second = recalc.run_batch("SEED-2", publish_policy="局部发布")
            assert _window(second, "日:2026-11-24")["actual"] == 60.0
            assert _window(second, "周:2026-W48")["actual"] == 150.0
            assert _dashboard()["日"]["actual"] == 60.0
            assert _dashboard()["周"]["actual"] == 150.0
        finally:
            recalc.reset_state()


def test_api_routes_are_wired():
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert "/api/recalc/dashboard" in paths
    assert "/api/recalc/vouchers/bulk" in paths
    assert "/api/recalc/batches" in paths
    assert "/api/recalc/batches/{batch_code}" in paths


def test_published_windows_report_effective_round_and_basis():
    recalc.bulk_upsert_vouchers([
        {"凭据编号": "RV-40", "原订单编号": "ORD-20261124-001", "退款金额": 40, "核验状态": "已核验"},
    ])
    recalc.run_batch("B1", voucher_ids=["RV-40"])
    recalc.run_batch("B2", voucher_ids=["RV-40"])
    rows = {item["window"]: item for item in recalc.published_windows()}
    assert rows["日:2026-11-24"]["effective_round"] == 1
    assert rows["日:2026-11-24"]["effective_batch"] == "B1"
    assert rows["日:2026-11-24"]["last_recalc_batch"] == "B2"
    assert rows["日:2026-11-24"]["actual"] == 60.0
    assert rows["周:2026-W48"]["actual"] == 160.0
