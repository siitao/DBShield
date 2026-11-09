# -*- coding: UTF-8 -*-
"""P2 收口用例：分页上限、查询历史默认页大小、敏感序列化字段。"""


def test_pagination_has_max_page_size():
    from sql_api.pagination import CustomizedPagination

    assert CustomizedPagination.max_page_size == 500


def test_query_logs_default_limit_is_bounded():
    from sql_api.serializers import SqlQueryLogsQuerySerializer

    serializer = SqlQueryLogsQuerySerializer(data={})
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["limit"] == 20
    assert serializer.validated_data["offset"] == 0


def test_query_logs_limit_rejects_zero_and_oversize():
    from sql_api.serializers import SqlQueryLogsQuerySerializer

    assert not SqlQueryLogsQuerySerializer(data={"limit": 0}).is_valid()
    assert not SqlQueryLogsQuerySerializer(data={"limit": 10000}).is_valid()


def test_tunnel_secret_fields_are_write_only():
    from sql_api.serializers import TunnelSerializer

    fields = TunnelSerializer().fields
    assert fields["password"].write_only is True
    assert fields["pkey"].write_only is True
    assert fields["pkey_password"].write_only is True


def test_dashboard_scope_filters_normal_user(monkeypatch):
    from types import SimpleNamespace

    from sql_api import api_dashboard

    user = SimpleNamespace(
        is_superuser=False,
        has_perm=lambda perm: False,
    )
    monkeypatch.setattr(
        api_dashboard, "user_groups", lambda u: [SimpleNamespace(group_id=11)]
    )
    monkeypatch.setattr(
        api_dashboard,
        "user_instances",
        lambda u: SimpleNamespace(values_list=lambda *a: [(7, "ins-a")]),
    )

    assert api_dashboard._dashboard_scope(user) == ([11], ["ins-a"], [7])


def test_dashboard_scope_unrestricted_for_auditor():
    from types import SimpleNamespace

    from sql_api import api_dashboard

    user = SimpleNamespace(
        is_superuser=False,
        has_perm=lambda perm: perm == "sql.audit_user",
    )
    assert api_dashboard._dashboard_scope(user) == (None, None, None)
