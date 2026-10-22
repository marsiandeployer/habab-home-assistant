param([switch]$XiaomiHome)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Set-Location $PSScriptRoot
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Find-Docker {
    $command = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($candidate in @(
        "$env:ProgramFiles\Docker\Docker\resources\bin\docker.exe",
        "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
    )) { if (Test-Path $candidate) { return $candidate } }
    return $null
}

function Add-DockerDirectoryToPath {
    param([string]$DockerPath)
    $dockerDirectory = Split-Path -Parent $DockerPath
    $pathSeparator = [string][IO.Path]::PathSeparator
    $pathEntries = @($env:PATH -split [regex]::Escape($pathSeparator))
    if (-not ($pathEntries | Where-Object {
        [string]::Equals($_, $dockerDirectory, [StringComparison]::OrdinalIgnoreCase)
    })) {
        $env:PATH = if ($env:PATH) { "$dockerDirectory$pathSeparator$env:PATH" } else { $dockerDirectory }
    }
}

function Run-Docker {
    param([string[]]$DockerArgs)
    & $script:Docker @DockerArgs
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed (exit $LASTEXITCODE). See the output above." }
}

function Get-DockerKind {
    $previousPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 can turn native stderr into terminating errors.
        $ErrorActionPreference = 'Continue'
        $result = & $script:Docker info --format '{{.OSType}}' 2>$null
        if ($LASTEXITCODE -eq 0) { return ([string]$result).Trim() }
        return $null
    } finally { $ErrorActionPreference = $previousPreference }
}

