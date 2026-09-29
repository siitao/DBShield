# -*- coding: UTF-8 -*-
"""表级结构对比服务的纯函数用例：diff 规则与 ALTER 生成，不连库。"""

from types import SimpleNamespace

from sql.services import schema_diff_service as sds


def col(
    name,
    ctype="varchar(64)",
    nullable="NO",
    default=None,
    extra="",
    comment="",
    collation=None,
    charset=None,
    position=1,
):
    return {
        "TABLE_NAME": "t1",
        "COLUMN_NAME": name,
        "ORDINAL_POSITION": position,
        "COLUMN_TYPE": ctype,
        "IS_NULLABLE": nullable,
        "COLUMN_DEFAULT": default,
        "COLUMN_KEY": "",
        "EXTRA": extra,
        "COLUMN_COMMENT": comment,
        "CHARACTER_SET_NAME": charset,
        "COLLATION_NAME": collation,
    }


def idx(name, columns, unique=False, type="BTREE"):
    return {"name": name, "unique": unique, "type": type, "columns": columns}


def table_def(columns, indexes=None, engine="InnoDB", collation="utf8mb4_general_ci", comment="", auto_inc=None):
    return {
        "columns": columns,
        "indexes": {i["name"]: i for i in (indexes or [])},
        "options": {
            "TABLE_NAME": "t1",
            "ENGINE": engine,
            "TABLE_COLLATION": collation,
            "TABLE_COMMENT": comment,
            "AUTO_INCREMENT": auto_inc,
        },
    }


def schema(tables):
    return {
        "table_options": {name: d["options"] for name, d in tables.items()},
        "columns": {name: d["columns"] for name, d in tables.items()},
        "indexes": {name: d["indexes"] for name, d in tables.items()},
    }


def diff_one_table(src_def, dst_def, **kwargs):
    src = schema({"t1": src_def})
    dst = schema({"t1": dst_def})
    result = sds.diff_schemas(src, dst, {}, {}, **kwargs)
    assert result["summary"]["total"] == 1
    return result["tables"][0]


def pick(items, kind=None, action=None):
    return [
        i
        for i in items
        if (kind is None or i["kind"] == kind) and (action is None or i["action"] == action)
    ]


def test_column_added_generates_add_with_after():
    src = table_def([
        col("id", "int", position=1),
        col("name", position=2),
        col("memo", position=3),
    ])
    dst = table_def([col("id", "int", position=1), col("name", position=2)])
    tb = diff_one_table(src, dst)
    (item,) = pick(tb["items"], "column", "add")
    assert item["object"] == "memo"
    assert "ADD COLUMN `memo` varchar(64) NOT NULL AFTER `name`" in item["patch"]
    assert item["revert"] == "DROP COLUMN `memo`"


def test_added_first_column_uses_first():
    src = table_def([col("flag", "tinyint(1)", position=1), col("id", "int", position=2)])
    dst = table_def([col("id", "int", position=1)])
    tb = diff_one_table(src, dst)
    (item,) = pick(tb["items"], "column", "add")
    assert item["patch"].endswith(" FIRST")


def test_column_type_change_generates_modify_both_directions():
    src = table_def([col("name", "varchar(128)")])
    dst = table_def([col("name", "varchar(64)")])
    tb = diff_one_table(src, dst)
    (item,) = pick(tb["items"], "column", "modify")
    assert "MODIFY COLUMN `name` varchar(128) NOT NULL" in item["patch"]
    assert "MODIFY COLUMN `name` varchar(64) NOT NULL" in item["revert"]


def test_rename_detected_as_change_not_drop_add():
    src = table_def([col("tag_name", position=1), col("id", "int", position=2)])
    dst = table_def([col("name", position=1), col("id", "int", position=2)])
    tb = diff_one_table(src, dst)
    renames = pick(tb["items"], "column", "rename")
    assert len(renames) == 1
    assert renames[0]["object"] == "name"
    assert renames[0]["source"] == "tag_name"
    assert renames[0]["patch"].startswith("CHANGE `name` `tag_name`")
    assert renames[0]["revert"].startswith("CHANGE `tag_name` `name`")
    assert not pick(tb["items"], "column", "drop")


def test_dropped_column_marked_danger():
    src = table_def([col("id", "int")])
    dst = table_def([col("id", "int"), col("old_col")])
    tb = diff_one_table(src, dst)
    (item,) = pick(tb["items"], "column", "drop")
    assert item["danger"] is True
    assert item["patch"] == "DROP COLUMN `old_col`"
    assert "ADD COLUMN `old_col` varchar(64) NOT NULL" in item["revert"]


def test_default_clause_quoting():
    src = table_def([col("cnt", "int", default="0"), col("name", default="abc")])
    dst = table_def([col("cnt", "int"), col("name")])
    tb = diff_one_table(src, dst)
    patches = {i["object"]: i["patch"] for i in pick(tb["items"], "column", "modify")}
    assert "DEFAULT 0" in patches["cnt"]
    assert "DEFAULT 'abc'" in patches["name"]


