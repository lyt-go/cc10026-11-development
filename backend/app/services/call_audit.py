"""调用审计领域服务：管理API调用的审计记录、归档和查询追溯。

跨模块数据流：
- 消费 usage_billing 快照 → 用量异常触发审计核查
- 消费 subscription 共享记录 → 审批通过的订阅记录审计
- 消费 export_task 快照 → 导出操作记录审计
- 审计记录定期归档 → 写入 data/audit/ 共享目录
- 被 error_classify 消费 → 异常调用触发错误归类
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class CallAuditService(BaseModuleService):
    MODULE = "call_audit"
    ENTITY = "调用记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "请求号", "调用方", "目标API", "请求参数", "响应码", "响应耗时", "返回行数", "时间戳"]
    REQUIRED_FIELDS = ["记录编号", "调用方", "目标API"]
    STATUS_ORDER = ["已记录", "已校验", "已归档", "已过期"]
    ACTION_RULES = {"确认校验": "已校验", "提交归档": "已归档", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    def record_api_call(self, caller: str, api_name: str, params: str = "",
                        response_code: str = "200", latency_ms: float = 0.0,
                        result_rows: int = 0, request_no: str | None = None,
                        business_fields: dict[str, Any] | None = None) -> dict[str, Any]:
        """记录一次API调用到审计日志。

        request_no 是同一次原始调用的公共关联标识，分发、重发和用量对账都靠它。
        未显式传入时按日期与流水号生成，保证每条审计明细都有可回溯的请求号。
        """
        from app.services.data_manager import write_audit
        from app.store import store as s

        audit_rows = s.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1
        record_no = f"CALL-{new_id:04d}"
        request_no = (request_no or "").strip() or f"REQ-{self._today_compact()}-{new_id:04d}"
        business_fields = dict(business_fields or {})

        audit_data = {
            "audit_id": new_id,
            "record_no": record_no,
            "request_no": request_no,
            "caller": caller,
            "api_name": api_name,
            "request_params": params,
            "response_code": response_code,
            "latency_ms": latency_ms,
            "result_rows": result_rows,
            "timestamp": self._now_iso(),
            "business_fields": business_fields,
            "status": "已记录",
        }
        write_audit(new_id, audit_data)

        # 同时记录到内存
        entry = {
            "id": new_id,
            "记录编号": record_no,
            "请求号": request_no,
            "调用方": caller,
            "目标API": api_name,
            "请求参数": params,
            "响应码": response_code,
            "响应耗时": str(latency_ms),
            "返回行数": str(result_rows),
            "时间戳": self._now_iso(),
            "业务字段": business_fields,
            "status": "已记录",
            "pending": True,
            "abnormal": response_code != "200",
        }
        audit_rows.append(entry)
        return entry

    def _today_compact(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")

    def _now_iso(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).isoformat()

    def verify_audits(self, caller: str, api_name: str) -> list[dict[str, Any]]:
        """校验调用方对指定API的审计记录。"""
        from app.services.data_manager import list_audit, read_api_snapshot

        # 检查API是否已注册
        registry = read_api_snapshot()
        api_exists = any(
            isinstance(v, dict) and v.get("api_name") == api_name
            for v in (registry.values() if isinstance(registry, dict) else []))

        if not api_exists:
            return []

        audits = list_audit(api_name=api_name, caller=caller)
        # 校验订阅状态
        from app.services.data_manager import list_subscriptions
        subs = list_subscriptions()
        valid_sub = any(
            s.get("api_name") == api_name and s.get("applicant") == caller and s.get("approval_status") == "已通过"
            for s in subs
        )

        for audit in audits:
            audit["subscription_valid"] = valid_sub
            audit["verified"] = valid_sub
        return audits

    def archive_old_records(self, before_date: str) -> dict[str, Any]:
        """归档指定日期之前的审计记录。"""
        from app.services.data_manager import archive_audit_records
        return archive_audit_records(before_date)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "提交归档":
            self.archive_old_records(self._today_str())

        if action == "生成分发数据包":
            from app.services.audit_distribution import audit_distribution
            result = audit_distribution.distribute(entry_id, dry_run=True)
            if not result.get("ok"):
                return None, result.get("message", "分发核对失败")
            recipients = [p.get("recipient") for p in result.get("packages", [])]
            rejected = [r.get("recipient") for r in result.get("rejected", [])]
            message = (f"分发核对完成：可发放 {len(recipients)} 份"
                       f"{('（' + '、'.join(recipients) + '）') if recipients else ''}；"
                       f"拒发 {len(rejected)} 份")
            return result, message

        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def _today_str(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        audit_list = []
        for row in rows:
            audit_list.append({
                "id": row.get("id"),
                "record_no": row.get("记录编号"),
                "request_no": row.get("请求号"),
                "caller": row.get("调用方"),
                "api": row.get("目标API"),
                "params": row.get("请求参数"),
                "response_code": row.get("响应码"),
                "latency": row.get("响应耗时"),
                "result_rows": row.get("返回行数"),
                "timestamp": row.get("时间戳"),
                "business_fields": row.get("业务字段") or {},
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(audit_list),
            "audits": audit_list,
            "archived_count": sum(1 for r in audit_list if r.get("status") == "已归档"),
            "abnormal_count": sum(1 for r in rows if r.get("abnormal")),
        }