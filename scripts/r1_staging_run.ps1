<#
One-command, non-production IMAP staging replay.

The access token remains in the external credentials file; this wrapper only
sets process-local routing values and never writes or prints the token.
#>
[CmdletBinding()]
param(
    [string]$CredentialFile = 'G:\我的云端硬盘\凭据\home-infra-credentials.md',
    [string]$Mailbox = 'redeatt@163.com',
    [string]$EntityRef = '10000000-0000-4000-8000-000000000001',
    [string]$GatewayUrl = 'http://127.0.0.1:8653/v1/intake',
    [ValidateSet('imap', 'graph', 'mbox')]
    [string]$Transport = 'imap',
    [ValidateSet('password', 'xoauth2')]
    [string]$ImapAuth = 'password',
    [string]$ImapHost = 'imap.163.com',
    [int]$ImapPort = 993,
    [string]$ImapCredentialKey = 'LEDGERBRIDGE_STAGING_IMAP_AUTHORIZATION_CODE',
    [string]$MboxPath = ''
)

$ErrorActionPreference = 'Stop'

if (-not [System.IO.Path]::IsPathRooted($CredentialFile)) {
    throw 'CredentialFile must be an absolute path outside the repository'
}
if ($ImapPort -lt 1 -or $ImapPort -gt 65535) {
    throw 'ImapPort must be between 1 and 65535'
}
if ($Transport -ne 'mbox' -and -not (Test-Path -LiteralPath $CredentialFile -PathType Leaf)) {
    throw "Credential file not found: $CredentialFile"
}
if ($Transport -eq 'mbox' -and (-not $MboxPath -or -not (Test-Path -LiteralPath $MboxPath -PathType Leaf))) {
    throw 'MboxPath must point to a Thunderbird mbox file when Transport=mbox'
}

$env:LEDGERBRIDGE_STAGING_NETWORK = '1'
$env:LEDGERBRIDGE_STAGING_CREDENTIAL_FILE = $CredentialFile
$env:LEDGERBRIDGE_STAGING_MAILBOX = $Mailbox
$env:LEDGERBRIDGE_STAGING_ENTITY_REF = $EntityRef
$env:LEDGERBRIDGE_STAGING_GATEWAY_URL = $GatewayUrl
$env:LEDGERBRIDGE_STAGING_IMAP_AUTH = $ImapAuth
$env:LEDGERBRIDGE_STAGING_IMAP_HOST = $ImapHost
$env:LEDGERBRIDGE_STAGING_IMAP_PORT = [string]$ImapPort
$env:LEDGERBRIDGE_STAGING_IMAP_CREDENTIAL_KEY = $ImapCredentialKey

Write-Output 'Starting bounded IMAP staging replay (no production writes).'
Write-Output "Mailbox: $Mailbox"
Write-Output $(if ($Transport -eq 'mbox') { "Source: Thunderbird mbox ($MboxPath)" } else { "Credential source: external file (value is not displayed)" })
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
    if ($Transport -eq 'mbox') {
        uv run --frozen --extra dev python scripts/r1_staging_mbox_replay.py --mbox $MboxPath
    } elseif ($Transport -eq 'imap') {
        uv run --frozen --extra dev python scripts/r1_staging_imap_replay.py
    } else {
        uv run --frozen --extra dev python scripts/r1_staging_graph_replay.py
    }
    if ($LASTEXITCODE -ne 0) {
        throw "staging replay failed with exit code $LASTEXITCODE"
    }
} finally {
    if ($null -ne $gatewayProcess -and -not $gatewayProcess.HasExited) {
        Stop-Process -Id $gatewayProcess.Id -Force
    }
    if ($null -ne $gatewayProcess) {
        $listeners = Get-NetTCPConnection -LocalPort 8653 -State Listen -ErrorAction SilentlyContinue
        foreach ($listener in $listeners) {
            Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    }
}
