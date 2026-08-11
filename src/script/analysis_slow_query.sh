#!/bin/bash
DIR="$( cd "$( dirname "$0"  )" && pwd  )"
cd ${DIR}

#配置DBShield数据库的连接地址
dbshield_db_host="127.0.0.1"
dbshield_db_port=3306
dbshield_db_user="root"
dbshield_db_password="123456"
dbshield_db_database="archery"

#被分析实例的慢日志位置，建议定期清理日志文件，否则会影响分析效率
slowquery_file="/home/mysql/log_slow.log"

#pt-query-digest可执行文件路径
pt_query_digest="/usr/bin/pt-query-digest"

#被分析实例的连接信息
hostname="mysql_host:mysql_port" # 需要和DBShield实例配置中的内容保持一致，用于筛选，配置错误会导致数据无法展示

#获取上次分析时间，初始化时请删除last_analysis_time_$hostname文件，可分析全部日志数据
if [[ -s last_analysis_time_${hostname} ]]; then
    last_analysis_time=`cat last_analysis_time_${hostname}`
else
    last_analysis_time='1000-01-01 00:00:00'
fi

#收集日志
#RDS需要增加--no-version-check选项
${pt_query_digest} \
--user=${dbshield_db_user} --password=${dbshield_db_password} --host=${dbshield_db_host} --port=${dbshield_db_port} \
--review h=${dbshield_db_host},D=${dbshield_db_database},t=mysql_slow_query_review  \
--history h=${dbshield_db_host},D=${dbshield_db_database},t=mysql_slow_query_review_history  \
--no-report --limit=100% --charset=utf8 \
--since "$last_analysis_time" \
--filter="\$event->{Bytes} = length(\$event->{arg}) and \$event->{hostname}=\"$hostname\"  and \$event->{client}=\$event->{ip} " \
${slowquery_file} > /tmp/analysis_slow_query.log

if [[ $? -ne 0 ]]; then
echo "failed"
else
echo `date +"%Y-%m-%d %H:%M:%S"`>last_analysis_time_${hostname}
fi
