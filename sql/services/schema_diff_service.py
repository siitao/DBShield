# -*- coding: UTF-8 -*-
"""表级结构对比服务：information_schema 自研 diff，生成 patch/revert ALTER。

- 查询模型：每侧固定几条 information_schema 批量查询（引擎层
  get_tables_schema / get_object_names），与选中表数量无关；
- diff 与 ALTER 生成是纯函数（dict 进出），不依赖连接，可离线测试；
- 产出 SQL 不执行，由前端转入 SQL 工单走 goInception 审核；
- 每张表的变更合并为一条 ALTER（多子句），契合 goInception
  er_alter_table_once 审核规则。
"""

import logging

from sql.services.instance_service import resolve_instance_and_engine

logger = logging.getLogger("default")

OBJECT_KIND_LABELS = {
    "views": "视图",
    "triggers": "触发器",
    "routines": "存储过程/函数",
}

_NUMERIC_TYPES = (
    "tinyint", "smallint", "mediumint", "int", "integer", "bigint",
    "decimal", "numeric", "float", "double", "real", "year", "bit",
)


def normalize_collation(name):
    """5.7 的 utf8_* 在 8.0 的 information_schema 中叫 utf8mb3_*，归一到 utf8_* 再比较"""
    if not name:
        return name
    return name.replace("utf8mb3_", "utf8_")


def _escape_ident(name):
    return f"`{str(name).replace('`', '``')}`"


def _escape_string(value):
    return "'{}'".format(str(value).replace("'", "''"))


def _on_update_text(col):
    """EXTRA 中的 ON UPDATE 子句文本（大写归一），无则返回空串"""
    extra = (col.get("EXTRA") or "").strip()
    lower = extra.lower()
    if "on update" not in lower:
        return ""
    return extra[lower.index("on update") + len("on update"):].strip().upper()


def _normalized_default(col):
    """归一化的默认值：None 表示无默认值。

    8.0 对显式 DEFAULT NULL 存储为字符串 "NULL"，5.7 存储为 NULL；
    时间类型的 CURRENT_TIMESTAMP/NOW() 是同义词，统一为 CURRENT_TIMESTAMP。
    """
    default = col.get("COLUMN_DEFAULT")
    if default is None:
        return None
    text = str(default)
    if text.upper() == "NULL":
        return None
    col_type = (col.get("COLUMN_TYPE") or "").lower()
    if col_type.startswith(("timestamp", "datetime")) and text.upper() in (
        "CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP()", "NOW()",
    ):
        return "CURRENT_TIMESTAMP"
    return text


def _default_clause(col):
    """DEFAULT 子句；无默认值返回空串。数值列的默认值不加引号。"""
    default = _normalized_default(col)
    if default is None:
        return ""
    col_type = (col.get("COLUMN_TYPE") or "").lower()
    if default.startswith("b'"):
        return f"DEFAULT {default}"
    if col_type.startswith(_NUMERIC_TYPES):
        try:
            float(default)
            return f"DEFAULT {default}"
        except ValueError:
            pass
    return f"DEFAULT {_escape_string(default)}"


def _charset_clause(col, table_collation):
    """列级字符集子句；仅当列排序规则与表默认不同（跨 5.7/8.0 归一后）时输出"""
    if not col.get("CHARACTER_SET_NAME"):
        return ""
    col_coll = normalize_collation(col.get("COLLATION_NAME"))
    tbl_coll = normalize_collation(table_collation)
    if not col_coll or not tbl_coll or col_coll == tbl_coll:
        return ""
    return f"CHARACTER SET {col['CHARACTER_SET_NAME']} COLLATE {col['COLLATION_NAME']}"


def _column_definition(col, table_collation=None):
    """生成完整列定义（不含 FIRST/AFTER 位置子句）"""
    parts = [_escape_ident(col["COLUMN_NAME"]), col["COLUMN_TYPE"]]
    parts.append("NOT NULL" if col.get("IS_NULLABLE") == "NO" else "NULL")
    default_clause = _default_clause(col)
    if default_clause:
        parts.append(default_clause)
    extra = (col.get("EXTRA") or "").strip()
    if "auto_increment" in extra.lower():
        parts.append("AUTO_INCREMENT")
    on_update = _on_update_text(col)
    if on_update:
        parts.append(f"ON UPDATE {on_update}")
    charset_clause = _charset_clause(col, table_collation)
    if charset_clause:
        parts.append(charset_clause)
    if col.get("COLUMN_COMMENT"):
        parts.append(f"COMMENT {_escape_string(col['COLUMN_COMMENT'])}")
    return " ".join(parts)


