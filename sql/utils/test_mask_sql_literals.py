# -*- coding: UTF-8 -*-
"""AI 外发 SQL 字面量脱敏的离线用例。"""

from sql.utils.sql_utils import mask_sql_literals


def test_mask_single_quoted_and_numeric_literals():
    sql = "select * from t where name='张三' and id=123"
    assert mask_sql_literals(sql) == "select * from t where name='?' and id=0"


def test_mask_double_quoted_strings_for_mysql():
    sql = 'select "secret" from t'
    assert mask_sql_literals(sql, "mysql") == 'select "?" from t'
    assert mask_sql_literals(sql) == 'select "?" from t'


def test_keep_double_quoted_identifiers_for_pgsql():
    sql = 'select "user_name" from "users" where id=1'
    assert mask_sql_literals(sql, "pgsql") == (
        'select "user_name" from "users" where id=0'
    )


def test_mask_dollar_quoted_strings():
    assert mask_sql_literals("select $$secret$$", "pgsql") == "select '?'"
    assert mask_sql_literals("select $tag$secret$tag$", "pgsql") == "select '?'"


def test_mask_hex_literals():
    assert mask_sql_literals("select 0xdeadbeef", "mysql") == "select 0"
