param(
    [ValidateRange(0,65535)][int]$Port = 0,
    [ValidateRange(5,120)][int]$Minutes = 60
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$sessionDirectory = $null

function Find-SshTool([string]$Name) {
    $command = Get-Command ($Name + '.exe') -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($base in @("$env:WINDIR\System32\OpenSSH", "$env:WINDIR\Sysnative\OpenSSH")) {
        $candidate = Join-Path $base ($Name + '.exe')
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Set-PrivateDirectory([string]$Directory) {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($identity in @($sid, (New-Object Security.Principal.SecurityIdentifier 'S-1-5-18'))) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Directory -AclObject $acl
}

try {
    if (-not $Port) {
        $configuredPort = $env:HABAB_HOME_PORT
        $saved = Join-Path $PSScriptRoot '.env.opencode-windows'
        if (-not $configuredPort -and (Test-Path -LiteralPath $saved)) {
            $line = Get-Content -LiteralPath $saved | Where-Object { $_ -match '^HABAB_HOME_PORT=[0-9]+$' } | Select-Object -Last 1
            if ($line) { $configuredPort = $line.Split('=')[1] }
        }
        if (-not $configuredPort) { $configuredPort = '8123' }
        if (-not [int]::TryParse($configuredPort, [ref]$Port) -or $Port -lt 1024 -or $Port -gt 65535) { throw 'Invalid Home Assistant port.' }
    }
    if ($Port -lt 1024) { throw 'Home Assistant port must be between 1024 and 65535.' }
    Write-Host "Checking local Home Assistant at http://127.0.0.1:$Port ..."
    try { $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/" -TimeoutSec 5 }
    catch { throw 'Local Home Assistant is unavailable. Run start-windows.bat first, then open support_tunnel.bat.' }
    if ($response.StatusCode -ne 200) { throw 'Local application did not return HTTP 200.' }

    $ssh = Find-SshTool 'ssh'
    $keygen = Find-SshTool 'ssh-keygen'
    if (-not $ssh -or -not $keygen) {
        Write-Host 'Installing the Windows OpenSSH CLIENT. Approve the Windows administrator prompt.'
        $installation = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList '-NoProfile', '-Command', 'Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0; if (-not $?) { exit 1 }'
        if ($installation.ExitCode -ne 0) { throw 'OpenSSH Client installation failed. Install it in Windows Optional Features and rerun this BAT.' }
        $ssh = Find-SshTool 'ssh'; $keygen = Find-SshTool 'ssh-keygen'
        if (-not $ssh -or -not $keygen) { throw 'OpenSSH Client is not available yet. Restart Windows if requested and rerun this BAT.' }
    }
    $relay = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'support-relay.json') | ConvertFrom-Json
    if ($relay.host -notmatch '^[a-zA-Z0-9.-]+$' -or $relay.port -lt 1 -or $relay.port -gt 65535 -or $relay.host_key -notmatch '^ssh-ed25519 [A-Za-z0-9+/=]+$') { throw 'Invalid relay configuration.' }
    $session = [Guid]::NewGuid().ToString('N')
    $remotePort = 21000 + ([Convert]::ToInt32($session.Substring(0,4),16) % 10000)
    $relayUser = 'habab-s-' + $session.Substring(0,16)
    $sessionDirectory = Join-Path (Join-Path $env:LOCALAPPDATA 'HababHome\Support') $session
    New-Item -ItemType Directory -Force -Path $sessionDirectory | Out-Null
    Set-PrivateDirectory $sessionDirectory
    $identity = Join-Path $sessionDirectory 'identity'
    # A raw Windows argument string preserves the empty passphrase on PowerShell 5.1.
    $keyArguments = '-q -t ed25519 -N "" -f "' + $identity + '" -C "habab-support-' + $session + '"'
    $generated = Start-Process -FilePath $keygen -ArgumentList $keyArguments -NoNewWindow -Wait -PassThru
    if ($generated.ExitCode -ne 0) { throw 'Could not create the local support key.' }
    $publicKey = ([IO.File]::ReadAllText($identity + '.pub').Trim() -split '\s+')[1]
    $request = 'HABAB1:' + $session + ':' + $publicKey + ':' + $Minutes
    [IO.File]::WriteAllText((Join-Path $sessionDirectory 'support_request.txt'), $request, [Text.Encoding]::ASCII)
    $knownHosts = Join-Path $sessionDirectory 'known_hosts'
    [IO.File]::WriteAllText($knownHosts, ('[' + $relay.host + ']:' + $relay.port + ' ' + $relay.host_key + "`n"), [Text.Encoding]::ASCII)
    Write-Host ''
    Write-Host 'Copy this PUBLIC support request into your chat with support:'
    Write-Host $request
    Write-Host ''
    Write-Host "This session allows access to Home Assistant on local port $Port. Native HA login is still required."
    Write-Host 'Your private SSH key stays on this computer. Close this window to disconnect.'
    Read-Host 'After support confirms the request is authorized, press Enter to connect' | Out-Null
    $sshArguments = @(
        '-F', 'NUL', '-T', '-a', '-p', [string]$relay.port,
        '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
        '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
        '-o', 'HostKeyAlgorithms=ssh-ed25519', '-o', 'StrictHostKeyChecking=yes',
        '-o', ('UserKnownHostsFile=' + $knownHosts.Replace('\','/')), '-o', 'GlobalKnownHostsFile=NUL',
        '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=15',
        '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
        '-i', $identity, '-R', ('127.0.0.1:' + $remotePort + ':127.0.0.1:' + $Port),
        ($relayUser + '@' + $relay.host)
    )
    Write-Host 'Connecting. Wait for READY: Home Assistant before reporting success.'
    & $ssh @sshArguments
    if ($LASTEXITCODE -ne 0) { throw 'SSH support session ended with an error. Read the SSH message above; start a new support request if the previous one expired.' }
    Write-Host 'Support session closed.'
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally {
    if ($sessionDirectory -and (Test-Path -LiteralPath $sessionDirectory)) {
        Remove-Item -LiteralPath $sessionDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
}
