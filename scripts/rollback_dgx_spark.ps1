[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SshAlias,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-f0-9]{16}$')]
    [string]$ReleaseId,
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

$RemoteRelease = "$RemoteRoot/releases/$ReleaseId/source"
ssh -o BatchMode=yes $SshAlias "sh '$RemoteRelease/scripts/rollback_remote_dgx.sh' '$RemoteRelease'"
if ($LASTEXITCODE -ne 0) {
    throw "DGX Demo rollback failed. Existing model services were not targeted."
}
