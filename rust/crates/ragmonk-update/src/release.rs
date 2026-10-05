//! The latest release of this project's own repository
//! (`update/checker.py`). HTTPS only, no project data sent, and a tag
//! that is not a strict `MAJOR.MINOR.PATCH` is rejected.
//!
//! Debug builds read `RAGMONK_UPDATE_TEST_BASE` (a stand-in for both the
//! API and the download host) so the tests can serve fake releases.
//! Release builds ignore it: nothing in config or the environment can
//! point an update elsewhere.

use std::time::Duration;

use serde_json::Value;

use crate::{versioning, GITHUB_OWNER, GITHUB_REPO};

pub const TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Release {
    /// Normalized (no leading `v`).
    pub version: String,
    /// The git tag the release lives under.
    pub tag_name: String,
    pub html_url: String,
}

/// The test stand-in host (debug builds only).
pub fn test_base() -> Option<String> {
    if cfg!(debug_assertions) {
        std::env::var("RAGMONK_UPDATE_TEST_BASE")
            .ok()
            .filter(|b| !b.is_empty())
            .map(|b| b.trim_end_matches('/').to_owned())
    } else {
        None
    }
}

fn latest_url() -> String {
    match test_base() {
        Some(base) => format!("{base}/api/releases/latest"),
        None => {
            format!("https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest")
        }
    }
}

/// Where a release asset is downloaded from: built from the validated
/// tag, never from a URL in the release description.
pub fn asset_url(tag: &str, name: &str) -> String {
    match test_base() {
        Some(base) => format!("{base}/download/{tag}/{name}"),
        None => format!(
            "https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/download/{tag}/{name}"
        ),
    }
}

pub fn client(timeout: Duration) -> reqwest::blocking::Client {
    reqwest::blocking::Client::builder()
        .timeout(timeout)
        .user_agent(format!("ragmonk/{}", ragmonk_core::version::version()))
        .build()
        .unwrap_or_else(|_| reqwest::blocking::Client::new())
}

/// The latest release, or why it could not be determined.
pub fn fetch_latest() -> Result<Release, String> {
    let resp = client(TIMEOUT)
        .get(latest_url())
        .header("accept", "application/vnd.github+json")
        .send()
        .map_err(|e| format!("GitHub is unreachable: {e}"))?;
    let status = resp.status().as_u16();
    if status >= 400 {
        return Err(format!(
            "GitHub returned HTTP {status} for the latest release"
        ));
    }
    let data: Value = resp
        .json()
        .map_err(|e| format!("GitHub returned a non-JSON response: {e}"))?;
    let tag = data["tag_name"]
        .as_str()
        .filter(|t| !t.is_empty())
        .ok_or("GitHub's latest-release response had no tag_name")?;
    let version = versioning::normalize(tag).to_owned();
    if !versioning::is_valid(&version) {
        return Err(format!(
            "latest release tag '{tag}' is not a valid MAJOR.MINOR.PATCH version"
        ));
    }
    Ok(Release {
        version,
        tag_name: tag.to_owned(),
        html_url: data["html_url"].as_str().unwrap_or_default().to_owned(),
    })
}

/// Downloads a release asset fully into memory (archives are tens of MB).
pub fn download(url: &str) -> Result<Vec<u8>, String> {
    let resp = client(Duration::from_secs(600))
        .get(url)
        .send()
        .map_err(|e| format!("download failed: {url}: {e}"))?;
    let status = resp.status().as_u16();
    if status >= 400 {
        return Err(format!("download failed: {url}: HTTP {status}"));
    }
    resp.bytes()
        .map(|b| b.to_vec())
        .map_err(|e| format!("download failed: {url}: {e}"))
}