def test_on_update_extracted_from_extra():
    src = table_def([col("updated_at", "datetime", nullable="YES", extra="on update CURRENT_TIMESTAMP")])
    dst = table_def([col("updated_at", "datetime", nullable="YES")])
    tb = diff_one_table(src, dst)
    (item,) = pick(tb["items"], "column", "modify")
    assert "ON UPDATE CURRENT_TIMESTAMP" in item["patch"]


def test_index_add_drop_and_primary_danger():
    src = table_def(
        [col("id", "int"), col("name")],
        indexes=[idx("PRIMARY", ["id"], unique=True), idx("idx_name", ["name"])],
    )
    dst = table_def(
        [col("id", "int"), col("name")],
        indexes=[idx("PRIMARY", ["id"], unique=True), idx("uk_old", ["name"], unique=True)],
    )
    tb = diff_one_table(src, dst)
    adds = pick(tb["items"], "index", "add")
    drops = pick(tb["items"], "index", "drop")
    assert adds[0]["patch"] == "ADD INDEX `idx_name` (`name`)"
    assert drops[0]["patch"] == "DROP INDEX `uk_old`"
    assert drops[0]["revert"] == "ADD UNIQUE KEY `uk_old` (`name`)"
    assert drops[0]["danger"] is False
    src_pk_only = table_def([col("id", "int")], indexes=[idx("PRIMARY", ["id"], unique=True)])
    dst_no_pk = table_def([col("id", "int")])
    tb2 = diff_one_table(src_pk_only, dst_no_pk)
    (add_pk,) = pick(tb2["items"], "index", "add")
    assert add_pk["patch"] == "ADD PRIMARY KEY (`id`)"
    # 反向：目标多出主键，patch 侧 DROP PRIMARY KEY 为危险操作
    tb3 = diff_one_table(dst_no_pk, src_pk_only)
    (drop_pk,) = pick(tb3["items"], "index", "drop")
    assert drop_pk["danger"] is True
    assert drop_pk["patch"] == "DROP PRIMARY KEY"


def test_index_modified_generates_drop_then_add():
    src = table_def([col("a", "int"), col("b", "int")], indexes=[idx("idx_ab", ["a", "b"])])
    dst = table_def([col("a", "int"), col("b", "int")], indexes=[idx("idx_ab", ["a"])])
    tb = diff_one_table(src, dst)
    (item,) = pick(tb["items"], "index", "modify")
    assert item["patch"].startswith("DROP INDEX `idx_ab`，ADD INDEX `idx_ab` (`a`, `b`)")
    assert item["revert"].startswith("DROP INDEX `idx_ab`，ADD INDEX `idx_ab` (`a`)")


def test_engine_and_collation_option_diffs():
    src = table_def([col("id", "int")], engine="InnoDB", collation="utf8mb4_general_ci")
    dst = table_def([col("id", "int")], engine="MyISAM", collation="utf8mb4_unicode_ci")
    tb = diff_one_table(src, dst)
    options = {i["object"]: i for i in pick(tb["items"], "table_option")}
    assert options["ENGINE"]["patch"] == "ENGINE=InnoDB"
    assert options["ENGINE"]["revert"] == "ENGINE=MyISAM"
    assert options["字符集"]["patch"] == "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"
    assert options["字符集"]["revert"] == "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"


def test_utf8mb3_alias_not_reported_as_diff():
    src = table_def([col("name", collation="utf8_general_ci", charset="utf8")], collation="utf8_general_ci")
    dst = table_def(
        [col("name", collation="utf8mb3_general_ci", charset="utf8mb3")],
        collation="utf8mb3_general_ci",
    )
    tb = diff_one_table(src, dst)
    assert tb["status"] == "same"
    assert tb["items"] == []


def test_comment_diff_respects_sync_comments():
    src = table_def([col("id", "int", comment="主键")], comment="表注释A")
    dst = table_def([col("id", "int", comment="旧注释")], comment="表注释B")
    tb_off = diff_one_table(src, dst, sync_comments=False)
    assert pick(tb_off["items"], "table_option") == []
    tb_on = diff_one_table(src, dst, sync_comments=True)
    option_names = {i["object"] for i in pick(tb_on["items"], "table_option")}
    assert "表注释" in option_names


def test_auto_inc_diff_respects_sync_auto_inc():
    src = table_def([col("id", "int")], auto_inc=100)
    dst = table_def([col("id", "int")], auto_inc=5)
    tb_off = diff_one_table(src, dst, sync_auto_inc=False)
    assert tb_off["status"] == "same"
    tb_on = diff_one_table(src, dst, sync_auto_inc=True)
    (item,) = pick(tb_on["items"], "table_option")
    assert item["patch"] == "AUTO_INCREMENT=100"
    assert item["revert"] == "AUTO_INCREMENT=5"


