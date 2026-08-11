# -*- coding: UTF-8 -*-
"""Phase 5 完成后的 sql/urls.py · 仅保留根路径重定向 + jsi18n。

所有页面已由 Vue3 SPA 接管（/login、/config、/dashboard 等 SPA 路由）。
后端不再渲染任何业务 HTML 模板，仅保留：
  - /、/index/        根路径重定向（兼容旧书签）
  - /jsi18n/          Django admin 内部 i18n catalog

SSO 登录入口（/oidc/、/dingding/、/cas/）由 dbshield/urls.py 条件注册，
callback 由第三方库处理，不在此处。
"""

from django.urls import path
from django.views.i18n import JavaScriptCatalog

from sql import views

urlpatterns = [
    # ---- 根路径重定向（兼容旧书签 /index/） ----
    path("", views.index),
    path("index/", views.index),

    # ---- Django 内部 ----
    path("jsi18n/", JavaScriptCatalog.as_view(), name="javascript-catalog"),
]
