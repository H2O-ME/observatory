"""0.3.26：安装包下载端点的 Range 契约。

服务端（``server/observatory/console/install.py`` 的 ``download_agent``）契约：
单区间 Range → 206 + Content-Range + Accept-Ranges；语法/区间非法（含 start 越界）
→ 416 + ``Content-Range: bytes */size``；多区间/非 bytes 单位/解析不了 → 200 整包
（不误伤老客户端）。

受控端的重试/续传单元行为在 ``tests/unit/test_link_download_resume.py``。
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

pytestmark = pytest.mark.integration

VERSION = "5.0.7"
PATH = f"/api/v1/public/agent/{VERSION}/package"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from tests.helpers import running_app

    with running_app(tmp_path_factory.mktemp("package-range")) as running:
        yield running


def _admin(client):
    from tests.helpers import TEST_ADMIN_PASSWORD

    response = client.post("/api/v1/auth/login", json={"password": TEST_ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    token = response.json()["data"]["token"]
    return {"Authorization": "Bearer " + token}


@pytest.fixture()
def package(client):
    """上传一个已知内容的安装包，返回 (admin_headers, zip_bytes)。"""

    headers = _admin(client)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        payload = b"MZ" + bytes(range(256)) * 32
        archive.writestr("observatory-agent/observatory-agent.exe", payload)
    content = buffer.getvalue()
    posted = client.post(
        "/api/v1/updates/agent",
        files={"file": (f"observatory-agent-{VERSION}.zip", content, "application/zip")},
        data={"notes": "range-test"},
        headers=headers,
    )
    assert posted.status_code == 200, posted.text
    return headers, content


def test_package_download_without_range_is_200_with_accept_ranges(client, package):
    headers, content = package
    response = client.get(PATH)
    assert response.status_code == 200
    assert response.headers.get("accept-ranges") == "bytes"
    assert response.headers.get("x-sha256") == hashlib.sha256(content).hexdigest()


def test_package_download_single_range_206(client, package):
    headers, content = package
    response = client.get(PATH, headers={"Range": "bytes=10-19"})
    assert response.status_code == 206
    size = len(content)
    assert response.headers["content-range"] == "bytes 10-19/" + str(size)
    assert response.content == content[10:20]


def test_open_range_and_suffix_range(client, package):
    headers, content = package
    open_ended = client.get(PATH, headers={"Range": "bytes=50-"})
    assert open_ended.status_code == 206
    assert open_ended.content == content[50:]

    suffix = client.get(PATH, headers={"Range": "bytes=-7"})
    assert suffix.status_code == 206
    assert suffix.content == content[-7:]
    expected = "bytes " + str(len(content) - 7) + "-" + str(len(content) - 1) + "/" + str(len(content))
    assert suffix.headers["content-range"] == expected


def test_invalid_range_is_416(client, package):
    headers, content = package
    size = len(content)
    out_of_bounds = client.get(PATH, headers={"Range": "bytes=" + str(size) + "-"})
    assert out_of_bounds.status_code == 416
    assert out_of_bounds.headers["content-range"] == "bytes */" + str(size)

    reversed_range = client.get(PATH, headers={"Range": "bytes=5-2"})
    assert reversed_range.status_code == 416
    assert reversed_range.headers["content-range"] == "bytes */" + str(size)


def test_unsupported_range_falls_back_to_200(client, package):
    headers, content = package
    multi = client.get(PATH, headers={"Range": "bytes=0-1,5-6"})
    assert multi.status_code == 200
    assert multi.content == content

    nonsense = client.get(PATH, headers={"Range": "bytes=abc"})
    assert nonsense.status_code == 200
    assert nonsense.content == content


def test_upload_content_roundtrip(client, package):
    headers, content = package
    response = client.get(PATH, headers={"Range": "bytes=0-"})
    assert response.content == content