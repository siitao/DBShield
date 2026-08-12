# 基础镜像 ARG 必须声明在第一个 FROM 之前（全局 ARG），
# 否则后续 FROM 无法解析（报 base name should not be blank）
#
# 两级构建：先构建基础镜像（含 /opt/venv4dbshield 与 SQLAdvisor/SOAR/my2sql 等外部工具），
# 再以此为基础构建应用镜像：
#   docker build -f src/docker/Dockerfile-base -t dbshield-base:v1 .
#   docker build -f src/docker/Dockerfile -t dbshield-vue3:v1 .   # 默认引用 dbshield-base:v1
# 也可用 --build-arg BASE_IMAGE=<其他镜像> 覆盖（如 ghcr.io/<owner>/<repo>-base:sha-xxx）
ARG BASE_IMAGE="dbshield-base:v1"

# ---- 前端构建阶段：Vue3 SPA ----
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
# 用构建阶段产物覆盖 dist（npm run build 产出）
COPY --from=frontend /app/dist /opt/dbshield/frontend/dist

WORKDIR /opt/

RUN useradd nginx

# 设置国内镜像源加速
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list \
    && sed -i 's/security.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list

# 配置 pip 使用国内镜像源
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PIP_TRUSTED_HOST=mirrors.aliyun.com

# dbshield
# cx-Orable 安装需要低一点的 setuptools 版本，同时需要 --no-build-isolation 参数，否则会安装失败
# 后续可能需要重新设计 engine 和相关依赖, 防止依赖过于复杂
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

#port
EXPOSE 9123

#start service
ENTRYPOINT ["bash", "/opt/dbshield/src/docker/startup.sh"]

