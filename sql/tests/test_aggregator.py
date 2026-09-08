# -*- coding:utf-8 -*-
"""慢查询聚合/采集：行级 upsert 重试、批量 upsert 降级与聚合防重叠锁测试"""
import pytest
from django.db import IntegrityError, OperationalError

from sql.collectors import aggregator, base
from sql.models import MySQLSlowQuerySummary

LOOKUP = {"instance_id": 4, "sql_hash": "118ea1236747ae485f92e1c1638e533e"}
DEFAULTS = {"query_time_avg": 0.1}

DEADLOCK = OperationalError(1213, "Deadlock found when trying to get lock")
DUPLICATE = IntegrityError(1062, "Duplicate entry", None)

UPDATE_FIELDS = ["query_time_avg"]


def make_summary_objs(*sql_hashes):
    return [
        MySQLSlowQuerySummary(instance_id=4, sql_hash=h, query_time_avg=0.5)
        for h in sql_hashes
    ]


@pytest.fixture
def mock_sleep(mocker):
    return mocker.patch("sql.collectors.base.time.sleep")


class TestUpdateOrCreateWithRetry:
    def test_deadlock_retry_then_success(self, mocker, mock_sleep):
        upsert = mocker.patch.object(
            MySQLSlowQuerySummary.objects,
            "update_or_create",
            side_effect=[DEADLOCK, (object(), True)],
        )

        _, created = base.update_or_create_with_retry(
            MySQLSlowQuerySummary, lookup=LOOKUP, defaults=DEFAULTS
        )

        assert created is True
        assert upsert.call_count == 2
        mock_sleep.assert_called_once()

    def test_duplicate_retry_then_success(self, mocker, mock_sleep):
        upsert = mocker.patch.object(
            MySQLSlowQuerySummary.objects,
            "update_or_create",
            side_effect=[DUPLICATE, (object(), False)],
        )

        _, created = base.update_or_create_with_retry(
            MySQLSlowQuerySummary, lookup=LOOKUP, defaults=DEFAULTS
        )

        assert created is False
        assert upsert.call_count == 2

    def test_raise_after_retries_exhausted(self, mocker, mock_sleep):
        upsert = mocker.patch.object(
            MySQLSlowQuerySummary.objects,
            "update_or_create",
            side_effect=DEADLOCK,
        )

        with pytest.raises(OperationalError):
            base.update_or_create_with_retry(
                MySQLSlowQuerySummary, lookup=LOOKUP, defaults=DEFAULTS
            )

        assert upsert.call_count == base.UPSERT_MAX_RETRIES

    def test_kwargs_passed_through(self, mocker, mock_sleep):
        upsert = mocker.patch.object(
            MySQLSlowQuerySummary.objects,
            "update_or_create",
            return_value=(object(), False),
        )

        base.update_or_create_with_retry(
            MySQLSlowQuerySummary, lookup=LOOKUP, defaults=DEFAULTS
        )

        upsert.assert_called_once_with(**LOOKUP, defaults=DEFAULTS)


