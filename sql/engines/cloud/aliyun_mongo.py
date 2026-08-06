# -*- coding: UTF-8 -*-
"""
阿里云 MongoDB 慢查询引擎
"""
import hashlib
import json
import logging
import datetime

from aliyunsdkcore.client import AcsClient
from aliyunsdkdds.request.v20151201 import DescribeSlowLogRecordsRequest
import simplejson

from sql.models import AliyunRdsConfig

logger = logging.getLogger("default")


class AliyunMongoEngine:
    """阿里云 MongoDB 引擎"""

    def __init__(self, instance=None):
        self.instance = instance
        self.instance_name = instance.instance_name if instance else ""

    def _get_rds_config(self):
        """获取阿里云 RDS 配置"""
        try:
            return AliyunRdsConfig.objects.get(instance__instance_name=self.instance_name)
        except AliyunRdsConfig.DoesNotExist:
            raise Exception(f"实例 {self.instance_name} 未关联阿里云 RDS 配置")

    def _format_time(self, time_str, for_query=False):
        """格式化时间为阿里云 API 需要的格式"""
        if isinstance(time_str, str):
            try:
                dt = datetime.datetime.strptime(time_str, "%Y-%m-%d")
            except ValueError:
                try:
                    dt = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return time_str
        elif isinstance(time_str, datetime.datetime):
            dt = time_str
        else:
            return str(time_str)

        if for_query:
            return dt.strftime("%Y-%m-%dT%H:%MZ")
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def slowquery_review(self, start_time, end_time, db_name=None, limit=50, offset=0):
        """
        获取 MongoDB 慢查询统计（聚合）

        注意：阿里云 MongoDB API 没有直接的统计接口，
        这里通过明细数据聚合实现
        """
        try:
            # 先获取明细数据
            result = self.slowquery_review_history(
                start_time, end_time, db_name, None, limit, offset
            )

            if not result.get("rows"):
                return {"total": 0, "rows": []}

            # 按 SQLText 聚合统计
            stats = {}
            for row in result["rows"]:
                sql_text = row.get("SQLText", "")
                if not sql_text:
                    continue

                if sql_text not in stats:
                    stats[sql_text] = {
                        "SQLText": sql_text,
                        "DBName": row.get("DBName", ""),
                        "TableName": row.get("TableName", ""),
                        "TotalExecutionCounts": 0,
                        "TotalExecutionTimes": 0,
                        "QueryTimeAvg": 0,
                        "QueryTimePct95": 0,
                        "DocsExamined": 0,
                        "ReturnRowCounts": 0,
                        "HostAddress": row.get("HostAddress", ""),
                        "AccountName": row.get("AccountName", ""),
                        "first_seen": None,
                        "last_seen": None,
                    }

                stat = stats[sql_text]
                stat["TotalExecutionCounts"] += 1
                query_time = float(row.get("QueryTimes", 0))
                stat["TotalExecutionTimes"] += query_time
                stat["DocsExamined"] += int(row.get("DocsExamined", 0))
                stat["ReturnRowCounts"] += int(row.get("ReturnRowCounts", 0))

                # 更新时间
                exec_time = row.get("ExecutionStartTime", "")
                if exec_time:
                    if not stat["first_seen"] or exec_time < stat["first_seen"]:
                        stat["first_seen"] = exec_time
                    if not stat["last_seen"] or exec_time > stat["last_seen"]:
                        stat["last_seen"] = exec_time

            # 计算平均值和格式化
            rows = []
            for sql_text, stat in stats.items():
                if stat["TotalExecutionCounts"] > 0:
                    stat["QueryTimeAvg"] = round(
                        stat["TotalExecutionTimes"] / stat["TotalExecutionCounts"], 2
                    )
                stat["TotalExecutionTimes"] = round(stat["TotalExecutionTimes"], 2)
                stat["CreateTime"] = stat["last_seen"]
                # 阿里云 MongoDB 没有原生 SQL ID，用命令文本 md5 作为稳定指纹，
                # 供前端诊断入口与后端 _collect_aliyun_stats 按指纹匹配。
                # 注意用各统计自己的 sql_text，不能复用外层循环残留变量
                stat["SQLId"] = hashlib.md5(sql_text.encode("utf-8")).hexdigest()
                rows.append(stat)

            # 按总执行时间排序
            rows.sort(key=lambda x: x["TotalExecutionTimes"], reverse=True)

            # 分页
            total = len(rows)
            start = int(offset) if offset else 0
            end = start + int(limit) if limit else total
            rows = rows[start:end]

            return {"total": total, "rows": rows}

        except Exception as e:
            logger.error(f"获取阿里云MongoDB慢查询统计失败: {e}", exc_info=True)
            return {"status": 1, "msg": f"获取阿里云MongoDB慢查询统计失败: {e}", "rows": []}

    def slowquery_review_history(self, start_time, end_time, db_name=None, sql_id=None, limit=50, offset=0):
        """获取 MongoDB 慢查询明细"""
        try:
            rds_config = self._get_rds_config()

            # 格式化时间
            start_str = self._format_time(start_time, for_query=True)
            end_str = self._format_time(end_time, for_query=True)

            # 计算页数。阿里云 DescribeSlowLogRecords 的 PageSize 只接受 [30, 100]，
            # 前端"20条/页"等小分页会报 InvalidPageSize，这里钳制到合法区间
            page_size = min(max(int(limit), 30), 100)
            page_number = (int(offset) // page_size) + 1

            # 创建客户端
            ak = rds_config.ak.raw_key_id
            secret = rds_config.ak.raw_key_secret
            client = AcsClient(ak=ak, secret=secret)

            # 创建请求
            request = DescribeSlowLogRecordsRequest.DescribeSlowLogRecordsRequest()
            request.set_DBInstanceId(rds_config.rds_dbinstanceid)
            request.set_StartTime(start_str)
            request.set_EndTime(end_str)
            request.set_PageSize(page_size)
            request.set_PageNumber(page_number)
            request.set_OrderType("desc")

            if db_name:
                request.set_DBName(db_name)

            # 调用阿里云 API
            response = client.do_action_with_exception(request)
            data = simplejson.loads(response.decode("utf-8"))

            total = data.get("TotalRecordCount", 0)
            items = data.get("Items", {}).get("LogRecords", [])

            # 格式化返回数据
            rows = []
            for item in items:
                # 解析时间
                exec_time = item.get("ExecutionStartTime", "")
                if exec_time:
                    try:
                        # 转换 UTC 时间为本地时间
                        dt = datetime.datetime.strptime(exec_time, "%Y-%m-%dT%H:%M:%SZ")
                        exec_time = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        pass

                row = {
                    "ExecutionStartTime": exec_time,
                    "HostAddress": item.get("HostAddress", ""),
                    "DBName": item.get("DBName", ""),
                    "TableName": item.get("TableName", ""),
                    "SQLText": item.get("SQLText", ""),
                    "QueryTimes": item.get("QueryTimes", ""),
                    "DocsExamined": item.get("DocsExamined", 0),
                    "ReturnRowCounts": item.get("ReturnRowCounts", 0),
                    "KeysExamined": item.get("KeysExamined", 0),
                    "AccountName": item.get("AccountName", ""),
                    # 与统计页一致的稳定指纹（md5(SQLText)），供 AI 诊断入口匹配
                    "SQLId": hashlib.md5(str(item.get("SQLText", "")).encode("utf-8")).hexdigest(),
                }
                rows.append(row)

            return {"total": total, "rows": rows}

        except Exception as e:
            logger.error(f"获取阿里云MongoDB慢查询明细失败: {e}", exc_info=True)
            return {"status": 1, "msg": f"获取阿里云MongoDB慢查询明细失败: {e}", "rows": []}

    def query(self, db_name=None, sql="", limit_num=0, close_conn=True, **kwargs):
        """执行查询（占位，MongoDB RDS 不支持直接查询）"""
        raise NotImplementedError("阿里云 MongoDB 不支持直接查询")

    def close(self):
        """关闭连接（占位）"""
        pass
