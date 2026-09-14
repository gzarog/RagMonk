"""``update/checker.py`` with a mocked ``httpx`` transport -- zero real
network calls. Covers success, an invalid (non-semver) release tag, an
HTTP error response, a network failure, and a non-JSON response, all
mapped to ``UpdateCheckError``.
"""

from __future__ import annotations

import httpx
import pytest

from ragpilot.update import checker


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def test_fetch_latest_release_returns_normalized_version_on_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        expected_path = f"/repos/{checker.GITHUB_OWNER}/{checker.GITHUB_REPO}/releases/latest"
        assert request.url.path == expected_path
        return httpx.Response(
            200,
            json={
                "tag_name": "v0.1.8",
                "html_url": "https://github.com/gzarog/Ragpilotv2/releases/tag/v0.1.8",
            },
        )

    release = checker.fetch_latest_release(http_client=_client(httpx.MockTransport(handler)))

    assert release.version == "0.1.8"
    assert release.tag_name == "v0.1.8"
    assert release.html_url.endswith("v0.1.8")


def test_fetch_latest_release_rejects_a_non_semver_tag() -> None:
    # Real-world case: this repository's current release is tagged
    # "ragpilot_0_1_0", not a "v*.*.*" tag -- must be rejected, not
    # silently accepted as an unparseable "latest version".
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tag_name": "ragpilot_0_1_0", "html_url": "..."})

    with pytest.raises(checker.UpdateCheckError, match="not a valid"):
        checker.fetch_latest_release(http_client=_client(httpx.MockTransport(handler)))


def test_fetch_latest_release_raises_on_http_error_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    with pytest.raises(checker.UpdateCheckError, match="404"):
        checker.fetch_latest_release(http_client=_client(httpx.MockTransport(handler)))


def test_fetch_latest_release_raises_on_network_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(checker.UpdateCheckError, match="unreachable"):
        checker.fetch_latest_release(http_client=_client(httpx.MockTransport(handler)))


def test_fetch_latest_release_raises_on_non_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(checker.UpdateCheckError, match="non-JSON"):
        checker.fetch_latest_release(http_client=_client(httpx.MockTransport(handler)))


def test_fetch_latest_release_raises_when_tag_name_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"html_url": "..."})

    with pytest.raises(checker.UpdateCheckError, match="tag_name"):
        checker.fetch_latest_release(http_client=_client(httpx.MockTransport(handler)))


def test_only_this_repository_is_queried() -> None:
    assert checker.GITHUB_OWNER == "gzarog"
    assert checker.GITHUB_REPO == "Ragpilotv2"
    assert checker._RELEASES_LATEST_URL.startswith("https://api.github.com/repos/gzarog/Ragpilotv2/")