def _column_signature(col, table_collation=None, sync_comments=False):
    """参与对比的列特征（与 _column_definition 输出的语义一致）"""
    return (
        (col.get("COLUMN_TYPE") or "").strip().lower(),
        col.get("IS_NULLABLE"),
        _normalized_default(col),
        "auto_increment" in (col.get("EXTRA") or "").lower(),
        _on_update_text(col),
        normalize_collation(col.get("COLLATION_NAME")),
        (col.get("COLUMN_COMMENT") or "") if sync_comments else "",
    )


def _position_clause(order, name, other_names, pending):
    """ADD 列的位置子句：前驱列在对侧已存在或位于本次变更链中 → AFTER，源首列 → FIRST，否则追加到末尾"""
    idx = order.index(name)
    if idx == 0:
        return " FIRST"
    prev = order[idx - 1]
    if prev in other_names or prev in pending:
        return f" AFTER {_escape_ident(prev)}"
    return ""


def _index_definition(idx):
    """ADD INDEX 子句（含类型）"""
    cols = ", ".join(_escape_ident(c) for c in idx["columns"])
    idx_type = (idx.get("type") or "BTREE").upper()
    if idx["name"] == "PRIMARY":
        return f"ADD PRIMARY KEY ({cols})"
    kind = "INDEX"
    if idx["unique"]:
        kind = "UNIQUE KEY"
    elif idx_type == "FULLTEXT":
        kind = "FULLTEXT INDEX"
    elif idx_type == "SPATIAL":
        kind = "SPATIAL INDEX"
    clause = f"ADD {kind} {_escape_ident(idx['name'])}"
    if idx_type not in ("BTREE", "FULLTEXT", "SPATIAL"):
        clause += f" USING {idx_type}"
    return f"{clause} ({cols})"


def _index_drop_clause(idx):
    if idx["name"] == "PRIMARY":
        return "DROP PRIMARY KEY"
    return f"DROP INDEX {_escape_ident(idx['name'])}"


def _table_option_diffs(src_opt, dst_opt, sync_auto_inc=False, sync_comments=False):
    """表选项差异：[(object, source显示, target显示, patch子句, revert子句)]"""
    diffs = []
    src_engine = (src_opt.get("ENGINE") or "").strip()
    dst_engine = (dst_opt.get("ENGINE") or "").strip()
    if src_engine.upper() != dst_engine.upper():
        diffs.append((
            "ENGINE", src_engine or "-", dst_engine or "-",
            f"ENGINE={src_engine}", f"ENGINE={dst_engine}",
        ))

    src_coll = normalize_collation(src_opt.get("TABLE_COLLATION"))
    dst_coll = normalize_collation(dst_opt.get("TABLE_COLLATION"))
    if src_coll != dst_coll:
        src_charset = (src_coll or "").split("_")[0]
        dst_charset = (dst_coll or "").split("_")[0]
        patch = f"DEFAULT CHARSET={src_charset} COLLATE={src_coll}" if src_coll else ""
        revert = f"DEFAULT CHARSET={dst_charset} COLLATE={dst_coll}" if dst_coll else ""
        diffs.append(("字符集", src_coll or "-", dst_coll or "-", patch, revert))

    if sync_comments:
        src_comment = src_opt.get("TABLE_COMMENT") or ""
        dst_comment = dst_opt.get("TABLE_COMMENT") or ""
        if src_comment != dst_comment:
            diffs.append((
                "表注释", src_comment or "-", dst_comment or "-",
                f"COMMENT={_escape_string(src_comment)}",
                f"COMMENT={_escape_string(dst_comment)}",
            ))

    if sync_auto_inc:
        src_auto = src_opt.get("AUTO_INCREMENT")
        dst_auto = dst_opt.get("AUTO_INCREMENT")
        if src_auto != dst_auto and src_auto:
            diffs.append((
                "AUTO_INCREMENT", str(src_auto), str(dst_auto),
                f"AUTO_INCREMENT={src_auto}", f"AUTO_INCREMENT={dst_auto}",
            ))
    return diffs


def _index_signature(idx):
    return (idx["unique"], (idx.get("type") or "").upper(), list(idx["columns"]))


