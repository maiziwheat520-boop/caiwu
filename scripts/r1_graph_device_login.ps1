<#
One-time Microsoft personal-account device-code login for staging only.

The access token is written only to the approved external credentials file and
is never printed, returned, or committed. No refresh token is persisted.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')]
    [string]$ClientId,
    [string]$CredentialFile = 'G:\我的云端硬盘\凭据\home-infra-credentials.md',
    [int]$TimeoutSeconds = 900
)

$ErrorActionPreference = 'Stop'
$credentialRoot = [System.IO.Path]::GetFullPath('G:\我的云端硬盘\凭据')
$credentialPath = [System.IO.Path]::GetFullPath($CredentialFile)
$rootPrefix = $credentialRoot.TrimEnd('\') + '\'
if (-not $credentialPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'CredentialFile must remain under G:\我的云端硬盘\凭据'
}
if (-not (Test-Path -LiteralPath $credentialPath -PathType Leaf)) {
    throw "Credential file not found: $credentialPath"
}
if ($TimeoutSeconds -lt 60 -or $TimeoutSeconds -gt 900) {
    throw 'TimeoutSeconds must be between 60 and 900'
}

$scope = 'https://graph.microsoft.com/Mail.Read offline_access openid profile User.Read'
$device = Invoke-RestMethod -Method Post `
    -Uri 'https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode' `
    -ContentType 'application/x-www-form-urlencoded' `
    -Body @{ client_id = $ClientId; scope = $scope }
if (-not $device.device_code -or -not $device.user_code -or -not $device.verification_uri) {
    throw 'Microsoft device authorization response is incomplete'
}

Write-Output 'Open the verification URL below and enter the one-time code:'
Write-Output $device.verification_uri
Write-Output "Code: $($device.user_code)"
Write-Output 'Sign in as redeatt@outlook.com and consent only to Mail.Read.'

$interval = [Math]::Max([int]$device.interval, 5)
$deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
$tokenResponse = $null
while ([DateTimeOffset]::UtcNow -lt $deadline) {
    Start-Sleep -Seconds $interval
    try {
        $tokenResponse = Invoke-RestMethod -Method Post `
            -Uri 'https://login.microsoftonline.com/consumers/oauth2/v2.0/token' `
            -ContentType 'application/x-www-form-urlencoded' `
            -Body @{
                grant_type = 'urn:ietf:params:oauth:grant-type:device_code'
                client_id = $ClientId
                device_code = $device.device_code
            }
        break
    } catch {
        $detail = $_.ErrorDetails.Message | ConvertFrom-Json -ErrorAction SilentlyContinue
        $errorCode = if ($detail.error) { [string]$detail.error } else { '' }
        if ($errorCode -eq 'authorization_pending') { continue }
        if ($errorCode -eq 'slow_down') { $interval += 5; continue }
        if ($errorCode -eq 'authorization_declined' -or $errorCode -eq 'expired_token') {
            throw "Microsoft device authorization failed: $errorCode"
        }
        throw 'Microsoft device token request failed'
    }
}
if ($null -eq $tokenResponse -or [string]::IsNullOrWhiteSpace([string]$tokenResponse.access_token)) {
    throw 'Timed out waiting for Microsoft device authorization'
}

$token = [string]$tokenResponse.access_token
if ($token.Length -gt 8192) { throw 'Access token exceeds the staging limit' }
$lines = [System.IO.File]::ReadAllLines($credentialPath)
$output = [System.Collections.Generic.List[string]]::new()
$replaced = $false
foreach ($line in $lines) {
    if ($line -match '^LEDGERBRIDGE_STAGING_ACCESS_TOKEN=') {
        if (-not $replaced) {
            $output.Add("LEDGERBRIDGE_STAGING_ACCESS_TOKEN=$token")
            $replaced = $true
        }
    } else {
        $output.Add($line)
    }
}
if (-not $replaced) { $output.Add("LEDGERBRIDGE_STAGING_ACCESS_TOKEN=$token") }
[System.IO.File]::WriteAllLines($credentialPath, $output, [System.Text.UTF8Encoding]::new($false))
Write-Output 'Access token stored in the external credentials file; value was not displayed.'
