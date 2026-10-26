param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Enable', 'Disable', 'Status')]
    [string]$Action,
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'home.json')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$stateDirectory = Join-Path $env:LOCALAPPDATA 'HababHome\ProductTunnel'
$statePath = Join-Path $stateDirectory 'state.json'
$stopPath = "$statePath.stop"
$connectPath = Join-Path $PSScriptRoot 'connect.ps1'

function Get-ConnectProcess([int]$ProcessId) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($process -and $process.CommandLine -and $process.CommandLine.Contains($connectPath)) {
        return $process
    }
    return $null
}

function Get-TunnelStatus {
    if (-not (Test-Path -LiteralPath $statePath)) {
        return @{ status = 'disconnected'; message = 'Удалённый доступ отключён.'; process_id = 0 }
    }
    try {
        $state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    } catch {
        return @{ status = 'connecting'; message = 'Считываю состояние туннеля.'; process_id = 0 }
    }
    if ($state.status -eq 'error') { return $state }
    $processId = 0
    if ($state.process_id -and [int]::TryParse([string]$state.process_id, [ref]$processId)) {
        if (Get-ConnectProcess $processId) { return $state }
    }
    return @{ status = 'disconnected'; message = 'Процесс туннеля остановлен.'; process_id = 0 }
}

function Write-Status($State) {
    $State | ConvertTo-Json -Compress
}

function Save-TunnelStatus($State) {
    $temporaryPath = "$statePath.tmp"
    $json = $State | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($temporaryPath, $json, [Text.Encoding]::UTF8)
    Move-Item -Force -LiteralPath $temporaryPath -Destination $statePath
}

New-Item -ItemType Directory -Force -Path $stateDirectory | Out-Null
$status = Get-TunnelStatus

switch ($Action) {
    'Status' {
        Write-Status $status
    }
    'Enable' {
        if ($status.status -in @('connecting', 'running', 'retrying')) {
            Write-Status $status
            break
        }
        Remove-Item -Force -LiteralPath $stopPath -ErrorAction SilentlyContinue
        $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
        $arguments = @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
            '-File', $connectPath, '-ConfigPath', $ConfigPath, '-StatePath', $statePath
        ) | ForEach-Object {
            $argument = [string]$_
            '"' + $argument.Replace('"', '\"') + '"'
        }
        $process = Start-Process -FilePath $powershell -ArgumentList ($arguments -join ' ') -WindowStyle Hidden -PassThru
        for ($attempt = 0; $attempt -lt 10; $attempt++) {
            Start-Sleep -Milliseconds 200
            $started = Get-TunnelStatus
            if ($started.process_id -eq $process.Id) { break }
        }
        if ($started.process_id -eq $process.Id) {
            Write-Status $started
        } else {
            $failure = @{ status = 'error'; message = 'Не удалось запустить клиент туннеля.'; updated_at = [DateTimeOffset]::UtcNow.ToString('o'); process_id = 0 }
            Save-TunnelStatus $failure
            Write-Status $failure
        }
    }
    'Disable' {
        $processId = 0
        if ($status.process_id -and [int]::TryParse([string]$status.process_id, [ref]$processId) -and (Get-ConnectProcess $processId)) {
            New-Item -ItemType File -Force -Path $stopPath | Out-Null
            for ($attempt = 0; $attempt -lt 15 -and (Get-ConnectProcess $processId); $attempt++) {
                Start-Sleep -Seconds 1
            }
            if (Get-ConnectProcess $processId) {
                & taskkill.exe /PID $processId /T /F | Out-Null
            }
        }
        Remove-Item -Force -LiteralPath $stopPath -ErrorAction SilentlyContinue
        $disconnected = @{ status = 'disconnected'; message = 'Удалённый доступ отключён.'; updated_at = [DateTimeOffset]::UtcNow.ToString('o'); process_id = 0 }
        Save-TunnelStatus $disconnected
        Write-Status $disconnected
    }
}
