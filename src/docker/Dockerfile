# 基础镜像 ARG 必须声明在第一个 FROM 之前（全局 ARG），
# 否则后续 FROM 无法解析（报 base name should not be blank）
#
# 两级构建：先构建基础镜像（含 /opt/venv4dbshield 与 SQLAdvisor/SOAR/my2sql 等外部工具），
# 再以此为基础构建应用镜像：
#   docker build -f src/docker/Dockerfile-base -t dbshield-base:v1 .
#   docker build -f src/docker/Dockerfile -t dbshield-vue3:v1 .   # 默认引用 dbshield-base:v1
# 也可用 --build-arg BASE_IMAGE=<其他镜像> 覆盖（如 ghcr.io/<owner>/<repo>-base:sha-xxx）
ARG BASE_IMAGE="dbshield-base:v1"

FROM node:22-alpine AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- 运行时阶段 ----
FROM ${BASE_IMAGE}
SHELL ["/bin/bash", "-c"]
COPY . /opt/dbshield/
COPY --from=frontend /app/dist /opt/dbshield/frontend/dist

WORKDIR /opt/

RUN useradd nginx


RUN rm -rf /var/lib/apt/lists/* \
    && printf '%s\n' \
       "deb http://snapshot.debian.org/archive/debian/20260825T000000Z bullseye main contrib non-free" \
       "deb http://snapshot.debian.org/archive/debian/20260825T000000Z bullseye-updates main contrib non-free" \
       "deb http://snapshot.debian.org/archive/debian-security/20260825T000000Z bullseye-security main contrib non-free" \
       > /etc/apt/sources.list \
    && echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid-until

# 配置 pip 使用国内镜像源
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PIP_TRUSTED_HOST=mirrors.aliyun.com

# dbshield
RUN apt-get update \
    && apt-get install -yq --no-install-recommends nginx mariadb-client \
    && source venv4dbshield/bin/activate \
    && pip install --no-cache-dir "setuptools<82" wheel \
    && pip install --no-build-isolation --no-cache-dir "cx-Oracle==8.3.0" \
    && pip install --no-cache-dir -r /opt/dbshield/requirements.txt \
    && cp -f /opt/dbshield/src/docker/nginx.conf /etc/nginx/ \
    && cp -f /opt/dbshield/src/docker/supervisord.conf /etc/ \
    && mv /opt/sqladvisor /opt/dbshield/src/plugins/ \
    && mv /opt/soar /opt/dbshield/src/plugins/ \
    && mv /opt/my2sql /opt/dbshield/src/plugins/ \
    && apt-get -yq remove gcc curl \
    && apt-get clean \
    && rm -rf /var/cache/apt/* \
    && rm -rf /root/.cache

EXPOSE 9123

ENTRYPOINT ["bash", "/opt/dbshield/src/docker/startup.sh"]

