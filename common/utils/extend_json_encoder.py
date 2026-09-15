# -*- coding: UTF-8 -*-
import base64
import simplejson as json
import psycopg2

from decimal import Decimal
from datetime import datetime, date, timedelta
from functools import singledispatch
from ipaddress import IPv4Address, IPv6Address
from uuid import UUID
from bson.objectid import ObjectId
from bson.timestamp import Timestamp
from bson.decimal128 import Decimal128
from bson.regex import Regex


@singledispatch
def convert(o):
    raise TypeError("can not convert type")


@convert.register(datetime)
def _(o):
    return o.strftime("%Y-%m-%d %H:%M:%S")


@convert.register(date)
def _(o):
    return o.strftime("%Y-%m-%d")


@convert.register(timedelta)
def _(o):
    return o.__str__()


@convert.register(Decimal)
def _(o):
    return str(o)


@convert.register(memoryview)
def _(o):
    return str(o)


@convert.register(set)
def _(o):
    return list(o)


@convert.register(UUID)
def _(o):
    return str(o)


@convert.register(IPv4Address)
def _(o):
    return str(o)


@convert.register(IPv6Address)
def _(o):
    return str(o)


@convert.register(ObjectId)
def _(o):
    return str(o)


@convert.register(Timestamp)
def _(o):
    return str(o)


@convert.register(Decimal128)
def _(o):
    return str(o)


@convert.register(Regex)
def _(o):
    return str(o)


@convert.register(bytes)
def _(o):
    """bson.Binary 等 bytes 子类 → utf-8 字符串（失败时 base64）。"""
    try:
        return o.decode("utf-8")
    except Exception:
        return base64.b64encode(o).decode("utf-8")


class ExtendJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        try:
            return convert(obj)
        except TypeError:
            return super(ExtendJSONEncoder, self).default(obj)


class ExtendJSONEncoderFTime(json.JSONEncoder):
    def default(self, obj):
        try:
            if isinstance(obj, psycopg2._range.DateTimeTZRange):
                return obj.lower.isoformat(" ") + "--" + obj.upper.isoformat(" ")
            elif isinstance(obj, datetime):
                return obj.isoformat(" ")
            else:
                return convert(obj)
        except TypeError:
            return super(ExtendJSONEncoderFTime, self).default(obj)


class ExtendJSONEncoderBytes(json.JSONEncoder):
    def default(self, obj):
        try:
            # 使用convert.register处理会报错 ValueError: Circular reference detected
            # 不是utf-8格式的bytes格式需要先进行base64编码转换
            if isinstance(obj, bytes):
                try:
                    return obj.decode("utf-8")
                except UnicodeDecodeError:
                    return base64.b64encode(obj).decode("utf-8")
            else:
                return convert(obj)
        except TypeError:
            return super(ExtendJSONEncoderBytes, self).default(obj)


def _sanitize_for_json(obj):
    """递归将 bytes/Binary 等 simplejson 无法处理的类型转为 str。
    simplejson 对 bytes 会在 C 层先尝试 UTF-8 解码，失败直接抛异常，
    不会调用 default()，因此必须在 dumps 之前预处理。"""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except Exception:
            return base64.b64encode(obj).decode("ascii")
    return obj


def encode_json(data):
    """用 ExtendJSONEncoder + bigint_as_string 将数据转为 JSON-safe dict。
    消除跨视图文件中反复出现的 _encode_json 私有函数。"""
    data = _sanitize_for_json(data)
    return json.loads(
        json.dumps(data, cls=ExtendJSONEncoder, bigint_as_string=True)
    )


def encode_json_bytes(data):
    """json 模块版本的 ExtendJSONEncoderBytes（bytes→str）。"""
    data = _sanitize_for_json(data)
    return json.loads(json.dumps(data, cls=ExtendJSONEncoderBytes))