def diff_table(src_table, dst_table, sync_auto_inc=False, sync_comments=False):
    """对比单张表（双侧均存在），返回 items 列表。

    每个 item: {kind, action, object, source, target, patch, revert, danger}
    patch/revert 为单条 ALTER 内的子句（不含 ALTER TABLE 头）。
    """
    items = []
    src_cols = {c["COLUMN_NAME"]: c for c in src_table["columns"]}
    dst_cols = {c["COLUMN_NAME"]: c for c in dst_table["columns"]}
    src_order = [c["COLUMN_NAME"] for c in src_table["columns"]]
    dst_order = [c["COLUMN_NAME"] for c in dst_table["columns"]]
    src_coll = src_table["options"].get("TABLE_COLLATION")
    dst_coll = dst_table["options"].get("TABLE_COLLATION")

    added = [n for n in src_order if n not in dst_cols]
    removed = [n for n in dst_order if n not in src_cols]

    # 改名识别：签名一致（不含注释）的删/增对生成 CHANGE 而非 DROP+ADD，避免丢数据
    for rm in list(removed):
        rm_sig = _column_signature(dst_cols[rm], dst_coll)[:6]
        match = next(
            (
                ad for ad in added
                if _column_signature(src_cols[ad], src_coll)[:6] == rm_sig
            ),
            None,
        )
        if match is None:
            continue
        removed.remove(rm)
        added.remove(match)
        items.append({
            "kind": "column", "action": "rename", "object": rm,
            "source": match, "target": rm, "danger": False,
            "patch": f"CHANGE {_escape_ident(rm)} {_column_definition(src_cols[match], src_coll)}",
            "revert": f"CHANGE {_escape_ident(match)} {_column_definition(dst_cols[rm], dst_coll)}",
        })

    for name in added:
        col = src_cols[name]
        position = _position_clause(src_order, name, set(dst_cols), set(added))
        items.append({
            "kind": "column", "action": "add", "object": name,
            "source": col.get("COLUMN_TYPE"), "target": None, "danger": False,
            "patch": f"ADD COLUMN {_column_definition(col, src_coll)}{position}",
            "revert": f"DROP COLUMN {_escape_ident(name)}",
        })

    for name in removed:
        col = dst_cols[name]
        position = _position_clause(dst_order, name, set(src_cols), set(removed))
        items.append({
            "kind": "column", "action": "drop", "object": name,
            "source": None, "target": col.get("COLUMN_TYPE"), "danger": True,
            "patch": f"DROP COLUMN {_escape_ident(name)}",
            "revert": f"ADD COLUMN {_column_definition(col, dst_coll)}{position}",
        })

    for name in src_order:
        if name not in dst_cols:
            continue
        src_col, dst_col = src_cols[name], dst_cols[name]
        if _column_signature(src_col, src_coll, sync_comments) == _column_signature(
            dst_col, dst_coll, sync_comments
        ):
            continue
        patch_position = _position_clause(src_order, name, set(dst_cols), set())
        revert_position = _position_clause(dst_order, name, set(src_cols), set())
        items.append({
            "kind": "column", "action": "modify", "object": name,
            "source": src_col.get("COLUMN_TYPE"), "target": dst_col.get("COLUMN_TYPE"),
            "danger": False,
            "patch": f"MODIFY COLUMN {_column_definition(src_col, src_coll)}{patch_position}",
            "revert": f"MODIFY COLUMN {_column_definition(dst_col, dst_coll)}{revert_position}",
        })

    src_indexes = src_table["indexes"]
    dst_indexes = dst_table["indexes"]
    for name, idx in sorted(src_indexes.items()):
        if name not in dst_indexes:
            items.append({
                "kind": "index", "action": "add", "object": name,
                "source": ", ".join(idx["columns"]), "target": None, "danger": False,
                "patch": _index_definition(idx),
                "revert": _index_drop_clause(idx),
            })
    for name, idx in sorted(dst_indexes.items()):
        if name not in src_indexes:
            items.append({
                "kind": "index", "action": "drop", "object": name,
                "source": None, "target": ", ".join(idx["columns"]),
                "danger": name == "PRIMARY",
                "patch": _index_drop_clause(idx),
                "revert": _index_definition(idx),
            })
    for name in sorted(set(src_indexes) & set(dst_indexes)):
        if _index_signature(src_indexes[name]) == _index_signature(dst_indexes[name]):
            continue
        items.append({
            "kind": "index", "action": "modify", "object": name,
            "source": ", ".join(src_indexes[name]["columns"]),
            "target": ", ".join(dst_indexes[name]["columns"]),
            "danger": name == "PRIMARY",
            "patch": "{}，{}".format(
                _index_drop_clause(dst_indexes[name]), _index_definition(src_indexes[name])
            ),
            "revert": "{}，{}".format(
                _index_drop_clause(src_indexes[name]), _index_definition(dst_indexes[name])
            ),
        })

    for object_name, source, target, patch, revert in _table_option_diffs(
        src_table["options"], dst_table["options"],
        sync_auto_inc=sync_auto_inc, sync_comments=sync_comments,
    ):
        items.append({
            "kind": "table_option", "action": "modify", "object": object_name,
            "source": source, "target": target, "danger": False,
            "patch": patch, "revert": revert,
        })
    return items


def build_alter(table, items, side):
    """把单张表的变更合并为一条 ALTER（无变更返回空串）"""
    frags = [item[side] for item in items if item.get(side)]
    if not frags:
        return ""
    return "ALTER TABLE {} {}".format(_escape_ident(table), ",\n  ".join(frags)) + ";"


