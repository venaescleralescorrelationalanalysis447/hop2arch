# Unit tests for the pure helpers inside hop-scan.ps1.
#
# The scanner is never executed here, and it must not be: it reads the registry,
# enumerates disks and writes files, none of which belongs in a test run. Instead
# the script is parsed, a whitelist of functions that touch nothing but their own
# arguments is lifted out of the syntax tree, and those are defined in this
# session and called with synthetic input.
#
# That covers the parts most likely to be quietly wrong - version-stripping,
# timestamp arithmetic, Valve's config format - and leaves the parts that talk to
# Windows for a real run on a real machine.
#
# Run it from anywhere:
#     powershell -NoProfile -ExecutionPolicy Bypass -File windows\tests\hop-scan.Tests.ps1
#
# Exit code 0 means every assertion held. Windows PowerShell 5.1 is the target,
# so nothing in this file may use pwsh 7 syntax either.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path (Split-Path -Parent $here) 'hop-scan.ps1'
if (-not (Test-Path -LiteralPath $source)) { throw "cannot find hop-scan.ps1 next to $here" }

$errors = $null
$tokens = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)
if ($errors -and $errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Output ("  parse error line {0}: {1}" -f $_.Extent.StartLineNumber, $_.Message) }
    throw "hop-scan.ps1 does not parse; nothing else here is meaningful"
}
$ast = [System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)

# Functions whose only inputs are their parameters. Adding one to this list is a
# claim that it touches no registry, no disk and no environment - check before
# you add, because everything here runs.
$pure = @(
    'Format-Bytes', 'Get-RelativePath', 'ConvertTo-HtmlText', 'ConvertTo-NullableString',
    'Expand-VdfString', 'ConvertFrom-Vdf', 'Get-GpuVendor', 'Get-PartitionKind',
    'ConvertTo-NormalKey', 'Get-Prop', 'ConvertFrom-ChromeTime', 'Test-SshKeyEncrypted',
    'Get-WindowsToIanaMap'
)

$loaded = @()
foreach ($fn in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    if ($pure -notcontains $fn.Name) { continue }
    . ([ScriptBlock]::Create($fn.Extent.Text))
    $loaded += $fn.Name
}
$missing = @($pure | Where-Object { $loaded -notcontains $_ })
if ($missing.Count -gt 0) { throw ("these functions were renamed or removed: {0}" -f ($missing -join ', ')) }

$script:Passed = 0
$script:Failed = 0
function Assert-Equal {
    param([string]$Label, $Actual, $Expected)
    $a = '<null>'; if ($null -ne $Actual) { $a = [string]$Actual }
    $e = '<null>'; if ($null -ne $Expected) { $e = [string]$Expected }
    if ($a -ceq $e) {
        $script:Passed++
    } else {
        $script:Failed++
        Write-Output ("  FAIL  {0}" -f $Label)
        Write-Output ("          got      '{0}'" -f $a)
        Write-Output ("          expected '{0}'" -f $e)
    }
}

# --- Format-Bytes ---------------------------------------------------------
# '{0:N1}' follows the current culture, so a Russian or German machine renders
# "1,0 KB". That is display text for the console summary and for warning prose,
# never a JSON value, so the separator is right rather than broken. Assert the
# unit choice and the rounding, and build the expectation in the same culture.
function Format-N1 { param($Value) return ('{0:N1}' -f [double]$Value) }
Assert-Equal 'Format-Bytes null'  (Format-Bytes $null)         'n/a'
Assert-Equal 'Format-Bytes junk'  (Format-Bytes 'abc')         'n/a'
Assert-Equal 'Format-Bytes 0'     (Format-Bytes 0)             ((Format-N1 0) + ' B')
Assert-Equal 'Format-Bytes 1023'  (Format-Bytes 1023)          ((Format-N1 1023) + ' B')
Assert-Equal 'Format-Bytes 1KB'   (Format-Bytes 1024)          ((Format-N1 1) + ' KB')
Assert-Equal 'Format-Bytes 1TB'   (Format-Bytes 1099511627776) ((Format-N1 1) + ' TB')

# The numbers that reach the hopfile must not carry a locale with them.
# memory_gb is built as [int][math]::Round(...); prove the serialiser agrees.
Assert-Equal 'json int is invariant'    (ConvertTo-Json -InputObject @{ memory_gb = [int][math]::Round((34359738368 / 1GB), 0) } -Compress) '{"memory_gb":32}'
Assert-Equal 'json double is invariant' (ConvertTo-Json -InputObject @{ x = [double]1.5 } -Compress) '{"x":1.5}'

