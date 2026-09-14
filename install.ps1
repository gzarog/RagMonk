# Installs the RAGpilot CLI: downloads the source for $env:RAGPILOT_REF
# (default: main), creates an isolated virtual environment, installs the
# package into it, and puts a `ragpilot` launcher on a per-user bin
# directory added to the user's PATH.
#
# Requires Python 3.12+ already on PATH -- this script does not install
# Python itself. See README.md's Installation section for details.

$ErrorActionPreference = "Stop"

$Repo = "gzarog/Ragpilotv2"
$Ref = if ($env:RAGPILOT_REF) { $env:RAGPILOT_REF } else { "main" }
$InstallDir = if ($env:RAGPILOT_INSTALL_DIR) { $env:RAGPILOT_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA "RAGpilot" }
$AppDir = Join-Path $InstallDir "app"
$VenvDir = Join-Path $InstallDir "venv"
$BinDir = if ($env:RAGPILOT_BIN_DIR) { $env:RAGPILOT_BIN_DIR } else { Join-Path $InstallDir "bin" }
# Same default as ragpilot's own core/paths.py::runtime_dir() on Windows --
# RAGPILOT_INSTALL_DIR/RAGPILOT_HOME happen to share a default today, but
# are independent overrides, so this is computed the same way rather than
# assumed equal to InstallDir above.
$RagpilotHome = if ($env:RAGPILOT_HOME) { $env:RAGPILOT_HOME } else { Join-Path $env:LOCALAPPDATA "RAGpilot" }

function Find-Python {
    # Each candidate is a hashtable { Exe; Args } rather than a flat array --
    # PowerShell's `1..0` range operator returns @(1, 0), not an empty array,
    # which would break slicing off "the rest" of a single-element array.
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $candidates += , @{ Exe = "py"; Args = @("-3.12") }
        $candidates += , @{ Exe = "py"; Args = @("-3") }
    }
    foreach ($name in @("python3.12", "python3", "python")) {
        if (Get-Command $name -ErrorAction SilentlyContinue) {
            $candidates += , @{ Exe = $name; Args = @() }
        }
    }
    foreach ($c in $candidates) {
        try {
            $ver = & $c.Exe @($c.Args) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver) {
                $parts = $ver.Trim().Split(".")
                if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12) {
                    return $c
                }
            }
        } catch {}
    }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    Write-Error "RAGpilot requires Python 3.12+, but no suitable interpreter was found on PATH.`nInstall Python 3.12 or newer (https://www.python.org/downloads/) and re-run this script."
    exit 1
}
$pythonVersion = & $Python.Exe @($Python.Args) --version
Write-Host "Using $pythonVersion at $($Python.Exe) $($Python.Args -join ' ')"

Write-Host "Downloading RAGpilot ($Ref)..."
$ZipPath = Join-Path ([System.IO.Path]::GetTempPath()) "ragpilot-$([guid]::NewGuid()).zip"
$DownloadUrl = "https://github.com/$Repo/archive/refs/heads/$Ref.zip"
# GitHub's archive/codeload endpoint can briefly 404 a branch that was just
# pushed (its zipball cache lags the push by a few seconds) -- retry a
# handful of times with linear backoff before giving up, rather than
# failing outright on what is usually a transient race.
$maxAttempts = 5
for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    try {
        Invoke-WebRequest -Uri $DownloadUrl -OutFile $ZipPath -UseBasicParsing
        break
    } catch {
        if ($attempt -ge $maxAttempts) {
            Write-Error "Failed to download $DownloadUrl after $attempt attempts: $_"
            exit 1
        }
        Write-Host "Download attempt $attempt failed, retrying in ${attempt}s..."
        Start-Sleep -Seconds $attempt
    }
}

$ExtractRoot = Join-Path ([System.IO.Path]::GetTempPath()) "ragpilot-extract-$([guid]::NewGuid())"
Expand-Archive -Path $ZipPath -DestinationPath $ExtractRoot -Force
Remove-Item -Force $ZipPath

$ExtractedDir = Get-ChildItem -Path $ExtractRoot -Directory | Select-Object -First 1
if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
New-Item -ItemType Directory -Force -Path (Split-Path $AppDir -Parent) | Out-Null
Move-Item -Path $ExtractedDir.FullName -Destination $AppDir
Remove-Item -Recurse -Force $ExtractRoot

# Resolve a version to report via SETUPTOOLS_SCM_PRETEND_VERSION: the
# downloaded zipball above has no .git for hatch-vcs to derive one from,
# so it would otherwise always fall back to the hardcoded "0.0.0"
# placeholder (see pyproject.toml's [tool.hatch.version]
# fallback-version). $Ref is used directly when it already looks like
# this project's own release-tag shape (an upgrade -- update/installer.py
# always passes an exact, already-validated tag here); the default
# "main" instead queries GitHub for the latest actual release, since
# "main" itself isn't a version. Any other custom/branch $Ref is left
# unresolved -- reporting an unrelated release's version for arbitrary
# branch content would be actively misleading. A failed or missing
# lookup (offline, no releases yet) just skips the override, same as
# before this existed.
$PretendVersion = $null
if ($Ref -match '^[vV]?\d+\.\d+\.\d+$') {
    $PretendVersion = $Ref -replace '^[vV]', ''
} elseif ($Ref -eq "main") {
    try {
        $LatestRelease = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" `
            -Headers @{ Accept = "application/vnd.github+json" }
        if ($LatestRelease.tag_name -match '^[vV]?\d+\.\d+\.\d+$') {
            $PretendVersion = $LatestRelease.tag_name -replace '^[vV]', ''
        }
    } catch {
        Write-Host "Could not determine the latest release version (continuing without it): $_"
    }
}

