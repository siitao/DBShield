import base64

import simplejson as json
from common.utils.extend_json_encoder import convert
from rest_framework.renderers import JSONRenderer
from rest_framework.utils import encoders

# JS Number 最大安全整数 2^53-1，超过该值的整数 JSON.parse 时会丢失精度
# （如 BIGINT 主键 1767432277098876929 → 1767432277098877000），统一转字符串
MAX_SAFE_INTEGER = 2**53 - 1


def bigint_safe(value):
    """递归将超出 JS Number 安全整数范围的 int 转为字符串。

    仅转换绝对值超过 2^53-1 的 int（bool 是 int 子类需排除），
    小整数保持原样，避免影响前端数字运算/比较语义。
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value) if abs(value) > MAX_SAFE_INTEGER else value
    if isinstance(value, dict):
        return {key: bigint_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [bigint_safe(item) for item in value]
    return value


class SimpleJSONRenderer(JSONRenderer):
    encoder = encoders.JSONEncoder()

    @classmethod
    def sanitize(cls, value):
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                return base64.b64encode(value).decode("ascii")
        if isinstance(value, dict):
            return {
                str(cls.sanitize(key)): cls.sanitize(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls.sanitize(item) for item in value]
        if isinstance(value, set):
            return [cls.sanitize(item) for item in value]
        return value

    @classmethod
    def default(cls, obj):
        try:
            return convert(obj)
        except TypeError:
            return cls.encoder.default(obj)

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if data is None:
            return b""

        renderer_context = renderer_context or {}
        indent = self.get_indent(accepted_media_type, renderer_context)

        if indent is None:
            separators = self.compact and (",", ":") or (", ", ": ")
        else:
            separators = (",", ": ")

        ret = json.dumps(
            bigint_safe(self.sanitize(data)),
            indent=indent,
            ensure_ascii=self.ensure_ascii,
            allow_nan=not self.strict,
            separators=separators,
            default=self.default,
        )
        # Keep DRF's default escaping so JSON stays safe if embedded into script content.
        ret = ret.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        return ret.encode()


class BigIntSafeJSONRenderer(JSONRenderer):
    """DRF 默认 JSON 渲染器的安全版本：序列化前将超出 JS Number 安全整数范围的
    int 转为字符串，防止前端 JSON.parse 精度丢失（如 BIGINT 主键）。"""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return super().render(
            bigint_safe(data), accepted_media_type, renderer_context
        )
