# -*- coding: UTF-8 -*-
"""认证配置写回 .env 的输入校验。"""

import pytest

from common.auth_settings_reload import (
    _ENV_MANAGED_BEGIN,
    _ENV_MANAGED_END,
    _build_managed_block,
)


def test_reject_newline_in_auth_value():
    with pytest.raises(ValueError):
        _build_managed_block(
            {
                "auth_provider": "ldap",
                "auth_ldap_server_uri": "ldap://example\nDEBUG=true",
            }
        )


def test_reject_managed_marker_in_auth_value():
    with pytest.raises(ValueError):
        _build_managed_block(
            {
                "auth_provider": "ldap",
                "auth_ldap_server_uri": f"ldap://example{_ENV_MANAGED_END}",
            }
        )


def test_normal_block_contains_flags():
    block = _build_managed_block(
        {"auth_provider": "ldap", "auth_ldap_server_uri": "ldap://example"}
    )
    assert _ENV_MANAGED_BEGIN in block
    assert "ENABLE_LDAP=true" in block
    assert "AUTH_LDAP_SERVER_URI=ldap://example" in block
    assert _ENV_MANAGED_END in block
