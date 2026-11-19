# -*- coding:utf-8 -*-
"""Mongo 慢查询采集器连接参数测试

实例 host 支持逗号分隔的复制集种子节点，必须交给 pymongo 按 seed list
解析，不能拼成 ``mongodb://host:port/``。
"""
from unittest.mock import patch

from sql.collectors.mongo_collector import MongoSlowQueryCollector


class FakeMongoInstance:
    id = 1
    instance_name = "mongo-rs"
    db_type = "mongo"
    host = "172.30.82.7:3717, 172.30.82.8:3717,172.30.82.5:3717"
    port = 3717
    db_name = ""
    user = "root"
    password = "p@ss"
    is_ssl = False
    verify_ssl = True

    def get_username_password(self):
        return self.user, self.password


def _collector(instance=None):
    return MongoSlowQueryCollector(instance or FakeMongoInstance())


@patch("pymongo.MongoClient")
def test_replica_set_hosts_passed_as_seed_list(mock_client):
    _collector()._get_mongo_client()

    _, kwargs = mock_client.call_args
    assert kwargs["host"] == "172.30.82.7:3717,172.30.82.8:3717,172.30.82.5:3717"
    assert kwargs["port"] == 3717
    assert kwargs["username"] == "root"
    assert kwargs["password"] == "p@ss"
    assert kwargs["authSource"] == "admin"
    assert kwargs["serverSelectionTimeoutMS"] == 5000


@patch("pymongo.MongoClient")
def test_auth_source_uses_instance_db(mock_client):
    instance = FakeMongoInstance()
    instance.db_name = "appdb"
    _collector(instance)._get_mongo_client()

    _, kwargs = mock_client.call_args
    assert kwargs["authSource"] == "appdb"


@patch("pymongo.MongoClient")
def test_no_auth_options_when_user_empty(mock_client):
    instance = FakeMongoInstance()
    instance.user = ""
    instance.password = ""
    _collector(instance)._get_mongo_client()

    _, kwargs = mock_client.call_args
    assert "username" not in kwargs
    assert "authSource" not in kwargs


@patch("pymongo.MongoClient")
def test_tls_options(mock_client):
    instance = FakeMongoInstance()
    instance.is_ssl = True
    instance.verify_ssl = False
    _collector(instance)._get_mongo_client()

    _, kwargs = mock_client.call_args
    assert kwargs["tls"] is True
    assert kwargs["tlsInsecure"] is True
