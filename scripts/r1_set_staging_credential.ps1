<#
Write one staging credential using a hidden local prompt.

The secret is never supplied as a command-line argument and is never printed.
#>
[CmdletBinding()]
param(
    [string]$CredentialFile = 'G:\我的云端硬盘\凭据\home-infra-credentials.md',
    [ValidateSet('LEDGERBRIDGE_STAGING_IMAP_AUTHORIZATION_CODE', 'LEDGERBRIDGE_STAGING_IMAP_APP_PASSWORD', 'LEDGERBRIDGE_STAGING_IMAP_ACCESS_TOKEN')]
    [string]$Key = 'LEDGERBRIDGE_STAGING_IMAP_AUTHORIZATION_CODE'
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath('G:\我的云端硬盘\凭据')
$path = [System.IO.Path]::GetFullPath($CredentialFile)
$prefix = $root.TrimEnd('\') + '\'
if (-not $path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'CredentialFile must remain under G:\我的云端硬盘\凭据'
}
if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "Credential file not found: $path"
}

$secure = Read-Host "Enter $Key (hidden; do not paste it into chat)" -AsSecureString
$ptr = [IntPtr]::Zero
try {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    if ([string]::IsNullOrWhiteSpace($value) -or $value.Length -gt 8192) {
        throw 'Credential value is empty or too long'
    }
    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($line in [System.IO.File]::ReadAllLines($path)) {
        if ($line -notmatch "^$Key=") { $lines.Add($line) }
    }
    $lines.Add("$Key=$value")
    [System.IO.File]::WriteAllLines($path, $lines, [System.Text.UTF8Encoding]::new($false))
} finally {
    if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    $secure.Dispose()
    $value = $null
}
Write-Output "Saved $Key to the external credentials file (value not displayed)."
