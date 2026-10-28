"""P1 契约：免票据自动注册、凭证优先于在线状态、删除后可重新入网。

锁定 D1 定稿的判定顺序（``console/enroll.py`` 模块 docstring）：
凭证校验先于 ``device_online``，卸载标记优先于残留会话，可选部署密钥先于任何写入。

为控制耗时，本文件共用一次 app 启动（模块级 fixture），每个用例使用互不相同的
UID；需要隔离状态时显式清理限流桶。
"""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """整个文件共用一次 app 启动（迁移 + 媒体层启动约 10-20s）。"""

    from tests.helpers import running_app  # noqa: PLC0415

    with running_app(tmp_path_factory.mktemp("enroll-open")) as running:
        yield running


def _dir_of(client) -> Path:
    return Path(client.app.state.settings.data_dir)


def _enroll(client, uid: str, *, prev_token: str = "", code: str = ""):
    body = {
        "uid": uid,
        "hostname": f"{uid.lower()}-host",
        "os_name": "Windows",
        "os_version": "11",
        "arch": "AMD64",
        "agent_version": "0.3.1",
    }
    if code:
        body["code"] = code
    headers = {"X-Device-Token": prev_token} if prev_token else {}
    return client.post("/api/v1/agent/enroll", json=body, headers=headers)


def _link(client, token: str):
    """一次即返的链路探测。

    空批次会被 HTTP 载体当成**长轮询**挂起 ``poll_hold_s``（默认 20s）——那不
    是慢，是协议语义（`carrier_http.py:158`）。测试里带一条 event 上行，载体
    立即返回下行批次，单次调用从 20s 降到毫秒级。
    """

    return client.post(
        "/api/v1/agent/link",
        headers={"X-Device-Token": token},
        json={
            "v": 3,
            "last_ack_id": "",
            "items": [
                {
                    "v": 3,
                    "id": f"probe-{time.time_ns()}",
                    "t": "event",
                    "ts_ms": int(time.time() * 1000),
                    "body": {"event": "probe", "data": {}},
                }
            ],
        },
    )


def _sql(client, statement: str, params: tuple = ()):
    connection = sqlite3.connect(_dir_of(client) / "observatory.db")
    try:
        rows = connection.execute(statement, params).fetchall()
        connection.commit()
        return rows
    finally:
        connection.close()


def _age_out(client, uid: str, *, seconds: int = 600) -> None:
    _sql(
        client,
        "UPDATE devices SET last_seen_ms = ? WHERE uid = ?",
        (int(time.time() * 1000) - seconds * 1000, uid),
    )


