"""AI 评测黄金集：prompt / 解析 / 规则改动的回归安全网。

两层：
- 离线层（CI 常跑，无 DB 依赖）：固定 LLM 输出样本 → 解析、归一、规则兜底
  的确定性断言，覆盖诊断、审核（单条/批量）、严重度统计规则、风险词表归一。
  离线样本的"期望值"即黄金标准：改动 prompt 结构、解析逻辑或规则阈值时，
  先更新样本与期望，再改实现，评审时即可看到行为变化。
- 在线冒烟层（默认跳过）：设置 ARCHERY_AI_SMOKE=1 并提供 ARCHERY_AI_KEY
  （可选 ARCHERY_AI_BASE_URL / ARCHERY_AI_MODEL）后真实调用小样本，
  断言结构化字段可解析且枚举合法。消耗真实 token，仅供发版前人工触发。
"""

import os

import pytest

from common.utils.ai_risk import (
    AI_LEVEL_HIGH,
    AI_LEVEL_LOW,
    AI_LEVEL_MEDIUM,
    AI_LEVEL_UNKNOWN,
    normalize_level,
    score_band,
    severity_from_stats,
)
from common.utils.openai import (
    DIAG_BOTTLENECK_MISSING_INDEX,
    DIAG_BOTTLENECK_OTHER,
    OpenaiClient,
)

# ============ 离线黄金样本：慢查诊断 ============

GOLDEN_DIAGNOSIS = [
    {
        "name": "全表扫描：脏格式输出（前缀文字+代码块包裹）",
        "llm_output": (
            '好的，以下是诊断结果：\n```json\n'
            '{"root_cause": "status 字段无索引导致全表扫描", "severity": "high", '
            '"bottleneck_type": "full_scan", '
            '"evidence": ["执行计划 type=ALL", "扫描/返回比 1000:1"], '
            '"suggestions": [{"type": "index_ddl", "desc": "为 status 建复合索引", '
            '"index_ddl": "ALTER TABLE t ADD INDEX idx_status(status)", '
            '"before": "", "after": ""}], "confidence": 0.9}\n'
            "```\n以上。"
        ),
        "stats": {
            "query_time_p95": 8000,
            "parse_total_row_counts": 1000000,
            "return_total_row_counts": 1000,
        },
        "expect": {
            "root_cause": "status 字段无索引导致全表扫描",
            "severity": AI_LEVEL_HIGH,
            "bottleneck": "full_scan",
            "evidence_len": 2,
            "suggestions_len": 1,
            "index_ddl": "ALTER TABLE t ADD INDEX idx_status(status)",
            "confidence": 0.9,
        },
    },
    {
        "name": "缺索引：模型误判 low 被统计规则覆写为 medium",
        "llm_output": (
            '{"root_cause": "user_id 查询无可用索引", "severity": "low", '
            '"bottleneck_type": "missing_index", "evidence": ["COLLSCAN"], '
            '"suggestions": [{"type": "index_ddl", "desc": "建索引", '
            '"index_ddl": "db.orders.createIndex({user_id: 1})", '
            '"before": "", "after": ""}], "confidence": 0.7}'
        ),
        # p95 3000ms：>1000 不>5000，扫描/返回比 15 → 规则恰好落 medium
        "stats": {
            "query_time_p95": 3000,
            "parse_total_row_counts": 3000,
            "return_total_row_counts": 200,
        },
        "expect": {
            "severity": AI_LEVEL_MEDIUM,
            "bottleneck": DIAG_BOTTLENECK_MISSING_INDEX,
        },
    },
    {
        "name": "非法枚举：severity/bottleneck 越界归一，且不触发统计覆写",
        "llm_output": (
            '{"root_cause": "CPU 打满", "severity": "extreme", '
            '"bottleneck_type": "cpu_throttling", "evidence": [], '
            '"suggestions": [], "confidence": "0.5"}'
        ),
        "stats": {},
        "expect": {
            "severity": AI_LEVEL_UNKNOWN,
            "bottleneck": DIAG_BOTTLENECK_OTHER,
            "confidence": 0.5,
        },
    },
]


@pytest.mark.parametrize("sample", GOLDEN_DIAGNOSIS, ids=lambda s: s["name"])
def test_golden_diagnosis(sample):
    result = OpenaiClient._parse_diagnosis_json(sample["llm_output"])
    OpenaiClient._apply_stat_severity(result, sample.get("stats") or {})
    expect = sample["expect"]
    assert "_is_fallback" not in result
    if "root_cause" in expect:
        assert result["root_cause"] == expect["root_cause"]
    assert result["severity"] == expect["severity"]
    assert result["bottleneck_type"] == expect["bottleneck"]
    if "evidence_len" in expect:
        assert len(result["evidence"]) == expect["evidence_len"]
    if "suggestions_len" in expect:
        assert len(result["suggestions"]) == expect["suggestions_len"]
        assert result["suggestions"][0]["index_ddl"] == expect["index_ddl"]
    if "confidence" in expect:
        assert result["confidence"] == expect["confidence"]
    # report_markdown 字段已移除（结构化字段即完整内容）
    assert "report_markdown" not in result


