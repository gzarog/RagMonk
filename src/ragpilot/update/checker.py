"""Checks GitHub for RAGpilot's latest release.

Security (see this plan's Security Requirements section):

- Only ever queries this project's own repository -- ``GITHUB_OWNER``/
  ``GITHUB_REPO`` are hardcoded, not read from config or the environment,
  so an update check can never be redirected to a different repository by
  a tampered config file.
- HTTPS only (the GitHub API is HTTPS-only by construction; this module
  never falls back to plain HTTP).
- A release whose tag is not a valid semantic version is rejected --
  ``versioning.is_valid`` -- rather than trusted as-is.
- No indexed RAGpilot data is ever sent: this is a single unauthenticated
  GET with no request body and no project-derived data in the URL/headers.
"""

from __future__ import annotations

import httpx

from ragpilot.update import versioning
from ragpilot.update.models import ReleaseInfo

GITHUB_OWNER = "gzarog"
GITHUB_REPO = "Ragpilotv2"

DEFAULT_TIMEOUT_SECONDS = 10.0

_RELEASES_LATEST_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"


class UpdateCheckError(Exception):
    """GitHub could not be reached, or returned something this module
    does not trust. Callers decide how to degrade -- ``cli/update.py``'s
    explicit ``update check`` reports it; a later phase's background
    auto-checker swallows it entirely (see this plan's Offline Behavior
    section).
    """


def fetch_latest_release(
    *, http_client: httpx.Client | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> ReleaseInfo:
    """Real callers leave ``http_client`` unset (a short-lived client is
    created and closed here); tests inject one wrapping
    ``httpx.MockTransport`` -- the same DI seam as ``ai/factory.py``'s
    providers and ``ai/ollama.py``'s client.
    """
    if http_client is not None:
        return _fetch(http_client, timeout=timeout)
    with httpx.Client(timeout=timeout) as client:
        return _fetch(client, timeout=timeout)


def _fetch(client: httpx.Client, *, timeout: float) -> ReleaseInfo:
    try:
        response = client.get(
            _RELEASES_LATEST_URL,
            timeout=timeout,
            headers={"Accept": "application/vnd.github+json"},
        )
    except httpx.HTTPError as exc:
        raise UpdateCheckError(f"GitHub is unreachable: {exc}") from exc

    if response.status_code >= 400:
        raise UpdateCheckError(
            f"GitHub returned HTTP {response.status_code} for the latest release"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise UpdateCheckError(f"GitHub returned a non-JSON response: {exc}") from exc

    tag_name = data.get("tag_name")
    if not isinstance(tag_name, str) or not tag_name:
        raise UpdateCheckError("GitHub's latest-release response had no tag_name")

    version = versioning.normalize(tag_name)
    if not versioning.is_valid(version):
        raise UpdateCheckError(
            f"latest release tag {tag_name!r} is not a valid MAJOR.MINOR.PATCH version"
        )

    html_url = data.get("html_url")
    return ReleaseInfo(
        version=version,
        tag_name=tag_name,
        html_url=html_url if isinstance(html_url, str) else "",
    )