def test_no_diff_is_same_with_empty_sql():
    tb = diff_one_table(table_def([col("id", "int")]), table_def([col("id", "int")]))
    assert tb["status"] == "same"
    assert tb["items"] == []
    assert tb["patch_sql"] == ""
    assert tb["revert_sql"] == ""


def test_missing_table_reports_without_sql():
    src = schema({"t1": table_def([col("id", "int")]), "t2": table_def([col("id", "int")])})
    dst = schema({"t1": table_def([col("id", "int")])})
    result = sds.diff_schemas(src, dst, {}, {})
    assert result["summary"]["missing"] == 1
    (missing,) = [t for t in result["tables"] if t["table"] == "t2"]
    assert missing["status"] == "only_in_source"
    assert missing["patch_sql"] == ""
    (present,) = [t for t in result["tables"] if t["table"] == "t1"]
    assert present["status"] == "same"


def test_object_presence_diff():
    src_objects = {"views": ["v1", "v2"], "triggers": ["tg1"], "routines": []}
    dst_objects = {"views": ["v2"], "triggers": [], "routines": ["p1"]}
    result = sds.diff_schemas(schema({}), schema({}), src_objects, dst_objects)
    # v1,v2,tg1,p1 中 v2 双侧共有，其余 3 个为单侧独有
    assert result["summary"]["objects"] == 3
    sides = {(o["kind"], o["name"], o["side"]) for o in result["objects"]}
    assert ("视图", "v1", "source_only") in sides
    assert ("触发器", "tg1", "source_only") in sides
    assert ("存储过程/函数", "p1", "target_only") in sides


def test_merged_single_alter_per_table():
    src = table_def(
        [col("id", "int", position=1), col("name", position=2), col("extra", position=3)],
        indexes=[idx("idx_extra", ["extra"])],
        comment="新注释",
    )
    dst = table_def([col("id", "int", position=1), col("name", "varchar(32)", position=2)], comment="旧注释")
    tb = diff_one_table(src, dst, sync_comments=True)
    assert tb["status"] == "diff"
    assert tb["patch_sql"].startswith("ALTER TABLE `t1` ")
    assert tb["patch_sql"].count("ALTER TABLE") == 1
    assert tb["patch_sql"].endswith(";")
    # 四类变更都在同一条语句里：加列 + 改列 + 加索引 + 表注释
    for fragment in ("ADD COLUMN `extra`", "MODIFY COLUMN `name`", "ADD INDEX `idx_extra`", "COMMENT="):
        assert fragment in tb["patch_sql"]


def test_build_alter_empty_for_no_frags():
    assert sds.build_alter("t1", [{"patch": "", "revert": ""}], "patch") == ""


def test_table_diff_service_permission_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise Exception("no access")

    monkeypatch.setattr(sds, "resolve_instance_and_engine", _raise)
    result = sds.table_diff(
        user=object(),
        instance_name="a",
        db_name="db",
        target_instance_name="b",
        target_db_name="db2",
    )
    assert result["status"] == 1
    assert result["data"]["tables"] == []


def test_table_diff_service_happy_path(monkeypatch):
    calls = []

    class FakeEngine:
        def get_tables_schema(self, db_name, tables=None):
            calls.append((db_name, tables))
            return {"table_options": {}, "columns": {}, "indexes": {}}

        def get_object_names(self, db_name):
            return {"views": [], "triggers": [], "routines": []}

    monkeypatch.setattr(
        sds,
        "resolve_instance_and_engine",
        lambda user, instance_name=None, db_type=None: (SimpleNamespace(db_type="mysql"), FakeEngine()),
    )
    result = sds.table_diff(
        user=SimpleNamespace(),
        instance_name="src",
        db_name="db1",
        target_instance_name="dst",
        target_db_name="db2",
        tables=["t1"],
        sync_auto_inc=True,
    )
    assert result["status"] == 0
    assert result["data"]["summary"]["total"] == 0
    # 双侧都按选定表过滤拉取
    assert calls == [("db1", ["t1"]), ("db2", ["t1"])]


def test_table_diff_service_engine_error(monkeypatch):
    class FakeEngine:
        def get_tables_schema(self, db_name, tables=None):
            return {"error": "cannot connect"}

        def get_object_names(self, db_name):
            return {"views": [], "triggers": [], "routines": []}

    monkeypatch.setattr(
        sds,
        "resolve_instance_and_engine",
        lambda user, instance_name=None, db_type=None: (SimpleNamespace(db_type="mysql"), FakeEngine()),
    )
    result = sds.table_diff(
        user=SimpleNamespace(),
        instance_name="src",
        db_name="db1",
        target_instance_name="dst",
        target_db_name="db2",
    )
    assert result["status"] == 1
    assert "cannot connect" in result["msg"]