# ============ 离线黄金样本：工单审核（单条 + 批量） ============

GOLDEN_REVIEW_SINGLE = [
    {
        "name": "大表 DDL：裸换行 markdown + 字符串布尔",
        "llm_output": (
            '{"risk_level": "high", "risk_score": 88, '
            '"summary": "大表加索引将长时间锁表", '
            '"suggestion": "问题清单：\n1. 锁表风险高\n改写建议：走 gh-ost", '
            '"ddl_lock_risk": "high", "affected_rows_estimate": "约132万行", '
            '"use_osc": "true"}'
        ),
        "expect": {
            "risk_level": AI_LEVEL_HIGH,
            "risk_score": 88,
            "ddl_lock_risk": "high",
            "use_osc": True,
            "affected_rows_estimate": "约132万行",
        },
    },
    {
        "name": "低风险查询：越界分数截断 + 非法等级归 unknown",
        "llm_output": (
            '{"risk_level": " catastrophic", "risk_score": 120, '
            '"summary": "常规查询", "suggestion": "", '
            '"ddl_lock_risk": "none", "affected_rows_estimate": "", "use_osc": false}'
        ),
        "expect": {
            "risk_level": AI_LEVEL_UNKNOWN,
            "risk_score": 100,
            "ddl_lock_risk": "none",
            "use_osc": False,
        },
    },
]


@pytest.mark.parametrize("sample", GOLDEN_REVIEW_SINGLE, ids=lambda s: s["name"])
def test_golden_review_single(sample):
    result = OpenaiClient._parse_review_json(sample["llm_output"])
    expect = sample["expect"]
    for key, value in expect.items():
        assert result[key] == value, f"字段 {key} 不符"


def test_golden_review_batch_aligned_and_wrapper():
    """批量：index 乱序对齐 + {"reviews":[...]} 包裹形态。"""
    llm_output = (
        '{"reviews": ['
        '{"index": 2, "risk_level": "high", "risk_score": 90, "summary": "无 WHERE 删除", '
        '"suggestion": "", "ddl_lock_risk": "none", "affected_rows_estimate": "全表", "use_osc": false},'
        '{"index": 0, "risk_level": "low", "risk_score": 10, "summary": "常规更新", '
        '"suggestion": "", "ddl_lock_risk": "none", "affected_rows_estimate": "3 行", "use_osc": false},'
        '{"index": 1, "risk_level": "medium", "risk_score": 55, "summary": "小表加列", '
        '"suggestion": "", "ddl_lock_risk": "low", "affected_rows_estimate": "", "use_osc": false}'
        "]}"
    )
    results = OpenaiClient._parse_review_batch_json(llm_output, 3)
    assert results is not None and len(results) == 3
    assert results[0]["risk_level"] == AI_LEVEL_LOW
    assert results[1]["risk_level"] == AI_LEVEL_MEDIUM
    assert results[2]["risk_level"] == AI_LEVEL_HIGH


def test_golden_review_batch_count_mismatch_yields_none_holes():
    """批量条数不足：缺失位为 None（调用方逐条回退）。"""
    llm_output = (
        '[{"index": 0, "risk_level": "low", "risk_score": 5, "summary": "ok", '
        '"suggestion": "", "ddl_lock_risk": "none", "affected_rows_estimate": "", "use_osc": false}]'
    )
    results = OpenaiClient._parse_review_batch_json(llm_output, 3)
    assert results is not None
    assert results[0]["risk_level"] == AI_LEVEL_LOW
    assert results[1] is None
    assert results[2] is None


def test_golden_review_batch_garbage_returns_none():
    """整体不可解析 → None（调用方整批回退逐条）。"""
    assert OpenaiClient._parse_review_batch_json("not json at all", 2) is None
    assert OpenaiClient._parse_review_batch_json("", 2) is None


# ============ 风险词表与统计规则边界 ============


