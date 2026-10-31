param([string]$OutputDirectory = '')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$distribution = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path (Split-Path -Parent $distribution) 'public/downloads'
}
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$metadata = Get-Content -LiteralPath (Join-Path $OutputDirectory 'windows-package.json') -Raw | ConvertFrom-Json
if ($metadata.source_dirty -ne 'false' -or $metadata.source_revision -notmatch '^[a-f0-9]{40,64}$') {
    throw 'The Windows EXE must be built from a known, clean source revision.'
}
if ($metadata.filename -notmatch '^habab-home-windows-[a-f0-9]{12}\.zip$') {
    throw 'Invalid Windows payload filename.'
}
$archive = Join-Path $OutputDirectory $metadata.filename
if ((Get-Item -LiteralPath $archive).Length -ne $metadata.bytes -or
    (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant() -ne $metadata.sha256) {
    throw 'Windows payload checksum/size does not match windows-package.json.'
}
if ($metadata.version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid product version.' }

# windows-latest includes Inno Setup 6. Use its compiler directly, no setup action.
$compiler = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6/ISCC.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = (Get-Command ISCC.exe -ErrorAction Stop).Source
}
$compilerVersion = (Get-Item -LiteralPath $compiler).VersionInfo.ProductVersion
$stage = Join-Path ([IO.Path]::GetTempPath()) ('habab-windows-' + [Guid]::NewGuid().ToString('N'))
$smokeBase = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$smokeRoot = Join-Path $smokeBase ('Habab-installer-smoke-' + [Guid]::NewGuid().ToString('N'))
$smokeInstall = Join-Path $smokeRoot 'installed'
try {
    Expand-Archive -LiteralPath $archive -DestinationPath $stage
    $embedded = Get-Content -LiteralPath (Join-Path $stage 'release.json') -Raw | ConvertFrom-Json
    if ($embedded.source_revision -ne $metadata.source_revision -or $embedded.source_dirty -ne 'false') {
        throw 'Windows payload source provenance does not match its metadata.'
    }
    & $compiler '/Qp' "/DPayloadDir=$stage" "/DOutputDir=$OutputDirectory" "/DAppVersion=$($metadata.version)" (Join-Path $PSScriptRoot 'habab-home.iss')
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed (exit $LASTEXITCODE)." }

    # Exercise the actual EXE without running its postinstall launcher. Do not
    # replace an existing per-user installation when building outside fresh CI.
    foreach ($key in @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{29724728-3CC7-434A-8290-D9B60E814B71}_is1',
        'HKCU:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\{29724728-3CC7-434A-8290-D9B60E814B71}_is1'
    )) {
        if (Test-Path -LiteralPath $key) { throw 'Installer smoke requires a user without an existing Habab Home installation.' }
    }
    New-Item -ItemType Directory -Path $smokeRoot | Out-Null
    $executable = Join-Path $OutputDirectory 'habab_home_assistant.exe'
    try {
        $installation = Start-Process -FilePath $executable -Wait -PassThru -ArgumentList @(
            '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/NOICONS', '/TASKS=',
            "/DIR=`"$smokeInstall`"", "/LOG=`"$(Join-Path $smokeRoot 'install.log')`""
        )
        if ($installation.ExitCode -ne 0) { throw "Installer smoke failed (exit $($installation.ExitCode))." }
        if (-not (Test-Path -LiteralPath (Join-Path $smokeInstall 'start-windows.bat'))) {
            throw 'Installer smoke did not install the Windows launcher.'
        }
        $installedNames = @(Get-ChildItem -LiteralPath $smokeInstall -Recurse -File | ForEach-Object {
            [IO.Path]::GetRelativePath($smokeInstall, $_.FullName).Replace('\', '/')
        } | Where-Object { $_ -notmatch '^unins[0-9]+\.(exe|dat|msg)$' } | Sort-Object)
        if (Compare-Object -ReferenceObject @($metadata.files | Sort-Object) -DifferenceObject $installedNames) {
            throw 'Installed file inventory does not match the Windows payload.'
        }
        foreach ($name in $metadata.files) {
            $expected = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $stage $name)).Hash
            $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $smokeInstall $name)).Hash
            if ($actual -ne $expected) { throw "Installed payload checksum mismatch: $name" }
        }
    } finally {
        $uninstaller = Join-Path $smokeInstall 'unins000.exe'
        if (Test-Path -LiteralPath $uninstaller) {
            $removal = Start-Process -FilePath $uninstaller -Wait -PassThru -ArgumentList @(
                '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                "/LOG=`"$(Join-Path $smokeRoot 'uninstall.log')`""
            )
            if ($removal.ExitCode -ne 0) { throw "Installer smoke uninstall failed (exit $($removal.ExitCode))." }
        }
    }
    if (Test-Path -LiteralPath (Join-Path $smokeInstall 'start-windows.bat')) {
        throw 'Installer smoke uninstall left the installed launcher behind.'
    }
    Write-Host 'Windows installer smoke passed: silent install, complete payload checksums, silent uninstall; no runtime launch.'
    & python (Join-Path $distribution 'package-windows.py') --output-dir $OutputDirectory --record-installer --compiler-version $compilerVersion
    if ($LASTEXITCODE -ne 0) { throw 'Failed to record Windows installer checksum/provenance.' }
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    if (Test-Path -LiteralPath $smokeRoot) { Remove-Item -LiteralPath $smokeRoot -Recurse -Force }
}
