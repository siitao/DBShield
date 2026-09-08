"""
AI 用量查询 API（超管）

路由：
  GET /api/v1/ai_usage/summary/?days=7   — 总览 + 按能力/按天/按用户聚合
  GET /api/v1/ai_usage/list/             — 明细分页（capability/user_name/status/instance_name 筛选）

数据源为 AIUsageLog 流水（record_ai_usage 旁路写入）。缓存命中行
cache_hit=true 且 tokens 为 0，计入调用量用于命中率统计，不计成本。
"""
import datetime

from common.utils.extend_json_encoder import encode_json as _encode
from django.db.models import Avg, Count, F, Q, Sum
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from sql.models import AIUsageLog
from sql_api.api_config import IsSuperuser


def _date_or_none(value):
    """TruncDate 返回 date，转 ISO 字符串便于 JSON 序列化与前端展示。"""
    return value.isoformat() if value else None


def _norm_agg(agg: dict) -> dict:
    """Sum 无数据时为 None，统一归 0；耗时保留 1 位小数。"""
    for key in ("calls", "cache_hits", "failed", "prompt_tokens", "completion_tokens"):
        agg[key] = agg.get(key) or 0
    avg = agg.get("avg_latency_ms")
    agg["avg_latency_ms"] = round(float(avg), 1) if avg is not None else 0
    return agg


class AiUsageSummaryView(APIView):
    """AI 用量总览聚合"""

    permission_classes = [IsAuthenticated, IsSuperuser]

    def get(self, request):
        try:
            days = min(max(int(request.query_params.get("days", 7) or 7), 1), 90)
        except (TypeError, ValueError):
            days = 7
        cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
        qs = AIUsageLog.objects.filter(created_at__gte=cutoff)

        totals = _norm_agg(qs.aggregate(
            calls=Count("id"),
            cache_hits=Count("id", filter=Q(cache_hit=True)),
            failed=Count("id", filter=Q(status="failed")),
            prompt_tokens=Sum("prompt_tokens"),
            completion_tokens=Sum("completion_tokens"),
            avg_latency_ms=Avg("latency_ms"),
        ))

        by_capability = [
            _norm_agg(row) for row in qs.values("capability").annotate(
                calls=Count("id"),
                cache_hits=Count("id", filter=Q(cache_hit=True)),
                failed=Count("id", filter=Q(status="failed")),
                prompt_tokens=Sum("prompt_tokens"),
                completion_tokens=Sum("completion_tokens"),
                avg_latency_ms=Avg("latency_ms"),
            ).order_by("-calls")
        ]

        by_day = [
            {
                "day": _date_or_none(row.pop("day")),
                **_norm_agg(row),
            }
            for row in qs.annotate(day=TruncDate("created_at")).values("day").annotate(
                calls=Count("id"),
                cache_hits=Count("id", filter=Q(cache_hit=True)),
                failed=Count("id", filter=Q(status="failed")),
                prompt_tokens=Sum("prompt_tokens"),
                completion_tokens=Sum("completion_tokens"),
                avg_latency_ms=Avg("latency_ms"),
            ).order_by("day")
        ]

        by_user = [
            _norm_agg(row) for row in qs.values("user_name").annotate(
                calls=Count("id"),
                cache_hits=Count("id", filter=Q(cache_hit=True)),
                failed=Count("id", filter=Q(status="failed")),
                prompt_tokens=Sum("prompt_tokens"),
                completion_tokens=Sum("completion_tokens"),
                avg_latency_ms=Avg("latency_ms"),
            ).annotate(
                # 聚合间的运算需落在后续 annotate 中用 F() 引用（同段内
                # Sum+Sum 会触发 "is an aggregate" FieldError）
                total_tokens=F("prompt_tokens") + F("completion_tokens"),
            ).order_by("-total_tokens")[:10]
        ]

        return JsonResponse(
            _encode({
                "days": days,
                "totals": totals,
                "by_capability": by_capability,
                "by_day": by_day,
                "by_user": by_user,
            }),
            safe=False,
        )


class AiUsageListView(APIView):
    """AI 用量明细分页"""

    permission_classes = [IsAuthenticated, IsSuperuser]

    def get(self, request):
        qp = request.query_params
        try:
            limit = min(max(int(qp.get("limit", 20) or 20), 1), 200)
            offset = max(int(qp.get("offset", 0) or 0), 0)
        except (TypeError, ValueError):
            limit, offset = 20, 0

        qs = AIUsageLog.objects.all()
        for param in ("capability", "user_name", "status", "instance_name"):
            value = (qp.get(param) or "").strip()
            if value:
                qs = qs.filter(**{param: value})

        total = qs.count()
        rows = list(
            qs.order_by("-id")[offset:offset + limit].values(
                "id", "created_at", "capability", "model", "db_type",
                "instance_name", "db_name", "user_name",
                "prompt_tokens", "completion_tokens",
                "latency_ms", "cache_hit", "status", "error",
            )
        )
        return JsonResponse(_encode({"total": total, "rows": rows}), safe=False)
