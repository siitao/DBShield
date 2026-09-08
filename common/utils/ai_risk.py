"""AI 风险词表统一（工单审核 / 慢查诊断共用）。

两条链路历史上各自演化出相近词汇（审核 risk_level/risk_score/ddl_lock_risk，
诊断 severity/bottleneck_type/confidence），本模块收敛公共语义：
- 等级枚举与归一（normalize_level）
- 严重度统计规则（severity_from_stats：p95 / 扫描返回比阈值单点维护，
  诊断 prompt 的判定说明与代码兜底共用，改阈值只改这里）
- 审核分数量级换算（score_band，与审核 prompt 的评分标准一致）
- 中文标签映射（等级 / 锁表风险 / 瓶颈类型），供报告拼装与前端兜底
"""

AI_LEVEL_LOW = "low"
AI_LEVEL_MEDIUM = "medium"
AI_LEVEL_HIGH = "high"
AI_LEVEL_UNKNOWN = "unknown"
VALID_LEVELS = (AI_LEVEL_LOW, AI_LEVEL_MEDIUM, AI_LEVEL_HIGH, AI_LEVEL_UNKNOWN)
# 有效判定等级（unknown 视为"无有效判定"，工单汇总按无 AI 数据处理）
VALID_JUDGED_LEVELS = (AI_LEVEL_LOW, AI_LEVEL_MEDIUM, AI_LEVEL_HIGH)

# 严重度统计规则阈值（与诊断 prompt 第 2 条判定说明保持一致）
SEVERITY_HIGH_P95_MS = 5000
SEVERITY_HIGH_SCAN_RATIO = 1000
SEVERITY_MEDIUM_P95_MS = 1000
SEVERITY_MEDIUM_SCAN_RATIO = 100

LEVEL_LABELS = {
    AI_LEVEL_LOW: "低危",
    AI_LEVEL_MEDIUM: "中危",
    AI_LEVEL_HIGH: "高危",
    AI_LEVEL_UNKNOWN: "未知",
}

LOCK_LABELS = {
    "none": "非 DDL/无锁表风险",
    "low": "低",
    "medium": "中",
    "high": "高",
}

BOTTLENECK_LABELS = {
    "full_scan": "全表扫描",
    "missing_index": "缺索引",
    "lock_wait": "锁等待",
    "filesort": "文件排序",
    "tmp_table": "临时表",
    "type_cast": "类型转换",
    "other": "其他",
}


def normalize_level(value) -> str:
    """任意模型输出归一为合法等级枚举，非法值回退 unknown。"""
    level = str(value or "").lower()
    return level if level in VALID_LEVELS else AI_LEVEL_UNKNOWN


def severity_from_stats(p95_ms, rows_examined, rows_returned):
    """按统计指标判定严重度（诊断链路的代码兜底，与 prompt 规则同源）。

    p95>5000ms 或扫描/返回比>1000 → high；p95>1000ms 或比>100 → medium；
    统计缺失/无法计算时返回 None（调用方保留模型判定，不覆写）。
    """
    try:
        p95 = float(p95_ms or 0)
        examined = float(rows_examined or 0)
        returned = float(rows_returned or 0)
    except (TypeError, ValueError):
        return None
    ratio = examined / returned if returned and returned > 0 else 0
    if p95 > SEVERITY_HIGH_P95_MS or ratio > SEVERITY_HIGH_SCAN_RATIO:
        return AI_LEVEL_HIGH
    if p95 > SEVERITY_MEDIUM_P95_MS or ratio > SEVERITY_MEDIUM_SCAN_RATIO:
        return AI_LEVEL_MEDIUM
    return None


def score_band(score) -> str:
    """审核分数量级换算（0-39 low / 40-70 medium / 71-100 high），越界归 unknown。"""
    try:
        score = int(score)
    except (TypeError, ValueError):
        return AI_LEVEL_UNKNOWN
    if not (0 <= score <= 100):
        return AI_LEVEL_UNKNOWN
    if score >= 71:
        return AI_LEVEL_HIGH
    if score >= 40:
        return AI_LEVEL_MEDIUM
    return AI_LEVEL_LOW


def level_label(level) -> str:
    return LEVEL_LABELS.get(normalize_level(level), "未知")


def bottleneck_label(bottleneck_type) -> str:
    return BOTTLENECK_LABELS.get(str(bottleneck_type or ""), "其他")
