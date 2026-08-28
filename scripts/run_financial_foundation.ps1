[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InputDirectory,

    [Parameter(Mandatory = $true)]
    [string]$BocWorkbook,

    [Parameter(Mandatory = $true)]
    [string]$Output,

    [int]$Year = 2026,
    [ValidateRange(1, 12)]
    [int]$Month = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$inputRoot = (Resolve-Path -LiteralPath $InputDirectory).Path
$bocPath = (Resolve-Path -LiteralPath $BocWorkbook).Path
$wechatFiles = @(Get-ChildItem -LiteralPath $inputRoot -File -Filter "wechat_*.xlsx" | Sort-Object FullName)
$alipayFiles = @(Get-ChildItem -LiteralPath $inputRoot -File -Filter "alipay_*.csv" | Sort-Object FullName)

if ($wechatFiles.Count -eq 0) {
    throw "No WeChat exports were found in the input directory."
}
if ($alipayFiles.Count -eq 0) {
    throw "No Alipay exports were found in the input directory."
}

$bundledRoot = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node"
$bundledNode = Join-Path $bundledRoot "bin\node.exe"
if (Test-Path -LiteralPath $bundledNode) {
    $nodeExecutable = $bundledNode
    $env:NODE_PATH = Join-Path $bundledRoot "node_modules"
} else {
    $nodeExecutable = (Get-Command node -ErrorAction Stop).Source
}

$builder = Join-Path $PSScriptRoot "build_financial_foundation_workbook.mjs"
$arguments = @(
    $builder,
    "--boc-workbook", $bocPath,
    "--year", [string]$Year,
    "--month", [string]$Month,
    "--output", $Output
)
foreach ($file in $wechatFiles) {
    $arguments += @("--wechat", $file.FullName)
}
foreach ($file in $alipayFiles) {
    $arguments += @("--alipay", $file.FullName)
}

& $nodeExecutable @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Financial foundation build failed with exit code $LASTEXITCODE."
}

$resolvedOutput = (Resolve-Path -LiteralPath $Output).Path
$manifestPath = [System.IO.Path]::ChangeExtension($resolvedOutput, ".manifest.json")
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.checks.duplicateRecordIds -ne 0 -or $manifest.checks.missingEvidence -ne 0 -or $manifest.checks.unbalancedInternalLinks -ne 0) {
    throw "Financial foundation checks did not pass. Review the manifest before using the workbook."
}

[pscustomobject]@{
    Workbook = $resolvedOutput
    Period = $manifest.period
    Records = $manifest.counts.records
    Accounts = $manifest.counts.accounts
    PendingEvidence = $manifest.counts.pending
    Checks = "passed"
}
