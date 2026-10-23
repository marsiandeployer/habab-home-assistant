$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$hostName = 'homeassistant.local'
$hostsPath = Join-Path $env:WINDIR 'System32\drivers\etc\hosts'

function Get-CallbackMappings {
    $mappings = @()
    foreach ($line in [IO.File]::ReadAllLines($hostsPath)) {
        $content = ($line -split '#', 2)[0].Trim()
        if (-not $content) { continue }
        $parts = $content -split '\s+'
        if ($parts.Length -lt 2) { continue }
        foreach ($alias in $parts[1..($parts.Length - 1)]) {
            if ([string]::Equals($alias, $hostName, [StringComparison]::OrdinalIgnoreCase)) {
                $mappings += $parts[0]
            }
        }
    }
    return $mappings
}

function Test-CallbackMapping {
    $mappings = @(Get-CallbackMappings)
    if ($mappings.Count -eq 0) { return $false }
    if (@($mappings | Where-Object { $_ -ne '127.0.0.1' }).Count -gt 0) {
        throw 'CONFLICT: homeassistant.local already has a different hosts-file mapping. It was left unchanged.'
    }
    return $true
}

try {
    if (-not (Test-Path -LiteralPath $hostsPath)) { throw 'The Windows hosts file was not found.' }
    if (Test-CallbackMapping) {
        Write-Host 'Xiaomi callback alias already points to 127.0.0.1.'
        exit 0
    }

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host 'Windows will ask for administrator approval to add one loopback alias to the hosts file.'
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"'
        try {
            $elevated = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru -ArgumentList $arguments
        } catch {
            $nativeErrorCode = if ($_.Exception -is [System.ComponentModel.Win32Exception]) { $_.Exception.NativeErrorCode } else { 0 }
            if ($nativeErrorCode -eq 1223 -or $_.Exception.Message -match '(?i)(cancelled|canceled|declined by the user)') {
                Write-Host 'UAC permission was declined. No hosts-file changes were made.' -ForegroundColor Yellow
                exit 3
            }
            throw
        }
        if ($elevated.ExitCode -ne 0) { throw 'Windows did not add the Xiaomi callback alias.' }
        if (-not (Test-CallbackMapping)) { throw 'The Xiaomi callback alias was not found after the Windows prompt.' }
        exit 0
    }

    # Recheck after elevation and append only our one mapping. Existing lines are never rewritten or removed.
    if (-not (Test-CallbackMapping)) {
        $bytes = [IO.File]::ReadAllBytes($hostsPath)
        $encoding = [Text.Encoding]::ASCII
        $hasFinalNewline = $bytes.Length -gt 0 -and $bytes[$bytes.Length - 1] -in @(10, 13)
        if ($bytes.Length -ge 2 -and $bytes[0] -eq 255 -and $bytes[1] -eq 254) {
            $encoding = [Text.Encoding]::Unicode
            $hasFinalNewline = $bytes.Length -ge 4 -and $bytes[$bytes.Length - 2] -eq 10 -and $bytes[$bytes.Length - 1] -eq 0
        } elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 254 -and $bytes[1] -eq 255) {
            $encoding = [Text.Encoding]::BigEndianUnicode
            $hasFinalNewline = $bytes.Length -ge 4 -and $bytes[$bytes.Length - 2] -eq 0 -and $bytes[$bytes.Length - 1] -eq 10
        }
        $prefix = if ($bytes.Length -gt 0 -and -not $hasFinalNewline) { [Environment]::NewLine } else { '' }
        $entry = $prefix + "127.0.0.1 homeassistant.local # Habab Home Xiaomi OAuth callback" + [Environment]::NewLine
        [IO.File]::AppendAllText($hostsPath, $entry, $encoding)
    }
    if (-not (Test-CallbackMapping)) { throw 'The Xiaomi callback alias could not be verified.' }
    Write-Host 'Added the Xiaomi callback alias to the Windows hosts file.'
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    if ($_.Exception.Message -like 'CONFLICT:*') { exit 2 }
    exit 1
}
