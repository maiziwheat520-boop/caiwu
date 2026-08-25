<#
Obtain a staging IMAP OAuth2 token through the public Thunderbird client.

This is a staging convenience only. It uses no client secret and stores only
the short-lived access token in the external credentials file; refresh tokens
are deliberately discarded. Production must register and audit its own app.
#>
[CmdletBinding()]
param(
    [string]$CredentialFile = 'G:\我的云端硬盘\凭据\home-infra-credentials.md',
    [string]$AccountHint = 'redeatt@163.com'
)

$ErrorActionPreference = 'Stop'
$clientId = '9e5f94bc-e8a4-4e73-b8be-63364c29d753'
# Thunderbird's public Microsoft client is registered for the common endpoint.
# The consumers-only endpoint can reject the same personal account with
# AADSTS50020 ("personal Microsoft accounts are not supported").
$tenant = 'common'
$redirectUri = 'https://localhost'
$scope = 'https://outlook.office.com/IMAP.AccessAsUser.All offline_access openid profile'
$key = 'LEDGERBRIDGE_STAGING_IMAP_ACCESS_TOKEN'

function ConvertTo-Base64Url([byte[]]$Bytes) {
    [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-RandomToken([int]$Length) {
    $bytes = [byte[]]::new($Length)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    ConvertTo-Base64Url $bytes
}

function Read-HiddenText([string]$Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $ptr = [IntPtr]::Zero
    try {
        $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    } finally {
        if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
        $secure.Dispose()
    }
}

$root = [System.IO.Path]::GetFullPath('G:\我的云端硬盘\凭据')
$path = [System.IO.Path]::GetFullPath($CredentialFile)
$prefix = $root.TrimEnd('\') + '\'
if (-not $path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'CredentialFile must remain under G:\我的云端硬盘\凭据'
}
if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "Credential file not found: $path"
}
if (Select-String -LiteralPath $path -Pattern "^$key=" -Quiet) {
    throw "$key already exists; remove the old staging token before starting a new login"
}

$verifier = New-RandomToken 64
$state = New-RandomToken 32
$hash = [Security.Cryptography.SHA256]::HashData([Text.Encoding]::ASCII.GetBytes($verifier))
$challenge = ConvertTo-Base64Url $hash
$query = [System.Web.HttpUtility]::ParseQueryString('')
$query['client_id'] = $clientId
$query['response_type'] = 'code'
$query['redirect_uri'] = $redirectUri
$query['response_mode'] = 'query'
$query['scope'] = $scope
$query['login_hint'] = $AccountHint
$query['prompt'] = 'select_account'
$query['state'] = $state
$query['code_challenge'] = $challenge
$query['code_challenge_method'] = 'S256'
$authorizeUrl = "https://login.microsoftonline.com/$tenant/oauth2/v2.0/authorize?$query"

Write-Output "Open this URL in a browser and sign in as $AccountHint (or the Microsoft alias that owns the Outlook mailbox):"
Write-Output $authorizeUrl
Write-Output 'After consent, the browser may show a localhost error. Copy the full address bar URL and paste it into the hidden prompt below.'
$callback = Read-HiddenText 'Paste callback URL (hidden)'
$callbackUri = [Uri]$callback
$parameters = [System.Web.HttpUtility]::ParseQueryString($callbackUri.Query)
if ($parameters['state'] -ne $state) { throw 'OAuth state validation failed' }
if (-not $parameters['code']) { throw 'OAuth callback did not contain a code' }

$tokenResponse = Invoke-RestMethod -Method Post `
    -Uri "https://login.microsoftonline.com/$tenant/oauth2/v2.0/token" `
    -ContentType 'application/x-www-form-urlencoded' `
    -Body @{
        client_id = $clientId
        grant_type = 'authorization_code'
        code = $parameters['code']
        redirect_uri = $redirectUri
        code_verifier = $verifier
        scope = $scope
    }
if (-not $tokenResponse.access_token) { throw 'OAuth token response did not contain an access token' }
$lines = [System.Collections.Generic.List[string]]::new()
foreach ($line in [System.IO.File]::ReadAllLines($path)) { $lines.Add($line) }
$lines.Add("$key=$($tokenResponse.access_token)")
[System.IO.File]::WriteAllLines($path, $lines, [System.Text.UTF8Encoding]::new($false))
Write-Output 'IMAP OAuth access token stored in the external credentials file (value not displayed).'
