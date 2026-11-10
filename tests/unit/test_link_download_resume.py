"""0.3.26：安装包下载的短重试 + Range 断点续传（0.3.17 遗留账）。

0.3.25 发布夜三楼两台设备拉 26.5MB 包同报"全部线路不可达"（空错误串 = 超时），
旧实现单次 GET 无重试无续传。这里用 ``httpx.MockTransport`` 状态机模拟差线路：

* 首轮传输中途停摆（先吐半截再断流）→ 下一轮必须带 ``Range: bytes=<n>-`` 续传；
* 服务端回 206 接着拉 → 拼出完整文件；
* 服务端无视 Range 回 200 → 丢弃半截从头重来，文件仍完整；
* 连续全灭 → 恰好尝试 ``DOWNLOAD_ATTEMPTS`` 轮后抛 ``LinkUnavailable``；
* 4xx 语义错误绝不重试。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from observatory_agent.core import link as link_mod
from observatory_agent.core.link import Link

pytestmark = pytest.mark.unit

PAYLOAD = bytes(range(256)) * 40  # 10240 字节，够切出非零半截


def make_link(handler) -> Link:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    return Link(
        base_urls=["http://cdn.test"],
        token="device-token",
        device_uid="OBS-TEST",
        agent_version="0.3.26",
        http_client=client,
    )


@pytest.fixture()
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(link_mod, "DOWNLOAD_BACKOFF_S", 0.0)


def test_resume_after_mid_body_stall(tmp_path, no_backoff) -> None:
    """半截 + 206 续传：第二轮带 Range 头，最终文件完整。"""

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("range", ""))
        if len(calls) == 1:

            async def stream():
                yield PAYLOAD[:400]
                raise httpx.ReadTimeout("line stalled", request=request)

            return httpx.Response(200, content=stream())
        start = int(request.headers["range"].split("=")[1].rstrip("-"))
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"},
            content=PAYLOAD[start:],
        )

    link = make_link(handler)
    target = asyncio.run(link.download_package("0.3.26", tmp_path / "pkg.zip"))
    assert target.read_bytes() == PAYLOAD
    assert calls == ["", "bytes=400-"]


def test_server_without_range_support_restarts_from_scratch(tmp_path, no_backoff) -> None:
    """半截 + 服务端无视 Range 回 200：推倒重来，文件仍完整。"""

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("range", ""))
        if len(calls) == 1:

            async def stream():
                yield PAYLOAD[:400]
                raise httpx.ReadTimeout("line stalled", request=request)

            return httpx.Response(200, content=stream())
        return httpx.Response(200, content=PAYLOAD)

    link = make_link(handler)
    target = asyncio.run(link.download_package("0.3.26", tmp_path / "pkg.zip"))
    assert target.read_bytes() == PAYLOAD
    assert calls == ["", "bytes=400-"]
    assert not target.with_suffix(".zip.part").exists()


def test_exhausted_attempts_raise_and_keep_part(tmp_path, no_backoff, monkeypatch) -> None:
    """连续全灭：恰好 DOWNLOAD_ATTEMPTS 轮，LinkUnavailable，.part 保留。"""

    monkeypatch.setattr(link_mod, "DOWNLOAD_ATTEMPTS", 3)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1

        async def stream():
            yield b"x" * 10
            raise httpx.ConnectError("all lines down", request=request)

        return httpx.Response(200, content=stream())

    link = make_link(handler)
    with pytest.raises(Exception, match="连续 3 轮失败"):
        asyncio.run(link.download_package("0.3.26", tmp_path / "pkg.zip"))
    assert calls["n"] == 3
    assert (tmp_path / "pkg.zip.part").is_file()


def test_http_4xx_is_never_retried(tmp_path, no_backoff) -> None:
    """410（包下架）是语义错误：立即抛，不烧重试轮次。"""

    from observatory_agent.core.link import LinkGoneError

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(410, text="gone")

    link = make_link(handler)
    with pytest.raises(LinkGoneError):
        asyncio.run(link.download_package("9.9.9", tmp_path / "pkg.zip"))
    assert calls["n"] == 1


def test_empty_package_is_rejected(tmp_path, no_backoff) -> None:
    """200 但零字节：立即失败，不把空文件 replace 成成品。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    link = make_link(handler)
    with pytest.raises(Exception, match="安装包为空"):
        asyncio.run(link.download_package("0.3.26", tmp_path / "pkg.zip"))
    assert not (tmp_path / "pkg.zip").exists()


def test_content_range_parser() -> None:
    parse = link_mod._parse_content_range
    assert parse("bytes 400-10239/10240") == (400, 10240)
    assert parse("bytes 400-10239/*") == (400, None)
    assert parse("BYTES 0-0/1") == (0, 1)
    assert parse("") == (None, None)
    assert parse("nonsense") == (None, None)
