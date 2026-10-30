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

function Set-SourceProvenance {
    $revision = 'unknown'
    $dirty = 'unknown'
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($git) {
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $tracked = & $git.Source -C $PSScriptRoot ls-files --error-unmatch Dockerfile release.json 2>$null
            if ($LASTEXITCODE -eq 0) {
                $candidate = (& $git.Source -C $PSScriptRoot rev-parse --verify HEAD 2>$null | Out-String).Trim()
                if ($LASTEXITCODE -eq 0 -and $candidate -match '^[0-9a-f]{40,64}$') {
                    $revision = $candidate
                    $status = & $git.Source -C $PSScriptRoot status --porcelain --untracked-files=all 2>$null
                    if ($LASTEXITCODE -eq 0) { $dirty = if ($status) { 'true' } else { 'false' } }
                }
            }
        } finally { $ErrorActionPreference = $previousPreference }
    }
    if ($revision -eq 'unknown') {
        try {
            $release = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'release.json') -Raw | ConvertFrom-Json
            if ($release.source_revision -is [string] -and $release.source_revision -match '^[0-9a-f]{40,64}$') {
                $revision = $release.source_revision
                if ($release.source_dirty -is [bool]) {
                    $dirty = if ($release.source_dirty) { 'true' } else { 'false' }
                } elseif ($release.source_dirty -in @('true', 'false', 'unknown')) {
                    $dirty = $release.source_dirty
                }
            }
        } catch { }
    }
    $env:HABAB_SOURCE_REVISION = $revision
    $env:HABAB_SOURCE_DIRTY = $dirty
    Write-Host "Building source revision $revision (dirty: $dirty)."
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

    # Prepare the registered Xiaomi callback name during the ordinary setup.
    # This only changes the Windows hosts file; it does not install or enable
    # the optional Xiaomi integration. A declined/unavailable callback must not
    # prevent the general Home Assistant installation from starting.
    $callbackReady = $false
    if ($portNumber -eq 8123) {
        $callbackHelper = Join-Path $PSScriptRoot 'ensure-xiaomi-callback.ps1'
        Write-Host 'Preparing the optional Xiaomi callback address. Windows may ask once for permission to add homeassistant.local to this computer.'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $callbackHelper
        $callbackExitCode = $LASTEXITCODE
        if ($callbackExitCode -eq 0) {
            $callbackReady = $true
        } elseif ($XiaomiHome) {
            throw 'Xiaomi Home needs homeassistant.local to resolve to this computer. Callback preparation did not complete; approve the Windows prompt or resolve the existing hosts-file conflict, then rerun this BAT.'
        } else {
            Write-Host 'Xiaomi callback preparation was not completed. Home Assistant will still start; if you later choose Xiaomi Home, use the protected callback field in its setup card.' -ForegroundColor Yellow
        }
    } else {
        Write-Host "Xiaomi's registered callback uses port 8123. This Home Assistant instance uses port $portNumber; it will still start, and the Xiaomi setup card has a protected manual callback option if needed." -ForegroundColor Yellow
    }

    Write-Host 'Building Home Assistant. The first download can take several minutes.'
    $env:HABAB_OPENCODE_SHA256 = 'bootstrap-pending'
    Set-SourceProvenance
    $checkoutTunnel = Join-Path (Split-Path -Parent $PSScriptRoot) 'product-tunnel\web_tunnel.py'
    $stageDirectory = Join-Path $PSScriptRoot 'product-tunnel'
    $stagedTunnel = Join-Path $stageDirectory 'web_tunnel.py'
    $stagedByThisRun = $false
    if (Test-Path -LiteralPath $checkoutTunnel -PathType Leaf) {
        if (Test-Path -LiteralPath $stagedTunnel -PathType Leaf) {
            $sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $checkoutTunnel).Hash
            $stagedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $stagedTunnel).Hash
            if ($sourceHash -ne $stagedHash) { throw 'A stale staged tunnel source exists in the Docker context. Remove the generated distribution/product-tunnel copy and rerun.' }
        } else {
            New-Item -ItemType Directory -Force -Path $stageDirectory | Out-Null
            Copy-Item -LiteralPath $checkoutTunnel -Destination $stagedTunnel
            $stagedByThisRun = $true
        }
    } elseif (Test-Path -LiteralPath $stagedTunnel -PathType Leaf) {
        # A downloaded ZIP already contains the canonical source at its build-context path.
        $stagedTunnel = (Resolve-Path -LiteralPath $stagedTunnel).Path
    } else {
        throw 'Canonical product tunnel source is missing from this Windows kit.'
    }
    if ($stagedByThisRun) {
        try {
            Run-Docker -DockerArgs @('compose', '-p', 'habab-home-windows', '-f', 'compose.windows.yaml', 'build', 'homeassistant')
        } finally {
            Remove-Item -LiteralPath $stagedTunnel -Force -ErrorAction SilentlyContinue
            if ((Test-Path -LiteralPath $stageDirectory) -and -not (Get-ChildItem -LiteralPath $stageDirectory -Force)) {
                Remove-Item -LiteralPath $stageDirectory -Force
            }
        }
    } else {
        Run-Docker -DockerArgs @('compose', '-p', 'habab-home-windows', '-f', 'compose.windows.yaml', 'build', 'homeassistant')
    }
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
        if (-not $callbackReady) { throw 'The Xiaomi callback address is not ready. Rerun with the ordinary launcher after resolving the Windows hosts-file prompt or conflict.' }
    }
    $startArguments = @('compose', '--env-file', '.env.opencode-windows', '-p', 'habab-home-windows', '-f', 'compose.windows.yaml', 'up', '-d')
    # Core discovers custom integrations at startup. The image may be unchanged
    # when switching an already-running ordinary installation to Xiaomi mode.
    if ($XiaomiHome) { $startArguments += '--force-recreate' }
    Run-Docker -DockerArgs ($startArguments + @('homeassistant'))
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
