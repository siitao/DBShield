# -*- coding: UTF-8 -*-
"""Phase 5 完成后的 views.py · 仅保留根路径重定向。

登录 / 2FA / 配置管理等页面全部由 Vue3 SPA 接管：
  - /login、/login/2fa  → SPA Login.vue（含 SSO 入口 + 2FA 验证）
  - /config             → SPA config/Index.vue（超管配置项管理）

后端不再渲染任何业务 HTML 模板。SSO callback 由第三方库处理（dbshield/urls.py）。
"""
import logging

from django.http import HttpResponseRedirect

from common.config import SysConfig

logger = logging.getLogger("default")


def index(request):
    """根路径重定向到配置的首页（默认 /sqlworkflow/）。

    SPA 路由会进一步把未登录用户导向 /login，已登录用户按权限进入对应页。
    """
    index_path_url = SysConfig().get("index_path_url", "sqlworkflow")
    return HttpResponseRedirect(f"/{index_path_url.strip('/')}/")