def diff_schemas(src, dst, src_objects, dst_objects, sync_auto_inc=False, sync_comments=False):
    """对比双侧 schema（引擎 get_tables_schema/get_object_names 的返回值），产出完整结果"""
    summary = {"total": 0, "diff": 0, "same": 0, "missing": 0, "objects": 0}
    tables = []
    patch_parts, revert_parts = [], []

    all_tables = sorted(set(src["table_options"]) | set(dst["table_options"]))
    for table in all_tables:
        summary["total"] += 1
        in_src = table in src["table_options"]
        in_dst = table in dst["table_options"]
        if not in_src or not in_dst:
            summary["missing"] += 1
            tables.append({
                "table": table,
                "status": "only_in_source" if in_src else "only_in_target",
                "items": [{
                    "kind": "table", "action": "missing", "object": table,
                    "source": "存在" if in_src else "不存在",
                    "target": "存在" if in_dst else "不存在",
                    "danger": False, "patch": "", "revert": "",
                }],
                "patch_sql": "", "revert_sql": "",
            })
            continue

        src_table = {
            "columns": src["columns"].get(table, []),
            "indexes": src["indexes"].get(table, {}),
            "options": src["table_options"][table],
        }
        dst_table = {
            "columns": dst["columns"].get(table, []),
            "indexes": dst["indexes"].get(table, {}),
            "options": dst["table_options"][table],
        }
        items = diff_table(
            src_table, dst_table,
            sync_auto_inc=sync_auto_inc, sync_comments=sync_comments,
        )
        patch_sql = build_alter(table, items, "patch")
        revert_sql = build_alter(table, items, "revert")
        if items:
            summary["diff"] += 1
            patch_parts.append(patch_sql)
            revert_parts.append(revert_sql)
        else:
            summary["same"] += 1
        tables.append({
            "table": table, "status": "diff" if items else "same",
            "items": items, "patch_sql": patch_sql, "revert_sql": revert_sql,
        })

    objects = []
    for key, label in OBJECT_KIND_LABELS.items():
        src_names = set(src_objects.get(key) or [])
        dst_names = set(dst_objects.get(key) or [])
        for name in sorted(src_names - dst_names):
            summary["objects"] += 1
            objects.append({"kind": label, "name": name, "side": "source_only"})
        for name in sorted(dst_names - src_names):
            summary["objects"] += 1
            objects.append({"kind": label, "name": name, "side": "target_only"})

    return {
        "summary": summary,
        "tables": tables,
        "objects": objects,
        "patch_sql": "\n".join(patch_parts),
        "revert_sql": "\n".join(revert_parts),
    }


EMPTY_DATA = {
    "summary": {"total": 0, "diff": 0, "same": 0, "missing": 0, "objects": 0},
    "tables": [],
    "objects": [],
    "patch_sql": "",
    "revert_sql": "",
}


def table_diff(
    user,
    instance_name,
    db_name,
    target_instance_name,
    target_db_name,
    tables=None,
    sync_auto_inc=False,
    sync_comments=False,
):
    """表级结构对比入口：校验双侧实例权限 → 拉取元数据 → diff → 生成 SQL。"""
    try:
        instance, engine = resolve_instance_and_engine(
            user, instance_name=instance_name, db_type="mysql"
        )
        target_instance, target_engine = resolve_instance_and_engine(
            user, instance_name=target_instance_name, db_type="mysql"
        )
    except Exception:
        return {
            "status": 1,
            "msg": "实例不存在、非 MySQL 类型或你所在组未关联",
            "data": EMPTY_DATA,
        }

    selected = [t for t in (tables or []) if t]
    src = engine.get_tables_schema(db_name, tables=selected or None)
    dst = target_engine.get_tables_schema(target_db_name, tables=selected or None)
    src_objects = engine.get_object_names(db_name)
    dst_objects = target_engine.get_object_names(target_db_name)
    for side, fetched in (
        (f"{instance_name}/{db_name}", src),
        (f"{target_instance_name}/{target_db_name}", dst),
        (f"{instance_name}/{db_name}", src_objects),
        (f"{target_instance_name}/{target_db_name}", dst_objects),
    ):
        if fetched.get("error"):
            logger.warning("表级结构对比拉取元数据失败 %s: %s", side, fetched["error"])
            return {
                "status": 1,
                "msg": f"获取 {side} 表结构失败: {fetched['error']}",
                "data": EMPTY_DATA,
            }

    data = diff_schemas(
        src, dst, src_objects, dst_objects,
        sync_auto_inc=sync_auto_inc, sync_comments=sync_comments,
    )
    logger.info(
        "表级结构对比完成 源=%s/%s 目标=%s/%s 表数=%s 差异=%s",
        instance_name, db_name, target_instance_name, target_db_name,
        data["summary"]["total"], data["summary"]["diff"],
    )
    return {"status": 0, "msg": "ok", "data": data}
