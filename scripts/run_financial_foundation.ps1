[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InputDirectory,

    [Parameter(Mandatory = $true)]
    [string]$BocWorkbook,

    [Parameter(Mandatory = $true)]
    [string]$Output,

    [ValidateRange(2026, 2100)]
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
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.checks.duplicateRecordIds -ne 0 -or $manifest.checks.missingEvidence -ne 0 -or $manifest.checks.unbalancedInternalLinks -ne 0) {
    throw "Financial foundation checks did not pass. Review the manifest before using the workbook."
}

Write-Output "AUTOMATION_MATERIALS_NEEDED"
if (@($manifest.materialsNeeded).Count -eq 0) {
    Write-Output "- No materials are required for this period."
}
else {
    foreach ($item in @($manifest.materialsNeeded)) {
        $amount = [math]::Round(([long]$item.expenseMinor / 100), 2)
        $materialLine = "- {0}: {1}; period {2}; {3} transactions; expense CNY {4:N2}." -f $item.item, $item.action, $item.period, $item.transactionCount, $amount
        Write-Output $materialLine
    }
}

[pscustomobject]@{
    Workbook = $resolvedOutput
    Period = $manifest.period
    Records = $manifest.counts.records
    Accounts = $manifest.counts.accounts
    PendingEvidence = $manifest.counts.pending
    MaterialsNeeded = @($manifest.materialsNeeded)
    Checks = "passed"
}
