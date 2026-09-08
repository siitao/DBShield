#!/bin/bash
set -euxo pipefail
#sqladvisor
curl -o sqladvisor -L https://github.com/LeoQuote/SQLAdvisor/releases/download/v2.1/sqladvisor-linux-amd64
chmod +x sqladvisor
curl -o sqlparser.tar.gz -L https://github.com/LeoQuote/SQLAdvisor/releases/download/v2.1/sqlparser-linux-amd64.tar.gz
tar -xzvf sqlparser.tar.gz
mv sqlparser /usr/local/sqlparser
rm -rf sqlparser*
#soar
curl -L -q https://github.com/XiaoMi/soar/releases/download/$SOAR_VERSION/soar.linux-amd64 -o soar
chmod +x soar
#my2sql
# 钉死到 commit，避免 master 漂移导致构建不可复现
curl -L -q https://raw.githubusercontent.com/liuhr/my2sql/69b39554cb116d02fba389ff258ca9736dea7437/releases/centOS_release_7.x/my2sql -o my2sql
chmod +x my2sql
#msodbc
curl -q -L https://packages.microsoft.com/keys/microsoft.asc -o /etc/apt/trusted.gpg.d/microsoft.asc
curl -q -L https://packages.microsoft.com/config/debian/11/prod.list -o /etc/apt/sources.list.d/mssql-release.list
apt-get update && ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev
#oracle client
mkdir -p /opt/oracle 
cd /opt/oracle
curl -q -L -o oracle-install.zip https://download.oracle.com/otn_software/linux/instantclient/1921000/instantclient-basic-linux.x64-19.21.0.0.0dbru.zip
unzip oracle-install.zip
apt-get install -yq --no-install-recommends libaio1
sh -c "echo /opt/oracle/instantclient_19_21 > /etc/ld.so.conf.d/oracle-instantclient.conf"
ldconfig
rm -rf oracle-install.zip
cd -
# 编译依赖：mysqlclient 需 gcc/libmariadb-dev，python-ldap 需 libldap2-dev/libsasl2-dev
apt-get install -yq --no-install-recommends gcc libmariadb-dev libldap2-dev libsasl2-dev ldap-utils
# mysql 软链, 供 sqladvisor 使用
ln -s /usr/lib/x86_64-linux-gnu/libmariadb.so.3 /usr/lib/x86_64-linux-gnu/libmysqlclient.so.18
apt-get clean
ln -snf /usr/share/zoneinfo/$TZ /etc/localtime
echo $TZ > /etc/timezone
python3 -m venv venv4dbshield