Write-Host "Creating virtual environment at $VenvDir..."
# Any existing venv is renamed out of the way first, rather than deleted,
# and the new one is then built fresh directly at $VenvDir -- never at a
# temporary path later swapped in. Two Windows constraints rule out the
# alternatives: `ragpilot update install` re-runs this exact script from
# inside the currently-running $VenvDir\Scripts\ragpilot.exe, and deleting
# or overwriting that file while its own process is executing fails with
# a sharing violation ("[WinError 32] ... being used by another
# process") -- but pip's own generated console-script launchers (like
# that ragpilot.exe) embed the venv's exact interpreter *path* at install
# time, so a venv built elsewhere and then renamed into place afterward
# ends up with launchers pointing at a path that no longer exists --
# renaming the *old* venv out from under the running process, before
# building the new one straight at the real path, avoids both problems
# at once. A directory rename (unlike an in-place delete/overwrite)
# succeeds even while a file inside it is open, so this works even mid
# self-upgrade; a stale "${VenvDir}.old" left behind because it was still
# in use is cleaned up automatically at the top of the next run, once
# nothing has it open anymore. (${VenvDir}, not bare $VenvDir, immediately
# before the literal ".old" text below: PowerShell parses a bare
# "$VenvDir.old" in a double-quoted string as member access --
# $VenvDir.old -- not concatenation, and since strings have no such
# property it silently evaluates to empty rather than erroring.)
$VenvDirOld = "${VenvDir}.old"
if (Test-Path $VenvDirOld) { Remove-Item -Recurse -Force $VenvDirOld -ErrorAction SilentlyContinue }
if (Test-Path $VenvDir) { Rename-Item -Path $VenvDir -NewName (Split-Path $VenvDirOld -Leaf) }
& $Python.Exe @($Python.Args) -m venv $VenvDir

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
Write-Host "Installing RAGpilot (this downloads its dependencies, including torch -- may take a few minutes)..."
& $VenvPython -m pip install --quiet --upgrade pip
# Purge pip's cache before the real install: an entry written by whatever
# pip version was previously on this machine can fail to deserialize under
# the version just upgraded to above ("WARNING: Cache entry deserialization
# failed, entry ignored") -- pip already degrades safely from that (just
# re-downloads), but starting from a clean cache means it shouldn't happen
# at all. A cache that doesn't exist yet, or isn't writable, is not a
# reason to abort the install.
try { & $VenvPython -m pip cache purge *> $null } catch {}
# Still explained below in case some other/newer cache mismatch shows up
# despite the purge above: harmless either way, pip just re-downloads that
# entry instead of using a stale cache.
Write-Host "(you may see `"Cache entry deserialization failed`" warnings below -- harmless, pip just re-downloads that entry)"
# Deliberately not --quiet here: pip's normal download/build progress output
# is the only feedback during a multi-minute, multi-hundred-MB install (torch
# chief among the dependencies) -- silencing it makes a slow-but-working
# install indistinguishable from a hung one.
if ($PretendVersion) { $env:SETUPTOOLS_SCM_PRETEND_VERSION = $PretendVersion }
try {
    & $VenvPython -m pip install $AppDir
} finally {
    if ($PretendVersion) { Remove-Item Env:\SETUPTOOLS_SCM_PRETEND_VERSION -ErrorAction SilentlyContinue }
}

# Best-effort: if the old venv above is still in use (a self-upgrade, the
# running process's own files), this silently leaves it behind for the
# next run's cleanup at the top of this section instead of failing here.
Remove-Item -Recurse -Force $VenvDirOld -ErrorAction SilentlyContinue

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$LauncherPath = Join-Path $BinDir "ragpilot.cmd"
$VenvRagpilot = Join-Path $VenvDir "Scripts\ragpilot.exe"
"@echo off`r`n`"$VenvRagpilot`" %*" | Set-Content -Path $LauncherPath -Encoding ASCII
Write-Host "RAGpilot installed: $LauncherPath"

$UserPath = [Environment]::GetEnvironmentVariable("PATH", "User")
if (";$UserPath;" -notlike "*;$BinDir;*") {
    $NewPath = if ($UserPath) { "$UserPath;$BinDir" } else { $BinDir }
    [Environment]::SetEnvironmentVariable("PATH", $NewPath, "User")
    Write-Host ""
    Write-Host "Added $BinDir to your user PATH. Open a new terminal for this to take effect."
}


# Lets `ragpilot update install` (update/installer.py) detect that this is
# an install-script install and where to re-run this same script, rather
# than guessing from the running interpreter's own path -- see this
# file's own record of itself as the one thing that can't guess itself.
New-Item -ItemType Directory -Force -Path $RagpilotHome | Out-Null
$InstallInfo = [ordered]@{
    install_method = "install-script"
    repository     = $Repo
    install_dir    = $InstallDir
    venv_dir       = $VenvDir
    bin_dir        = $BinDir
}
$InstallInfo | ConvertTo-Json | Set-Content -Path (Join-Path $RagpilotHome "install_info.json") -Encoding UTF8

Write-Host ""
Write-Host "Run 'ragpilot version' to verify, then 'ragpilot init' to get started."