# --- Get-GpuVendor --------------------------------------------------------
# hop plan picks the driver packages straight off this, so a wrong bucket is a
# machine that boots without graphics.
Assert-Equal 'gpu nvidia'    (Get-GpuVendor 'NVIDIA GeForce RTX 3060 Laptop GPU') 'nvidia'
Assert-Equal 'gpu amd'       (Get-GpuVendor 'AMD Radeon RX 6800 XT')              'amd'
Assert-Equal 'gpu intel'     (Get-GpuVendor 'Intel(R) UHD Graphics 620')          'intel'
Assert-Equal 'gpu intel arc' (Get-GpuVendor 'Intel Arc A770')                     'intel'
Assert-Equal 'gpu basic'     (Get-GpuVendor 'Microsoft Basic Display Adapter')    'other'
Assert-Equal 'gpu vbox'      (Get-GpuVendor 'VirtualBox Graphics Adapter')        'other'
Assert-Equal 'gpu empty'     (Get-GpuVendor '')                                   'other'

# --- Get-PartitionKind ----------------------------------------------------
Assert-Equal 'part efi by guid'      (Get-PartitionKind '' 'c12a7328-f81f-11d2-ba4b-00a0c93ec93b') 'efi'
Assert-Equal 'part recovery by guid' (Get-PartitionKind '' 'DE94BBA4-06D1-4D40-A16A-BFD50179D6AC') 'recovery'
Assert-Equal 'part reserved by name' (Get-PartitionKind 'Reserved' '')                             'reserved'
Assert-Equal 'part efi by name'      (Get-PartitionKind 'System' '')                               'efi'
Assert-Equal 'part basic'            (Get-PartitionKind 'Basic' '')                                'basic'

# --- ConvertTo-NormalKey --------------------------------------------------
# The dedup key. Two records of the same program must collapse; two different
# programs must not.
Assert-Equal 'key drops parens'   (ConvertTo-NormalKey 'Mozilla Firefox (x64 ru)') 'mozillafirefox'
Assert-Equal 'key drops version'  (ConvertTo-NormalKey 'Mozilla Firefox 128.0')    'mozillafirefox'
Assert-Equal 'key empty'          (ConvertTo-NormalKey '')                         ''
Assert-Equal 'key keeps distinct' (ConvertTo-NormalKey 'Notepad++')                'notepad'
# Known and accepted: a version is stripped before the key is built, so Python 2
# and Python 3 installed side by side collapse into one entry. The mapping
# database answers both with the same rule, so the report does not change.
Assert-Equal 'key python 3' (ConvertTo-NormalKey 'Python 3.12.4') 'python'
Assert-Equal 'key python 2' (ConvertTo-NormalKey 'Python 2.7.18') 'python'

# --- ConvertTo-NullableString ---------------------------------------------
# The spec asks for null, not "", where a value is simply not known. The two
# mean different things to whoever reads the hopfile.
Assert-Equal 'nullable empty' (ConvertTo-NullableString '')      $null
Assert-Equal 'nullable blank' (ConvertTo-NullableString '   ')   $null
Assert-Equal 'nullable trims' (ConvertTo-NullableString '  hi ') 'hi'

# --- ConvertTo-HtmlText ---------------------------------------------------
Assert-Equal 'html escapes' (ConvertTo-HtmlText '<a href="x">A&B</a>') '&lt;a href=&quot;x&quot;&gt;A&amp;B&lt;/a&gt;'

# --- Get-Prop -------------------------------------------------------------
# Strict mode turns a missing property into a terminating error, and half of
# what the scanner touches has properties that exist only on some builds.
Assert-Equal 'prop dict hit'    (Get-Prop -Object @{ a = 1 } -Name 'a' -Default 9)     1
Assert-Equal 'prop dict miss'   (Get-Prop -Object @{ a = 1 } -Name 'b' -Default 9)     9
Assert-Equal 'prop dict null'   (Get-Prop -Object @{ a = $null } -Name 'a' -Default 9) 9
Assert-Equal 'prop null object' (Get-Prop -Object $null -Name 'a' -Default 9)          9
Assert-Equal 'prop psobject'    (Get-Prop -Object ([pscustomobject]@{ n = 'x' }) -Name 'n' -Default 'd')  'x'
Assert-Equal 'prop absent'      (Get-Prop -Object ([pscustomobject]@{ n = 'x' }) -Name 'zz' -Default 'd') 'd'

# --- Get-RelativePath -----------------------------------------------------
# This produces payload_dir, which the Arch side resolves against the hopfile.
Assert-Equal 'relative sibling' (Get-RelativePath -From 'C:\hop' -To 'C:\hop\hop-payload') 'hop-payload'
Assert-Equal 'relative nested'  (Get-RelativePath -From 'C:\hop' -To 'C:\hop\a\b.txt')     'a/b.txt'

# --- Valve's KeyValues format ---------------------------------------------
Assert-Equal 'vdf unescapes' (Expand-VdfString 'C:\\Program Files\\Steam') 'C:\Program Files\Steam'

