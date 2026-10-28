"""S6 回归：/auth/session 必须与普通鉴权对"会话是否有效"给出一致答案。

审计现场：``/auth/session`` 只 decode JWT，不校验凭据版本（F15 ``pwv``）。
改密 / 变更 TOTP 后它仍回答 ``authenticated=true``，而普通 API 已经 401，
前端在"已登录 → 请求 401 → 刷新 session 说已登录"之间反复跳转。

修复后两者复用 ``console.deps.check_console_session``，本文件同时断言两侧。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

NEW_PASSWORD = "new-admin-password-123"


def _login(client, password: str) -> dict[str, str]:
    from tests.helpers import TEST_ADMIN_PASSWORD  # noqa: PLC0415

    response = client.post("/api/v1/auth/login", json={"password": password or TEST_ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    token = response.json()["data"]["token"]
    return {"Authorization": f"Bearer {token}"}


def _session(client, headers: dict[str, str]) -> bool:
    response = client.get("/api/v1/auth/session", headers=headers)
    assert response.status_code == 200, response.text
    return bool(response.json()["data"]["authenticated"])


def _devices_status(client, headers: dict[str, str]) -> int:
    return client.get("/api/v1/devices", headers=headers).status_code


def test_password_change_revokes_session_endpoint_and_api(tmp_dir: Path) -> None:
    from tests.helpers import TEST_ADMIN_PASSWORD, running_app  # noqa: PLC0415

    with running_app(tmp_dir) as client:
        headers = _login(client, TEST_ADMIN_PASSWORD)
        assert _session(client, headers) is True
        assert _devices_status(client, headers) == 200

        changed = client.post(
            "/api/v1/auth/password",
            json={"current_password": TEST_ADMIN_PASSWORD, "new_password": NEW_PASSWORD},
            headers=headers,
        )
        assert changed.status_code == 200, changed.text

        # (a) session 端点必须说"未认证"——否则前端会反复跳转刷新
        assert _session(client, headers) is False, "改密后旧会话在 /auth/session 必须失效"

        # (b) 普通 API 同时 401，且是统一错误信封
        response = client.get("/api/v1/devices", headers=headers)
        assert response.status_code == 401, response.text
        payload = response.json()
        assert payload["code"] == "AUTH"
        assert payload["retryable"] is False
        assert "凭据已变更" in payload["message"]

        # (c) 新密码登录拿到的新会话在两边都有效
        fresh = _login(client, NEW_PASSWORD)
        assert _session(client, fresh) is True
        assert _devices_status(client, fresh) == 200


def test_totp_enable_revokes_session_endpoint(tmp_dir: Path) -> None:
    """TOTP 变更同样 bump 凭据版本：session 端点不得再报已登录。"""

    pyotp = pytest.importorskip("pyotp")

    from tests.helpers import TEST_ADMIN_PASSWORD, running_app  # noqa: PLC0415

    with running_app(tmp_dir) as client:
        headers = _login(client, TEST_ADMIN_PASSWORD)
        setup = client.post("/api/v1/auth/totp/setup", headers=headers)
        assert setup.status_code == 200, setup.text
        secret = setup.json()["data"]["secret"]

        enabled = client.post(
            "/api/v1/auth/totp/enable",
            json={"secret": secret, "code": pyotp.TOTP(secret).now(), "password": TEST_ADMIN_PASSWORD},
            headers=headers,
        )
        assert enabled.status_code == 200, enabled.text

        assert _session(client, headers) is False, "TOTP 变更后旧会话在 /auth/session 必须失效"
        assert _devices_status(client, headers) == 401


def test_session_without_token_is_anonymous(tmp_dir: Path) -> None:
    from tests.helpers import running_app  # noqa: PLC0415

    with running_app(tmp_dir) as client:
        response = client.get("/api/v1/auth/session")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["authenticated"] is False
