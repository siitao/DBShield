# -*- coding: UTF-8 -*-
"""表级结构对比 API 用例：权限、入参校验、服务调用透传（mock 服务，不连库）。"""

import pytest
from rest_framework.test import APIClient

TABLEDIFF = "/api/v1/schema/tablediff/"

VALID_PAYLOAD = {
    "instance_name": "src_ins",
    "db_name": "db1",
    "target_instance_name": "dst_ins",
    "target_db_name": "db2",
    "tables": ["t1", "t2"],
    "sync_auto_inc": False,
    "sync_comments": True,
}


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def authenticated_user(normal_user):
    return normal_user


@pytest.fixture
def privileged_user(super_user):
    return super_user


@pytest.mark.django_db
def test_tablediff_requires_menu_permission(api_client, authenticated_user):
    """无 sql.menu_schemasync 权限返回 403"""
    api_client.force_authenticate(user=authenticated_user)
    response = api_client.post(TABLEDIFF, VALID_PAYLOAD, format="json")

    assert response.status_code == 403


@pytest.mark.django_db
def test_tablediff_calls_service_with_validated_params(
    monkeypatch, api_client, privileged_user
):
    captured = {}

    def fake_table_diff(**kwargs):
        captured.update(kwargs)
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "summary": {"total": 1, "diff": 1, "same": 0, "missing": 0, "objects": 0},
                "tables": [],
                "objects": [],
                "patch_sql": "ALTER TABLE `t1` ADD COLUMN `c` int;",
                "revert_sql": "",
            },
        }

    monkeypatch.setattr("sql_api.api_schema.table_diff", fake_table_diff)

    api_client.force_authenticate(user=privileged_user)
    response = api_client.post(TABLEDIFF, VALID_PAYLOAD, format="json")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == 0
    assert body["data"]["patch_sql"].startswith("ALTER TABLE `t1`")
    assert captured["instance_name"] == "src_ins"
    assert captured["target_instance_name"] == "dst_ins"
    assert captured["db_name"] == "db1"
    assert captured["target_db_name"] == "db2"
    assert captured["tables"] == ["t1", "t2"]
    assert captured["sync_comments"] is True
    assert captured["sync_auto_inc"] is False


@pytest.mark.django_db
def test_tablediff_rejects_dangerous_table_names(
    monkeypatch, api_client, privileged_user
):
    def _boom(*args, **kwargs):
        raise AssertionError("入参非法时不应触达服务层")

    monkeypatch.setattr("sql_api.api_schema.table_diff", _boom)

    api_client.force_authenticate(user=privileged_user)
    bad_payloads = [
        {**VALID_PAYLOAD, "tables": ["t1; drop database x"]},
        {**VALID_PAYLOAD, "tables": ["`t1`"]},
        {**VALID_PAYLOAD, "tables": ["a'b"]},
    ]
    for payload in bad_payloads:
        response = api_client.post(TABLEDIFF, payload, format="json")
        assert response.status_code == 400, payload


@pytest.mark.django_db
def test_tablediff_rejects_too_many_tables(api_client, privileged_user):
    api_client.force_authenticate(user=privileged_user)
    payload = {**VALID_PAYLOAD, "tables": [f"t{i}" for i in range(201)]}
    response = api_client.post(TABLEDIFF, payload, format="json")

    assert response.status_code == 400


@pytest.mark.django_db
def test_tablediff_tables_optional_means_whole_db(
    monkeypatch, api_client, privileged_user
):
    captured = {}

    def fake_table_diff(**kwargs):
        captured.update(kwargs)
        return {"status": 0, "msg": "ok", "data": {"summary": {}, "tables": [], "objects": [], "patch_sql": "", "revert_sql": ""}}

    monkeypatch.setattr("sql_api.api_schema.table_diff", fake_table_diff)

    api_client.force_authenticate(user=privileged_user)
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "tables"}
    response = api_client.post(TABLEDIFF, payload, format="json")

    assert response.status_code == 200
    assert captured["tables"] == []
