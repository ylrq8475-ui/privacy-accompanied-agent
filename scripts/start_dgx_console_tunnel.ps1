[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SshAlias,
    [ValidateRange(1024, 65535)]
    [int]$LocalPort = 8000,
    [ValidatePattern('^[a-z0-9][a-z0-9_-]{0,62}$')]
    [string]$ProjectName = "spark-active-companion-demo"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($SshAlias -notmatch '^[A-Za-z0-9._-]{1,128}$') {
    throw "SshAlias must be an existing SSH config alias, not a host or command."
}
if (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue) {
    throw "Local loopback port $LocalPort is already in use."
}

$BackendIds = @(
    ssh -o BatchMode=yes -o ConnectTimeout=10 $SshAlias `
        docker ps `
        --filter "label=com.docker.compose.project=$ProjectName" `
        --filter "label=com.docker.compose.service=backend" `
        --quiet
)
if ($LASTEXITCODE -ne 0 -or $BackendIds.Count -ne 1) {
    throw "Exactly one running DGX Demo backend container is required."
}
$InspectJson = ssh -o BatchMode=yes -o ConnectTimeout=10 $SshAlias docker inspect $BackendIds[0]
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect the DGX Demo backend container."
}
$Inspect = $InspectJson | ConvertFrom-Json
$PrivateNetwork = "${ProjectName}_demo-private"
$Network = $Inspect[0].NetworkSettings.Networks.PSObject.Properties |
    Where-Object { $_.Name -eq $PrivateNetwork } |
    Select-Object -First 1
if (-not $Network -or $Network.Value.IPAddress -notmatch '^\d{1,3}(\.\d{1,3}){3}$') {
    throw "DGX Demo-private backend address is unavailable."
}
$BackendIp = $Network.Value.IPAddress

$Tunnel = Start-Process ssh.exe `
    -ArgumentList @(
        "-N",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-L", "127.0.0.1:${LocalPort}:${BackendIp}:8000",
        $SshAlias
    ) `
    -WindowStyle Hidden `
    -PassThru

$Ready = $false
for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
    Start-Sleep -Milliseconds 250
    if ($Tunnel.HasExited) {
        break
    }
    if (Test-NetConnection 127.0.0.1 -Port $LocalPort -InformationLevel Quiet -WarningAction SilentlyContinue) {
        $Ready = $true
        break
    }
}
if (-not $Ready) {
    if (-not $Tunnel.HasExited) {
        Stop-Process -Id $Tunnel.Id
    }
    throw "SSH loopback tunnel did not become ready."
}

[pscustomobject]@{
    process_id = $Tunnel.Id
    local_url = "http://127.0.0.1:${LocalPort}/console/"
    local_port = $LocalPort
    remote_published_port = $null
    remote_target = "${PrivateNetwork}/backend:8000"
}
