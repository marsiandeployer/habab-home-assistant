param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'home.json')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-OpenSsh([string]$Name) {
    $command = Get-Command ($Name + '.exe') -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($base in @("$env:WINDIR\System32\OpenSSH", "$env:WINDIR\Sysnative\OpenSSH")) {
        $candidate = Join-Path $base ($Name + '.exe')
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

try {
    $config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
    foreach ($name in @('relay_host', 'relay_port', 'relay_user', 'remote_port', 'local_port', 'identity_file', 'host_key')) {
        if ($null -eq $config.$name -or [string]::IsNullOrWhiteSpace([string]$config.$name)) {
            throw "Missing product tunnel setting: $name"
        }
    }
    if ($config.relay_host -notmatch '^[A-Za-z0-9.-]+$' -or
        $config.relay_user -notmatch '^[A-Za-z0-9_-]+$' -or
        $config.relay_port -lt 1 -or $config.relay_port -gt 65535 -or
        $config.remote_port -lt 1024 -or $config.remote_port -gt 65535 -or
        $config.local_port -lt 1024 -or $config.local_port -gt 65535 -or
        $config.host_key -notmatch '^ssh-ed25519 [A-Za-z0-9+/=]+$') {
        throw 'Invalid product tunnel configuration.'
    }

    $identity = [Environment]::ExpandEnvironmentVariables([string]$config.identity_file)
    if (-not (Test-Path -LiteralPath $identity -PathType Leaf)) {
        throw 'The enrolled private key is missing. Enroll a per-home public key before starting this tunnel.'
    }
    $ssh = Get-OpenSsh 'ssh'
    if (-not $ssh) { throw 'Windows OpenSSH Client is required.' }

    $healthUri = "http://127.0.0.1:$($config.local_port)/"
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUri -TimeoutSec 5
    } catch {
        throw "Home Assistant is not reachable at $healthUri. Start the local Home Assistant first."
    }
    if ($response.StatusCode -ne 200) { throw 'Local Home Assistant did not return HTTP 200.' }

    $stateDirectory = Join-Path $env:LOCALAPPDATA 'HababHome\ProductTunnel'
    New-Item -ItemType Directory -Force -Path $stateDirectory | Out-Null
    $knownHosts = Join-Path $stateDirectory 'known_hosts'
    if ($config.relay_port -eq 22) {
        $knownHostName = $config.relay_host
    } else {
        $knownHostName = "[$($config.relay_host)]:$($config.relay_port)"
    }
    [IO.File]::WriteAllText($knownHosts, ("{0} {1}`n" -f $knownHostName, $config.host_key), [Text.Encoding]::ASCII)

    Write-Host 'Product tunnel is connecting. Home Assistant native login remains required.'
    Write-Host 'Keep this window open; press Ctrl+C to disconnect.'
    $delay = 5
    while ($true) {
        $sshArguments = @(
            '-F', 'NUL', '-N', '-T', '-a', '-p', [string]$config.relay_port,
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
            '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
            '-o', 'HostKeyAlgorithms=ssh-ed25519', '-o', 'StrictHostKeyChecking=yes',
            '-o', ('UserKnownHostsFile=' + $knownHosts.Replace('\', '/')), '-o', 'GlobalKnownHostsFile=NUL',
            '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=15',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            '-i', $identity,
            '-R', ('127.0.0.1:{0}:127.0.0.1:{1}' -f $config.remote_port, $config.local_port),
            ($config.relay_user + '@' + $config.relay_host)
        )
        & $ssh @sshArguments
        if ($LASTEXITCODE -eq 0) { $delay = 5 } else { Write-Host "Tunnel disconnected; retrying in $delay seconds." }
        Start-Sleep -Seconds $delay
        $delay = [Math]::Min($delay * 2, 60)
    }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
