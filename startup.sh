#!/bin/bash

# 收集所有的静态文件到STATIC_ROOT
python3 manage.py collectstatic -v0 --noinput

# 数据库迁移（幂等；最多重试10次以等待数据库就绪）
migrate_ok=0
for i in $(seq 1 10); do
    if python3 manage.py migrate --noinput; then
        migrate_ok=1
        break
    fi
    echo "迁移失败（第 $i/10 次），3 秒后重试..."
    sleep 3
done
if [ "$migrate_ok" -ne 1 ]; then
    echo 数据库迁移最终失败，终止启动（请检查数据库连接与 django_migrations 状态）
    exit 1
fi

# 启动服务
supervisord -c supervisord.conf

