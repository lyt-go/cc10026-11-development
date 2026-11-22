"""审计分发测试：同一条请求、两份部分重叠的订阅、权限核对与字段隔离。

不依赖 pytest 也能跑：python3 tests/test_audit_distribution.py
（装了 pytest 时按标准测试用例收集。）
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATA_ROOT = tempfile.mkdtemp(prefix="audit_dist_test_")
os.environ["DATA_ROOT"] = DATA_ROOT

REQUEST_NO = "REQ-20260902-0002"
API_NAME = "客户订单查询API"
RISK_SUB, SETTLE_SUB, EXTERNAL_SUB = 4, 5, 6


def setup_function() -> None:
    from app.seed import SEED_ROWS
    from app.store import store

    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    if os.path.exists(DATA_ROOT):
        shutil.rmtree(DATA_ROOT, ignore_errors=True)


def _service():
    from app.services.audit_distribution import audit_distribution

    return audit_distribution


def test_shared_request_number_and_rows_for_both_subscriptions():
    """同一条审计明细分发给两个订阅：请求号和返回行数一致，可共同核对。"""
    result = _service().distribute(2)
    assert result["ok"]
    assert result["shared_numbers"] == {"request_no": REQUEST_NO, "returned_rows": 1280}

    packages = {p["subscription_id"]: p for p in result["packages"]}
    assert set(packages) == {RISK_SUB, SETTLE_SUB}

    for package in packages.values():
        assert package["request_no"] == REQUEST_NO
        assert package["returned_rows"] == 1280
        assert package["row_count"] == 1
        fields = {column["field"]: column["value"] for column in package["columns"]}
        assert fields["请求号"] == REQUEST_NO
        assert fields["返回行数"] == 1280
        # 用量对账：公共数字能回到同一次请求
        recon = package["reconciliation"]
        assert recon["request_no"] == REQUEST_NO
        assert recon["usage_record_no"] == "USAG-0004"
        assert recon["difference"] == 1280 - 1180


def test_controlled_fields_are_isolated_per_recipient():
    """专有业务字段只出现在对应订阅的数据包里，另一份读不到。"""
    result = _service().distribute(2)
    packages = {p["subscription_id"]: p for p in result["packages"]}
    risk = {c["field"] for c in packages[RISK_SUB]["columns"]}
    settle = {c["field"] for c in packages[SETTLE_SUB]["columns"]}

    assert "客户等级" in risk and "客户等级" not in settle
    assert "订单金额" in settle and "订单金额" not in risk
    assert "结算批次" in settle and "结算批次" not in risk
    assert packages[RISK_SUB]["controlled_fields"] == ["客户等级"]
    assert packages[SETTLE_SUB]["controlled_fields"] == ["订单金额", "结算批次"]
    assert not packages[RISK_SUB]["cross_scope_leaks"]
    assert not packages[SETTLE_SUB]["cross_scope_leaks"]


def test_unapproved_subscription_is_rejected_before_distribution():
    """分发前核对订阅权限：待审批订阅拿不到任何数据包。"""
    result = _service().distribute(2)
    recipients = [p["subscription_id"] for p in result["packages"]]
    assert EXTERNAL_SUB not in recipients

    rejected = {r["subscription_id"]: r for r in result["rejected"]}
    assert EXTERNAL_SUB in rejected
    reasons = "；".join(rejected[EXTERNAL_SUB]["reasons"])
    assert "审批" in reasons and "范围" in reasons

    preview = _service().preview_permissions(2)
    checks = {r["subscription_id"]: r for r in preview["recipients"]}
    assert checks[EXTERNAL_SUB]["eligible"] is False
    assert checks[RISK_SUB]["eligible"] is True
    assert checks[SETTLE_SUB]["eligible"] is True


def test_unverified_audit_record_cannot_be_distributed():
    """尚未校验的审计明细不进入分发。"""
    result = _service().distribute(1)
    assert result["ok"]
    assert result["packages"] == []
    assert result["rejected"] == []


def test_out_of_scope_business_fields_are_withheld():
    """审计明细超出订阅范围时，专有字段整体不交付，共享对账列仍可核对。"""
    from app.store import store

    store.find("call_audit", 4)["status"] = "已校验"
    result = _service().distribute(4)
    packages = {p["subscription_id"]: p for p in result["packages"]}

    risk = packages[RISK_SUB]
    assert risk["controlled_fields"] == []
    assert risk["scope"]["in_scope"] is False
    assert [c["field"] for c in risk["columns"]] == risk["shared_fields"]
    assert any(w["field"] == "客户等级" for w in risk["withheld_fields"])
    assert risk["returned_rows"] == 52


def test_masking_follows_confirmed_subscription_scope():
    """按订阅已确认的脱敏配置交付：调用方在风控包里被掩码，在结算包里保持原值。"""
    result = _service().distribute(2)
    packages = {p["subscription_id"]: p for p in result["packages"]}
    risk_fields = {c["field"]: c for c in packages[RISK_SUB]["columns"]}
    settle_fields = {c["field"]: c for c in packages[SETTLE_SUB]["columns"]}

    assert risk_fields["调用方"]["value"] == "华东**中心"
    assert risk_fields["调用方"]["masked"] is True
    assert settle_fields["调用方"]["value"] == "华东运营中心"
    assert settle_fields["调用方"]["masked"] is False


def test_compare_two_overlapping_subscriptions():
    """两份权限范围部分重叠的订阅取同一条请求，对照可用列与行数。"""
    comparison = _service().compare(REQUEST_NO, [RISK_SUB, SETTLE_SUB])
    assert comparison["ok"]
    assert comparison["reconciliation"]["same_request_no"] is True
    assert comparison["reconciliation"]["same_returned_rows"] is True
    assert comparison["reconciliation"]["returned_rows"] == 1280

    overlap = set(comparison["overlap_columns"])
    assert {"请求号", "返回行数"} <= overlap
    assert "客户等级" not in overlap and "订单金额" not in overlap
    assert comparison["unique_columns"][str(RISK_SUB)] == ["客户等级"]
    assert set(comparison["unique_columns"][str(SETTLE_SUB)]) == {"订单金额", "结算批次"}
    assert comparison["cross_scope_leaks"] == []
    assert comparison["isolated"] is True

    for recipient in comparison["recipients"]:
        assert recipient["row_count"] == 1
        assert recipient["returned_rows"] == 1280


def test_reissue_keeps_request_number_and_confirmed_scope():
    """重发按请求号回到原始来源，沿用首次发放的收件方与字段范围。"""
    first = _service().distribute(2)
    original = next(p for p in first["packages"] if p["subscription_id"] == RISK_SUB)

    again = _service().reissue(REQUEST_NO, RISK_SUB)
    assert again["ok"]
    package = again["package"]
    assert package["package_id"] == original["package_id"]
    assert package["request_no"] == REQUEST_NO
    assert package["subscription_id"] == RISK_SUB
    assert package["reissue_count"] == 1
    assert package["reissued"] is True
    assert package["source"]["original_request_no"] == REQUEST_NO
    assert package["source"]["original_audit_id"] == 2
    assert package["source_drift"] == {"returned_rows_changed": False, "audit_id_changed": False}
    # 重发仍然不串字段
    assert {c["field"] for c in package["columns"]} == {c["field"] for c in original["columns"]}


def test_reissue_refuses_never_issued_or_unapproved():
    """从未发放过或不满足权限的订阅不能借重发拿到数据。"""
    _service().distribute(2)
    assert not _service().reissue(REQUEST_NO, SETTLE_SUB + 1)["ok"]
    denied = _service().reissue(REQUEST_NO, EXTERNAL_SUB)
    assert denied["ok"] is False
    assert "从未" in denied["message"]


def test_compare_does_not_leak_to_denied_subscription():
    """对照里带入未通过权限的订阅时，该收件方拿不到任何可用列和数字。"""
    comparison = _service().compare(REQUEST_NO, [RISK_SUB, EXTERNAL_SUB])
    denied = next(r for r in comparison["recipients"] if r["subscription_id"] == EXTERNAL_SUB)
    assert denied["eligible"] is False
    assert denied["usable_columns"] == []
    assert denied["returned_rows"] is None
    assert denied["controlled_columns"] == []
    assert denied["denied_reasons"]

    approved = next(r for r in comparison["recipients"] if r["subscription_id"] == RISK_SUB)
    assert "客户等级" in approved["usable_columns"]
    # 拒发方不能污染共同列统计
    assert comparison["unique_columns"][str(EXTERNAL_SUB)] == []


def test_read_package_roundtrip():
    """数据包按请求号落盘，能按请求号 + 订阅读回。"""
    _service().distribute(2)
    stored = _service().read_package(REQUEST_NO, SETTLE_SUB)
    assert stored["ok"]
    assert stored["package"]["request_no"] == REQUEST_NO
    assert stored["package"]["subscription_id"] == SETTLE_SUB
    # 落盘内容同样不含对方的专有字段
    names = {c["field"] for c in stored["package"]["columns"]}
    assert "客户等级" not in names


if __name__ == "__main__":
    failures = 0
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    for name, test in tests:
        setup_function()
        try:
            test()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    shutil.rmtree(DATA_ROOT, ignore_errors=True)
    sys.exit(1 if failures else 0)
