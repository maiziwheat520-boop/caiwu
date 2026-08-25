<#
One-command, non-production Outlook staging replay.

The access token remains in the external credentials file; this wrapper only
sets process-local routing values and never writes or prints the token.
#>
[CmdletBinding()]
param(
    [string]$CredentialFile = 'G:\我的云端硬盘\凭据\home-infra-credentials.md',
    [string]$Mailbox = 'redeatt@outlook.com',
    [string]$EntityRef = '10000000-0000-4000-8000-000000000001',
    [string]$GatewayUrl = 'http://127.0.0.1:8653/v1/intake',
    [ValidateSet('imap', 'graph')]
    [string]$Transport = 'imap',
    [ValidateSet('password', 'xoauth2')]
    [string]$ImapAuth = 'xoauth2'
)

$ErrorActionPreference = 'Stop'

if (-not [System.IO.Path]::IsPathRooted($CredentialFile)) {
    throw 'CredentialFile must be an absolute path outside the repository'
}
if (-not (Test-Path -LiteralPath $CredentialFile -PathType Leaf)) {
    throw "Credential file not found: $CredentialFile"
}

$env:LEDGERBRIDGE_STAGING_NETWORK = '1'
$env:LEDGERBRIDGE_STAGING_CREDENTIAL_FILE = $CredentialFile
$env:LEDGERBRIDGE_STAGING_MAILBOX = $Mailbox
$env:LEDGERBRIDGE_STAGING_ENTITY_REF = $EntityRef
$env:LEDGERBRIDGE_STAGING_GATEWAY_URL = $GatewayUrl
$env:LEDGERBRIDGE_STAGING_IMAP_AUTH = $ImapAuth

Write-Output 'Starting bounded Outlook staging replay (no production writes).'
Write-Output "Mailbox: $Mailbox"
Write-Output "Credential source: external file (value is not displayed)"
$gatewayProcess = $null
$gatewayReady = $false
$tcp = [System.Net.Sockets.TcpClient]::new()
try {
    $tcp.Connect('127.0.0.1', 8653)
    $gatewayReady = $true
} catch {
    $gatewayReady = $false
} finally {
    $tcp.Dispose()
}

try {
    if (-not $gatewayReady) {
        $gatewayProcess = Start-Process -FilePath 'uv' -ArgumentList @(
            'run', '--frozen', '--extra', 'dev', 'python',
            'scripts/r1_synthetic_data_gateway.py'
        ) -WorkingDirectory (Get-Location) -WindowStyle Hidden -PassThru
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            Start-Sleep -Milliseconds 200
            $probe = [System.Net.Sockets.TcpClient]::new()
            try {
                $probe.Connect('127.0.0.1', 8653)
                $gatewayReady = $true
                break
            } catch {
                if ($gatewayProcess.HasExited) {
                    throw 'loopback staging gateway exited during startup'
                }
            } finally {
                $probe.Dispose()
            }
        }
    }
    if (-not $gatewayReady) {
        throw 'loopback staging gateway did not become ready'
    }
    $replayScript = if ($Transport -eq 'imap') {
        'scripts/r1_staging_imap_replay.py'
    } else {
        'scripts/r1_staging_graph_replay.py'
    }
    uv run --frozen --extra dev python $replayScript
    if ($LASTEXITCODE -ne 0) {
        throw "staging replay failed with exit code $LASTEXITCODE"
    }
} finally {
    if ($null -ne $gatewayProcess -and -not $gatewayProcess.HasExited) {
        Stop-Process -Id $gatewayProcess.Id -Force
    }
}
