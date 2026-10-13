# actionlint.ps1 — the local, fast feedback path of the W-actionlint rule.
#
# Local rule (workflow-file discipline, 2026-10-12; also recorded in
# SESSION-HANDOFF.md): before any push that touches .github/workflows/, run
# actionlint locally against the changed files. The pinned version and the pin
# rule are the same as the CI job's: actionlint v1.7.12 (released 2026-03-30),
# expected SHA-256 read from the release's published checksums file at pin time
# (2026-10-12) and written into the workflow, where it is reviewed at the pull
# request. CI pins the linux amd64 tarball; this script pins the windows amd64
# zip of the same release, because the checksums file published one hash per
# artifact and the local machine is windows. CI runs the same actionlint on
# every push; this script is only the earlier feedback loop. AGENT-BRIEF.md is
# unchanged: the rule lives in the handoff and in this comment.
#
# The script refuses to run a binary whose download did not match the pinned
# hash. If the pinned binary is already on disk it is reused; if it is missing,
# the script downloads and verifies it, then lints every file under
# .github/workflows/ (plus .github/actions/, as actionlint always does).
#
# Usage (from the repository root):  .\scripts\actionlint.ps1 [actionlint args ...]
# Exit code mirrors actionlint: 0 no diagnostics, 1 diagnostics found.

$ErrorActionPreference = 'Stop'

$Version = '1.7.12'
$ExpectedZipSha256 = '6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9'
# The CI job pins the linux tarball of the same release:
#   actionlint_1.7.12_linux_amd64.tar.gz =
#     8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8

$CacheDir = Join-Path $env:TEMP "actionlint-$Version"
$Exe = Join-Path $CacheDir 'actionlint.exe'
$Zip = Join-Path $CacheDir "actionlint_${Version}_windows_amd64.zip"

if (-not (Test-Path $Exe)) {
    Write-Host "actionlint v$Version not cached locally; downloading to $CacheDir"
    New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
    & curl.exe -fsSL -o $Zip "https://github.com/rhysd/actionlint/releases/download/v$Version/actionlint_${Version}_windows_amd64.zip"
    if ($LASTEXITCODE -ne 0) { throw 'download of the pinned actionlint archive failed' }
    $Actual = (Get-FileHash -Algorithm SHA256 -Path $Zip).Hash.ToLowerInvariant()
    if ($Actual -ne $ExpectedZipSha256) {
        throw "refusing to run actionlint: downloaded archive sha256 $Actual does not match the pinned value $ExpectedZipSha256"
    }
    Expand-Archive -Force -Path $Zip -DestinationPath $CacheDir
}

& $Exe @args
exit $LASTEXITCODE