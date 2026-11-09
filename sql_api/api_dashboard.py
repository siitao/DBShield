"""Dashboard 图表数据（JSON）。

旧版 ``common/dashboard.py:DashboardApi`` 返回 pyecharts ``render_embed()`` 的 HTML/JS 片段，
无法直接在 Vue/echarts 渲染。这里直接复用 ``ChartDao`` 的原始行数据，返回结构化 JSON，
供前端用 echarts 自行绘制。日期范围必传（默认由前端给近 30 天）。

数据范围：超管/审计员看全局；其他用户只看自己资源组内的工单、自己可访问实例的查询
与慢查数据，避免 Dashboard 泄露其他组信息。
"""

import datetime
import logging

from drf_spectacular.utils import extend_schema
from rest_framework import views, permissions
from rest_framework.response import Response

from common.utils.chart_dao import ChartDao
from sql.utils.resource_group import user_groups, user_instances

logger = logging.getLogger("default")


def _validate_date(date_str):
    """校验 YYYY-MM-DD，非法抛 ValueError。"""
    return datetime.datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")


def _pairs(rows):
    """[(label, value), ...] → [{name, value}]（pie/bar 通用）。"""
    return [{"name": str(r[0]), "value": r[1]} for r in rows]


def _dashboard_scope(user):
    """返回 (group_ids, instance_names, instance_ids)。

    超管/审计员返回三个 None 表示不限制；其他用户按资源组/可访问实例收口。
    """
    if user.is_superuser or user.has_perm("sql.audit_user"):
        return None, None, None
    group_ids = [g.group_id for g in user_groups(user)]
    instances = list(user_instances(user).values_list("id", "instance_name"))
    return (
        group_ids,
        [name for _, name in instances],
        [instance_id for instance_id, _ in instances],
    )


class DashboardCharts(views.APIView):
    """Dashboard 10 张图表的原始数据（echarts 友好）。"""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Dashboard 图表数据",
        description="返回 SQL 上线/查询/慢查等 10 张图表的原始数据，供前端 echarts 渲染。",
    )
    def get(self, request):
        start_date_str = request.GET.get("start_date")
        end_date_str = request.GET.get("end_date")
        try:
            start_date = _validate_date(start_date_str)
            end_date = _validate_date(end_date_str)
        except (ValueError, TypeError):
            return Response({"error": "日期有误，格式需为 YYYY-MM-DD"}, status=400)

        chart_dao = ChartDao()
        group_ids, instance_names, instance_ids = _dashboard_scope(request.user)

        # 日期序列（bar1 / line1 横轴对齐用）
        date_list = chart_dao.get_date_list(
            datetime.datetime.strptime(start_date, "%Y-%m-%d"),
            datetime.datetime.strptime(end_date, "%Y-%m-%d"),
        )

        def by_date(dao_rows):
            """{date: count} → 按 date_list 对齐的值数组（缺日补 0）。"""
            d = {row[0]: row[1] for row in dao_rows}
            return [d.get(day, 0) for day in date_list]

        def safe(fn, default):
            """单张图查询独立容错：表不存在/查询失败时返回默认值，不让整个 dashboard 挂掉。"""
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                logger.warning("Dashboard 图表数据查询失败: %s", e)
                return default

        def bar_from_rows(name, dao_result):
            rows = dao_result["rows"]
            return {
                "x": [r[0] for r in rows],
                "series": [{"name": name, "data": [r[1] for r in rows]}],
            }

        EMPTY_BAR = {"x": [], "series": [{"name": "", "data": []}]}

        # bar1 SQL 上线数量（按日）
        bar1 = safe(
            lambda: {
                "x": date_list,
                "series": [
                    {
                        "name": "SQL 上线数量",
                        "data": by_date(
                            chart_dao.workflow_by_date(start_date, end_date, group_ids)[
                                "rows"
                            ]
                        ),
                    }
                ],
            },
            {"x": date_list, "series": [{"name": "SQL 上线数量", "data": []}]},
        )
        # pie1 SQL 上线统计（按组）
        pie1 = safe(
            lambda: _pairs(
                chart_dao.workflow_by_group(start_date, end_date, group_ids)["rows"]
            ),
            [],
        )
        # pie2 SQL 语法类型
        pie2 = safe(
            lambda: _pairs(
                chart_dao.syntax_type(start_date, end_date, group_ids)["rows"]
            ),
            [],
        )
        # bar2 SQL 上线用户
        bar2 = safe(
            lambda: bar_from_rows(
                "工单数",
                chart_dao.workflow_by_user(start_date, end_date, group_ids),
            ),
            EMPTY_BAR,
        )
        # line1 SQL 查询统计（检索行数 + 检索次数，按日双 series）
        line1 = safe(
            lambda: {
                "x": date_list,
                "series": [
                    {
                        "name": "检索行数",
                        "data": by_date(
                            chart_dao.querylog_effect_row_by_date(
                                start_date, end_date, instance_names
                            )["rows"]
                        ),
                    },
                    {
                        "name": "检索次数",
                        "data": by_date(
                            chart_dao.querylog_count_by_date(
                                start_date, end_date, instance_names
                            )["rows"]
                        ),
                    },
                ],
            },
            {
                "x": date_list,
                "series": [
                    {"name": "检索行数", "data": []},
                    {"name": "检索次数", "data": []},
                ],
            },
        )
        # pie4 SQL 查询用户（检索行数）
        pie4 = safe(
            lambda: _pairs(
                chart_dao.querylog_effect_row_by_user(
                    start_date, end_date, instance_names
                )["rows"]
            ),
            [],
        )
        # pie5 DB 检索行数
        pie5 = safe(
            lambda: _pairs(
                chart_dao.querylog_effect_row_by_db(
                    start_date, end_date, instance_names
                )["rows"]
            ),
            [],
        )
        # pie3 慢查询 db/user 维度（依赖慢查询表，可能未配置）
        pie3 = safe(
            lambda: _pairs(
                chart_dao.slow_query_count_by_db_by_user(instance_ids)["rows"]
            ),
            [],
        )
        # bar3 慢查询 db 维度（依赖慢查询表）
        bar3 = safe(
            lambda: bar_from_rows(
                "慢查数", chart_dao.slow_query_count_by_db(instance_ids)
            ),
            EMPTY_BAR,
        )
        # bar5 SQL 上线工单
        bar5 = safe(
            lambda: bar_from_rows(
                "工单数",
                chart_dao.query_sql_prod_bill(start_date, end_date, group_ids),
            ),
            EMPTY_BAR,
        )

        return Response(
            {
                "bar1": bar1,
                "bar2": bar2,
                "bar3": bar3,
                "bar5": bar5,
                "pie1": pie1,
                "pie2": pie2,
                "pie3": pie3,
                "pie4": pie4,
                "pie5": pie5,
                "line1": line1,
            }
        )