def _admin_headers(client) -> dict[str, str]:
    from tests.helpers import TEST_ADMIN_PASSWORD  # noqa: PLC0415

    response = client.post("/api/v1/auth/login", json={"password": TEST_ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['token']}"}


def test_open_enrol_proof_first_lifecycle(client) -> None:
    """首次注册免票据；凭证优先于在线状态；无凭证的两个拒绝分支可区分。"""

    # 1) 全新机器：无票据、无审批
    first = _enroll(client, "OBS-P1-A")
    assert first.status_code == 200, first.text
    token = first.json()["token"]
    assert first.json()["device_uid"] == "OBS-P1-A"
    assert len(_sql(client, "SELECT id FROM devices WHERE uid='OBS-P1-A'")) == 1

    # 2) 旧客户端仍发 install_token：接受并忽略
    legacy = client.post(
        "/api/v1/agent/enroll",
        json={"uid": "OBS-P1-LEGACY", "install_token": "definitely-not-a-ticket"},
    )
    assert legacy.status_code == 200, legacy.text

    # 3) 在线 + 自带有效凭证 → 必须允许轮换（旧实现会被 device_online 拦住）
    assert _link(client, token).status_code == 200
    rotated = _enroll(client, "OBS-P1-A", prev_token=token)
    assert rotated.status_code == 200, rotated.text
    new_token = rotated.json()["token"]
    assert new_token != token
    assert _link(client, new_token).status_code == 200

    # 4) 在线 + 无凭证 → 401 device_online
    takeover = _enroll(client, "OBS-P1-A")
    assert takeover.status_code == 401
    assert "device_online" in takeover.text

    # 5) 无凭证 + 无会话 → 401 proof_required（与上面可区分）
    assert _enroll(client, "OBS-P1-B").status_code == 200
    unauth = _enroll(client, "OBS-P1-B")
    assert unauth.status_code == 401
    assert "proof_required" in unauth.text


def test_rotation_grace_then_expiry(client) -> None:
    """轮换后旧 token 在宽限期内可用（重试语义），过期后 401。"""

    old = _enroll(client, "OBS-P1-GRACE").json()["token"]
    rotated = _enroll(client, "OBS-P1-GRACE", prev_token=old)
    assert rotated.status_code == 200
    assert _link(client, old).status_code == 200, "宽限期内 superseded token 必须可用"
    # 用旧 token 再轮换一次也是合法重试路径
    assert _enroll(client, "OBS-P1-GRACE", prev_token=old).status_code == 200

    _sql(client, "UPDATE device_token_history SET superseded_at_ms = ?", (int(time.time() * 1000) - 3_600_000,))
    assert _link(client, old).status_code == 401


def test_delete_forgets_and_reinstall_registers(client) -> None:
    """删除是"忘记记录"：旧 token 401、历史 token 级联清空、同 UID 重装自动注册。"""

    headers = _admin_headers(client)
    old = _enroll(client, "OBS-P1-DEL").json()["token"]
    assert _enroll(client, "OBS-P1-DEL", prev_token=old).status_code == 200
    device_id = _sql(client, "SELECT id FROM devices WHERE uid='OBS-P1-DEL'")[0][0]
    assert _sql(client, "SELECT COUNT(*) FROM device_token_history WHERE device_id=?", (device_id,))[0][0] >= 1

    _age_out(client, "OBS-P1-DEL")
    assert client.delete("/api/v1/devices/OBS-P1-DEL", headers=headers).status_code == 200

    left = _sql(client, "SELECT COUNT(*) FROM device_token_history WHERE device_id=?", (device_id,))[0][0]
    assert left == 0, "CASCADE 必须随设备行清掉历史 token"
    assert _link(client, old).status_code == 401, "删除后旧 token 只是未知 token"

    again = _enroll(client, "OBS-P1-DEL")
    assert again.status_code == 200, again.text
    assert _link(client, again.json()["token"]).status_code == 200


def test_uninstalled_readmit_ignores_stale_session(client) -> None:
    """行已卸载时残留 fresh session 不得阻塞重装；重入必须清空卸载标记。"""

    from sqlalchemy import select  # noqa: PLC0415

    from observatory.core.clock import now_ms  # noqa: PLC0415
    from observatory.core.models import Device  # noqa: PLC0415

    token = _enroll(client, "OBS-P1-READMIT").json()["token"]
    assert _link(client, token).status_code == 200  # 制造 fresh session

    async def mark_uninstalled() -> None:
        async with client.app.state.db.sessionmaker() as session:
            row = (await session.execute(select(Device).where(Device.uid == "OBS-P1-READMIT"))).scalars().first()
            assert row is not None
            row.uninstalled_at_ms = now_ms()
            await session.commit()

    client.portal.call(mark_uninstalled)
    assert _link(client, token).status_code == 410, "行存在且已卸载 → 410 语义保留"

    # 重装通常无凭证（helper 删了 config），且可能有残留会话
    again = _enroll(client, "OBS-P1-READMIT")
    assert again.status_code == 200, again.text
    assert _link(client, again.json()["token"]).status_code == 200
    assert _sql(client, "SELECT uninstalled_at_ms FROM devices WHERE uid='OBS-P1-READMIT'")[0][0] is None


def test_admission_key_and_limits_are_temporary(client) -> None:
    """密钥只保护新建设备行且失败零副作用；限流只临时拒绝、可恢复。"""

    settings = client.app.state.settings
    existing = _enroll(client, "OBS-P1-KEY-EXIST").json()["token"]  # 先在开放模式下建行

    saved_key = settings.enroll_key
    try:
        settings.enroll_key = "s3cret-deployment-key"

        # 失败不得留下设备行 / token
        refused = _enroll(client, "OBS-P1-KEY")
        assert refused.status_code == 401
        assert "key_required" in refused.text
        assert _sql(client, "SELECT COUNT(*) FROM devices WHERE uid='OBS-P1-KEY'")[0][0] == 0
        assert _enroll(client, "OBS-P1-KEY", code="s3cret-deployment-key").status_code == 200

        # 已在网设备的轮换不受密钥影响（不踢车队）
        assert _enroll(client, "OBS-P1-KEY-EXIST", prev_token=existing).status_code == 200
    finally:
        settings.enroll_key = saved_key

    # --- 限流：临时 429，重置后恢复 ---
    limiter = client.app.state.enroll_uid_limiter
    saved = limiter.limit
    limiter.limit = 2
    try:
        proof = _enroll(client, "OBS-P1-LIMIT").json()["token"]
        assert _enroll(client, "OBS-P1-LIMIT", prev_token=proof).status_code == 200
        blocked = _enroll(client, "OBS-P1-LIMIT", prev_token=proof)
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers
        limiter.reset("enroll:uid:OBS-P1-LIMIT")
        limiter.limit = saved
        assert _enroll(client, "OBS-P1-LIMIT", prev_token=proof).status_code == 200, "限流不得变成封禁"
    finally:
        limiter.limit = saved
        limiter.reset("enroll:uid:OBS-P1-LIMIT")


def test_concurrent_enrol_single_row_no_5xx(client) -> None:
    """同 UID 并发首次注册：只建一行且无 5xx（并发轮换见轮换用例）。"""

    limiter = client.app.state.enroll_uid_limiter
    saved_limit = limiter.limit
    saved_key = client.app.state.settings.enroll_key
    client.app.state.settings.enroll_key = ""
    limiter.limit = 50
    try:
        def one() -> int:
            return _enroll(client, "OBS-P1-RACE").status_code

        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(lambda _: one(), range(6)))
    finally:
        limiter.limit = saved_limit
        client.app.state.settings.enroll_key = saved_key
        limiter.reset("enroll:uid:OBS-P1-RACE")

    assert statuses.count(200) == 1, f"只应有一个请求建行: {statuses}"
    assert set(statuses) <= {200, 401}, f"不应出现 5xx: {statuses}"
    assert len(_sql(client, "SELECT id FROM devices WHERE uid='OBS-P1-RACE'")) == 1