try {
    Write-Host 'Habab Home: local Home Assistant + OpenCode + DeepSeek'
    Write-Host 'OpenCode for Windows is not required. The Linux executable stays in a Docker volume.'
    $Docker = Find-Docker
    if (-not $Docker) {
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if ($winget) {
            Write-Host 'Installing Docker Desktop through winget. Complete the official installer if prompted.'
            & $winget.Source install --exact --id Docker.DockerDesktop --accept-source-agreements
            if ($LASTEXITCODE -notin @(0, 3010)) { throw 'Docker Desktop installation failed. Install it from https://docs.docker.com/desktop/setup/install/windows-install/ and rerun this BAT.' }
        } else {
            $cpu = $env:PROCESSOR_ARCHITECTURE
            $platform = if ($cpu -eq 'ARM64') { 'arm64' } else { 'amd64' }
            $installer = Join-Path $env:TEMP 'Habab-Docker-Desktop-Installer.exe'
            Write-Host 'Downloading the official Docker Desktop installer.'
            Invoke-WebRequest -UseBasicParsing -Uri "https://desktop.docker.com/win/main/$platform/Docker%20Desktop%20Installer.exe" -OutFile $installer
            $signature = Get-AuthenticodeSignature $installer
            if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Docker') { throw 'Docker installer signature verification failed.' }
            try { Start-Process -FilePath $installer -ArgumentList 'install' -Wait }
            finally { Remove-Item $installer -ErrorAction SilentlyContinue }
        }
        $Docker = Find-Docker
        if (-not $Docker) { throw 'Restart Windows if requested, then run this BAT again. Docker Desktop was not found yet.' }
    }

    # Credential helpers are launched through PATH. A newly installed Docker
    # Desktop may not yet have refreshed this process's PATH.
    Add-DockerDirectoryToPath -DockerPath $Docker

    $desktop = @(
        "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
        "$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not (Get-DockerKind) -and $desktop) { Start-Process -FilePath $desktop }
    Write-Host 'Waiting for Docker Desktop. Accept its terms in the Docker window if shown.'
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        $kind = Get-DockerKind
        if ($kind) {
            if ($kind -ne 'linux') { throw 'Switch Docker Desktop to Linux containers and rerun this BAT.' }
            $ready = $true
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) {
        $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
        $wslReady = $false
        if ($wsl) {
            $previousPreference = $ErrorActionPreference
            try { $ErrorActionPreference = 'Continue'; & $wsl.Source --version 2>$null | Out-Null; $wslReady = $LASTEXITCODE -eq 0 }
            finally { $ErrorActionPreference = $previousPreference }
        }
        if (-not $wslReady) {
            Write-Host 'Installing WSL 2. Approve the Windows administrator prompt; a restart may be required.'
            Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList '-NoProfile', '-Command', 'wsl.exe --install --no-distribution; Read-Host "Press Enter after reading the WSL result"'
            throw 'Restart Windows, then run this BAT again. If Docker still fails, enable hardware virtualization in BIOS/UEFI.'
        }
        throw 'Docker is not ready. Open Docker Desktop, complete its startup, and rerun this BAT. If Windows requests a restart, restart first.'
    }
    Run-Docker -DockerArgs @('compose', 'version')

    # Preserve a previously selected local port when the launcher is rerun.
    if (-not $env:HABAB_HOME_PORT) {
        $savedEnvironment = Join-Path $PSScriptRoot '.env.opencode-windows'
        if (Test-Path -LiteralPath $savedEnvironment) {
            $savedPort = Get-Content -LiteralPath $savedEnvironment |
                Where-Object { $_ -match '^HABAB_HOME_PORT=[0-9]+$' } |
                Select-Object -Last 1
            if ($savedPort) { $env:HABAB_HOME_PORT = $savedPort.Split('=')[1] }
        }
        if (-not $env:HABAB_HOME_PORT) { $env:HABAB_HOME_PORT = '8123' }
    }
    $portNumber = 0
    if (-not [int]::TryParse($env:HABAB_HOME_PORT, [ref]$portNumber) -or $portNumber -lt 1024 -or $portNumber -gt 65535) { throw 'HABAB_HOME_PORT must be a number between 1024 and 65535.' }
    if ($XiaomiHome -and $portNumber -ne 8123) { throw 'Xiaomi Home OAuth is registered for homeassistant.local:8123. Run start-xiaomi-home.bat with port 8123.' }
    Write-Host 'Building Home Assistant. The first download can take several minutes.'
    $env:HABAB_OPENCODE_SHA256 = 'bootstrap-pending'
    Run-Docker -DockerArgs @('compose', '-p', 'habab-home-windows', '-f', 'compose.windows.yaml', 'build', 'homeassistant')
    Run-Docker -DockerArgs @('volume', 'create', 'habab-home-opencode-bin')
    $mount = "$($PSScriptRoot):/bootstrap:ro"
    $installation = & $Docker run --rm --entrypoint python3 -v 'habab-home-opencode-bin:/opt/habab-opencode' -v $mount 'habab-home:windows-test' '/bootstrap/install-opencode-volume.py'
    if ($LASTEXITCODE -ne 0) { throw 'The pinned Linux OpenCode download or integrity check failed.' }
    $installation | ForEach-Object { Write-Host $_ }
    $digestLine = $installation | Where-Object { $_ -match '^HABAB_OPENCODE_SHA256=[a-f0-9]{64}$' } | Select-Object -Last 1
    if (-not $digestLine) { throw 'OpenCode SHA-256 was not returned.' }
    $env:HABAB_OPENCODE_SHA256 = $digestLine.Split('=')[1]
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot '.env.opencode-windows'), "$digestLine`nHABAB_HOME_PORT=$portNumber`n", [Text.UTF8Encoding]::new($false))
    if ($XiaomiHome) {
        Write-Host 'Installing the official Xiaomi Home component for your personal Home Assistant installation.'
        Write-Host 'Source and license: https://github.com/XiaoMi/ha_xiaomi_home'
        Run-Docker -DockerArgs @('compose', '--env-file', '.env.opencode-windows', '-p', 'habab-home-windows', '-f', 'compose.windows.yaml', 'run', '--rm', '--no-deps', '--entrypoint', 'python3', 'homeassistant', '/usr/local/bin/habab-install-xiaomi-home')
        Write-Host 'The Xiaomi OAuth callback requires homeassistant.local to resolve to this computer.'
        $callbackHelper = Join-Path $PSScriptRoot 'ensure-xiaomi-callback.ps1'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $callbackHelper
        if ($LASTEXITCODE -ne 0) { throw 'Could not configure the Xiaomi OAuth callback. Approve the Windows hosts-file prompt and rerun start-xiaomi-home.bat.' }
    }
    Run-Docker -DockerArgs @('compose', '--env-file', '.env.opencode-windows', '-p', 'habab-home-windows', '-f', 'compose.windows.yaml', 'up', '-d', 'homeassistant')
    $url = "http://127.0.0.1:$portNumber"
    Write-Host 'Waiting for Home Assistant...'
    $httpReady = $false
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 3
            if ($response.StatusCode -eq 200) { $httpReady = $true; break }
        } catch { }
        Start-Sleep -Seconds 2
    }
    if (-not $httpReady) { throw 'Home Assistant is not ready yet. Run docker compose --env-file .env.opencode-windows -p habab-home-windows -f compose.windows.yaml logs --tail 80, then rerun this BAT.' }
    Write-Host "Ready: $url"
    Write-Host 'Create your owner account, open AI, save your own DeepSeek API key, then use the chat icon in the dashboard.'
    Write-Host 'No API keys are included. Windows Bluetooth/USB is not passed through automatically.'
    if ($XiaomiHome) {
        Write-Host 'Opening the official Xiaomi Home integration. Choose Russia and complete Xiaomi login in your browser.'
        Start-Process "$url/config/integrations/dashboard/add?domain=xiaomi_home"
    } else {
        Start-Process $url
    }
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