def test_golden_severity_rule_boundaries():
    """严重度统计规则边界（阈值改动必须过这里）：high 严格大于，越过 high 后落 medium。"""
    # p95 边界：5000 落 medium（>1000 但不>5000），5000.1 触发 high
    assert severity_from_stats(5000, 10, 10) == AI_LEVEL_MEDIUM
    assert severity_from_stats(5000.1, 10, 10) == AI_LEVEL_HIGH
    # 扫描/返回比边界：1000 落 medium（>100 但不>1000），1000.1 触发 high
    assert severity_from_stats(0, 1000, 1) == AI_LEVEL_MEDIUM
    assert severity_from_stats(0, 1001, 1) == AI_LEVEL_HIGH
    # medium 档
    assert severity_from_stats(2000, 10, 10) == AI_LEVEL_MEDIUM
    assert severity_from_stats(0, 200, 1) == AI_LEVEL_MEDIUM
    # medium 下界：不严格大于 1000/100 则不判定（保留模型结论）
    assert severity_from_stats(1000, 10, 10) is None
    assert severity_from_stats(0, 100, 1) is None
    # 低值/缺失
    assert severity_from_stats(500, 10, 10) is None
    assert severity_from_stats(None, None, None) is None


def test_golden_score_band_boundaries():
    assert score_band(0) == AI_LEVEL_LOW
    assert score_band(39) == AI_LEVEL_LOW
    assert score_band(40) == AI_LEVEL_MEDIUM
    assert score_band(70) == AI_LEVEL_MEDIUM
    assert score_band(71) == AI_LEVEL_HIGH
    assert score_band(100) == AI_LEVEL_HIGH
    assert score_band(101) == AI_LEVEL_UNKNOWN
    assert score_band("abc") == AI_LEVEL_UNKNOWN


def test_golden_normalize_level():
    assert normalize_level("HIGH") == AI_LEVEL_HIGH
    assert normalize_level("") == AI_LEVEL_UNKNOWN
    assert normalize_level(None) == AI_LEVEL_UNKNOWN
    assert normalize_level("extreme") == AI_LEVEL_UNKNOWN


# ============ 在线冒烟（默认跳过） ============

_SMOKE_ENABLED = os.environ.get("ARCHERY_AI_SMOKE") == "1"
_SMOKE_KEY = os.environ.get("ARCHERY_AI_KEY", "")
requires_smoke = pytest.mark.skipif(
    not (_SMOKE_ENABLED and _SMOKE_KEY),
    reason="在线冒烟需 ARCHERY_AI_SMOKE=1 + ARCHERY_AI_KEY（消耗真实 token，发版前人工触发）",
)


@pytest.fixture
def smoke_client(setup_sys_config):
    setup_sys_config.set("openai_base_url", os.environ.get("ARCHERY_AI_BASE_URL", ""))
    setup_sys_config.set("openai_api_key", _SMOKE_KEY)
    setup_sys_config.set(
        "default_chat_model", os.environ.get("ARCHERY_AI_MODEL", "gpt-4o-mini")
    )
    client = OpenaiClient()
    yield client
    client.client.close()


@requires_smoke
def test_smoke_diagnosis_live(smoke_client):
    """真实调用：诊断输出可解析、枚举合法。"""
    result = smoke_client.diagnose_slowquery_by_openai(
        db_type="mysql",
        db_name="smoke_db",
        sample_sql="SELECT * FROM orders WHERE user_id = 1 AND status = 'pending'",
        stats={
            "query_time_p95": 6000,
            "total_execution_counts": 1200,
            "parse_total_row_counts": 900000,
            "return_total_row_counts": 800,
        },
        trend_summary="近 7 天 p95 由 1.2s 升至 6s",
        table_schemas="CREATE TABLE orders (id bigint PRIMARY KEY, user_id bigint, status varchar(20), created_at datetime);",
        explain_text="type=ALL, rows=900000, Extra=Using where",
    )
    assert not result.get("_is_fallback"), f"诊断降级：{result}"
    from common.utils.ai_risk import VALID_LEVELS

    assert result["severity"] in VALID_LEVELS
    assert result["bottleneck_type"] in OpenaiClient.DIAG_VALID_BOTTLENECKS
    assert isinstance(result["evidence"], list)
    assert 0.0 <= result["confidence"] <= 1.0


@requires_smoke
def test_smoke_review_live(smoke_client):
    """真实调用：审核输出可解析、字段归一合法。"""
    result = smoke_client.review_sql_by_openai(
        db_type="mysql",
        db_name="smoke_db",
        sql_text="ALTER TABLE orders ADD INDEX idx_user(user_id)",
        table_schemas="CREATE TABLE orders (id bigint PRIMARY KEY, user_id bigint, status varchar(20));",
        table_rows="orders: 约 1500000 行",
    )
    from common.utils.ai_risk import VALID_LEVELS

    assert result["risk_level"] in VALID_LEVELS
    assert 0 <= result["risk_score"] <= 100
    assert result["ddl_lock_risk"] in ("none", "low", "medium", "high")
    assert isinstance(result["use_osc"], bool)
