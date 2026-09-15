# -*- coding: UTF-8 -*-
"""兼容层：AI 网关已更名为 :mod:`common.utils.ai_gateway`。

此文件保留一个版本期，供外部脚本/旧引用平滑过渡；
项目内部代码一律 ``from common.utils.ai_gateway import ...``。
"""

from common.utils.ai_gateway import *  # noqa: F401,F403
from common.utils.ai_gateway import (  # noqa: F401
    AI_REVIEW_FALLBACK,
    AI_SCENARIOS,
    DIAG_BOTTLENECK_MISSING_INDEX,
    DIAG_BOTTLENECK_OTHER,
    DIAG_VALID_BOTTLENECKS,
    DIAGNOSIS_FALLBACK,
    OpenaiClient,
    check_openai_config,
    diagnose_slowquery_by_openai,
    optimize_sql_by_openai,
    record_ai_usage,
    test_openai_connection,
)
