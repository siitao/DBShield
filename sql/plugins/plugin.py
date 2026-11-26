# -*- coding: UTF-8 -*-
"""
@author: hhyo
@license: Apache Licence
@file: plugin.py
@time: 2019/03/04
"""

__author__ = "hhyo"

import logging
import re
import subprocess
import traceback

logger = logging.getLogger("default")

# 外部工具默认执行超时（秒）。my2sql 因解析大 binlog 有更长的专用超时；
# soar/sqladvisor/schemasync 等工具必须同样设上限，否则工具卡住会把 Web worker
# 永久占住（持菜单权限的普通用户并发请求即可耗尽进程池）。
DEFAULT_TIMEOUT = 60

# DSN 里的口令：mysql://user:pass@host → mysql://user:***@host
# 口令段以 token 内最后一个 @ 定界：口令本身可能含 @ 与 /（如 p@ss、pa/ss），
# 而 host 段不会含 @；按首个 @ 或禁止 / 匹配都会漏脱敏
_DSN_CREDENTIAL_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)([^:@/\s]+):(\S+)")


def redact_credentials(text):
    """脱敏外部工具输出里的 DSN 口令。

    soar/sqladvisor/schemasync 会把 DSN 拼进错误信息，而这些输出会回给前端
    并写日志；不改写就等于把库口令抄送给任何有对应菜单权限的用户。
    """
    if not text:
        return text

    def _redact(match):
        scheme, user, rest = match.groups()
        pwd_end = rest.rfind("@")
        # 无 @（普通带端口的 URL 如 https://h:8080/p）或口令为空则不改写，
        # 避免把端口号误当口令
        if pwd_end <= 0:
            return match.group(0)
        return f"{scheme}{user}:***{rest[pwd_end:]}"

    return _DSN_CREDENTIAL_RE.sub(_redact, str(text))


class Plugin:
    def __init__(self, path):
        self.path = path
        self.required_args = []  # 必须参数
        self.disable_args = []  # 禁用参数

    def check_args(self, args):
        """
        检查请求参数列表
        :return: {'status': 0, 'msg': 'ok', 'data': {}}
        """
        args_check_result = {"status": 0, "msg": "ok", "data": {}}
        # 检查路径
        if self.path is None:
            return {"status": 1, "msg": "可执行文件路径不能为空！", "data": {}}
        # 检查禁用参数
        for arg in args.keys():
            if arg in self.disable_args:
                return {
                    "status": 1,
                    "msg": "{arg}参数已被禁用".format(arg=arg),
                    "data": {},
                }
        # 检查必须参数
        for req_arg in self.required_args:
            if req_arg not in args.keys():
                return {
                    "status": 1,
                    "msg": "必须指定{arg}参数".format(arg=req_arg),
                    "data": {},
                }
            elif args[req_arg] is None or args[req_arg] == "":
                return {
                    "status": 1,
                    "msg": "{arg}参数值不能为空".format(arg=req_arg),
                    "data": {},
                }
        return args_check_result

    def generate_args2cmd(self, args):
        """
        将请求参数转换为命令行参数
        :return:
        """
        cmd_args = [self.path]
        for arg, value in args.items():
            if not value:
                continue
            cmd_args.append(f"-{arg}")
            if not isinstance(value, bool):
                cmd_args.append(f"{value}")
        return cmd_args

    @staticmethod
    def execute_cmd(cmd_args, stdin_pipe=False):
        """
        执行命令并且返回process

        :param stdin_pipe: True 时把 stdin 接到管道，供调用方通过
            communicate(input=...) 传入敏感参数（如数据库口令），
            避免口令出现在进程命令行（ps / 任务管理器可读）。
        """
        try:
            p = subprocess.Popen(
                cmd_args,
                shell=False,
                # stdin 默认置空：外部工具在缺少参数时（如 my2sql 的 -password 为空会
                # fmt.Scanln 读密码）会一直等标准输入，导致请求/任务永久挂住
                stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            return p
        except Exception as e:
            logger.error("命令执行失败\n{}".format(traceback.format_exc()))
            raise RuntimeError("命令执行失败，失败原因:%s" % str(e))

    def run_with_timeout(self, cmd_args, timeout=DEFAULT_TIMEOUT):
        """执行命令并等待结束，超时则 kill 子进程并抛 RuntimeError。

        :return: (stdout, stderr)
        """
        process = self.execute_cmd(cmd_args)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return stdout, stderr
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            logger.warning(f"外部工具执行超时（>{timeout}s）：{self.path}")
            raise RuntimeError(
                f"外部工具执行超时（超过 {timeout} 秒），请缩小 SQL 规模或检查实例连通性"
            )
