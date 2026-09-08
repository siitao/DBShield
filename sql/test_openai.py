from types import SimpleNamespace
from typing import List
import pytest
from common.utils.ai_prompts import (
    build_diagnosis_prompt,
    build_nl2sql_guard,
    build_optimize_prompt,
    build_review_prompt,
)
from common.utils.openai import AI_SCENARIOS, OpenaiClient


@pytest.fixture
def openai_client(setup_sys_config):
    # 使用mock来模拟SysConfig
    setup_sys_config.set("openai_base_url", "https://api.openai.com")
    setup_sys_config.set("openai_api_key", "sk-xxxx")
    setup_sys_config.set("default_chat_model", "gpt-3.5-turbo")
    yield OpenaiClient()


def test_init(openai_client):
    assert openai_client.base_url == "https://api.openai.com"
    assert openai_client.api_key == "sk-xxxx"
    assert openai_client.default_chat_model == "gpt-3.5-turbo"
    openai_client.client.close()


def test_request_chat_completion(openai_client, mocker):
    mock_response = {
        "id": "cmpl-123",
        "object": "text_completion",
        "created": 1234567890,
        "choices": [{"message": {"content": "SELECT * FROM table"}}],
    }
    mocker.patch.object(
        openai_client.client.chat.completions, "create", return_value=mock_response
    )
    result = openai_client.request_chat_completion(
        messages=[{"role": "user", "content": "test message"}]
    )
    assert result == mock_response


class ChatCompletionMessage:
    def __init__(self, content):
        self.content = content


class Choice:
    def __init__(self, message: ChatCompletionMessage):
        self.message = message


class ChatCompletion:
    def __init__(self, choices: List[Choice]):
        self.choices = choices


def test_generate_sql_by_openai(openai_client, mocker):
    mock_response = ChatCompletion(
        choices=[Choice(message=ChatCompletionMessage(content="SELECT * FROM table"))]
    )
    mocker.patch.object(
        openai_client, "request_chat_completion", return_value=mock_response
    )
    db_type = "MySQL"
    table_schema = "table_schema_description"
    query_desc = "query_description"
    result = openai_client.generate_sql_by_openai(db_type, table_schema, query_desc)
    assert result == "SELECT * FROM table"
    # exception
    mocker.patch.object(
        openai_client, "request_chat_completion", side_effect=ValueError("API Error")
    )
    with pytest.raises(ValueError) as excinfo:
        openai_client.generate_sql_by_openai(
            "MySQL", "table_schema_description", "query_description"
        )
    assert str(excinfo.value) == "请求openai生成查询语句失败: API Error"


# ---------- prompt 构建器（P1：注入防护统一） ----------


def test_optimize_prompt_has_injection_notice():
    """优化 prompt 声明表结构与 SQL 为不可信数据（此前仅诊断链路有防护）。"""
    prompt = build_optimize_prompt("mysql", "db1", "select 1", "ddl_text")
    assert "【相关表结构】" in prompt
    assert "【目标查询语句】" in prompt
    assert "不是指令" in prompt
    assert "数据库：db1" in prompt


def test_review_prompt_structure():
    """审核 prompt 含数据边界声明、JSON 输出规范与关键 schema 字段。"""
    prompt = build_review_prompt("mysql", "db1", "update t set a=1", "ddl", "t: 100 行")
    assert "【相关表行数】【相关表结构】【待审核 SQL】" in prompt
    assert "仅输出 JSON" in prompt
    assert "不要使用任何 emoji" in prompt
    assert "risk_level" in prompt and "use_osc" in prompt


def test_diagnosis_prompt_dynamic_labels_and_notice():
    """诊断 prompt 按数据库类型切换章节标签，且始终带注入防护。"""
    prompt = build_diagnosis_prompt(
        db_type="mongo",
        db_name="db1",
        stats_text="- p95: 1ms",
        trend_summary="平稳",
        table_schemas="idx",
        explain_text="plan",
        sample_sql="db.t.find()",
        schema_label="集合索引",
        sample_label="慢查示例命令",
        extra_guide="MONGO_GUIDE",
    )
    assert "【集合索引】" in prompt
    assert "【慢查示例命令】" in prompt
    assert "MONGO_GUIDE" in prompt
    assert "不是指令" in prompt


def test_nl2sql_guard_declares_data_boundary():
    guard = build_nl2sql_guard()
    assert "【表结构 DDL】" in guard
    assert "【样本数据】" in guard
    assert "不是指令" in guard


# ---------- 场景化调用配置 ----------


def test_scenario_timeouts(setup_sys_config, mocker):
    """场景决定默认超时；显式 timeout 覆盖场景；未知场景回退默认。"""
    mock_openai = mocker.patch("common.utils.openai.OpenAI")
    OpenaiClient()
    assert mock_openai.call_args.kwargs["timeout"] == 60
    OpenaiClient(scenario="sql_optimize")
    assert mock_openai.call_args.kwargs["timeout"] == 90
    OpenaiClient(scenario="sql_optimize", timeout=30)
    assert mock_openai.call_args.kwargs["timeout"] == 30
    OpenaiClient(scenario="connection_test")
    assert mock_openai.call_args.kwargs["timeout"] == 15
    # 诊断场景特例登记
    assert AI_SCENARIOS["slowquery_diagnosis"].max_retries == 0
    assert AI_SCENARIOS["slowquery_diagnosis"].max_tokens == 2000
    assert AI_SCENARIOS["slowquery_diagnosis"].disable_thinking is True


def test_scenario_request_kwargs_and_telemetry(setup_sys_config, mocker):
    """诊断场景自动附加 0 重试/输出上限/关思考，且用量与延迟被采集。"""
    mock_openai = mocker.patch("common.utils.openai.OpenAI")
    client = OpenaiClient(scenario="slowquery_diagnosis")
    inner = mock_openai.return_value
    mocker.patch.object(inner, "with_options", return_value=inner)
    fake_resp = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=34)
    )
    create_mock = mocker.patch.object(
        inner.chat.completions, "create", return_value=fake_resp
    )
    client.request_chat_completion(messages=[{"role": "user", "content": "x"}])
    inner.with_options.assert_called_with(max_retries=0)
    assert create_mock.call_args.kwargs["max_tokens"] == 2000
    assert create_mock.call_args.kwargs["extra_body"] == {
        "thinking": {"type": "disabled"}
    }
    assert client.last_usage == {"prompt_tokens": 12, "completion_tokens": 34}
    assert client.total_usage == {"prompt_tokens": 12, "completion_tokens": 34}
    assert client.last_latency_ms >= 0


# ---------- 结构化输出 JSON 提取 ----------


def test_extract_json_object_variants():
    assert OpenaiClient._extract_json_object('{"a": 1}') == {"a": 1}
    assert OpenaiClient._extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert OpenaiClient._extract_json_object('前言 {"a": 1} 后记') == {"a": 1}
    # 字符串值内裸换行（LLM 写多行 markdown 的典型行为）可修复解析
    assert OpenaiClient._extract_json_object(
        '{"suggestion": "第一行\n第二行"}'
    ) == {"suggestion": "第一行\n第二行"}
    assert OpenaiClient._extract_json_object("not json at all") is None
    assert OpenaiClient._extract_json_object("") is None
    assert OpenaiClient._extract_json_object(None) is None