class TestBulkUpsert:
    def _patch_connection(self, mocker, supports_target: bool):
        mock_conn = mocker.patch("django.db.connection")
        mock_conn.features.supports_update_conflicts_with_target = supports_target
        return mock_conn

    def test_mysql_backend_omits_unique_fields(self, mocker):
        # MySQL 后端不支持 conflict target：传 unique_fields 会被 Django 拒绝
        self._patch_connection(mocker, supports_target=False)
        bulk_create = mocker.patch.object(MySQLSlowQuerySummary.objects, "bulk_create")
        objs = make_summary_objs("a", "b")

        persisted = base.bulk_upsert(
            MySQLSlowQuerySummary, objs, update_fields=UPDATE_FIELDS, log_label="测试"
        )

        assert persisted == 2
        assert bulk_create.call_count == 1
        kwargs = bulk_create.call_args.kwargs
        assert kwargs["update_conflicts"] is True
        assert kwargs["update_fields"] == UPDATE_FIELDS
        assert kwargs["batch_size"] == 500
        assert "unique_fields" not in kwargs

    def test_conflict_target_backend_passes_unique_fields(self, mocker):
        self._patch_connection(mocker, supports_target=True)
        bulk_create = mocker.patch.object(MySQLSlowQuerySummary.objects, "bulk_create")
        objs = make_summary_objs("a")

        base.bulk_upsert(MySQLSlowQuerySummary, objs, update_fields=UPDATE_FIELDS)

        assert (
            bulk_create.call_args.kwargs["unique_fields"] == base.SUMMARY_UNIQUE_FIELDS
        )

    def test_fallback_to_row_upsert_on_bulk_failure(self, mocker, mock_sleep):
        self._patch_connection(mocker, supports_target=False)
        objs = make_summary_objs("a", "b")
        mocker.patch.object(
            MySQLSlowQuerySummary.objects, "bulk_create", side_effect=DEADLOCK
        )
        upsert = mocker.patch.object(
            MySQLSlowQuerySummary.objects,
            "update_or_create",
            return_value=(object(), False),
        )

        persisted = base.bulk_upsert(
            MySQLSlowQuerySummary, objs, update_fields=UPDATE_FIELDS, log_label="测试"
        )

        assert persisted == 2
        assert upsert.call_count == 2
        upsert.assert_any_call(
            instance_id=4, sql_hash="a", defaults={"query_time_avg": 0.5}
        )
        upsert.assert_any_call(
            instance_id=4, sql_hash="b", defaults={"query_time_avg": 0.5}
        )

    def test_fallback_skips_row_after_retries_exhausted(self, mocker, mock_sleep):
        self._patch_connection(mocker, supports_target=False)
        objs = make_summary_objs("good", "poison")
        mocker.patch.object(
            MySQLSlowQuerySummary.objects, "bulk_create", side_effect=DEADLOCK
        )
        # 第一行成功，第二行连续重试耗尽（3 次 1062）
        upsert = mocker.patch.object(
            MySQLSlowQuerySummary.objects,
            "update_or_create",
            side_effect=[(objs[0], True), DUPLICATE, DUPLICATE, DUPLICATE],
        )

        persisted = base.bulk_upsert(
            MySQLSlowQuerySummary, objs, update_fields=UPDATE_FIELDS, log_label="测试"
        )

        assert persisted == 1
        assert upsert.call_count == 4

    def test_bulk_success_does_not_fallback(self, mocker, mock_sleep):
        self._patch_connection(mocker, supports_target=False)
        mocker.patch.object(MySQLSlowQuerySummary.objects, "bulk_create")
        upsert = mocker.patch.object(MySQLSlowQuerySummary.objects, "update_or_create")

        persisted = base.bulk_upsert(
            MySQLSlowQuerySummary, make_summary_objs("a"), update_fields=UPDATE_FIELDS
        )

        assert persisted == 1
        upsert.assert_not_called()


class TestAggregateAllSlowqueryLock:
    def test_skip_when_lock_held(self, mocker):
        mock_cache = mocker.patch.object(aggregator, "cache")
        mock_cache.add.return_value = False
        mysql = mocker.patch.object(aggregator, "aggregate_mysql_slowquery")

        result = aggregator.aggregate_all_slowquery()

        assert result is None
        mock_cache.add.assert_called_once_with(
            aggregator.AGGREGATE_LOCK_KEY, 1, aggregator.AGGREGATE_LOCK_TIMEOUT
        )
        mysql.assert_not_called()
        mock_cache.delete.assert_not_called()

    def test_acquire_run_and_release(self, mocker):
        mock_cache = mocker.patch.object(aggregator, "cache")
        mock_cache.add.return_value = True
        mocker.patch.object(aggregator, "aggregate_mysql_slowquery", return_value=3)
        mocker.patch.object(aggregator, "aggregate_pgsql_slowquery", return_value=2)
        mocker.patch.object(aggregator, "aggregate_mongo_slowquery", return_value=1)
        mocker.patch.object(aggregator, "aggregate_redis_slowquery", return_value=0)

        result = aggregator.aggregate_all_slowquery()

        assert result == {"mysql": 3, "pgsql": 2, "mongo": 1, "redis": 0}
        mock_cache.delete.assert_called_once_with(aggregator.AGGREGATE_LOCK_KEY)

    def test_release_lock_on_aggregate_error(self, mocker):
        mock_cache = mocker.patch.object(aggregator, "cache")
        mock_cache.add.return_value = True
        mocker.patch.object(
            aggregator, "aggregate_mysql_slowquery", side_effect=RuntimeError("boom")
        )

        with pytest.raises(RuntimeError):
            aggregator.aggregate_all_slowquery()

        mock_cache.delete.assert_called_once_with(aggregator.AGGREGATE_LOCK_KEY)
