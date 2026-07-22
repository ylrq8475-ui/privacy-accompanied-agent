[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SshAlias,
    [string]$RemoteRoot = "spark-active-companion-demo"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($SshAlias -notmatch '^[A-Za-z0-9._-]{1,128}$') {
    throw "SshAlias must be an existing SSH config alias, not a host or command."
}
if (
    $RemoteRoot.StartsWith("/") -or
    $RemoteRoot -match '(^|[\\/])\.\.([\\/]|$)' -or
    $RemoteRoot -notmatch '^[A-Za-z0-9._/-]{1,160}$'
) {
    throw "RemoteRoot must be a safe path relative to the SSH login directory."
}

$WorkspacePath = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StagingPath = Join-Path ([System.IO.Path]::GetTempPath()) ("spark-dgx-" + [guid]::NewGuid().ToString("N"))
$ArchivePath = "$StagingPath.tar"

$SafeDirectories = @(
    "backend",
    "external_connector",
    "track_catalog",
    "tests",
    "scripts",
    "console/src",
    "console/dist",
    "data/music"
)
$SafeFiles = @(
    "AGENTS.md",
    "IMPLEMENTATION_PLAN.md",
    "README.md",
    "requirements.txt",
    "Dockerfile",
    ".dockerignore",
    ".env.example",
    "docker-compose.dgx.yml",
    "docker-compose.dgx.audius.yml",
    "docker-compose.dgx.audius-sync.yml",
    "console/index.html",
    "console/package.json",
    "console/package-lock.json",
    "console/tsconfig.json",
    "console/tsconfig.app.json",
    "console/tsconfig.node.json",
    "console/vite.config.ts",
    "data/audius_playlists.example.json"
)
$ExcludedSegments = @("__pycache__", "node_modules", "dist", ".pytest_cache", ".mypy_cache", ".ruff_cache")

function Copy-SafeDirectory {
    param([Parameter(Mandatory = $true)][string]$RelativePath)
    $SourceRoot = Join-Path $WorkspacePath $RelativePath
    if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
        throw "Required source directory is missing: $RelativePath"
    }
    Get-ChildItem -LiteralPath $SourceRoot -Recurse -File | ForEach-Object {
        $ResolvedFile = $_.FullName
        $WorkspacePrefix = $WorkspacePath.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
        if (-not $ResolvedFile.StartsWith($WorkspacePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Source file escaped the workspace allowlist."
        }
        $RelativeFile = $ResolvedFile.Substring($WorkspacePrefix.Length)
        $Segments = $RelativeFile -split '[\\/]'
        if (
            $RelativePath -ne "console/dist" -and
            ($Segments | Where-Object { $_ -in $ExcludedSegments })
        ) {
            return
        }
        if ($_.Name -match '\.(pyc|pyo|tsbuildinfo)$') {
            return
        }
        $Destination = Join-Path $StagingPath $RelativeFile
        $DestinationParent = Split-Path -Parent $Destination
        New-Item -ItemType Directory -Force -Path $DestinationParent | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $Destination
    }
}

try {
    New-Item -ItemType Directory -Path $StagingPath | Out-Null
    foreach ($Directory in $SafeDirectories) {
        Copy-SafeDirectory -RelativePath $Directory
    }
    foreach ($RelativeFile in $SafeFiles) {
        $Source = Join-Path $WorkspacePath $RelativeFile
        if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
            throw "Required source file is missing: $RelativeFile"
        }
        $Destination = Join-Path $StagingPath $RelativeFile
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
        Copy-Item -LiteralPath $Source -Destination $Destination
    }

    $Forbidden = Get-ChildItem -LiteralPath $StagingPath -Recurse -File | Where-Object {
        (($_.Name -match '^\.env') -and $_.Name -ne '.env.example') -or
        $_.Name -match '(\.sqlite3($|-)|audius_playlists\.local\.json$)' -or
        $_.FullName -match '[\\/](runs|secrets|node_modules|\.git)[\\/]'
    }
    if ($Forbidden) {
        throw "Deployment staging contains a forbidden private or generated file."
    }

    tar -cf $ArchivePath -C $StagingPath .
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create deployment archive."
    }
    $Digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $ArchivePath).Hash.ToLowerInvariant()
    $ReleaseId = $Digest.Substring(0, 16)
    $ImageTag = "spark-active-companion-demo:$ReleaseId"
    $RemoteRelease = "$RemoteRoot/releases/$ReleaseId"

    ssh -o BatchMode=yes $SshAlias "mkdir -p '$RemoteRelease/source'"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to prepare the remote release directory."
    }
    scp -q $ArchivePath "${SshAlias}:$RemoteRelease/source.tar"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upload the deployment archive."
    }
    ssh -o BatchMode=yes $SshAlias "tar -xf '$RemoteRelease/source.tar' -C '$RemoteRelease/source'"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to extract the remote deployment archive."
    }
    ssh -o BatchMode=yes $SshAlias "sh '$RemoteRelease/source/scripts/deploy_remote_dgx.sh' '$ImageTag' '$RemoteRelease/source'"
    if ($LASTEXITCODE -ne 0) {
        throw "DGX deployment failed; inspect the versioned release logs before rollback."
    }

    [pscustomobject]@{
        release_id = $ReleaseId
        archive_sha256 = $Digest
        image = $ImageTag
        remote_root = $RemoteRoot
        remote_published_port = $null
        local_console_url = "http://127.0.0.1:8000/console/"
    } | ConvertTo-Json
}
finally {
    if (Test-Path -LiteralPath $StagingPath) {
        $ResolvedStaging = (Resolve-Path -LiteralPath $StagingPath).Path
        $ResolvedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        if ($ResolvedStaging.StartsWith($ResolvedTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $ResolvedStaging -Recurse -Force
        }
    }
    if (Test-Path -LiteralPath $ArchivePath) {
        $ResolvedArchive = (Resolve-Path -LiteralPath $ArchivePath).Path
        $ResolvedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        if ($ResolvedArchive.StartsWith($ResolvedTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $ResolvedArchive -Force
        }
    }
}
