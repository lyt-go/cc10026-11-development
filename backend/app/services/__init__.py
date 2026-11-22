"""领域服务注册表：通用资源路由按模块 key 分派到独立业务服务。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.api_registry import ApiRegistryService
from app.services.query_route import QueryRouteService
from app.services.subscription import SubscriptionService
from app.services.rate_limiter import RateLimiterService
from app.services.usage_billing import UsageBillingService
from app.services.cache_layer import CacheLayerService
from app.services.data_filter import DataFilterService
from app.services.field_mask import FieldMaskService
from app.services.sla_monitor import SlaMonitorService
from app.services.result_format import ResultFormatService
from app.services.query_optimize import QueryOptimizeService
from app.services.error_classify import ErrorClassifyService
from app.services.data_merge import DataMergeService
from app.services.export_task import ExportTaskService
from app.services.call_audit import CallAuditService
from app.services.version_compat import VersionCompatService
from app.services.batch_query import BatchQueryService
from app.services.sdk_config import SdkConfigService
from app.services.audit_distribution import AuditDistributionService

SERVICES: dict[str, BaseModuleService] = {
    "api_registry": ApiRegistryService(),
    "query_route": QueryRouteService(),
    "subscription": SubscriptionService(),
    "rate_limiter": RateLimiterService(),
    "usage_billing": UsageBillingService(),
    "cache_layer": CacheLayerService(),
    "data_filter": DataFilterService(),
    "field_mask": FieldMaskService(),
    "sla_monitor": SlaMonitorService(),
    "result_format": ResultFormatService(),
    "query_optimize": QueryOptimizeService(),
    "error_classify": ErrorClassifyService(),
    "data_merge": DataMergeService(),
    "export_task": ExportTaskService(),
    "call_audit": CallAuditService(),
    "version_compat": VersionCompatService(),
    "batch_query": BatchQueryService(),
    "sdk_config": SdkConfigService(),
}

# 审计分发不是独立业务对象，而是跨 call_audit / subscription / usage_billing
# 的交付能力，单独暴露给分发路由使用。
audit_distribution = AuditDistributionService()


def service_for(module: str) -> BaseModuleService | None:
    return SERVICES.get(module)