$appmanifest = @'
"AppState"
{
	"appid"		"730"
	"name"		"Counter-Strike 2"
	"SizeOnDisk"		"32000000000"
	// Valve writes comments into these
	"UserConfig"
	{
		"language"		"russian"
	}
}
'@
$parsed = ConvertFrom-Vdf -Text $appmanifest
Assert-Equal 'acf appid'  $parsed['AppState']['appid']                  '730'
Assert-Equal 'acf name'   $parsed['AppState']['name']                   'Counter-Strike 2'
Assert-Equal 'acf size'   $parsed['AppState']['SizeOnDisk']             '32000000000'
Assert-Equal 'acf nested' $parsed['AppState']['UserConfig']['language'] 'russian'
# Valve is not consistent about "AppState" versus "appstate", so the lookup has
# to be case-insensitive or half the library goes missing.
Assert-Equal 'acf case-insensitive' $parsed['appstate']['APPID'] '730'

$libraryfolders = @'
"libraryfolders"
{
	"0"
	{
		"path"		"C:\\Program Files (x86)\\Steam"
		"apps"
		{
			"730"		"32000000000"
		}
	}
	"1"
	{
		"path"		"D:\\SteamLibrary"
	}
}
'@
$libs = ConvertFrom-Vdf -Text $libraryfolders
Assert-Equal 'vdf library 0'    $libs['libraryfolders']['0']['path']       'C:\Program Files (x86)\Steam'
Assert-Equal 'vdf library 1'    $libs['libraryfolders']['1']['path']       'D:\SteamLibrary'
Assert-Equal 'vdf library apps' $libs['libraryfolders']['0']['apps']['730'] '32000000000'
Assert-Equal 'vdf empty input'  (ConvertFrom-Vdf -Text '').Count           0

# --- Test-SshKeyEncrypted -------------------------------------------------
Assert-Equal 'ssh rsa encrypted' (Test-SshKeyEncrypted -Text "-----BEGIN RSA PRIVATE KEY-----`nProc-Type: 4,ENCRYPTED`nDEK-Info: AES-128-CBC,X`n") $true
Assert-Equal 'ssh pkcs8 encrypted' (Test-SshKeyEncrypted -Text '-----BEGIN ENCRYPTED PRIVATE KEY-----') $true
Assert-Equal 'ssh rsa plain'     (Test-SshKeyEncrypted -Text "-----BEGIN RSA PRIVATE KEY-----`nMIIEow...`n") $false

# --- ConvertFrom-ChromeTime -----------------------------------------------
# Chromium counts MICROSECONDS since 1601-01-01. The function returns Unix epoch
# SECONDS as a string, because that is what ADD_DATE wants in the Netscape
# bookmark format - it is not meant to be an ISO date.
$epoch = ConvertFrom-ChromeTime -Value 13350000000000000
Assert-Equal 'chrome epoch' $epoch '1705526400'
Assert-Equal 'chrome date'  ([DateTimeOffset]::FromUnixTimeSeconds([int64]$epoch).UtcDateTime.ToString('yyyy-MM-dd')) '2024-01-17'
Assert-Equal 'chrome zero'  (ConvertFrom-ChromeTime -Value 0)     $null
Assert-Equal 'chrome empty' (ConvertFrom-ChromeTime -Value '')    $null
Assert-Equal 'chrome junk'  (ConvertFrom-ChromeTime -Value 'abc') $null
# Imported profiles carry pre-1970 timestamps. A negative ADD_DATE is rejected
# by the browsers this export exists to feed.
Assert-Equal 'chrome pre-1970' (ConvertFrom-ChromeTime -Value 11000000000000000) $null

# --- Windows -> IANA time zones -------------------------------------------
$zones = Get-WindowsToIanaMap
Assert-Equal 'tz is a map'  ($zones -is [System.Collections.IDictionary]) $true
Assert-Equal 'tz moscow'    $zones['Russian Standard Time'] 'Europe/Moscow'
Assert-Equal 'tz utc'       $zones['UTC']                   'Etc/UTC'
Assert-Equal 'tz pacific'   $zones['Pacific Standard Time'] 'America/Los_Angeles'
Assert-Equal 'tz london'    $zones['GMT Standard Time']     'Europe/London'
if ($zones.Count -lt 100) {
    $script:Failed++
    Write-Output ("  FAIL  timezone map has only {0} entries; it covered 135 when written" -f $zones.Count)
} else {
    $script:Passed++
}

Write-Output ''
Write-Output ("{0} function(s) under test, {1} passed, {2} failed" -f $loaded.Count, $script:Passed, $script:Failed)
if ($script:Failed -gt 0) { exit 1 }
exit 0
