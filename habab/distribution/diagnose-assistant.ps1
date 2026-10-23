$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Set-Location $PSScriptRoot

try {
    $python = Join-Path $PSScriptRoot 'diagnose-assistant.py'
    $environmentFile = Join-Path $PSScriptRoot '.env.opencode-windows'
    $composeFile = Join-Path $PSScriptRoot 'compose.windows.yaml'
    foreach ($requiredFile in @($python, $environmentFile, $composeFile)) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw 'Diagnostic files are incomplete. Reinstall the Habab Home package and retry.'
        }
    }

    $dockerCommand = Get-Command 'docker.exe' -ErrorAction SilentlyContinue
    $docker = if ($dockerCommand) { $dockerCommand.Source } else { $null }
    if (-not $docker) {
        $candidate = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $docker = $candidate }
    }
    if (-not $docker) { throw 'Docker Desktop CLI was not found. Start or repair Docker Desktop, then retry.' }

    $source = [IO.File]::ReadAllText($python, [Text.Encoding]::UTF8)
    $previousPreference = $ErrorActionPreference
    try {
        # Native stderr handling differs in Windows PowerShell 5.1. Keep it
        # bounded and suppress raw Docker output from the user-facing report.
        $ErrorActionPreference = 'Continue'
        $rawOutput = $source | & $docker compose --env-file .env.opencode-windows -p habab-home-windows -f compose.windows.yaml exec -T homeassistant python3 - 2>&1
        $dockerExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($dockerExitCode -ne 0) {
        throw 'Home Assistant container is not running or the local diagnostic could not complete. Start Home Assistant and retry.'
    }

    $rawJson = (@($rawOutput) -join "`n").Trim()
    try { $diagnostic = ConvertFrom-Json -InputObject $rawJson }
    catch { throw 'The container returned an unreadable diagnostic result.' }

    $allowedStatuses = @('ok', 'missing_db', 'unsupported_schema', 'unreadable_db', 'diagnostic_failed')
    if ($diagnostic.status -notin $allowedStatuses) { throw 'The container returned an unsupported diagnostic result.' }
    $allowedNames = @('APIError', 'AuthenticationError', 'ContextOverflowError', 'InvalidModelError', 'MessageAbortedError', 'ModelNotFoundError', 'ProviderAuthError', 'ProviderError', 'RateLimitError', 'UnknownError')
    $errors = @()
    foreach ($item in @($diagnostic.assistant_errors | Select-Object -First 3)) {
        $name = if ($item.name -in $allowedNames) { $item.name } else { 'other' }
        $httpStatus = $null
        if ($item.http_status -is [int] -or $item.http_status -is [long]) {
            if ($item.http_status -ge 100 -and $item.http_status -le 599) { $httpStatus = [int]$item.http_status }
        }
        $errors += [ordered]@{
            name = $name
            http_status = $httpStatus
            missing_reasoning_content = ($item.missing_reasoning_content -is [bool] -and $item.missing_reasoning_content)
            mentions_reasoning_content = ($item.mentions_reasoning_content -is [bool] -and $item.mentions_reasoning_content)
            invalid_model = ($item.invalid_model -is [bool] -and $item.invalid_model)
            quota = ($item.quota -is [bool] -and $item.quota)
            mentions_tool_calls = ($item.mentions_tool_calls -is [bool] -and $item.mentions_tool_calls)
            mentions_context_limit = ($item.mentions_context_limit -is [bool] -and $item.mentions_context_limit)
            mentions_timeout = ($item.mentions_timeout -is [bool] -and $item.mentions_timeout)
        }
    }
    [ordered]@{
        db_present = ($diagnostic.db_present -is [bool] -and $diagnostic.db_present)
        read_only = ($diagnostic.read_only -is [bool] -and $diagnostic.read_only)
        status = $diagnostic.status
        assistant_errors = @($errors)
    } | ConvertTo-Json -Depth 5 -Compress | Write-Output
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
