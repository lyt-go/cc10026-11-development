"""审计分发领域服务：把已校验的调用审计明细按订阅范围分发给复核责任人。

业务规则：
- 分发对象是「已校验」的调用审计明细，按请求号关联同一次原始调用。
- 分发前逐个核对收件方订阅权限：订阅审批、目标API、有效期、范围确认状态。
- 共享字段（请求号、返回行数等公共对账数字）对所有匹配订阅交付。
- 受控字段（订阅专有业务字段）只交付给声明了该字段且范围命中的人员。
- 脱敏按订阅已确认的脱敏配置执行，交付前再做一次跨订阅串字段检查。
- 重发按请求号回到原始审计明细，沿用首次发放的关联关系，不重新挑数据。
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.store import store

# 未在订阅上单独配置时使用的公共对账列
DEFAULT_SHARED_FIELDS = ["请求号", "目标API", "调用方", "响应码", "响应耗时", "返回行数", "时间戳"]

VERIFIED_STATUSES = ("已校验", "已归档")


class AuditDistributionService:
    MODULE = "call_audit"

    # ------------------------------------------------------------------
    # 权限与范围
    # ------------------------------------------------------------------

    def _audit_by_id(self, audit_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, audit_id)

    def _audit_by_request_no(self, request_no: str) -> dict[str, Any] | None:
        """按请求号回到原始审计明细，内存与落盘审计文件都查一遍。"""
        key = str(request_no or "").strip()
        if not key:
            return None
        for row in store.rows(self.MODULE):
            if str(row.get("请求号", "")).strip() == key:
                return row

        from app.services.data_manager import list_audit

        for record in list_audit():
            if str(record.get("request_no", "")).strip() == key:
                return record
        return None

    def _subscriptions_for_api(self, api_name: str) -> list[dict[str, Any]]:
        return [
            row for row in store.rows("subscription")
            if row.get("订阅API") == api_name
        ]

    def _subscription(self, subscription_id: int) -> dict[str, Any] | None:
        return store.find("subscription", subscription_id)

    def _scope_state(self, subscription: dict[str, Any], audit: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
        """核对审计明细的业务字段是否落在订阅声明的数据范围内。"""
        business = audit.get("业务字段") or {}
        details: list[dict[str, Any]] = []
        in_scope = True
        for key, expected in (subscription.get("范围条件") or {}).items():
            if not str(key).startswith("业务字段."):
                continue
            field = str(key).split(".", 1)[1]
            actual = business.get(field)
            if isinstance(expected, (list, tuple, set)):
                passed = actual in expected
            else:
                passed = str(actual) == str(expected)
            details.append({
                "field": field,
                "expected": list(expected) if isinstance(expected, (list, tuple, set)) else expected,
                "actual": actual,
                "passed": passed,
            })
            in_scope = in_scope and passed
        return in_scope, details

    def check_permission(self, subscription: dict[str, Any] | None,
                         audit: dict[str, Any]) -> dict[str, Any]:
        """分发前核对收件方订阅权限，返回逐项检查结果与拒发原因。"""
        checks: list[dict[str, Any]] = []
        reasons: list[str] = []

        def add(name: str, passed: bool, detail: str) -> None:
            checks.append({"name": name, "passed": passed, "detail": detail})
            if not passed:
                reasons.append(detail)

        add("订阅存在", subscription is not None,
            "订阅记录存在" if subscription else "未找到订阅记录")
        if subscription is None:
            return {"eligible": False, "checks": checks, "reasons": reasons}

        approved = (subscription.get("status") == "已通过"
                    and subscription.get("审批状态") == "已通过")
        add("审批通过", approved,
            "订阅审批已通过" if approved
            else f"订阅审批状态为「{subscription.get('审批状态') or subscription.get('status')}」，未通过")

        api_match = subscription.get("订阅API") == audit.get("目标API")
        add("目标API匹配", api_match,
            "订阅覆盖目标API" if api_match
            else f"订阅API「{subscription.get('订阅API')}」与审计目标API「{audit.get('目标API')}」不一致")

        confirmed = subscription.get("范围确认") == "已确认"
        add("范围已确认", confirmed,
            "字段交付范围已确认" if confirmed
            else f"字段交付范围尚未确认（当前：{subscription.get('范围确认') or '未填写'}）")

        audit_date = str(audit.get("时间戳") or "")[:10]
        valid_until = str(subscription.get("有效期") or "")[:10]
        valid = True
        detail = "订阅在有效期内"
        if valid_until and audit_date:
            valid = audit_date <= valid_until
            detail = (f"订阅有效期至 {valid_until}，覆盖调用日期 {audit_date}"
                      if valid else f"订阅已于 {valid_until} 到期，早于调用日期 {audit_date}")
        add("有效期覆盖", valid, detail)

        verified = str(audit.get("status") or "") in VERIFIED_STATUSES
        add("审计已校验", verified,
            f"审计记录状态为「{audit.get('status')}」" if verified
            else f"审计记录状态为「{audit.get('status')}」，尚未校验，不能分发")

        request_no = str(audit.get("请求号") or "").strip()
        add("请求号可关联", bool(request_no),
            f"请求号 {request_no} 可关联原始调用" if request_no else "审计明细缺少请求号，无法关联原始调用")

        return {"eligible": all(c["passed"] for c in checks),
                "checks": checks, "reasons": reasons}

    # ------------------------------------------------------------------
    # 字段组装、脱敏与隔离
    # ------------------------------------------------------------------

    def _mask_config(self, subscription: dict[str, Any], field: str) -> dict[str, Any]:
        """取该订阅针对字段的脱敏配置，订阅未配置时回落到已生效的全局规则。"""
        configured = (subscription.get("脱敏字段") or {}).get(field)
        if configured:
            return dict(configured)

        for rule in store.rows("field_mask"):
            if rule.get("status") != "已生效" or rule.get("目标字段") != field:
                continue
            scope = str(rule.get("脱敏范围") or "")
            if scope in ("", "全部", subscription.get("申请方"), subscription.get("订阅API")):
                return {
                    "脱敏算法": rule.get("脱敏算法"),
                    "保留前缀": rule.get("保留前缀"),
                    "保留后缀": rule.get("保留后缀"),
                    "例外条件": rule.get("例外条件"),
                }
        return {}

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return 0

    def _mask_value(self, value: Any, config: dict[str, Any]) -> tuple[Any, bool, str]:
        if not config:
            return value, False, ""

        raw = str(value)
        exception = str(config.get("例外条件") or "")
        if exception and exception.casefold() in raw.casefold():
            return value, False, ""

        algo = str(config.get("脱敏算法") or "")
        prefix_n = self._safe_int(config.get("保留前缀", 0))
        suffix_n = self._safe_int(config.get("保留后缀", 0))

        if algo == "全掩码":
            return "*" * len(raw), True, algo
        if algo == "前缀保留" and len(raw) > prefix_n:
            return raw[:prefix_n] + "*" * (len(raw) - prefix_n), True, algo
        if algo == "前后保留" and len(raw) > prefix_n + suffix_n:
            return (raw[:prefix_n] + "*" * (len(raw) - prefix_n - suffix_n) + raw[-suffix_n:]), True, algo
        if algo == "哈希脱敏":
            return hashlib.sha256(raw.encode()).hexdigest()[:16], True, algo
        return value, False, ""

    def _raw_field_value(self, audit: dict[str, Any], field: str) -> Any:
        business = audit.get("业务字段") or {}
        if field in business:
            return business[field]
        value = audit.get(field)
        # 返回行数、响应耗时是对账用的公共数字，统一成数值交付，避免字符串比较误差。
        if field in ("返回行数", "响应耗时") and str(value).strip().isdigit():
            return int(str(value).strip())
        return value

    def _assemble_columns(self, subscription: dict[str, Any], audit: dict[str, Any],
                          controlled_fields: list[str]) -> list[dict[str, Any]]:
        shared_fields = list(subscription.get("共享字段") or DEFAULT_SHARED_FIELDS)
        columns: list[dict[str, Any]] = []
        seen: set[str] = set()
        for field in [*shared_fields, *controlled_fields]:
            if field in seen:
                continue
            seen.add(field)
            raw = self._raw_field_value(audit, field)
            masked_value, masked, algo = self._mask_value(
                raw, self._mask_config(subscription, field))
            columns.append({
                "field": field,
                "class": "共享" if field in shared_fields else "受控",
                "value": masked_value,
                "masked": masked,
                "mask_algorithm": algo,
            })
        return columns

    def _usage_reconciliation(self, audit: dict[str, Any]) -> dict[str, Any]:
        """公共数字对应哪次请求：按 API + 调用方汇总用量，和返回行数做差。"""
        returned_rows = self._safe_int(audit.get("返回行数", 0))
        matched = [
            row for row in store.rows("usage_billing")
            if row.get("API名称") == audit.get("目标API")
            and row.get("调用方") == audit.get("调用方")
        ]
        usage_rows = sum(self._safe_int(row.get("数据行数", 0)) for row in matched)
        call_count = sum(self._safe_int(row.get("调用次数", 0)) for row in matched)
        return {
            "request_no": audit.get("请求号"),
            "returned_rows": returned_rows,
            "usage_record_no": matched[0].get("记录编号") if matched else None,
            "usage_record_count": len(matched),
            "usage_data_rows": usage_rows,
            "usage_call_count": call_count,
            "difference": returned_rows - usage_rows,
            "linked_by": "请求号",
        }

    # ------------------------------------------------------------------
    # 数据包
    # ------------------------------------------------------------------

    def build_package(self, subscription: dict[str, Any], audit: dict[str, Any],
                      *, reissued: bool = False, first_issued_at: str | None = None,
                      reissue_count: int = 0) -> dict[str, Any]:
        """按单个收件方订阅范围生成审计分发数据包（不落盘）。"""
        subscription_id = int(subscription.get("id", 0))
        request_no = str(audit.get("请求号") or "")
        permission = self.check_permission(subscription, audit)
        in_scope, scope_details = self._scope_state(subscription, audit)

        shared_fields = list(subscription.get("共享字段") or DEFAULT_SHARED_FIELDS)
        declared_controlled = [
            field for field in (subscription.get("专有字段") or [])
            if field not in shared_fields
        ]

        withheld: list[dict[str, Any]] = []
        if not permission["eligible"]:
            delivered_controlled: list[str] = []
            withheld = [{"field": f, "reason": "订阅权限未通过"} for f in declared_controlled]
        elif not in_scope:
            delivered_controlled = []
            failed = [item for item in scope_details if not item["passed"]]
            reason = "调用明细超出订阅数据范围：" + "；".join(
                f"{item['field']}='{item['actual']}' 不在 {item['expected']}" for item in failed)
            withheld = [{"field": f, "reason": reason} for f in declared_controlled]
        else:
            delivered_controlled = declared_controlled

        columns = self._assemble_columns(subscription, audit, delivered_controlled)
        delivered_names = {column["field"] for column in columns}
        leaked = [f for f in withheld if f["field"] in delivered_names]

        reconciliation = self._usage_reconciliation(audit)

        if not permission["eligible"]:
            status = "已拒发"
        else:
            status = "已发放"

        package = {
            "package_id": f"DIST-{request_no}-S{subscription_id}",
            "request_no": request_no,
            "audit_id": audit.get("id"),
            "subscription_id": subscription_id,
            "application_no": subscription.get("申请编号"),
            "recipient": subscription.get("申请方"),
            "api_name": audit.get("目标API"),
            "status": status,
            "reissued": reissued,
            "reissue_count": reissue_count,
            "permission": permission,
            "scope": {
                "confirmed": subscription.get("范围确认") == "已确认",
                "in_scope": in_scope,
                "checks": scope_details,
            },
            "reconciliation": reconciliation,
            "shared_fields": shared_fields,
            "controlled_fields": delivered_controlled,
            "withheld_fields": withheld,
            "columns": columns,
            "row_count": 1 if permission["eligible"] else 0,
            "returned_rows": reconciliation["returned_rows"],
            "cross_scope_leaks": leaked,
            "source": {
                "audit_id": audit.get("id"),
                "record_no": audit.get("记录编号"),
                "request_no": request_no,
                "linked_by": "请求号",
            },
        }
        if first_issued_at:
            package["first_issued_at"] = first_issued_at
        return package

    def preview_permissions(self, audit_id: int) -> dict[str, Any]:
        """分发前核对：列出该审计明细涉及的每个订阅的权限检查结果。"""
        audit = self._audit_by_id(audit_id)
        if audit is None:
            return {"ok": False, "message": f"调用记录 {audit_id} 不存在或已归档"}

        recipients = []
        for subscription in self._subscriptions_for_api(audit.get("目标API", "")):
            permission = self.check_permission(subscription, audit)
            in_scope, scope_details = self._scope_state(subscription, audit)
            recipients.append({
                "subscription_id": subscription.get("id"),
                "application_no": subscription.get("申请编号"),
                "recipient": subscription.get("申请方"),
                "eligible": permission["eligible"],
                "reasons": permission["reasons"],
                "checks": permission["checks"],
                "in_scope": in_scope,
                "scope_checks": scope_details,
            })
        return {
            "ok": True,
            "audit_id": audit_id,
            "request_no": audit.get("请求号"),
            "api_name": audit.get("目标API"),
            "audit_status": audit.get("status"),
            "recipients": recipients,
        }

    def distribute(self, audit_id: int, subscription_ids: list[int] | None = None,
                   *, dry_run: bool = False) -> dict[str, Any]:
        """生成并发放审计分发数据包，逐订阅核对权限并隔离专有字段。"""
        audit = self._audit_by_id(audit_id)
        if audit is None:
            return {"ok": False, "message": f"调用记录 {audit_id} 不存在或已归档"}
        request_no = str(audit.get("请求号") or "").strip()
        if not request_no:
            return {"ok": False, "message": "审计明细缺少请求号，无法关联原始调用"}

        subscriptions = self._subscriptions_for_api(audit.get("目标API", ""))
        if subscription_ids:
            wanted = {int(item) for item in subscription_ids}
            subscriptions = [s for s in subscriptions if int(s.get("id", 0)) in wanted]

        packages: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for subscription in subscriptions:
            permission = self.check_permission(subscription, audit)
            if not permission["eligible"]:
                rejected.append({
                    "subscription_id": subscription.get("id"),
                    "application_no": subscription.get("申请编号"),
                    "recipient": subscription.get("申请方"),
                    "reasons": permission["reasons"],
                })
                continue
            package = self.build_package(subscription, audit)
            if not dry_run:
                self._persist(package)
            packages.append(package)

        return {
            "ok": True,
            "dry_run": dry_run,
            "audit_id": audit_id,
            "request_no": request_no,
            "api_name": audit.get("目标API"),
            "returned_rows": self._safe_int(audit.get("返回行数", 0)),
            "shared_numbers": {
                "request_no": request_no,
                "returned_rows": self._safe_int(audit.get("返回行数", 0)),
            },
            "packages": packages,
            "rejected": rejected,
        }

    # ------------------------------------------------------------------
    # 落盘与重发
    # ------------------------------------------------------------------

    def _persist(self, package: dict[str, Any]) -> None:
        from app.services.data_manager import write_distribution_package

        write_distribution_package(
            package["request_no"], package["subscription_id"], package)

    def read_package(self, request_no: str, subscription_id: int) -> dict[str, Any]:
        """读取已发放的数据包，用于核对首次交付范围。"""
        from app.services.data_manager import read_distribution_package

        package = read_distribution_package(request_no, subscription_id)
        if not package:
            return {"ok": False, "message": f"请求号 {request_no} 未向订阅 {subscription_id} 发放过数据包"}
        return {"ok": True, "package": package}

    def reissue(self, request_no: str, subscription_id: int) -> dict[str, Any]:
        """重发：按请求号回到原始审计明细，沿用首次发放的收件方与字段范围。"""
        from app.services.data_manager import read_distribution_package

        original = read_distribution_package(request_no, subscription_id)
        if not original:
            return {"ok": False,
                    "message": f"请求号 {request_no} 从未向订阅 {subscription_id} 发放，不能重发"}
        if original.get("status") != "已发放":
            return {"ok": False, "message": "原数据包为拒发状态，不能重发"}

        audit = self._audit_by_request_no(request_no)
        if audit is None:
            return {"ok": False,
                    "message": f"请求号 {request_no} 找不到原始审计明细，重发中止"}

        subscription = self._subscription(subscription_id)
        permission = self.check_permission(subscription, audit)
        if not permission["eligible"]:
            return {"ok": False, "message": "重发前订阅权限复核未通过",
                    "permission": permission}

        package = self.build_package(
            subscription, audit, reissued=True,
            first_issued_at=original.get("first_issued_at"),
            reissue_count=int(original.get("reissue_count", 0)) + 1,
        )
        # 保留首次发放时的来源锚点，重发不换请求号、不换收件方
        package["source"]["first_issued_at"] = original.get("first_issued_at")
        package["source"]["original_audit_id"] = (original.get("source") or {}).get("audit_id")
        package["source"]["original_request_no"] = (original.get("source") or {}).get("request_no")
        package["source_drift"] = {
            "returned_rows_changed":
                self._safe_int((original.get("reconciliation") or {}).get("returned_rows", 0))
                != self._safe_int(audit.get("返回行数", 0)),
            "audit_id_changed":
                (original.get("source") or {}).get("audit_id") != audit.get("id"),
        }
        self._persist(package)
        return {"ok": True, "package": package}

    # ------------------------------------------------------------------
    # 双订阅对照
    # ------------------------------------------------------------------

    def compare(self, request_no: str, subscription_ids: list[int]) -> dict[str, Any]:
        """用两份权限范围部分重叠的订阅取同一条请求的复核材料，对照列与行数。"""
        audit = self._audit_by_request_no(request_no)
        if audit is None:
            return {"ok": False, "message": f"请求号 {request_no} 找不到原始审计明细"}

        packages = []
        for subscription_id in subscription_ids:
            subscription = self._subscription(int(subscription_id))
            if subscription is None:
                return {"ok": False, "message": f"订阅 {subscription_id} 不存在"}
            packages.append(self.build_package(subscription, audit))

        comparison_rows = []
        columns_by_recipient: dict[str, list[str]] = {}
        controlled_by_recipient: dict[str, list[str]] = {}
        for package in packages:
            sid = str(package["subscription_id"])
            eligible = bool(package["permission"]["eligible"])
            # 权限未通过的收件方不能借对照拿到任何可用列。
            names = [column["field"] for column in package["columns"]] if eligible else []
            columns_by_recipient[sid] = names
            controlled_by_recipient[sid] = list(package["controlled_fields"]) if eligible else []
            comparison_rows.append({
                "subscription_id": package["subscription_id"],
                "recipient": package["recipient"],
                "application_no": package["application_no"],
                "eligible": eligible,
                "in_scope": package["scope"]["in_scope"],
                "request_no": package["request_no"],
                "returned_rows": package["returned_rows"] if eligible else None,
                "row_count": package["row_count"],
                "usable_columns": names,
                "usable_column_count": len(names),
                "controlled_columns": list(package["controlled_fields"]) if eligible else [],
                "withheld_fields": package["withheld_fields"],
                "reconciliation": package["reconciliation"] if eligible else None,
                "denied_reasons": package["permission"]["reasons"],
            })

        name_sets = [set(names) for names in columns_by_recipient.values()]
        overlap = set.intersection(*name_sets) if name_sets else set()
        unique = {
            sid: sorted(set(names) - overlap) for sid, names in columns_by_recipient.items()
        }

        returned_rows = {
            package["returned_rows"] for package in packages
            if package["permission"]["eligible"]
        }
        request_nos = {
            package["request_no"] for package in packages
            if package["permission"]["eligible"]
        }

        # 跨订阅串字段检查：任何一方的受控列都不能出现在别人的可用列里
        leaks: list[dict[str, Any]] = []
        for package in packages:
            own = set(package["shared_fields"]) | set(package["controlled_fields"])
            for column in package["columns"]:
                if column["field"] not in own:
                    leaks.append({
                        "subscription_id": package["subscription_id"],
                        "field": column["field"],
                        "reason": "字段既不属于共享列也不属于本订阅受控列",
                    })
        for sid, controlled in controlled_by_recipient.items():
            for other_sid, columns in columns_by_recipient.items():
                if other_sid == sid:
                    continue
                for field in controlled:
                    if field in columns:
                        leaks.append({
                            "subscription_id": other_sid,
                            "field": field,
                            "reason": f"出现了订阅 {sid} 的专有字段",
                        })

        return {
            "ok": True,
            "request_no": request_no,
            "audit_id": audit.get("id"),
            "audit_status": audit.get("status"),
            "api_name": audit.get("目标API"),
            "recipients": comparison_rows,
            "overlap_columns": sorted(overlap),
            "unique_columns": unique,
            "reconciliation": {
                "same_request_no": len(request_nos) <= 1,
                "same_returned_rows": len(returned_rows) <= 1,
                "request_no": next(iter(request_nos), None),
                "returned_rows": next(iter(returned_rows), None),
            },
            "cross_scope_leaks": leaks,
            "isolated": not leaks,
        }


audit_distribution = AuditDistributionService()
