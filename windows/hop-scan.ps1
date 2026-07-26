#Requires -Version 5.1

<#
.SYNOPSIS
    hop2arch - inventory this Windows installation and write a hopfile.json.

.DESCRIPTION
    hop-scan.ps1 is the Windows half of hop2arch. It walks over the machine you
    are about to leave - hardware, disks, installed software, developer setup,
    browsers, Wi-Fi, games, personalisation - and writes a single JSON manifest
    (a "hopfile", see docs/HOPFILE.md) plus an optional payload directory with
    the actual bytes worth carrying over (SSH keys, bookmarks, wallpaper, dot
    files, Wi-Fi profiles, fonts).

    The script changes nothing on this machine. It only reads, and it only
    writes to the two output paths you can see printed at the end.

    It does not need administrator rights. Everything that would need elevation
    (BitLocker status, TPM details, Wi-Fi passwords) is attempted, and on
    failure degrades to null / "unknown" and appends a line to the hopfile's
    "warnings" array instead of stopping the scan.

.PARAMETER OutFile
    Where to write the manifest. Default: .\hopfile.json

.PARAMETER PayloadDir
    Where to write the payload directory. Default: .\hop-payload

.PARAMETER WithSecrets
    Opt in to copying secret material into the payload (see PRIVACY below).

.PARAMETER NoPayload
    Write the manifest only; copy no files at all.

.PARAMETER FastSize
    Skip the recursive size/file-count walk of the user folders. Use this when
    Downloads has 400 GB in it and you do not care about the number.

.PARAMETER Quiet
    Suppress the progress display and the closing summary.

.EXAMPLE
    .\hop-scan.ps1
    The normal run: hopfile.json + hop-payload\ in the current directory.

.EXAMPLE
    .\hop-scan.ps1 -WithSecrets -OutFile D:\hop\hopfile.json -PayloadDir D:\hop\payload
    Full run including private keys and Wi-Fi passwords, written to a USB stick.

.EXAMPLE
    .\hop-scan.ps1 -NoPayload -FastSize -Quiet
    Manifest only, no folder-size walk, no output. Good for a quick look.

.NOTES
    WHAT LEAVES THIS MACHINE
        Nothing. There is no network code in this script: no upload, no
        telemetry, no update check, no analytics. It reads local state and
        writes two local paths. Whatever you do with those files afterwards is
        entirely your decision - and `hop scrub hopfile.json` on the Arch side
        will strip the personal parts before you share one.

    WHAT -WithSecrets ADDS
        Without it the payload contains only non-secret material: public SSH
        keys, your public GPG keys, dot files, bookmarks, fonts, wallpaper, and
        Wi-Fi profile XML with the key blob still encrypted (useless off this
        machine). With it the payload additionally contains:
          * your PRIVATE SSH keys, byte for byte,
          * your PRIVATE GPG keys, ASCII-armoured,
          * Wi-Fi profiles exported with `key=clear`, i.e. plaintext passwords.
        Private key material is never written into hopfile.json itself, with or
        without the switch. Treat a -WithSecrets payload directory exactly like
        you would treat ~/.ssh: it is the keys to your life.

    Compatible with Windows PowerShell 5.1 and PowerShell 7+.
    The file is deliberately ASCII-only: Windows PowerShell 5.1 reads BOM-less
    script files as ANSI, and fancy punctuation would not survive that.
#>

# The blank line between #Requires and the block above is load-bearing. Windows
# PowerShell 5.1 folds any comment that touches a <# #> block into it and then
# finds no comment-based help at all, so `Get-Help .\hop-scan.ps1` would print
# the bare syntax - and that help is where the privacy promises are written.
# Keep the blank line; never put a comment directly above the <#.

[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '',
    Justification = 'The console output here is a progress display, never data - the data leaves through hopfile.json. Write-Output would put the progress lines into the pipeline and Write-Information is silent by default, so neither would show a person that a scan which can take minutes is still alive.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseSingularNouns', '',
    Justification = 'These helpers return collections and are named for what they return. Get-FirefoxProfile would read at every call site as though one profile came back.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
    Justification = 'New-SoftwareStore builds an in-memory hashtable and touches nothing. New-PayloadTextFile writes only inside the payload directory named on the command line, which -NoPayload already switches off; a -WhatIf on one internal helper would imply the script has a -WhatIf story it does not have.')]
[CmdletBinding()]
param(
    [ValidateNotNullOrEmpty()][string]$OutFile = "$PWD\hopfile.json",
    [ValidateNotNullOrEmpty()][string]$PayloadDir = "$PWD\hop-payload",
    [switch]$WithSecrets,
    [switch]$NoPayload,
    [switch]$FastSize,
    [switch]$Quiet
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------------------
# Script state
# --------------------------------------------------------------------------

$script:Generator       = 'hop-scan.ps1/0.1.0'
$script:HopfileVersion  = 1
$script:Warnings        = New-Object 'System.Collections.Generic.List[string]'
$script:PayloadEntries  = New-Object 'System.Collections.Generic.List[object]'
$script:StepIndex       = 0
$script:StepTotal       = 12
$script:StepWarnBase    = 0
$script:StepOpen        = $false
$script:IsElevated      = $false
$script:QuietMode       = [bool]$Quiet
$script:WantSecrets     = [bool]$WithSecrets
$script:SkipSizes       = [bool]$FastSize
$script:PayloadEnabled  = -not [bool]$NoPayload
$script:PayloadRoot     = $null
$script:MaxPayloadBytes = 32MB

# --------------------------------------------------------------------------
# Infrastructure: progress display, warnings, the collector wrapper
# --------------------------------------------------------------------------

function Write-Step {
    # Opens a progress line: "[ 3/12] Installed software      ". The result
    # suffix is printed later by Complete-Step, on the same line.
    param([Parameter(Mandatory = $true)][string]$Label)

    $script:StepIndex++
    $script:StepWarnBase = $script:Warnings.Count
    $script:StepOpen = $true
    if ($script:QuietMode) { return }
    Write-Host ('[{0,2}/{1}] ' -f $script:StepIndex, $script:StepTotal) -NoNewline -ForegroundColor DarkGray
    Write-Host ('{0,-24}' -f $Label) -NoNewline -ForegroundColor Gray
}

function Complete-Step {
    # Closes the current progress line. 'auto' resolves to "partial" when the
    # step added a warning and "ok" when it did not.
    param(
        [ValidateSet('auto', 'ok', 'partial', 'skipped', 'failed')][string]$Status = 'auto',
        [string]$Detail = ''
    )

    if (-not $script:StepOpen) { return }
    $script:StepOpen = $false

    $state = $Status
    if ($state -eq 'auto') {
        if ($script:Warnings.Count -gt $script:StepWarnBase) { $state = 'partial' } else { $state = 'ok' }
    }
    if ($script:QuietMode) { return }

    $colour = 'Green'
    switch ($state) {
        'ok'      { $colour = 'Green' }
        'partial' { $colour = 'Yellow' }
        'skipped' { $colour = 'DarkGray' }
        'failed'  { $colour = 'Red' }
    }
    Write-Host ('{0,-8}' -f $state) -NoNewline -ForegroundColor $colour
    if ($Detail) {
        Write-Host $Detail -ForegroundColor DarkGray
    } else {
        Write-Host ''
    }
}

function Write-Note {
    param([string]$Text = '', [string]$Colour = 'Gray')
    if ($script:QuietMode) { return }
    Write-Host $Text -ForegroundColor $Colour
}

function Add-HopWarning {
    # Every warning ends up verbatim in hopfile.warnings, so write them for a
    # human who has to decide something, not for a log file.
    param([Parameter(Mandatory = $true)][string]$Message)

    if (-not $script:Warnings.Contains($Message)) { $script:Warnings.Add($Message) }
    Write-Verbose ('warn: {0}' -f $Message)
}

function Invoke-Collector {
    # The one place where things are allowed to go wrong. Nothing in this
    # script throws past here: a broken collector costs you one key of the
    # hopfile and one line in warnings, not the whole scan.
    [OutputType([object])]
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Body,
        $Fallback = $null
    )

    Write-Verbose ('collector {0}: start' -f $Name)
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $result = & $Body
        $watch.Stop()
        Write-Verbose ('collector {0}: ok ({1} ms)' -f $Name, $watch.ElapsedMilliseconds)
        return $result
    } catch {
        $watch.Stop()
        $reason = 'unknown error'
        try { $reason = [string]$_.Exception.Message } catch { $reason = 'unknown error' }
        Add-HopWarning ('{0}: could not be collected ({1})' -f $Name, $reason)
        Write-Verbose ('collector {0}: FAILED ({1})' -f $Name, $reason)
        return $Fallback
    }
}

# --------------------------------------------------------------------------
# Infrastructure: small utilities
# --------------------------------------------------------------------------

function Get-Prop {
    # Strict mode turns a missing property into a terminating error, and half
    # of what we touch here (CIM objects, ConvertFrom-Json output, Storage
    # module objects) has properties that exist only on some Windows builds.
    [OutputType([object])]
    param(
        $Object,
        [Parameter(Mandatory = $true)][string]$Name,
        $Default = $null
    )

    if ($null -eq $Object) { return $Default }
    try {
        if ($Object -is [System.Collections.IDictionary]) {
            if ($Object.Contains($Name)) {
                $value = $Object[$Name]
                if ($null -eq $value) { return $Default }
                return $value
            }
            return $Default
        }
        $member = $Object.PSObject.Properties[$Name]
        if ($null -eq $member) { return $Default }
        $value = $member.Value
        if ($null -eq $value) { return $Default }
        return $value
    } catch {
        return $Default
    }
}

function Get-RegValue {
    # Reads a single registry value without exploding when the key, the value
    # or the permission to read them is missing.
    [OutputType([object])]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name,
        $Default = $null
    )

    try {
        if (-not (Test-Path -LiteralPath $Path)) { return $Default }
        $key = Get-Item -LiteralPath $Path -ErrorAction Stop
        $names = @($key.GetValueNames())
        if ($names -notcontains $Name) { return $Default }
        $value = $key.GetValue($Name)
        if ($null -eq $value) { return $Default }
        return $value
    } catch {
        return $Default
    }
}

function Get-CommandPath {
    # Resolves an external executable to a full path, or $null.
    [OutputType([string])]
    param([Parameter(Mandatory = $true)][string]$Name)

    try {
        $found = @(Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue)
        if ($found.Count -eq 0) { return $null }
        return [string]$found[0].Source
    } catch {
        return $null
    }
}

function Test-CmdletExists {
    [OutputType([bool])]
    param([Parameter(Mandatory = $true)][string]$Name)

    try {
        $found = @(Get-Command -Name $Name -ErrorAction SilentlyContinue)
        return ($found.Count -gt 0)
    } catch {
        return $false
    }
}

function Invoke-Native {
    # Runs an external tool and returns its output as plain strings.
    # $ErrorActionPreference is deliberately relaxed inside this function (it
    # is function-scoped, so it restores itself on return): plenty of healthy
    # tools - git, java, gpg - write to stderr, and with 'Stop' in force the
    # 2>&1 merge below would turn that into a terminating NativeCommandError.
    [OutputType([string[]])]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )

    $ErrorActionPreference = 'Continue'
    $lines = New-Object 'System.Collections.Generic.List[string]'
    try {
        $raw = & $FilePath @ArgumentList 2>&1
        foreach ($item in @($raw)) {
            if ($null -eq $item) { continue }
            $lines.Add([string]$item)
        }
    } catch {
        Write-Verbose ('native {0}: {1}' -f $FilePath, $_.Exception.Message)
    }
    return $lines.ToArray()
}

function Read-TextFile {
    # Get-Content in 5.1 falls back to the ANSI code page for BOM-less files,
    # which mangles every non-English bookmark title and profile name. Read
    # through .NET with UTF-8 instead (the overload still honours a BOM).
    [OutputType([string])]
    param([Parameter(Mandatory = $true)][string]$Path)

    return [System.IO.File]::ReadAllText($Path, (New-Object System.Text.UTF8Encoding($false)))
}

function Resolve-FullPath {
    # [System.IO.Path]::GetFullPath resolves against the *process* working
    # directory, which is not PowerShell's location. Join by hand first.
    [OutputType([string])]
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        if ([System.IO.Path]::IsPathRooted($Path)) { return [System.IO.Path]::GetFullPath($Path) }
        return [System.IO.Path]::GetFullPath((Join-Path (Get-Location).ProviderPath $Path))
    } catch {
        return $Path
    }
}

function Get-RelativePath {
    # Returns $To relative to the directory $From, with forward slashes, so it
    # reads the same in the hopfile on either operating system.
    [OutputType([string])]
    param(
        [Parameter(Mandatory = $true)][string]$From,
        [Parameter(Mandatory = $true)][string]$To
    )

    try {
        $fromUri = New-Object System.Uri (($From.TrimEnd('\') + '\'))
        $toUri = New-Object System.Uri $To
        $relative = $fromUri.MakeRelativeUri($toUri).ToString()
        return [System.Uri]::UnescapeDataString($relative)
    } catch {
        return ($To -replace '\\', '/')
    }
}

function Format-Bytes {
    [OutputType([string])]
    param([AllowNull()]$Bytes)

    if ($null -eq $Bytes) { return 'n/a' }
    $value = 0.0
    try { $value = [double]$Bytes } catch { return 'n/a' }
    $units = @('B', 'KB', 'MB', 'GB', 'TB', 'PB')
    $index = 0
    while ($value -ge 1024 -and $index -lt ($units.Count - 1)) {
        $value = $value / 1024
        $index++
    }
    return ('{0:N1} {1}' -f $value, $units[$index])
}

function ConvertTo-HtmlText {
    [OutputType([string])]
    param([AllowNull()][string]$Text)

    if ($null -eq $Text) { return '' }
    $out = $Text -replace '&', '&amp;'
    $out = $out -replace '<', '&lt;'
    $out = $out -replace '>', '&gt;'
    $out = $out -replace '"', '&quot;'
    return $out
}

function ConvertTo-NullableString {
    # A [string] parameter declared with a $null default still arrives as the
    # empty string, because PowerShell applies the type constraint to the
    # default as well. The hopfile spec asks for null - not "" - where a value
    # is simply not known, and the two mean different things to a reader.
    [OutputType([string])]
    param([AllowNull()][string]$Text)

    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    return $Text.Trim()
}

function Measure-FolderStats {
    # Size + file count for one folder, resilient to the usual mess: denied
    # subdirectories, junctions that loop, OneDrive placeholders.
    [OutputType([object])]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Path,
        [switch]$SkipSize
    )

    $stats = [ordered]@{ path = $Path; size_bytes = $null; files = $null }
    if (-not $Path) { return $stats }

    $exists = $false
    try { $exists = [System.IO.Directory]::Exists($Path) } catch { $exists = $false }
    if (-not $exists) { return $stats }
    if ($SkipSize) { return $stats }

    $bytes = [int64]0
    $files = 0
    $complete = $false

    # Fast path: a single lazy enumeration of the whole tree.
    try {
        foreach ($file in [System.IO.Directory]::EnumerateFiles($Path, '*', [System.IO.SearchOption]::AllDirectories)) {
            $info = New-Object System.IO.FileInfo -ArgumentList $file
            $bytes += $info.Length
            $files++
        }
        $complete = $true
    } catch {
        # .NET Framework aborts the entire walk on the first unreadable
        # directory, so fall through to the slower per-directory version.
        $complete = $false
    }

    if (-not $complete) {
        $bytes = [int64]0
        $files = 0
        $stack = New-Object 'System.Collections.Generic.Stack[string]'
        $stack.Push($Path)
        while ($stack.Count -gt 0) {
            $dir = $stack.Pop()
            try {
                foreach ($file in [System.IO.Directory]::EnumerateFiles($dir)) {
                    try {
                        $info = New-Object System.IO.FileInfo -ArgumentList $file
                        $bytes += $info.Length
                        $files++
                    } catch {
                        Write-Verbose ('size: skipped file {0}' -f $file)
                    }
                }
            } catch {
                Write-Verbose ('size: skipped directory {0}' -f $dir)
            }
            try {
                foreach ($sub in [System.IO.Directory]::EnumerateDirectories($dir)) {
                    $dirInfo = $null
                    try { $dirInfo = New-Object System.IO.DirectoryInfo -ArgumentList $sub } catch { $dirInfo = $null }
                    if ($null -eq $dirInfo) { continue }
                    # Junctions and symlinks can point back up the tree.
                    if (($dirInfo.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq [System.IO.FileAttributes]::ReparsePoint) { continue }
                    $stack.Push($sub)
                }
            } catch {
                Write-Verbose ('size: could not list subdirectories of {0}' -f $dir)
            }
        }
    }

    $stats['size_bytes'] = $bytes
    $stats['files'] = $files
    return $stats
}

# --------------------------------------------------------------------------
# Infrastructure: payload
# --------------------------------------------------------------------------

function Add-PayloadEntry {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('ssh', 'gpg', 'wifi', 'bookmarks', 'gitconfig', 'terminal', 'vscode', 'wallpaper', 'font', 'other')]
        [string]$Kind,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        $RestoreTo = $null,
        [string]$Mode = '0644'
    )

    $script:PayloadEntries.Add([ordered]@{
        kind       = $Kind
        path       = ($RelativePath -replace '\\', '/')
        restore_to = $RestoreTo
        mode       = $Mode
    })
}

function Copy-PayloadFile {
    # Copies one file into the payload directory and indexes it. Returns $true
    # when the file actually landed there.
    [OutputType([bool])]
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)]
        [ValidateSet('ssh', 'gpg', 'wifi', 'bookmarks', 'gitconfig', 'terminal', 'vscode', 'wallpaper', 'font', 'other')]
        [string]$Kind,
        $RestoreTo = $null,
        [string]$Mode = '0644'
    )

    if (-not $script:PayloadEnabled -or -not $script:PayloadRoot) { return $false }
    try {
        if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) { return $false }
        $source = Get-Item -LiteralPath $SourcePath -ErrorAction Stop
        if ($source.Length -gt $script:MaxPayloadBytes) {
            Add-HopWarning ('payload: skipped "{0}" ({1}) - larger than the {2} payload limit, copy it by hand if you need it.' -f `
                $source.FullName, (Format-Bytes $source.Length), (Format-Bytes $script:MaxPayloadBytes))
            return $false
        }
        $target = Join-Path $script:PayloadRoot $RelativePath
        $targetDir = Split-Path -Path $target -Parent
        if (-not (Test-Path -LiteralPath $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        }
        Copy-Item -LiteralPath $source.FullName -Destination $target -Force -ErrorAction Stop
        Add-PayloadEntry -Kind $Kind -RelativePath $RelativePath -RestoreTo $RestoreTo -Mode $Mode
        Write-Verbose ('payload: {0} -> {1}' -f $source.FullName, $RelativePath)
        return $true
    } catch {
        Add-HopWarning ('payload: could not copy "{0}" ({1})' -f $SourcePath, $_.Exception.Message)
        return $false
    }
}

function New-PayloadTextFile {
    # Same as Copy-PayloadFile but for content this script generates itself.
    [OutputType([bool])]
    param(
        [Parameter(Mandatory = $true)][string]$Content,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)]
        [ValidateSet('ssh', 'gpg', 'wifi', 'bookmarks', 'gitconfig', 'terminal', 'vscode', 'wallpaper', 'font', 'other')]
        [string]$Kind,
        $RestoreTo = $null,
        [string]$Mode = '0644'
    )

    if (-not $script:PayloadEnabled -or -not $script:PayloadRoot) { return $false }
    try {
        $target = Join-Path $script:PayloadRoot $RelativePath
        $targetDir = Split-Path -Path $target -Parent
        if (-not (Test-Path -LiteralPath $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        }
        [System.IO.File]::WriteAllText($target, $Content, (New-Object System.Text.UTF8Encoding($false)))
        Add-PayloadEntry -Kind $Kind -RelativePath $RelativePath -RestoreTo $RestoreTo -Mode $Mode
        return $true
    } catch {
        Add-HopWarning ('payload: could not write "{0}" ({1})' -f $RelativePath, $_.Exception.Message)
        return $false
    }
}

# --------------------------------------------------------------------------
# Collector: system
# --------------------------------------------------------------------------

function Get-WindowsToIanaMap {
    # Windows and the rest of the world disagree about time zone names. This is
    # the CLDR windowsZones mapping for the zones people actually live in; the
    # scanner reports null for anything not listed rather than guessing.
    [OutputType([hashtable])]
    param()

    return @{
        'Dateline Standard Time'          = 'Etc/GMT+12'
        'UTC-11'                          = 'Etc/GMT+11'
        'Aleutian Standard Time'          = 'America/Adak'
        'Hawaiian Standard Time'          = 'Pacific/Honolulu'
        'Marquesas Standard Time'         = 'Pacific/Marquesas'
        'Alaskan Standard Time'           = 'America/Anchorage'
        'UTC-09'                          = 'Etc/GMT+9'
        'Pacific Standard Time (Mexico)'  = 'America/Tijuana'
        'UTC-08'                          = 'Etc/GMT+8'
        'Pacific Standard Time'           = 'America/Los_Angeles'
        'US Mountain Standard Time'       = 'America/Phoenix'
        'Mountain Standard Time (Mexico)' = 'America/Chihuahua'
        'Mountain Standard Time'          = 'America/Denver'
        'Central America Standard Time'   = 'America/Guatemala'
        'Central Standard Time'           = 'America/Chicago'
        'Easter Island Standard Time'     = 'Pacific/Easter'
        'Central Standard Time (Mexico)'  = 'America/Mexico_City'
        'Canada Central Standard Time'    = 'America/Regina'
        'SA Pacific Standard Time'        = 'America/Bogota'
        'Eastern Standard Time (Mexico)'  = 'America/Cancun'
        'Eastern Standard Time'           = 'America/New_York'
        'Haiti Standard Time'             = 'America/Port-au-Prince'
        'Cuba Standard Time'              = 'America/Havana'
        'US Eastern Standard Time'        = 'America/Indiana/Indianapolis'
        'Paraguay Standard Time'          = 'America/Asuncion'
        'Atlantic Standard Time'          = 'America/Halifax'
        'Venezuela Standard Time'         = 'America/Caracas'
        'Central Brazilian Standard Time' = 'America/Cuiaba'
        'SA Western Standard Time'        = 'America/La_Paz'
        'Pacific SA Standard Time'        = 'America/Santiago'
        'Newfoundland Standard Time'      = 'America/St_Johns'
        'Tocantins Standard Time'         = 'America/Araguaina'
        'E. South America Standard Time'  = 'America/Sao_Paulo'
        'SA Eastern Standard Time'        = 'America/Cayenne'
        'Argentina Standard Time'         = 'America/Argentina/Buenos_Aires'
        'Greenland Standard Time'         = 'America/Nuuk'
        'Montevideo Standard Time'        = 'America/Montevideo'
        'Magallanes Standard Time'        = 'America/Punta_Arenas'
        'Saint Pierre Standard Time'      = 'America/Miquelon'
        'Bahia Standard Time'             = 'America/Bahia'
        'UTC-02'                          = 'Etc/GMT+2'
        'Azores Standard Time'            = 'Atlantic/Azores'
        'Cape Verde Standard Time'        = 'Atlantic/Cape_Verde'
        'UTC'                             = 'Etc/UTC'
        'GMT Standard Time'               = 'Europe/London'
        'Greenwich Standard Time'         = 'Atlantic/Reykjavik'
        'Sao Tome Standard Time'          = 'Africa/Sao_Tome'
        'Morocco Standard Time'           = 'Africa/Casablanca'
        'W. Europe Standard Time'         = 'Europe/Berlin'
        'Central Europe Standard Time'    = 'Europe/Budapest'
        'Romance Standard Time'           = 'Europe/Paris'
        'Central European Standard Time'  = 'Europe/Warsaw'
        'W. Central Africa Standard Time' = 'Africa/Lagos'
        'Jordan Standard Time'            = 'Asia/Amman'
        'GTB Standard Time'               = 'Europe/Bucharest'
        'Middle East Standard Time'       = 'Asia/Beirut'
        'Egypt Standard Time'             = 'Africa/Cairo'
        'E. Europe Standard Time'         = 'Europe/Chisinau'
        'Syria Standard Time'             = 'Asia/Damascus'
        'West Bank Standard Time'         = 'Asia/Hebron'
        'South Africa Standard Time'      = 'Africa/Johannesburg'
        'FLE Standard Time'               = 'Europe/Kyiv'
        'Israel Standard Time'            = 'Asia/Jerusalem'
        'Kaliningrad Standard Time'       = 'Europe/Kaliningrad'
        'Sudan Standard Time'             = 'Africa/Khartoum'
        'Libya Standard Time'             = 'Africa/Tripoli'
        'Namibia Standard Time'           = 'Africa/Windhoek'
        'Arabic Standard Time'            = 'Asia/Baghdad'
        'Turkey Standard Time'            = 'Europe/Istanbul'
        'Arab Standard Time'              = 'Asia/Riyadh'
        'Belarus Standard Time'           = 'Europe/Minsk'
        'Russian Standard Time'           = 'Europe/Moscow'
        'E. Africa Standard Time'         = 'Africa/Nairobi'
        'Iran Standard Time'              = 'Asia/Tehran'
        'Arabian Standard Time'           = 'Asia/Dubai'
        'Astrakhan Standard Time'         = 'Europe/Astrakhan'
        'Azerbaijan Standard Time'        = 'Asia/Baku'
        'Russia Time Zone 3'              = 'Europe/Samara'
        'Mauritius Standard Time'         = 'Indian/Mauritius'
        'Saratov Standard Time'           = 'Europe/Saratov'
        'Georgian Standard Time'          = 'Asia/Tbilisi'
        'Volgograd Standard Time'         = 'Europe/Volgograd'
        'Caucasus Standard Time'          = 'Asia/Yerevan'
        'Afghanistan Standard Time'       = 'Asia/Kabul'
        'West Asia Standard Time'         = 'Asia/Tashkent'
        'Ekaterinburg Standard Time'      = 'Asia/Yekaterinburg'
        'Pakistan Standard Time'          = 'Asia/Karachi'
        'India Standard Time'             = 'Asia/Kolkata'
        'Sri Lanka Standard Time'         = 'Asia/Colombo'
        'Nepal Standard Time'             = 'Asia/Kathmandu'
        'Central Asia Standard Time'      = 'Asia/Almaty'
        'Bangladesh Standard Time'        = 'Asia/Dhaka'
        'Omsk Standard Time'              = 'Asia/Omsk'
        'Myanmar Standard Time'           = 'Asia/Yangon'
        'SE Asia Standard Time'           = 'Asia/Bangkok'
        'Altai Standard Time'             = 'Asia/Barnaul'
        'W. Mongolia Standard Time'       = 'Asia/Hovd'
        'North Asia Standard Time'        = 'Asia/Krasnoyarsk'
        'N. Central Asia Standard Time'   = 'Asia/Novosibirsk'
        'Tomsk Standard Time'             = 'Asia/Tomsk'
        'China Standard Time'             = 'Asia/Shanghai'
        'North Asia East Standard Time'   = 'Asia/Irkutsk'
        'Singapore Standard Time'         = 'Asia/Singapore'
        'W. Australia Standard Time'      = 'Australia/Perth'
        'Taipei Standard Time'            = 'Asia/Taipei'
        'Ulaanbaatar Standard Time'       = 'Asia/Ulaanbaatar'
        'Aus Central W. Standard Time'    = 'Australia/Eucla'
        'Transbaikal Standard Time'       = 'Asia/Chita'
        'Tokyo Standard Time'             = 'Asia/Tokyo'
        'North Korea Standard Time'       = 'Asia/Pyongyang'
        'Korea Standard Time'             = 'Asia/Seoul'
        'Yakutsk Standard Time'           = 'Asia/Yakutsk'
        'Cen. Australia Standard Time'    = 'Australia/Adelaide'
        'AUS Central Standard Time'       = 'Australia/Darwin'
        'E. Australia Standard Time'      = 'Australia/Brisbane'
        'AUS Eastern Standard Time'       = 'Australia/Sydney'
        'West Pacific Standard Time'      = 'Pacific/Port_Moresby'
        'Tasmania Standard Time'          = 'Australia/Hobart'
        'Vladivostok Standard Time'       = 'Asia/Vladivostok'
        'Lord Howe Standard Time'         = 'Australia/Lord_Howe'
        'Bougainville Standard Time'      = 'Pacific/Bougainville'
        'Russia Time Zone 10'             = 'Asia/Srednekolymsk'
        'Magadan Standard Time'           = 'Asia/Magadan'
        'Norfolk Standard Time'           = 'Pacific/Norfolk'
        'Sakhalin Standard Time'          = 'Asia/Sakhalin'
        'Central Pacific Standard Time'   = 'Pacific/Guadalcanal'
        'Russia Time Zone 11'             = 'Asia/Kamchatka'
        'New Zealand Standard Time'       = 'Pacific/Auckland'
        'UTC+12'                          = 'Etc/GMT-12'
        'Fiji Standard Time'              = 'Pacific/Fiji'
        'Chatham Islands Standard Time'   = 'Pacific/Chatham'
        'UTC+13'                          = 'Etc/GMT-13'
        'Tonga Standard Time'             = 'Pacific/Tongatapu'
        'Samoa Standard Time'             = 'Pacific/Apia'
        'Line Islands Standard Time'      = 'Pacific/Kiritimati'
    }
}

function Get-GpuVendor {
    # hop plan picks driver packages off this, so keep it to the four buckets.
    [OutputType([string])]
    param([AllowEmptyString()][string]$Name)

    $lower = ''
    if ($Name) { $lower = $Name.ToLowerInvariant() }
    if ($lower -match 'nvidia|geforce|quadro|rtx |gtx ') { return 'nvidia' }
    if ($lower -match 'amd|radeon|ati |firepro|vega') { return 'amd' }
    if ($lower -match 'intel|iris|uhd graphics|hd graphics|arc ') { return 'intel' }
    return 'other'
}

function Get-SystemInfo {
    [OutputType([object])]
    param()

    $os = Invoke-Collector -Name 'system/os' -Body { Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop }
    $cs = Invoke-Collector -Name 'system/computer' -Body { Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop }

    # --- Windows -----------------------------------------------------------
    $caption = [string](Get-Prop -Object $os -Name 'Caption' -Default '')
    $caption = $caption.Trim()
    $version = [string](Get-Prop -Object $os -Name 'Version' -Default '')
    $build = [string](Get-Prop -Object $os -Name 'BuildNumber' -Default '')
    $edition = [string](Get-RegValue -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -Name 'EditionID' -Default '')

    # Some 11 installs still describe themselves as "Windows 10" in WMI.
    $buildNumber = 0
    if ($build -and [int]::TryParse($build, [ref]$buildNumber)) {
        if ($buildNumber -ge 22000 -and $caption -match 'Windows 10') {
            $caption = $caption -replace 'Windows 10', 'Windows 11'
        }
    }

    $installDate = $null
    $rawInstall = Get-Prop -Object $os -Name 'InstallDate'
    if ($rawInstall -is [datetime]) {
        # ConvertTo-Json would render a DateTime as "/Date(...)/" in 5.1.
        $installDate = $rawInstall.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    }

    # --- Firmware and Secure Boot -----------------------------------------
    $firmware = 'unknown'
    if ($env:firmware_type) {
        if ($env:firmware_type -match 'UEFI') { $firmware = 'UEFI' }
        elseif ($env:firmware_type -match 'Legacy|BIOS') { $firmware = 'BIOS' }
    }
    if ($firmware -eq 'unknown') {
        # This key only exists on UEFI machines and, unlike the SecureBoot
        # cmdlets, it is readable without elevation.
        if (Test-Path -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\SecureBoot\State') { $firmware = 'UEFI' }
    }

    $secureBoot = $null
    if (Test-CmdletExists -Name 'Confirm-SecureBootUEFI') {
        try {
            $secureBoot = [bool](Confirm-SecureBootUEFI -ErrorAction Stop)
            $firmware = 'UEFI'
        } catch {
            # "Cmdlet not supported on this platform" is how the SecureBoot
            # module says "this machine booted in legacy BIOS mode".
            if ($_.Exception.Message -match 'not supported') {
                if ($firmware -eq 'unknown') { $firmware = 'BIOS' }
            } else {
                Add-HopWarning 'Secure Boot state needs an elevated shell - reported as null.'
            }
            $secureBoot = $null
        }
    }
    if ($null -eq $secureBoot) {
        $sbValue = Get-RegValue -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\SecureBoot\State' -Name 'UEFISecureBootEnabled'
        if ($null -ne $sbValue) { $secureBoot = ([int]$sbValue -eq 1) }
    }
    if ($firmware -eq 'unknown' -and $script:IsElevated) {
        $bcd = Get-CommandPath -Name 'bcdedit'
        if ($bcd) {
            foreach ($line in (Invoke-Native -FilePath $bcd -ArgumentList @('/enum', '{current}'))) {
                if ($line -match 'winload\.efi|\\EFI\\') { $firmware = 'UEFI'; break }
                if ($line -match 'winload\.exe') { $firmware = 'BIOS'; break }
            }
        }
    }

    # --- TPM ---------------------------------------------------------------
    $tpm = $null
    try {
        $tpmObj = @(Get-CimInstance -Namespace 'root/cimv2/security/microsofttpm' -ClassName Win32_Tpm -ErrorAction Stop)
        if ($tpmObj.Count -gt 0) {
            $enabled = Get-Prop -Object $tpmObj[0] -Name 'IsEnabled_InitialValue'
            if ($null -eq $enabled) { $tpm = $true } else { $tpm = [bool]$enabled }
        } else {
            $tpm = $false
        }
    } catch {
        # The TPM namespace is admin-only. Fall back to the device list, which
        # is not, and only tells us presence.
        #
        # A device query that succeeds and finds nothing is an answer - this
        # machine has no TPM - and must not be reported as "ask an administrator".
        # Only a query that actually failed leaves the state unknown. Saying the
        # wrong reason is worse than saying nothing: the reader goes and opens an
        # elevated shell to chase a fact that was already settled.
        $probed = $false
        try {
            $pnp = @(Get-CimInstance -ClassName Win32_PnPEntity -Filter "Name LIKE '%Trusted Platform Module%'" -ErrorAction Stop)
            $probed = $true
            if ($pnp.Count -gt 0) { $tpm = $true } else { $tpm = $false }
        } catch {
            $tpm = $null
        }
        if (-not $probed) {
            Add-HopWarning 'TPM state could not be read from either the TPM namespace or the device list, so it is reported as null. The elevated shell would answer it, but nothing in the hop depends on the answer.'
        }
    }

    # --- Locale, keyboard, time zone --------------------------------------
    $locale = $null
    try { $locale = [string](Get-Culture).Name } catch { $locale = $null }
    if (-not $locale -and (Test-CmdletExists -Name 'Get-WinSystemLocale')) {
        try { $locale = [string](Get-WinSystemLocale).Name } catch { $locale = $null }
    }
    $uiLanguage = $null
    try { $uiLanguage = [string](Get-UICulture).Name } catch { $uiLanguage = $null }

    $layouts = New-Object 'System.Collections.Generic.List[string]'
    # HKCU\Keyboard Layout\Preload holds the raw KLIDs in load order: "1"="00000409".
    try {
        $preload = Get-Item -LiteralPath 'HKCU:\Keyboard Layout\Preload' -ErrorAction Stop
        foreach ($name in @($preload.GetValueNames() | Sort-Object)) {
            $klid = [string]$preload.GetValue($name)
            if ($klid -and -not $layouts.Contains($klid)) { $layouts.Add($klid) }
        }
    } catch {
        Write-Verbose 'keyboard: Preload key unreadable, falling back to Get-WinUserLanguageList'
    }
    if ($layouts.Count -eq 0 -and (Test-CmdletExists -Name 'Get-WinUserLanguageList')) {
        try {
            foreach ($lang in @(Get-WinUserLanguageList -ErrorAction Stop)) {
                foreach ($tip in @(Get-Prop -Object $lang -Name 'InputMethodTips' -Default @())) {
                    # Tips look like "0409:00000409" - the second half is the KLID.
                    $parts = @([string]$tip -split ':')
                    if ($parts.Count -ge 2 -and -not $layouts.Contains($parts[1])) { $layouts.Add($parts[1]) }
                }
            }
        } catch {
            Add-HopWarning 'Keyboard layouts could not be read.'
        }
    }

    $tzWindows = $null
    $tzOffset = $null
    try {
        $tzInfo = [System.TimeZoneInfo]::Local
        $tzWindows = [string]$tzInfo.Id
        $tzOffset = [int]$tzInfo.GetUtcOffset([datetime]::Now).TotalMinutes
    } catch {
        $tzWindows = $null
    }
    $tzIana = $null
    if ($tzWindows) {
        $map = Get-WindowsToIanaMap
        if ($map.ContainsKey($tzWindows)) { $tzIana = [string]$map[$tzWindows] }
    }

    # --- CPU, memory, GPU --------------------------------------------------
    $cpuName = $null
    $cpuVendor = $null
    $cores = 0
    $threads = 0
    try {
        $cpus = @(Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop)
        if ($cpus.Count -gt 0) {
            $cpuName = ([string](Get-Prop -Object $cpus[0] -Name 'Name' -Default '')).Trim()
            $cpuName = $cpuName -replace '\s{2,}', ' '
            $cpuVendor = [string](Get-Prop -Object $cpus[0] -Name 'Manufacturer' -Default '')
            foreach ($cpu in $cpus) {
                $cores += [int](Get-Prop -Object $cpu -Name 'NumberOfCores' -Default 0)
                $threads += [int](Get-Prop -Object $cpu -Name 'NumberOfLogicalProcessors' -Default 0)
            }
        }
    } catch {
        Add-HopWarning 'CPU details could not be read.'
    }

    $memoryGb = $null
    $totalMemory = Get-Prop -Object $cs -Name 'TotalPhysicalMemory'
    if ($null -ne $totalMemory) {
        try { $memoryGb = [int][math]::Round(([double]$totalMemory / 1GB), 0) } catch { $memoryGb = $null }
    }

    $gpus = New-Object 'System.Collections.Generic.List[object]'
    try {
        foreach ($video in @(Get-CimInstance -ClassName Win32_VideoController -ErrorAction Stop)) {
            $gpuName = ([string](Get-Prop -Object $video -Name 'Name' -Default '')).Trim()
            if (-not $gpuName) { continue }
            $gpus.Add([ordered]@{
                name           = $gpuName
                vendor         = (Get-GpuVendor -Name $gpuName)
                driver_version = [string](Get-Prop -Object $video -Name 'DriverVersion' -Default '')
            })
        }
    } catch {
        Add-HopWarning 'GPU details could not be read.'
    }

    # --- Chassis -----------------------------------------------------------
    $hasBattery = $false
    try { $hasBattery = (@(Get-CimInstance -ClassName Win32_Battery -ErrorAction Stop).Count -gt 0) } catch { $hasBattery = $false }

    $model = [string](Get-Prop -Object $cs -Name 'Model' -Default '')
    $manufacturer = [string](Get-Prop -Object $cs -Name 'Manufacturer' -Default '')
    $chassis = 'unknown'
    # A virtual machine that reports a battery is still a virtual machine, so
    # the hypervisor check wins over the battery check.
    if (($model + ' ' + $manufacturer) -match 'VMware|VirtualBox|KVM|Hyper-V|Virtual Machine|QEMU|Xen|Parallels|innotek|Bochs') {
        $chassis = 'vm'
    } elseif ($hasBattery) {
        $chassis = 'laptop'
    } else {
        $chassis = 'desktop'
        try {
            $enclosure = @(Get-CimInstance -ClassName Win32_SystemEnclosure -ErrorAction Stop)
            foreach ($item in $enclosure) {
                foreach ($type in @(Get-Prop -Object $item -Name 'ChassisTypes' -Default @())) {
                    # 8-14 are the portable form factors, 30-32 tablet/detachable.
                    if (@(8, 9, 10, 11, 12, 14, 18, 21, 30, 31, 32) -contains [int]$type) { $chassis = 'laptop' }
                }
            }
        } catch {
            Write-Verbose 'chassis: Win32_SystemEnclosure unreadable'
        }
    }

    return [ordered]@{
        hostname = [string]$env:COMPUTERNAME
        windows  = [ordered]@{
            caption      = $caption
            version      = $version
            build        = $build
            edition      = $edition
            install_date = $installDate
        }
        firmware         = $firmware
        secure_boot      = $secureBoot
        tpm              = $tpm
        locale           = $locale
        ui_language      = $uiLanguage
        keyboard_layouts = @($layouts.ToArray())
        timezone         = [ordered]@{
            windows            = $tzWindows
            iana               = $tzIana
            utc_offset_minutes = $tzOffset
        }
        cpu = [ordered]@{
            name    = $cpuName
            vendor  = $cpuVendor
            cores   = $cores
            threads = $threads
        }
        memory_gb = $memoryGb
        gpus      = @($gpus.ToArray())
        chassis   = $chassis
        battery   = $hasBattery
    }
}

# --------------------------------------------------------------------------
# Collector: disks
# --------------------------------------------------------------------------

function Get-BitLockerMap {
    # Drive letter (upper case, no colon) -> "on" | "off". Missing letters are
    # reported as "unknown" by the caller. manage-bde is deliberately not used.
    [OutputType([hashtable])]
    param()

    $map = @{}
    if (-not (Test-CmdletExists -Name 'Get-BitLockerVolume')) {
        # Home editions have Device Encryption but not the BitLocker module, so
        # silence here would read as "not encrypted" when it means "not asked".
        Add-HopWarning 'BitLocker: this edition of Windows has no BitLocker PowerShell module, so every volume is reported as "unknown". If Device Encryption is switched on, save the recovery key and decrypt (or fully back up) before you touch the partition table.'
        return $map
    }
    $volumes = @()
    try {
        $volumes = @(Get-BitLockerVolume -ErrorAction Stop)
    } catch {
        Add-HopWarning 'BitLocker status needs an elevated shell - volumes are reported as "unknown".'
        return $map
    }
    foreach ($volume in $volumes) {
        $mount = [string](Get-Prop -Object $volume -Name 'MountPoint' -Default '')
        if (-not $mount) { continue }
        $letter = $mount.TrimEnd('\').TrimEnd(':')
        if (-not $letter) { continue }
        $protection = [string](Get-Prop -Object $volume -Name 'ProtectionStatus' -Default 'Unknown')
        $volumeStatus = [string](Get-Prop -Object $volume -Name 'VolumeStatus' -Default '')
        $state = 'unknown'
        if ($protection -eq 'On') { $state = 'on' }
        elseif ($protection -eq 'Off') { $state = 'off' }
        # Protectors can be suspended while the volume is still encrypted.
        if ($state -eq 'off' -and $volumeStatus -match 'Encrypt') { $state = 'on' }
        $map[$letter.ToUpperInvariant()] = $state
    }
    return $map
}

function Get-PartitionKind {
    [OutputType([string])]
    param([AllowEmptyString()][string]$TypeName, [AllowEmptyString()][string]$GptType)

    $type = ''
    if ($TypeName) { $type = $TypeName }
    $guid = ''
    if ($GptType) { $guid = $GptType.ToLowerInvariant() }

    if ($guid -match 'c12a7328-f81f-11d2-ba4b-00a0c93ec93b') { return 'efi' }
    if ($guid -match 'de94bba4-06d1-4d40-a16a-bfd50179d6ac') { return 'recovery' }
    if ($guid -match 'e3c9e316-0b5c-4db8-817d-f92df00215ae') { return 'reserved' }
    if ($type -match 'Recovery') { return 'recovery' }
    if ($type -match 'Reserved|MSR') { return 'reserved' }
    if ($type -match 'System|EFI') { return 'efi' }
    return 'basic'
}

function Get-DiskInventoryFromStorage {
    # Preferred path: the Storage module (Windows 8 / Server 2012 and newer).
    [OutputType([object[]])]
    param([hashtable]$BitLocker = @{})

    $disks = New-Object 'System.Collections.Generic.List[object]'
    $systemDrive = ([string]$env:SystemDrive).TrimEnd(':')

    foreach ($disk in @(Get-Disk -ErrorAction Stop | Sort-Object -Property Number)) {
        $number = [int](Get-Prop -Object $disk -Name 'Number' -Default (-1))
        $model = [string](Get-Prop -Object $disk -Name 'FriendlyName' -Default '')
        if (-not $model) { $model = [string](Get-Prop -Object $disk -Name 'Model' -Default '') }

        $isSystem = [bool](Get-Prop -Object $disk -Name 'IsSystem' -Default $false)
        $isBoot = [bool](Get-Prop -Object $disk -Name 'IsBoot' -Default $false)

        $partitions = New-Object 'System.Collections.Generic.List[object]'
        $rawPartitions = @()
        try {
            $rawPartitions = @(Get-Partition -DiskNumber $number -ErrorAction Stop | Sort-Object -Property PartitionNumber)
        } catch {
            Write-Verbose ('disks: no partition list for disk {0}' -f $number)
        }

        foreach ($partition in $rawPartitions) {
            # Get-Partition returns [char]0 - not $null - for a partition with
            # no drive letter, and that NUL would end up in the JSON.
            $letterRaw = [string](Get-Prop -Object $partition -Name 'DriveLetter' -Default '')
            $letter = $null
            if ($letterRaw) {
                $trimmed = $letterRaw.Trim([char]0).Trim()
                if ($trimmed) { $letter = $trimmed.ToUpperInvariant() }
            }

            $volume = $null
            if ($letter) {
                try { $volume = Get-Volume -DriveLetter $letter -ErrorAction Stop } catch { $volume = $null }
            }
            if ($null -eq $volume) {
                try { $volume = $partition | Get-Volume -ErrorAction Stop } catch { $volume = $null }
            }

            $fileSystem = [string](Get-Prop -Object $volume -Name 'FileSystemType' -Default '')
            if (-not $fileSystem) { $fileSystem = [string](Get-Prop -Object $volume -Name 'FileSystem' -Default '') }

            $bitlocker = 'unknown'
            if ($letter -and $BitLocker.ContainsKey($letter)) { $bitlocker = [string]$BitLocker[$letter] }

            $partitions.Add([ordered]@{
                letter      = $letter
                label       = [string](Get-Prop -Object $volume -Name 'FileSystemLabel' -Default '')
                fs          = $fileSystem
                size_bytes  = [int64](Get-Prop -Object $partition -Name 'Size' -Default 0)
                free_bytes  = [int64](Get-Prop -Object $volume -Name 'SizeRemaining' -Default 0)
                bitlocker   = $bitlocker
                kind        = (Get-PartitionKind -TypeName ([string](Get-Prop -Object $partition -Name 'Type' -Default '')) `
                                                 -GptType ([string](Get-Prop -Object $partition -Name 'GptType' -Default '')))
            })

            if ($letter -and $letter -eq $systemDrive) { $isSystem = $true }
        }

        $disks.Add([ordered]@{
            index           = $number
            model           = $model
            size_bytes      = [int64](Get-Prop -Object $disk -Name 'Size' -Default 0)
            bus             = [string](Get-Prop -Object $disk -Name 'BusType' -Default 'unknown')
            partition_style = [string](Get-Prop -Object $disk -Name 'PartitionStyle' -Default 'unknown')
            system_disk     = ($isSystem -or $isBoot)
            partitions      = @($partitions.ToArray())
        })
    }
    return $disks.ToArray()
}

function Get-DiskInventoryFromWmi {
    # Fallback for machines without the Storage module: the classic
    # DiskDrive -> DiskPartition -> LogicalDisk association chain.
    [OutputType([object[]])]
    param([hashtable]$BitLocker = @{})

    $disks = New-Object 'System.Collections.Generic.List[object]'
    $systemDrive = ([string]$env:SystemDrive).TrimEnd(':')

    foreach ($drive in @(Get-CimInstance -ClassName Win32_DiskDrive -ErrorAction Stop | Sort-Object -Property Index)) {
        $partitions = New-Object 'System.Collections.Generic.List[object]'
        $style = 'unknown'
        $isSystem = $false

        $rawPartitions = @()
        try {
            $rawPartitions = @(Get-CimAssociatedInstance -InputObject $drive -ResultClassName Win32_DiskPartition -ErrorAction Stop)
        } catch {
            Write-Verbose 'disks: partition association unavailable'
        }

        foreach ($partition in $rawPartitions) {
            $typeName = [string](Get-Prop -Object $partition -Name 'Type' -Default '')
            if ($typeName -match 'GPT') { $style = 'GPT' } elseif ($typeName) { $style = 'MBR' }
            if ([bool](Get-Prop -Object $partition -Name 'BootPartition' -Default $false)) { $isSystem = $true }

            $logicalDisks = @()
            try {
                $logicalDisks = @(Get-CimAssociatedInstance -InputObject $partition -ResultClassName Win32_LogicalDisk -ErrorAction Stop)
            } catch {
                $logicalDisks = @()
            }

            if ($logicalDisks.Count -eq 0) {
                # A partition with no drive letter: EFI, MSR, recovery.
                $partitions.Add([ordered]@{
                    letter     = $null
                    label      = ''
                    fs         = ''
                    size_bytes = [int64](Get-Prop -Object $partition -Name 'Size' -Default 0)
                    free_bytes = 0
                    bitlocker  = 'unknown'
                    kind       = (Get-PartitionKind -TypeName $typeName -GptType '')
                })
                continue
            }

            foreach ($logical in $logicalDisks) {
                $deviceId = [string](Get-Prop -Object $logical -Name 'DeviceID' -Default '')
                $letter = $null
                if ($deviceId) { $letter = $deviceId.TrimEnd(':').ToUpperInvariant() }
                if ($letter -and $letter -eq $systemDrive) { $isSystem = $true }

                $bitlocker = 'unknown'
                if ($letter -and $BitLocker.ContainsKey($letter)) { $bitlocker = [string]$BitLocker[$letter] }

                $partitions.Add([ordered]@{
                    letter     = $letter
                    label      = [string](Get-Prop -Object $logical -Name 'VolumeName' -Default '')
                    fs         = [string](Get-Prop -Object $logical -Name 'FileSystem' -Default '')
                    size_bytes = [int64](Get-Prop -Object $logical -Name 'Size' -Default 0)
                    free_bytes = [int64](Get-Prop -Object $logical -Name 'FreeSpace' -Default 0)
                    bitlocker  = $bitlocker
                    kind       = (Get-PartitionKind -TypeName $typeName -GptType '')
                })
            }
        }

        $disks.Add([ordered]@{
            index           = [int](Get-Prop -Object $drive -Name 'Index' -Default (-1))
            model           = ([string](Get-Prop -Object $drive -Name 'Model' -Default '')).Trim()
            size_bytes      = [int64](Get-Prop -Object $drive -Name 'Size' -Default 0)
            bus             = [string](Get-Prop -Object $drive -Name 'InterfaceType' -Default 'unknown')
            partition_style = $style
            system_disk     = $isSystem
            partitions      = @($partitions.ToArray())
        })
    }
    return $disks.ToArray()
}

function Get-DiskInventory {
    [OutputType([object[]])]
    param()

    $bitlocker = Invoke-Collector -Name 'disks/bitlocker' -Body { Get-BitLockerMap } -Fallback @{}
    if ($null -eq $bitlocker) { $bitlocker = @{} }

    $disks = @()
    if (Test-CmdletExists -Name 'Get-Disk') {
        $disks = @(Invoke-Collector -Name 'disks/storage' -Body { Get-DiskInventoryFromStorage -BitLocker $bitlocker } -Fallback @())
    }
    if ($disks.Count -eq 0) {
        $disks = @(Invoke-Collector -Name 'disks/wmi' -Body { Get-DiskInventoryFromWmi -BitLocker $bitlocker } -Fallback @())
    }

    # One loud warning is worth more than a hundred "bitlocker": "on" fields:
    # repartitioning an encrypted volume from a Linux installer loses the data.
    $encrypted = New-Object 'System.Collections.Generic.List[string]'
    foreach ($disk in $disks) {
        foreach ($partition in @(Get-Prop -Object $disk -Name 'partitions' -Default @())) {
            if ([string](Get-Prop -Object $partition -Name 'bitlocker' -Default 'unknown') -eq 'on') {
                $letter = [string](Get-Prop -Object $partition -Name 'letter' -Default '?')
                if (-not $encrypted.Contains($letter)) { $encrypted.Add($letter) }
            }
        }
    }
    if ($encrypted.Count -gt 0) {
        Add-HopWarning ('BitLocker is ON for volume(s) {0}. Back up the recovery key and decrypt (or fully back up) before you touch the partition table.' -f ($encrypted -join ', '))
    }

    return $disks
}

# --------------------------------------------------------------------------
# Collector: user
# --------------------------------------------------------------------------

function Get-KnownFolderPath {
    # User Shell Folders wins over [Environment]::GetFolderPath because it is
    # what actually reflects redirection - a Documents folder moved onto D: or
    # taken over by OneDrive shows up here and nowhere else. Values are
    # REG_EXPAND_SZ ("%USERPROFILE%\Desktop"), and .GetValue() expands them.
    [OutputType([string])]
    param(
        [Parameter(Mandatory = $true)][string]$RegistryName,
        [AllowNull()][string]$SpecialFolder
    )

    $path = [string](Get-RegValue -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders' -Name $RegistryName -Default '')
    if (-not $path) {
        $path = [string](Get-RegValue -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders' -Name $RegistryName -Default '')
    }
    if ($path) {
        try {
            $path = [System.Environment]::ExpandEnvironmentVariables($path)
        } catch {
            Write-Verbose ('user: could not expand the environment variables in "{0}"' -f $path)
        }
    }
    if (-not $path -and $SpecialFolder) {
        try { $path = [string][System.Environment]::GetFolderPath($SpecialFolder) } catch { $path = '' }
    }
    return $path
}

function Get-UserInfo {
    [OutputType([object])]
    param()

    $userName = [string]$env:USERNAME
    $profilePath = [string]$env:USERPROFILE

    $fullName = $null
    try {
        # LocalAccount = TRUE keeps this from enumerating a domain.
        $filter = ("Name = '{0}' AND LocalAccount = TRUE" -f ($userName -replace "'", "''"))
        $account = @(Get-CimInstance -ClassName Win32_UserAccount -Filter $filter -ErrorAction Stop)
        if ($account.Count -gt 0) {
            $candidate = [string](Get-Prop -Object $account[0] -Name 'FullName' -Default '')
            if ($candidate) { $fullName = $candidate }
        }
    } catch {
        Write-Verbose 'user: Win32_UserAccount unavailable'
    }

    $known = @(
        [ordered]@{ Name = 'Desktop';   Reg = 'Desktop';                                Special = 'Desktop' },
        [ordered]@{ Name = 'Documents'; Reg = 'Personal';                               Special = 'MyDocuments' },
        [ordered]@{ Name = 'Downloads'; Reg = '{374DE290-123F-4565-9164-39C4925E467B}'; Special = $null },
        [ordered]@{ Name = 'Pictures';  Reg = 'My Pictures';                            Special = 'MyPictures' },
        [ordered]@{ Name = 'Music';     Reg = 'My Music';                               Special = 'MyMusic' },
        [ordered]@{ Name = 'Videos';    Reg = 'My Video';                               Special = 'MyVideos' }
    )

    $folders = [ordered]@{}
    foreach ($entry in $known) {
        $path = Get-KnownFolderPath -RegistryName ([string]$entry['Reg']) -SpecialFolder ([string]$entry['Special'])
        if (-not $path -and $entry['Name'] -eq 'Downloads') {
            $path = Join-Path $profilePath 'Downloads'
        }
        $folders[[string]$entry['Name']] = (Measure-FolderStats -Path $path -SkipSize:$script:SkipSizes)
    }

    # OneDrive: the environment variables are set by the client itself, the
    # registry key survives a signed-out client.
    $oneDrivePath = ''
    foreach ($candidate in @($env:OneDrive, $env:OneDriveConsumer, $env:OneDriveCommercial)) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { $oneDrivePath = [string]$candidate; break }
    }
    if (-not $oneDrivePath) {
        $regPath = [string](Get-RegValue -Path 'HKCU:\Software\Microsoft\OneDrive' -Name 'UserFolder' -Default '')
        if ($regPath -and (Test-Path -LiteralPath $regPath)) { $oneDrivePath = $regPath }
    }
    $oneDrive = [ordered]@{ present = $false; path = $null; size_bytes = $null }
    if ($oneDrivePath) {
        $stats = Measure-FolderStats -Path $oneDrivePath -SkipSize:$script:SkipSizes
        $oneDrive['present'] = $true
        $oneDrive['path'] = $oneDrivePath
        $oneDrive['size_bytes'] = $stats['size_bytes']
    }

    return [ordered]@{
        name         = $userName
        full_name    = $fullName
        profile_path = $profilePath
        folders      = $folders
        onedrive     = $oneDrive
    }
}

# --------------------------------------------------------------------------
# Collector: software
# --------------------------------------------------------------------------

function ConvertTo-NormalKey {
    # Collapses "Mozilla Firefox (x64 ru)" and "Mozilla Firefox 128.0" onto the
    # same key so the same program found twice is reported once.
    [OutputType([string])]
    param([AllowNull()][string]$Text)

    if (-not $Text) { return '' }
    $key = $Text.ToLowerInvariant()
    $key = $key -replace '\(.*?\)', ' '
    $key = $key -replace '\bversion\b', ' '
    $key = $key -replace '\b\d+(\.\d+)+\b', ' '
    $key = $key -replace '[^a-z0-9]+', ''
    return $key
}

function New-SoftwareStore {
    [OutputType([hashtable])]
    param()
    return @{
        Order  = (New-Object 'System.Collections.Generic.List[object]')
        Exact  = @{}   # "name|publisher" -> entry
        ByName = @{}   # "name"           -> first entry with that name
    }
}

function Add-SoftwareEntry {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Store,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Name,
        [Parameter(Mandatory = $true)][ValidateSet('registry', 'winget', 'store', 'choco', 'scoop')][string]$Source,
        [AllowNull()][string]$Version = $null,
        [AllowNull()][string]$Publisher = $null,
        [AllowNull()][string]$WingetId = $null,
        [AllowNull()][string]$InstallLocation = $null,
        [int64]$SizeBytes = 0,
        [bool]$SystemComponent = $false
    )

    if (-not $Name) { return }
    $clean = $Name.Trim()
    if (-not $clean) { return }

    # The four optional strings go into the hopfile as null when the source did
    # not know them; new locals because assigning $null back to a [string]
    # parameter would only turn it into "" again.
    $versionValue   = ConvertTo-NullableString -Text $Version
    $publisherValue = ConvertTo-NullableString -Text $Publisher
    $wingetValue    = ConvertTo-NullableString -Text $WingetId
    $locationValue  = ConvertTo-NullableString -Text $InstallLocation

    $nameKey = ConvertTo-NormalKey -Text $clean
    if (-not $nameKey) { return }
    $publisherKey = ConvertTo-NormalKey -Text $publisherValue
    $exactKey = ('{0}|{1}' -f $nameKey, $publisherKey)

    $entry = $null
    if ($Store.Exact.ContainsKey($exactKey)) {
        $entry = $Store.Exact[$exactKey]
    } elseif ($Store.ByName.ContainsKey($nameKey)) {
        # Same display name, and at least one of the two sources did not tell
        # us a publisher - winget and choco usually do not. Treat as the same.
        $candidate = $Store.ByName[$nameKey]
        $candidateKey = ConvertTo-NormalKey -Text ([string](Get-Prop -Object $candidate -Name 'publisher' -Default ''))
        if (-not $publisherKey -or -not $candidateKey) { $entry = $candidate }
    }

    if ($null -eq $entry) {
        $entry = [ordered]@{
            name             = $clean
            version          = $versionValue
            publisher        = $publisherValue
            sources          = (New-Object 'System.Collections.Generic.List[string]')
            winget_id        = $wingetValue
            install_location = $locationValue
            size_bytes       = $SizeBytes
            system_component = $SystemComponent
        }
        $entry['sources'].Add($Source)
        $Store.Order.Add($entry)
        $Store.Exact[$exactKey] = $entry
        if (-not $Store.ByName.ContainsKey($nameKey)) { $Store.ByName[$nameKey] = $entry }
        return
    }

    # Merge: first non-empty value wins, sources accumulate.
    if (-not $entry['sources'].Contains($Source)) { $entry['sources'].Add($Source) }
    if (-not $entry['version'] -and $versionValue) { $entry['version'] = $versionValue }
    if (-not $entry['publisher'] -and $publisherValue) { $entry['publisher'] = $publisherValue }
    if (-not $entry['winget_id'] -and $wingetValue) { $entry['winget_id'] = $wingetValue }
    if (-not $entry['install_location'] -and $locationValue) { $entry['install_location'] = $locationValue }
    if ([int64]$entry['size_bytes'] -eq 0 -and $SizeBytes -gt 0) { $entry['size_bytes'] = $SizeBytes }
    # A program is only a system component if every source agrees it is one.
    if (-not $SystemComponent) { $entry['system_component'] = $false }
    if (-not $Store.Exact.ContainsKey($exactKey)) { $Store.Exact[$exactKey] = $entry }
}

function Add-RegistryUninstallEntries {
    param([Parameter(Mandatory = $true)][hashtable]$Store)

    $roots = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKCU:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'
    )

    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $keys = @()
        try {
            $keys = @(Get-ChildItem -LiteralPath $root -ErrorAction Stop)
        } catch {
            Add-HopWarning ('software: could not read {0}' -f $root)
            continue
        }

        foreach ($key in $keys) {
            try {
                $displayName = [string]$key.GetValue('DisplayName')
                if (-not $displayName) { continue }

                # SystemComponent / ParentKeyName mark redistributables, update
                # helpers and patch entries. The spec wants them flagged, not
                # dropped - hop plan decides what to do with them.
                $isSystemComponent = $false
                $systemComponent = $key.GetValue('SystemComponent')
                if ($null -ne $systemComponent -and [int]$systemComponent -eq 1) { $isSystemComponent = $true }
                if ($key.GetValue('ParentKeyName')) { $isSystemComponent = $true }
                $releaseType = [string]$key.GetValue('ReleaseType')
                if ($releaseType -match 'Update|Hotfix|Security') { $isSystemComponent = $true }

                $size = [int64]0
                $estimated = $key.GetValue('EstimatedSize')
                if ($null -ne $estimated) {
                    try { $size = [int64]$estimated * 1024 } catch { $size = [int64]0 }   # stored in KB
                }

                Add-SoftwareEntry -Store $Store -Name $displayName -Source 'registry' `
                    -Version ([string]$key.GetValue('DisplayVersion')) `
                    -Publisher ([string]$key.GetValue('Publisher')) `
                    -InstallLocation ([string]$key.GetValue('InstallLocation')) `
                    -SizeBytes $size -SystemComponent $isSystemComponent
            } catch {
                Write-Verbose ('software: skipped an unreadable uninstall key under {0}' -f $root)
            }
        }
    }
}

function Get-WingetTable {
    # winget's table is column-aligned and fully localised, so the only stable
    # thing about it is the column *order*: Name, Id, Version, [Available],
    # Source. Find the header row (the line above the ---- rule) and cut the
    # data rows at the character offsets where the header words start.
    [OutputType([object[]])]
    param([AllowEmptyCollection()][string[]]$Raw = @())

    $rows = New-Object 'System.Collections.Generic.List[object]'
    $lines = New-Object 'System.Collections.Generic.List[string]'
    foreach ($item in $Raw) {
        $text = [string]$item
        # The progress spinner redraws itself with carriage returns; only the
        # last segment of such a line is real output.
        if ($text.Contains("`r")) {
            $segments = $text.Split("`r")
            $text = $segments[$segments.Length - 1]
        }
        # ... and it paints block characters we do not want in a package name.
        $text = $text -replace '[\u2500-\u259F]', ''
        $lines.Add($text)
    }

    $ruleIndex = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i].Trim() -match '^-{10,}$') { $ruleIndex = $i; break }
    }
    if ($ruleIndex -lt 1) { return $rows.ToArray() }

    $header = $lines[$ruleIndex - 1]
    $offsets = New-Object 'System.Collections.Generic.List[int]'
    $inGap = $true
    for ($i = 0; $i -lt $header.Length; $i++) {
        if ($header[$i] -eq ' ') {
            # Two or more spaces separate columns; a single space can sit
            # inside a localised header word.
            if (($i + 1) -lt $header.Length -and $header[$i + 1] -eq ' ') { $inGap = $true }
        } else {
            if ($inGap) { $offsets.Add($i) }
            $inGap = $false
        }
    }
    if ($offsets.Count -lt 3) { return $rows.ToArray() }

    $idStart = $offsets[1]
    $versionStart = $offsets[2]

    for ($i = $ruleIndex + 1; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        if (-not $line.Trim()) { continue }
        if ($line.Length -le $idStart) { continue }

        $name = $line.Substring(0, $idStart).Trim()
        $idLength = [Math]::Min($versionStart - $idStart, $line.Length - $idStart)
        $id = ''
        if ($idLength -gt 0) { $id = $line.Substring($idStart, $idLength).Trim() }
        $version = ''
        if ($line.Length -gt $versionStart) {
            $versionLength = $line.Length - $versionStart
            if ($offsets.Count -ge 4) { $versionLength = [Math]::Min($offsets[3] - $versionStart, $line.Length - $versionStart) }
            if ($versionLength -gt 0) { $version = $line.Substring($versionStart, $versionLength).Trim() }
        }

        # Wide (CJK) characters are padded by display width, not by character
        # count, so the offsets can slip. Fall back to a whitespace split.
        if (-not $id -or $id.Contains(' ')) {
            $fields = @($line -split '\s{2,}' | Where-Object { $_ })
            if ($fields.Count -lt 2) { continue }
            $name = $fields[0].Trim()
            $id = $fields[1].Trim()
            $version = ''
            if ($fields.Count -ge 3) { $version = $fields[2].Trim() }
        }

        if (-not $name -or -not $id) { continue }
        $rows.Add([ordered]@{ name = $name; id = $id; version = $version })
    }
    return $rows.ToArray()
}

function Add-WingetEntries {
    param([Parameter(Mandatory = $true)][hashtable]$Store)

    $winget = Get-CommandPath -Name 'winget'
    if (-not $winget) { return }

    # winget speaks UTF-8 and Windows PowerShell listens in the OEM code page.
    $previousEncoding = $null
    try {
        $previousEncoding = [Console]::OutputEncoding
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    } catch {
        $previousEncoding = $null
    }

    $rows = @()
    try {
        $raw = Invoke-Native -FilePath $winget -ArgumentList @('list', '--source', 'winget', '--accept-source-agreements', '--disable-interactivity')
        $rows = @(Get-WingetTable -Raw $raw)
        if ($rows.Count -eq 0) {
            # Older winget builds do not know --disable-interactivity.
            $raw = Invoke-Native -FilePath $winget -ArgumentList @('list', '--source', 'winget')
            $rows = @(Get-WingetTable -Raw $raw)
        }
    } catch {
        Add-HopWarning ('software: winget list failed ({0})' -f $_.Exception.Message)
    } finally {
        if ($null -ne $previousEncoding) {
            try {
                [Console]::OutputEncoding = $previousEncoding
            } catch {
                Write-Verbose 'software: the console output encoding could not be restored after winget.'
            }
        }
    }

    foreach ($row in $rows) {
        Add-SoftwareEntry -Store $Store -Name ([string]$row['name']) -Source 'winget' `
            -Version ([string]$row['version']) -WingetId ([string]$row['id'])
    }
}

function Add-StoreAppEntries {
    param([Parameter(Mandatory = $true)][hashtable]$Store)

    if (-not (Test-CmdletExists -Name 'Get-AppxPackage')) { return }
    $packages = @()
    try {
        $packages = @(Get-AppxPackage -ErrorAction Stop)
    } catch {
        Add-HopWarning ('software: Store apps could not be listed ({0})' -f $_.Exception.Message)
        return
    }

    # Frameworks, resource packages and the shell's own bits are not software
    # the user chose to install.
    $noise = '^(Microsoft\.Windows\.|Microsoft\.UI\.|Microsoft\.VCLibs|Microsoft\.NET\.|Microsoft\.Services\.|Microsoft\.AAD|Microsoft\.Async|Microsoft\.Advertising|Microsoft\.Credential|Microsoft\.ECApp|Microsoft\.LockApp|Microsoft\.MicrosoftEdgeDevToolsClient|Microsoft\.Win32WebViewHost|Microsoft\.XboxGameCallableUI|Microsoft\.AccountsControl|Windows\.|windows\.|c5e2524a|E2A4F912|F46D4000|1527c705|InputApp|NcsiUwpApp)'

    foreach ($package in $packages) {
        $name = [string](Get-Prop -Object $package -Name 'Name' -Default '')
        if (-not $name) { continue }
        if ([bool](Get-Prop -Object $package -Name 'IsFramework' -Default $false)) { continue }
        if ([bool](Get-Prop -Object $package -Name 'IsResourcePackage' -Default $false)) { continue }
        if ([string](Get-Prop -Object $package -Name 'SignatureKind' -Default '') -eq 'System') { continue }
        if ($name -match $noise) { continue }

        $publisher = [string](Get-Prop -Object $package -Name 'Publisher' -Default '')
        if ($publisher -match 'CN=([^,]+)') { $publisher = $Matches[1].Trim() }

        Add-SoftwareEntry -Store $Store -Name $name -Source 'store' `
            -Version ([string](Get-Prop -Object $package -Name 'Version' -Default '')) `
            -Publisher $publisher `
            -InstallLocation ([string](Get-Prop -Object $package -Name 'InstallLocation' -Default ''))
    }
}

function Add-ChocoEntries {
    param([Parameter(Mandatory = $true)][hashtable]$Store)

    $choco = Get-CommandPath -Name 'choco'
    if (-not $choco) { return }

    # -r gives "name|version" per line and no banner.
    $lines = @(Invoke-Native -FilePath $choco -ArgumentList @('list', '--local-only', '-r', '--no-color'))
    $parsed = @($lines | Where-Object { $_ -match '^[^|]+\|[^|]+$' })
    if ($parsed.Count -eq 0) {
        # Chocolatey 2.x removed --local-only; plain `list` is already local.
        $lines = @(Invoke-Native -FilePath $choco -ArgumentList @('list', '-r', '--no-color'))
        $parsed = @($lines | Where-Object { $_ -match '^[^|]+\|[^|]+$' })
    }

    foreach ($line in $parsed) {
        $fields = @([string]$line -split '\|')
        if ($fields.Count -lt 2) { continue }
        Add-SoftwareEntry -Store $Store -Name $fields[0].Trim() -Source 'choco' -Version $fields[1].Trim()
    }
}

function Add-ScoopEntries {
    param([Parameter(Mandatory = $true)][hashtable]$Store)

    $found = $false
    $scoop = Get-CommandPath -Name 'scoop'
    if ($scoop) {
        foreach ($raw in @(Invoke-Native -FilePath $scoop -ArgumentList @('list'))) {
            $line = ([string]$raw).Trim()
            if (-not $line) { continue }
            if ($line -match '^-+$') { continue }
            if ($line -match '^(Name|Installed apps)') { continue }
            $fields = @($line -split '\s{2,}' | Where-Object { $_ })
            if ($fields.Count -lt 2) { continue }
            Add-SoftwareEntry -Store $Store -Name $fields[0].Trim() -Source 'scoop' -Version $fields[1].Trim()
            $found = $true
        }
    }
    if ($found) { return }

    # scoop list is a PowerShell function in some setups and prints nothing
    # useful through a shim - read the install tree instead.
    $root = [string]$env:SCOOP
    if (-not $root) { $root = Join-Path ([string]$env:USERPROFILE) 'scoop' }
    $apps = Join-Path $root 'apps'
    if (-not (Test-Path -LiteralPath $apps)) { return }
    foreach ($app in @(Get-ChildItem -LiteralPath $apps -Directory -ErrorAction SilentlyContinue)) {
        $version = ''
        $manifest = Join-Path (Join-Path $app.FullName 'current') 'manifest.json'
        if (Test-Path -LiteralPath $manifest) {
            try {
                $json = ConvertFrom-Json -InputObject (Read-TextFile -Path $manifest)
                $version = [string](Get-Prop -Object $json -Name 'version' -Default '')
            } catch {
                $version = ''
            }
        }
        Add-SoftwareEntry -Store $Store -Name $app.Name -Source 'scoop' -Version $version
    }
}

function Get-SoftwareInventory {
    [OutputType([object[]])]
    param()

    $store = New-SoftwareStore
    Invoke-Collector -Name 'software/registry' -Body { Add-RegistryUninstallEntries -Store $store } | Out-Null
    Invoke-Collector -Name 'software/winget'   -Body { Add-WingetEntries -Store $store } | Out-Null
    Invoke-Collector -Name 'software/store'    -Body { Add-StoreAppEntries -Store $store } | Out-Null
    Invoke-Collector -Name 'software/choco'    -Body { Add-ChocoEntries -Store $store } | Out-Null
    Invoke-Collector -Name 'software/scoop'    -Body { Add-ScoopEntries -Store $store } | Out-Null

    $result = New-Object 'System.Collections.Generic.List[object]'
    foreach ($entry in $store.Order) {
        # The List[string] would serialise fine, but a plain array keeps the
        # JSON identical between PowerShell versions.
        $entry['sources'] = @($entry['sources'].ToArray())
        $result.Add($entry)
    }
    return $result.ToArray()
}

# --------------------------------------------------------------------------
# Collector: dev
# --------------------------------------------------------------------------

function Test-SshKeyEncrypted {
    [OutputType([bool])]
    param([Parameter(Mandatory = $true)][string]$Text)

    if ($Text -match 'Proc-Type:\s*4,ENCRYPTED') { return $true }
    if ($Text -match 'BEGIN ENCRYPTED PRIVATE KEY') { return $true }
    if ($Text -notmatch 'BEGIN OPENSSH PRIVATE KEY') { return $false }

    # OpenSSH's own container: the magic "openssh-key-v1\0" (15 bytes) is
    # followed by a 4-byte big-endian length and the cipher name. A cipher of
    # "none" means the key sits on disk without a passphrase.
    try {
        $base64 = ''
        foreach ($line in ($Text -split "`r?`n")) {
            $trimmed = $line.Trim()
            if (-not $trimmed) { continue }
            if ($trimmed.StartsWith('-----')) { continue }
            $base64 += $trimmed
        }
        $bytes = [Convert]::FromBase64String($base64)
        if ($bytes.Length -gt 24) {
            $length = ([int]$bytes[15] -shl 24) -bor ([int]$bytes[16] -shl 16) -bor ([int]$bytes[17] -shl 8) -bor [int]$bytes[18]
            if ($length -gt 0 -and $length -lt 64 -and (19 + $length) -le $bytes.Length) {
                $cipher = [System.Text.Encoding]::ASCII.GetString($bytes, 19, $length)
                return ($cipher -ne 'none')
            }
        }
    } catch {
        Write-Verbose 'ssh: could not decode an OpenSSH key header'
    }
    return $false
}

function Get-SshKeyInfo {
    # Only public key text ever reaches the JSON. The private bytes are copied
    # into the payload, and only under -WithSecrets.
    [OutputType([object[]])]
    param([Parameter(Mandatory = $true)][string]$SshDir)

    $keys = New-Object 'System.Collections.Generic.List[object]'
    if (-not (Test-Path -LiteralPath $SshDir)) { return $keys.ToArray() }

    $ignore = @('known_hosts', 'known_hosts.old', 'config', 'authorized_keys', 'environment', 'agent.env')
    $files = @()
    try {
        $files = @(Get-ChildItem -LiteralPath $SshDir -File -ErrorAction Stop)
    } catch {
        Add-HopWarning ('dev: could not list {0}' -f $SshDir)
        return $keys.ToArray()
    }

    foreach ($file in $files) {
        if ($file.Extension -eq '.pub') { continue }
        if ($ignore -contains $file.Name.ToLowerInvariant()) { continue }
        if ($file.Length -gt 64KB) { continue }

        $text = ''
        try { $text = Read-TextFile -Path $file.FullName } catch { continue }
        if ($text -notmatch '-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----') { continue }

        $publicText = $null
        $publicPath = $file.FullName + '.pub'
        if (Test-Path -LiteralPath $publicPath) {
            try { $publicText = (Read-TextFile -Path $publicPath).Trim() } catch { $publicText = $null }
        }

        $type = 'unknown'
        if ($publicText) {
            $algorithm = @($publicText -split '\s+')[0]
            switch -Regex ($algorithm) {
                '^sk-ssh-ed25519'   { $type = 'ed25519-sk' }
                '^sk-ecdsa'         { $type = 'ecdsa-sk' }
                '^ssh-ed25519'      { $type = 'ed25519' }
                '^ssh-rsa'          { $type = 'rsa' }
                '^ecdsa-sha2-nistp' { $type = 'ecdsa' }
                '^ssh-dss'          { $type = 'dsa' }
            }
        }
        if ($type -eq 'unknown') {
            if ($text -match 'BEGIN RSA PRIVATE KEY') { $type = 'rsa' }
            elseif ($text -match 'BEGIN EC PRIVATE KEY') { $type = 'ecdsa' }
            elseif ($text -match 'BEGIN DSA PRIVATE KEY') { $type = 'dsa' }
            elseif ($text -match 'BEGIN OPENSSH PRIVATE KEY') { $type = 'openssh' }
        }

        $keys.Add([ordered]@{
            file       = $file.Name
            type       = $type
            encrypted  = (Test-SshKeyEncrypted -Text $text)
            public_key = $publicText
        })
    }
    return $keys.ToArray()
}

function Get-RuntimeVersion {
    [OutputType([string])]
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$VersionArgs,
        [Parameter(Mandatory = $true)][string]$Pattern
    )

    $path = Get-CommandPath -Name $Exe
    if (-not $path) { return $null }

    # Windows ships zero-byte "app execution alias" stubs for python/python3
    # that open the Microsoft Store instead of running anything.
    if ($path -like '*\WindowsApps\*') {
        $stub = $null
        try { $stub = Get-Item -LiteralPath $path -ErrorAction Stop } catch { $stub = $null }
        if ($null -eq $stub -or $stub.Length -eq 0) {
            Write-Verbose ('runtimes: {0} is a Store alias stub, ignoring' -f $Exe)
            return $null
        }
    }

    foreach ($line in (Invoke-Native -FilePath $path -ArgumentList $VersionArgs)) {
        if ([string]$line -match $Pattern) { return [string]$Matches[1] }
    }
    return $null
}

function Get-WslDistros {
    [OutputType([object[]])]
    param([Parameter(Mandatory = $true)][string]$WslPath)

    $distros = New-Object 'System.Collections.Generic.List[object]'

    # wsl.exe writes UTF-16LE to the pipe. Without this the text arrives as
    # "U\0b\0u\0n\0t\0u\0" and every parse below quietly returns nothing.
    $previousEncoding = $null
    try {
        $previousEncoding = [Console]::OutputEncoding
        [Console]::OutputEncoding = [System.Text.Encoding]::Unicode
    } catch {
        $previousEncoding = $null
    }

    $lines = @()
    try {
        $lines = @(Invoke-Native -FilePath $WslPath -ArgumentList @('--list', '--verbose'))
    } finally {
        if ($null -ne $previousEncoding) {
            try {
                [Console]::OutputEncoding = $previousEncoding
            } catch {
                Write-Verbose 'dev: the console output encoding could not be restored after wsl.'
            }
        }
    }

    $skippedHeader = $false
    foreach ($raw in $lines) {
        # Belt and braces in case the console encoding switch did not take.
        $line = ([string]$raw) -replace "`0", ''
        if (-not $line.Trim()) { continue }
        if (-not $skippedHeader) { $skippedHeader = $true; continue }   # localised header row

        $isDefault = $line.TrimStart().StartsWith('*')
        $body = $line.TrimStart().TrimStart('*').Trim()
        $fields = @($body -split '\s+' | Where-Object { $_ })
        if ($fields.Count -lt 3) { continue }

        $version = 0
        if (-not [int]::TryParse($fields[2], [ref]$version)) { continue }

        $distros.Add([ordered]@{
            name    = [string]$fields[0]
            version = $version
            default = $isDefault
        })
    }
    return $distros.ToArray()
}

function Get-DevInfo {
    [OutputType([object])]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$DocumentsPath)

    $userHome = [string]$env:USERPROFILE

    # --- git ---------------------------------------------------------------
    $git = [ordered]@{ present = $false; user_name = $null; user_email = $null; default_branch = $null }
    $gitPath = Get-CommandPath -Name 'git'
    if ($gitPath) {
        $git['present'] = $true
        foreach ($setting in @(
            @{ Key = 'user.name';          Field = 'user_name' },
            @{ Key = 'user.email';         Field = 'user_email' },
            @{ Key = 'init.defaultBranch'; Field = 'default_branch' }
        )) {
            $value = @(Invoke-Native -FilePath $gitPath -ArgumentList @('config', '--global', '--get', [string]$setting['Key']))
            if ($value.Count -gt 0) {
                $first = ([string]$value[0]).Trim()
                # `git config --get` on a missing key prints nothing and exits 1.
                if ($first -and $first -notmatch '^(fatal|error|warning):') { $git[[string]$setting['Field']] = $first }
            }
        }
    }

    # --- ssh ---------------------------------------------------------------
    $sshDir = Join-Path $userHome '.ssh'
    $sshKeys = @(Invoke-Collector -Name 'dev/ssh' -Body { Get-SshKeyInfo -SshDir $sshDir } -Fallback @())

    # --- gpg ---------------------------------------------------------------
    $gpg = [ordered]@{ present = $false; key_ids = @() }
    $gpgPath = Get-CommandPath -Name 'gpg'
    if ($gpgPath) {
        $gpg['present'] = $true
        $keyIds = New-Object 'System.Collections.Generic.List[string]'
        # --with-colons is the machine-readable, locale-independent output.
        foreach ($line in (Invoke-Native -FilePath $gpgPath -ArgumentList @('--list-secret-keys', '--with-colons'))) {
            $fields = @([string]$line -split ':')
            if ($fields.Count -gt 4 -and $fields[0] -eq 'sec') {
                $keyId = [string]$fields[4]
                if ($keyId -and -not $keyIds.Contains($keyId)) { $keyIds.Add($keyId) }
            }
        }
        $gpg['key_ids'] = @($keyIds.ToArray())
    }

    # --- wsl ---------------------------------------------------------------
    $wsl = [ordered]@{ present = $false; distros = @() }
    $wslPath = Get-CommandPath -Name 'wsl'
    if ($wslPath) {
        $wsl['present'] = $true
        $wsl['distros'] = @(Invoke-Collector -Name 'dev/wsl' -Body { Get-WslDistros -WslPath $wslPath } -Fallback @())
    }

    # --- vscode ------------------------------------------------------------
    $vscode = [ordered]@{ present = $false; flavor = $null; extensions = @(); settings = $false }
    $flavors = @(
        @{ Exe = 'code';           Flavor = 'code';           UserDir = 'Code' },
        @{ Exe = 'codium';         Flavor = 'codium';         UserDir = 'VSCodium' },
        @{ Exe = 'code-insiders';  Flavor = 'code-insiders';  UserDir = 'Code - Insiders' }
    )
    foreach ($flavor in $flavors) {
        $exePath = Get-CommandPath -Name ([string]$flavor['Exe'])
        if (-not $exePath) { continue }
        $vscode['present'] = $true
        $vscode['flavor'] = [string]$flavor['Flavor']
        $extensions = New-Object 'System.Collections.Generic.List[string]'
        foreach ($line in (Invoke-Native -FilePath $exePath -ArgumentList @('--list-extensions'))) {
            $extension = ([string]$line).Trim()
            # Extension ids are always publisher.name with no spaces.
            if ($extension -match '^[A-Za-z0-9][A-Za-z0-9_.-]*\.[A-Za-z0-9][A-Za-z0-9_.-]*$') { $extensions.Add($extension) }
        }
        $vscode['extensions'] = @($extensions.ToArray())
        $settingsPath = Join-Path (Join-Path ([string]$env:APPDATA) ([string]$flavor['UserDir'])) 'User\settings.json'
        $vscode['settings'] = (Test-Path -LiteralPath $settingsPath)
        break
    }

    # --- runtimes ----------------------------------------------------------
    $runtimes = [ordered]@{
        node   = (Get-RuntimeVersion -Exe 'node'   -VersionArgs @('-v')        -Pattern 'v?(\d+\.\d+\.\d+)')
        python = (Get-RuntimeVersion -Exe 'python' -VersionArgs @('--version') -Pattern 'Python\s+([\d\.]+)')
        dotnet = (Get-RuntimeVersion -Exe 'dotnet' -VersionArgs @('--version') -Pattern '^\s*(\d+[\w\.\-]*)\s*$')
        java   = (Get-RuntimeVersion -Exe 'java'   -VersionArgs @('-version')  -Pattern 'version\s+"([^"]+)"')
        go     = (Get-RuntimeVersion -Exe 'go'     -VersionArgs @('version')   -Pattern 'go version go(\S+)')
        rustup = (Get-RuntimeVersion -Exe 'rustup' -VersionArgs @('--version') -Pattern 'rustup\s+(\d[\S]*)')
    }

    # --- shell -------------------------------------------------------------
    $documents = $DocumentsPath
    if (-not $documents) { $documents = Join-Path $userHome 'Documents' }
    $profileCandidates = New-Object 'System.Collections.Generic.List[string]'
    $profileCandidates.Add((Join-Path $documents 'WindowsPowerShell\Microsoft.PowerShell_profile.ps1'))
    $profileCandidates.Add((Join-Path $documents 'WindowsPowerShell\profile.ps1'))
    $profileCandidates.Add((Join-Path $documents 'PowerShell\Microsoft.PowerShell_profile.ps1'))
    $profileCandidates.Add((Join-Path $documents 'PowerShell\profile.ps1'))
    try {
        $profileVariable = Get-Variable -Name 'PROFILE' -ErrorAction SilentlyContinue
        if ($null -ne $profileVariable) {
            foreach ($candidate in @($profileVariable.Value.CurrentUserCurrentHost, $profileVariable.Value.CurrentUserAllHosts)) {
                if ($candidate -and -not $profileCandidates.Contains([string]$candidate)) { $profileCandidates.Add([string]$candidate) }
            }
        }
    } catch {
        Write-Verbose 'shell: $PROFILE is not available in this host'
    }
    $profileFound = $false
    foreach ($candidate in $profileCandidates) {
        if (Test-Path -LiteralPath $candidate) { $profileFound = $true; break }
    }

    $terminalSettings = @(
        (Join-Path ([string]$env:LOCALAPPDATA) 'Packages\Microsoft.WindowsTerminal_8wekyb3d8bbwe\LocalState\settings.json'),
        (Join-Path ([string]$env:LOCALAPPDATA) 'Packages\Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe\LocalState\settings.json'),
        (Join-Path ([string]$env:LOCALAPPDATA) 'Microsoft\Windows Terminal\settings.json')
    )
    $terminalFound = $false
    foreach ($candidate in $terminalSettings) {
        if (Test-Path -LiteralPath $candidate) { $terminalFound = $true; break }
    }

    return [ordered]@{
        git      = $git
        ssh_keys = @($sshKeys)
        gpg      = $gpg
        wsl      = $wsl
        vscode   = $vscode
        runtimes = $runtimes
        shell    = [ordered]@{
            powershell_profile = $profileFound
            windows_terminal   = $terminalFound
        }
    }
}

# --------------------------------------------------------------------------
# Collector: browsers
# --------------------------------------------------------------------------

function Get-BrowserDefinition {
    # id / display name / where the profile root lives / which engine.
    [OutputType([object[]])]
    param()

    $local = [string]$env:LOCALAPPDATA
    $roaming = [string]$env:APPDATA

    return @(
        [ordered]@{ id = 'chrome';  name = 'Google Chrome';  engine = 'chromium'; root = (Join-Path $local 'Google\Chrome\User Data');            progid = 'ChromeHTML' },
        [ordered]@{ id = 'edge';    name = 'Microsoft Edge'; engine = 'chromium'; root = (Join-Path $local 'Microsoft\Edge\User Data');           progid = 'MSEdgeHTM' },
        [ordered]@{ id = 'brave';   name = 'Brave';          engine = 'chromium'; root = (Join-Path $local 'BraveSoftware\Brave-Browser\User Data'); progid = 'BraveHTML' },
        [ordered]@{ id = 'vivaldi'; name = 'Vivaldi';        engine = 'chromium'; root = (Join-Path $local 'Vivaldi\User Data');                  progid = 'VivaldiHTM' },
        [ordered]@{ id = 'opera';   name = 'Opera';          engine = 'chromium'; root = (Join-Path $roaming 'Opera Software\Opera Stable');      progid = 'OperaStable' },
        [ordered]@{ id = 'opera';   name = 'Opera GX';       engine = 'chromium'; root = (Join-Path $roaming 'Opera Software\Opera GX Stable');   progid = 'OperaGXStable' },
        [ordered]@{ id = 'firefox'; name = 'Mozilla Firefox'; engine = 'gecko';   root = (Join-Path $roaming 'Mozilla\Firefox');                  progid = 'FirefoxURL' }
    )
}

function Get-DefaultBrowserProgId {
    [OutputType([string])]
    param()

    return [string](Get-RegValue `
        -Path 'HKCU:\SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice' `
        -Name 'ProgId' -Default '')
}

function Write-BookmarkTree {
    # Walks a Chromium bookmark node. Returns the number of URLs below it and,
    # when $Builder is given, appends Netscape bookmark HTML on the way.
    [OutputType([int])]
    param(
        [AllowNull()]$Node,
        [AllowNull()]$Builder,
        [int]$Indent = 1
    )

    if ($null -eq $Node) { return 0 }
    $count = 0
    $type = [string](Get-Prop -Object $Node -Name 'type' -Default '')
    $name = [string](Get-Prop -Object $Node -Name 'name' -Default '')
    $pad = ' ' * (4 * $Indent)

    if ($type -eq 'url') {
        $url = [string](Get-Prop -Object $Node -Name 'url' -Default '')
        if (-not $url) { return 0 }
        if ($null -ne $Builder) {
            $added = ''
            $rawDate = Get-Prop -Object $Node -Name 'date_added'
            if ($rawDate) {
                $unix = ConvertFrom-ChromeTime -Value ([string]$rawDate)
                if ($unix) { $added = (' ADD_DATE="{0}"' -f $unix) }
            }
            [void]$Builder.AppendLine(('{0}<DT><A HREF="{1}"{2}>{3}</A>' -f $pad, (ConvertTo-HtmlText -Text $url), $added, (ConvertTo-HtmlText -Text $name)))
        }
        return 1
    }

    $isFolder = ($type -eq 'folder')
    if ($null -ne $Builder -and $isFolder) {
        [void]$Builder.AppendLine(('{0}<DT><H3>{1}</H3>' -f $pad, (ConvertTo-HtmlText -Text $name)))
        [void]$Builder.AppendLine(('{0}<DL><p>' -f $pad))
    }
    foreach ($child in @(Get-Prop -Object $Node -Name 'children' -Default @())) {
        $count += (Write-BookmarkTree -Node $child -Builder $Builder -Indent ($Indent + 1))
    }
    if ($null -ne $Builder -and $isFolder) {
        [void]$Builder.AppendLine(('{0}</DL><p>' -f $pad))
    }
    return $count
}

function ConvertFrom-ChromeTime {
    # Chromium timestamps are microseconds since 1601-01-01 UTC.
    [OutputType([string])]
    param([AllowNull()][string]$Value)

    if (-not $Value) { return $null }
    try {
        $micro = [int64]$Value
        if ($micro -le 0) { return $null }
        $seconds = [int64][math]::Floor($micro / 1000000) - 11644473600
        if ($seconds -le 0) { return $null }
        return [string]$seconds
    } catch {
        return $null
    }
}

function Read-ChromiumBookmarks {
    # Returns @{ count = <int>; html = <string|null> } for one profile.
    [OutputType([hashtable])]
    param(
        [Parameter(Mandatory = $true)][string]$ProfilePath,
        [switch]$AsHtml
    )

    $result = @{ count = $null; html = $null }
    $file = Join-Path $ProfilePath 'Bookmarks'
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { return $result }

    $json = $null
    try {
        $json = ConvertFrom-Json -InputObject (Read-TextFile -Path $file)
    } catch {
        Add-HopWarning ('browsers: could not parse {0}' -f $file)
        return $result
    }

    $roots = Get-Prop -Object $json -Name 'roots'
    if ($null -eq $roots) { return $result }

    $builder = $null
    if ($AsHtml) {
        $builder = New-Object System.Text.StringBuilder
        [void]$builder.AppendLine('<!DOCTYPE NETSCAPE-Bookmark-file-1>')
        [void]$builder.AppendLine('<!-- exported by hop-scan.ps1 -->')
        [void]$builder.AppendLine('<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">')
        [void]$builder.AppendLine('<TITLE>Bookmarks</TITLE>')
        [void]$builder.AppendLine('<H1>Bookmarks</H1>')
        [void]$builder.AppendLine('<DL><p>')
    }

    $total = 0
    foreach ($property in @($roots.PSObject.Properties)) {
        $node = $property.Value
        # roots also holds scalars such as sync_transaction_version.
        if ($null -eq $node) { continue }
        if ($node -is [string] -or $node -is [int] -or $node -is [int64]) { continue }
        if ($null -eq (Get-Prop -Object $node -Name 'children')) { continue }
        $total += (Write-BookmarkTree -Node $node -Builder $builder -Indent 1)
    }

    if ($AsHtml -and $null -ne $builder) {
        [void]$builder.AppendLine('</DL><p>')
        $result['html'] = $builder.ToString()
    }
    $result['count'] = $total
    return $result
}

function Get-ChromiumProfiles {
    [OutputType([object[]])]
    param([Parameter(Mandatory = $true)][string]$Root)

    $profiles = New-Object 'System.Collections.Generic.List[object]'
    $localState = Join-Path $Root 'Local State'
    if (Test-Path -LiteralPath $localState -PathType Leaf) {
        try {
            $state = ConvertFrom-Json -InputObject (Read-TextFile -Path $localState)
            # profile.info_cache maps the on-disk directory name to metadata,
            # including the display name the user actually sees.
            $cache = Get-Prop -Object (Get-Prop -Object $state -Name 'profile') -Name 'info_cache'
            if ($null -ne $cache) {
                foreach ($property in @($cache.PSObject.Properties)) {
                    $directory = [string]$property.Name
                    $path = Join-Path $Root $directory
                    if (-not (Test-Path -LiteralPath $path)) { continue }
                    $display = [string](Get-Prop -Object $property.Value -Name 'name' -Default $directory)
                    $profiles.Add([ordered]@{ name = $display; path = $path })
                }
            }
        } catch {
            Add-HopWarning ('browsers: could not read {0}' -f $localState)
        }
    }

    if ($profiles.Count -eq 0) {
        # Opera and friends keep a single profile in the root itself.
        if (Test-Path -LiteralPath (Join-Path $Root 'Preferences')) {
            $profiles.Add([ordered]@{ name = 'Default'; path = $Root })
        } elseif (Test-Path -LiteralPath (Join-Path $Root 'Default')) {
            $profiles.Add([ordered]@{ name = 'Default'; path = (Join-Path $Root 'Default') })
        }
    }
    return $profiles.ToArray()
}

function Get-FirefoxProfiles {
    [OutputType([object[]])]
    param([Parameter(Mandatory = $true)][string]$Root)

    $profiles = New-Object 'System.Collections.Generic.List[object]'
    $ini = Join-Path $Root 'profiles.ini'
    if (-not (Test-Path -LiteralPath $ini -PathType Leaf)) { return $profiles.ToArray() }

    $text = ''
    try { $text = Read-TextFile -Path $ini } catch { return $profiles.ToArray() }

    $section = ''
    $name = ''
    $path = ''
    $isRelative = $true

    # A tiny INI reader: profiles.ini has [ProfileN] blocks with Name, Path and
    # IsRelative, and [Install...] / [General] blocks we ignore.
    $flush = {
        if ($section -match '^Profile\d+$' -and $path) {
            $full = $path -replace '/', '\'
            if ($isRelative) { $full = Join-Path $Root $full }
            $label = $name
            if (-not $label) { $label = $section }
            $profiles.Add([ordered]@{ name = $label; path = $full })
        }
    }

    foreach ($rawLine in ($text -split "`r?`n")) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith(';') -or $line.StartsWith('#')) { continue }
        if ($line -match '^\[(.+)\]$') {
            # Capture the group before $flush runs: the scriptblock does its own
            # -match and there is no point in relying on which scope $Matches
            # lands in.
            $nextSection = $Matches[1]
            & $flush
            $section = $nextSection
            $name = ''
            $path = ''
            $isRelative = $true
            continue
        }
        $separator = $line.IndexOf('=')
        if ($separator -lt 1) { continue }
        $key = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1).Trim()
        switch ($key.ToLowerInvariant()) {
            'name'       { $name = $value }
            'path'       { $path = $value }
            'isrelative' { $isRelative = ($value -eq '1') }
        }
    }
    & $flush

    return $profiles.ToArray()
}

function Get-BrowserInventory {
    [OutputType([object[]])]
    param()

    $browsers = New-Object 'System.Collections.Generic.List[object]'
    $defaultProgId = Get-DefaultBrowserProgId

    foreach ($definition in (Get-BrowserDefinition)) {
        $root = [string]$definition['root']
        if (-not $root -or -not (Test-Path -LiteralPath $root)) { continue }

        $engine = [string]$definition['engine']
        $profiles = @()
        if ($engine -eq 'gecko') {
            $profiles = @(Invoke-Collector -Name ('browsers/{0}' -f $definition['name']) -Body { Get-FirefoxProfiles -Root $root } -Fallback @())
        } else {
            $profiles = @(Invoke-Collector -Name ('browsers/{0}' -f $definition['name']) -Body { Get-ChromiumProfiles -Root $root } -Fallback @())
        }
        if ($profiles.Count -eq 0) { continue }

        # Firefox keeps bookmarks in places.sqlite; opening a live SQLite file
        # from PowerShell is not worth the trouble, so the count stays null and
        # the payload carries the latest JSONLZ4 backup instead.
        $bookmarkCount = $null
        if ($engine -eq 'chromium') {
            $bookmarkCount = 0
            foreach ($browserProfile in $profiles) {
                $bookmarks = Read-ChromiumBookmarks -ProfilePath ([string]$browserProfile['path'])
                $profileCount = Get-Prop -Object $bookmarks -Name 'count' -Default 0
                $bookmarkCount += [int]$profileCount
            }
        }

        $isDefault = $false
        if ($defaultProgId -and $defaultProgId -like ([string]$definition['progid'] + '*')) { $isDefault = $true }

        $browsers.Add([ordered]@{
            id             = [string]$definition['id']
            name           = [string]$definition['name']
            default        = $isDefault
            profiles       = @($profiles)
            bookmark_count = $bookmarkCount
        })
    }
    return $browsers.ToArray()
}

# --------------------------------------------------------------------------
# Collector: network
# --------------------------------------------------------------------------

function Get-XmlText {
    # Wi-Fi profile XML carries a default namespace, and the values we want sit
    # at different depths depending on the security type. local-name() sidesteps
    # both problems.
    [OutputType([string])]
    param(
        [Parameter(Mandatory = $true)][System.Xml.XmlDocument]$Xml,
        [Parameter(Mandatory = $true)][string]$LocalName
    )

    try {
        $node = $Xml.SelectSingleNode(('//*[local-name()="{0}"]' -f $LocalName))
        if ($null -eq $node) { return '' }
        return ([string]$node.InnerText).Trim()
    } catch {
        return ''
    }
}

function Get-WifiProfileNames {
    [OutputType([string[]])]
    param([Parameter(Mandatory = $true)][string]$NetshPath)

    $names = New-Object 'System.Collections.Generic.List[string]'
    foreach ($raw in (Invoke-Native -FilePath $NetshPath -ArgumentList @('wlan', 'show', 'profiles'))) {
        $line = [string]$raw
        # Localisation-tolerant: profile lines are the indented ones with a
        # colon and something after it, whatever the label says.
        if ($line -notmatch '^\s{2,}\S') { continue }
        $colon = $line.IndexOf(':')
        if ($colon -lt 0) { continue }
        $ssid = $line.Substring($colon + 1).Trim()
        if (-not $ssid) { continue }
        if (-not $names.Contains($ssid)) { $names.Add($ssid) }
    }
    return $names.ToArray()
}

function Get-NetworkInfo {
    # Also responsible for the Wi-Fi part of the payload: the exported XML is
    # both the most reliable source for the (unlocalised) authentication type
    # and the thing worth carrying over, so it is exported exactly once.
    [OutputType([object])]
    param()

    $wifi = New-Object 'System.Collections.Generic.List[object]'
    $netsh = Get-CommandPath -Name 'netsh'
    $names = @()
    if ($netsh) {
        $names = @(Invoke-Collector -Name 'network/wifi-list' -Body { Get-WifiProfileNames -NetshPath $netsh } -Fallback @())
    }

    # Export even when the profile list came back empty: the list parse is the
    # fragile part, the export is not.
    if ($netsh) {
        $exportDir = $null
        $exportIsPayload = $false
        if ($script:PayloadEnabled -and $script:PayloadRoot) {
            $exportDir = Join-Path $script:PayloadRoot 'wifi'
            $exportIsPayload = $true
        } else {
            # Nowhere to keep them: this export is a scratch copy, read for the
            # authentication type and deleted at the end of the block.
            $exportDir = Join-Path ([string]$env:TEMP) ('hop-wifi-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
        }

        # key=clear writes every Wi-Fi password to disk in plain text. Ask for
        # that only when the files are going to survive in the payload the user
        # asked for - under -NoPayload it would put plaintext passwords into
        # %TEMP% in exchange for nothing.
        $wantKeys = ($script:WantSecrets -and $script:IsElevated -and $exportIsPayload)
        if ($script:WantSecrets -and $exportIsPayload -and -not $script:IsElevated) {
            Add-HopWarning 'Wi-Fi passwords need an elevated shell: profiles were exported without their keys.'
        }

        $exported = @()
        try {
            New-Item -ItemType Directory -Path $exportDir -Force | Out-Null
            # netsh wants name="x"; PowerShell adds the quotes itself when the
            # argument contains spaces, so pass the bare key=value form.
            $exportArgs = New-Object 'System.Collections.Generic.List[string]'
            $exportArgs.Add('wlan')
            $exportArgs.Add('export')
            $exportArgs.Add('profile')
            $exportArgs.Add(('folder={0}' -f $exportDir))
            if ($wantKeys) { $exportArgs.Add('key=clear') }
            Invoke-Native -FilePath $netsh -ArgumentList $exportArgs.ToArray() | Out-Null
            $exported = @(Get-ChildItem -LiteralPath $exportDir -Filter '*.xml' -File -ErrorAction SilentlyContinue)
        } catch {
            Add-HopWarning ('network: Wi-Fi profiles could not be exported ({0})' -f $_.Exception.Message)
        }

        $seen = New-Object 'System.Collections.Generic.List[string]'
        foreach ($file in $exported) {
            $document = New-Object System.Xml.XmlDocument
            try {
                $document.Load($file.FullName)
            } catch {
                Write-Verbose ('network: unreadable profile XML {0}' -f $file.Name)
                continue
            }

            $ssid = Get-XmlText -Xml $document -LocalName 'name'
            $auth = Get-XmlText -Xml $document -LocalName 'authentication'
            $keyMaterial = Get-XmlText -Xml $document -LocalName 'keyMaterial'
            $protected = Get-XmlText -Xml $document -LocalName 'protected'
            # With key=clear the key is in plain text and <protected> is false.
            $hasSecret = ($wantKeys -and $keyMaterial -and ($protected -eq 'false'))

            if ($ssid) { $seen.Add($ssid) }
            $wifi.Add([ordered]@{
                ssid       = $ssid
                auth       = $auth
                has_secret = [bool]$hasSecret
            })

            if ($exportIsPayload) {
                $mode = '0644'
                if ($hasSecret) { $mode = '0600' }
                Add-PayloadEntry -Kind 'wifi' -RelativePath ('wifi/' + $file.Name) -RestoreTo $null -Mode $mode
            }
        }

        # Anything netsh listed but did not export (group policy profiles, or
        # an export that failed) still belongs in the manifest.
        foreach ($ssid in $names) {
            if ($seen.Contains($ssid)) { continue }
            $wifi.Add([ordered]@{ ssid = $ssid; auth = $null; has_secret = $false })
        }

        # Do not leave an empty wifi/ folder behind in either location.
        $keepDir = ($exportIsPayload -and $exported.Count -gt 0)
        if (-not $keepDir -and (Test-Path -LiteralPath $exportDir)) {
            try {
                Remove-Item -LiteralPath $exportDir -Recurse -Force -ErrorAction Stop
            } catch {
                Write-Verbose ('network: could not remove the export directory {0}' -f $exportDir)
            }
        }
    }

    # Hosts file: count the real entries, not the comment header Windows ships.
    $hostsEntries = $null
    $hostsPath = Join-Path ([string]$env:SystemRoot) 'System32\drivers\etc\hosts'
    try {
        if (Test-Path -LiteralPath $hostsPath -PathType Leaf) {
            $count = 0
            foreach ($line in ((Read-TextFile -Path $hostsPath) -split "`r?`n")) {
                $trimmed = $line.Trim()
                if (-not $trimmed) { continue }
                if ($trimmed.StartsWith('#')) { continue }
                $count++
            }
            $hostsEntries = $count
        }
    } catch {
        Add-HopWarning 'network: the hosts file could not be read.'
    }

    return [ordered]@{
        hostname      = [string]$env:COMPUTERNAME
        wifi_profiles = @($wifi.ToArray())
        hosts_entries = $hostsEntries
    }
}

# --------------------------------------------------------------------------
# Collector: gaming
# --------------------------------------------------------------------------

function Expand-VdfString {
    [OutputType([string])]
    param([AllowNull()][string]$Text)

    if (-not $Text) { return '' }
    if (-not $Text.Contains('\')) { return $Text }
    $builder = New-Object System.Text.StringBuilder
    for ($i = 0; $i -lt $Text.Length; $i++) {
        $char = $Text[$i]
        if ($char -eq '\' -and ($i + 1) -lt $Text.Length) {
            $i++
            $next = $Text[$i]
            switch ($next) {
                'n'     { [void]$builder.Append("`n") }
                't'     { [void]$builder.Append("`t") }
                default { [void]$builder.Append($next) }
            }
        } else {
            [void]$builder.Append($char)
        }
    }
    return $builder.ToString()
}

function ConvertFrom-Vdf {
    # Valve's KeyValues format, just enough of it for libraryfolders.vdf and
    # appmanifest_*.acf: quoted key/value pairs, nested braces, // comments.
    # Nested hashtables are case-insensitive, which is what we want because
    # Valve is not consistent about "AppState" vs "appstate".
    [OutputType([hashtable])]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)

    $root = @{}
    $stack = New-Object 'System.Collections.Generic.Stack[object]'
    $stack.Push($root)
    $pendingKey = $null

    foreach ($rawLine in ($Text -split "`r?`n")) {
        $line = $rawLine.Trim()
        if (-not $line) { continue }
        if ($line.StartsWith('//')) { continue }

        # "key" { on one line
        if ($line -match '^"((?:[^"\\]|\\.)*)"\s*\{\s*$') {
            $pendingKey = Expand-VdfString -Text $Matches[1]
            $line = '{'
        }

        if ($line.StartsWith('{')) {
            $parent = $stack.Peek()
            $key = $pendingKey
            if ($null -eq $key) { $key = ('__block{0}' -f $parent.Count) }
            $child = @{}
            $parent[$key] = $child
            $stack.Push($child)
            $pendingKey = $null
            continue
        }
        if ($line.StartsWith('}')) {
            if ($stack.Count -gt 1) { [void]$stack.Pop() }
            $pendingKey = $null
            continue
        }
        if ($line -match '^"((?:[^"\\]|\\.)*)"\s*"((?:[^"\\]|\\.)*)"') {
            $node = $stack.Peek()
            $node[(Expand-VdfString -Text $Matches[1])] = (Expand-VdfString -Text $Matches[2])
            $pendingKey = $null
            continue
        }
        if ($line -match '^"((?:[^"\\]|\\.)*)"\s*$') {
            $pendingKey = Expand-VdfString -Text $Matches[1]
            continue
        }
        # Unquoted tokens turn up in hand-edited files.
        $tokens = @($line -split '\s+', 2)
        if ($tokens.Count -eq 2) {
            $node = $stack.Peek()
            $node[$tokens[0].Trim('"')] = $tokens[1].Trim().Trim('"')
        } elseif ($tokens.Count -eq 1) {
            $pendingKey = $tokens[0].Trim('"')
        }
    }
    return $root
}

function Get-SteamInfo {
    [OutputType([object])]
    param()

    $steam = [ordered]@{ present = $false; libraries = @(); games = @() }

    $steamPath = [string](Get-RegValue -Path 'HKCU:\Software\Valve\Steam' -Name 'SteamPath' -Default '')
    if (-not $steamPath) {
        $steamPath = [string](Get-RegValue -Path 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam' -Name 'InstallPath' -Default '')
    }
    if (-not $steamPath) {
        $steamPath = [string](Get-RegValue -Path 'HKLM:\SOFTWARE\Valve\Steam' -Name 'InstallPath' -Default '')
    }
    if (-not $steamPath) { return $steam }
    $steamPath = $steamPath -replace '/', '\'
    if (-not (Test-Path -LiteralPath $steamPath)) { return $steam }
    $steam['present'] = $true

    $libraries = New-Object 'System.Collections.Generic.List[string]'
    $primary = Join-Path $steamPath 'steamapps'
    if (Test-Path -LiteralPath $primary) { $libraries.Add($primary) }

    $libraryFile = Join-Path $primary 'libraryfolders.vdf'
    if (Test-Path -LiteralPath $libraryFile -PathType Leaf) {
        try {
            $vdf = ConvertFrom-Vdf -Text (Read-TextFile -Path $libraryFile)
            $folders = $null
            if ($vdf.ContainsKey('libraryfolders')) { $folders = $vdf['libraryfolders'] }
            elseif ($vdf.ContainsKey('LibraryFolders')) { $folders = $vdf['LibraryFolders'] }
            if ($folders -is [System.Collections.IDictionary]) {
                foreach ($key in @($folders.Keys)) {
                    $value = $folders[$key]
                    $path = ''
                    if ($value -is [System.Collections.IDictionary]) {
                        # Current format: numbered blocks with a "path" key.
                        if ($value.Contains('path')) { $path = [string]$value['path'] }
                    } elseif ($key -match '^\d+$') {
                        # Pre-2021 format: "1" "D:\\SteamLibrary".
                        $path = [string]$value
                    }
                    if (-not $path) { continue }
                    $apps = Join-Path ($path -replace '/', '\') 'steamapps'
                    if ((Test-Path -LiteralPath $apps) -and -not $libraries.Contains($apps)) { $libraries.Add($apps) }
                }
            }
        } catch {
            Add-HopWarning 'gaming: libraryfolders.vdf could not be parsed - extra Steam libraries may be missing.'
        }
    }

    $games = New-Object 'System.Collections.Generic.List[object]'
    foreach ($library in $libraries) {
        $manifests = @()
        try {
            $manifests = @(Get-ChildItem -LiteralPath $library -Filter 'appmanifest_*.acf' -File -ErrorAction Stop)
        } catch {
            continue
        }
        foreach ($manifest in $manifests) {
            try {
                $vdf = ConvertFrom-Vdf -Text (Read-TextFile -Path $manifest.FullName)
                if (-not $vdf.ContainsKey('AppState')) { continue }
                $state = $vdf['AppState']
                if (-not ($state -is [System.Collections.IDictionary])) { continue }

                $appid = 0
                if ($state.Contains('appid')) { [void][int]::TryParse([string]$state['appid'], [ref]$appid) }
                $size = [int64]0
                if ($state.Contains('SizeOnDisk')) {
                    try { $size = [int64][string]$state['SizeOnDisk'] } catch { $size = [int64]0 }
                }
                $name = ''
                if ($state.Contains('name')) { $name = [string]$state['name'] }
                if (-not $name) { continue }

                $games.Add([ordered]@{ appid = $appid; name = $name; size_bytes = $size })
            } catch {
                Write-Verbose ('gaming: unreadable manifest {0}' -f $manifest.Name)
            }
        }
    }

    $steam['libraries'] = @($libraries.ToArray())
    $steam['games'] = @($games.ToArray() | Sort-Object -Property { [string]$_['name'] })
    return $steam
}

function Get-EpicInfo {
    [OutputType([object])]
    param()

    $epic = [ordered]@{ present = $false; games = @() }
    $manifestDir = Join-Path ([string]$env:ProgramData) 'Epic\EpicGamesLauncher\Data\Manifests'
    if (-not (Test-Path -LiteralPath $manifestDir)) { return $epic }
    $epic['present'] = $true

    $games = New-Object 'System.Collections.Generic.List[object]'
    $items = @()
    try { $items = @(Get-ChildItem -LiteralPath $manifestDir -Filter '*.item' -File -ErrorAction Stop) } catch { $items = @() }
    foreach ($item in $items) {
        try {
            $json = ConvertFrom-Json -InputObject (Read-TextFile -Path $item.FullName)
            $name = [string](Get-Prop -Object $json -Name 'DisplayName' -Default '')
            if (-not $name) { continue }
            $games.Add([ordered]@{
                appid            = [string](Get-Prop -Object $json -Name 'AppName' -Default '')
                name             = $name
                size_bytes       = [int64](Get-Prop -Object $json -Name 'InstallSize' -Default 0)
                install_location = [string](Get-Prop -Object $json -Name 'InstallLocation' -Default '')
            })
        } catch {
            Write-Verbose ('gaming: unreadable Epic manifest {0}' -f $item.Name)
        }
    }
    $epic['games'] = @($games.ToArray() | Sort-Object -Property { [string]$_['name'] })
    return $epic
}

function Get-GogInfo {
    [OutputType([object])]
    param()

    $gog = [ordered]@{ present = $false; games = @() }
    $roots = @('HKLM:\SOFTWARE\WOW6432Node\GOG.com\Games', 'HKLM:\SOFTWARE\GOG.com\Games')
    $games = New-Object 'System.Collections.Generic.List[object]'

    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $gog['present'] = $true
        $keys = @()
        try { $keys = @(Get-ChildItem -LiteralPath $root -ErrorAction Stop) } catch { continue }
        foreach ($key in $keys) {
            try {
                $name = [string]$key.GetValue('gameName')
                if (-not $name) { continue }
                $games.Add([ordered]@{
                    appid            = [string]$key.GetValue('gameID')
                    name             = $name
                    size_bytes       = 0
                    install_location = [string]$key.GetValue('path')
                })
            } catch {
                Write-Verbose 'gaming: unreadable GOG key'
            }
        }
    }
    $gog['games'] = @($games.ToArray() | Sort-Object -Property { [string]$_['name'] })
    return $gog
}

function Get-GamingInfo {
    [OutputType([object])]
    param()

    return [ordered]@{
        steam = (Invoke-Collector -Name 'gaming/steam' -Body { Get-SteamInfo } -Fallback ([ordered]@{ present = $false; libraries = @(); games = @() }))
        epic  = (Invoke-Collector -Name 'gaming/epic'  -Body { Get-EpicInfo }  -Fallback ([ordered]@{ present = $false; games = @() }))
        gog   = (Invoke-Collector -Name 'gaming/gog'   -Body { Get-GogInfo }   -Fallback ([ordered]@{ present = $false; games = @() }))
    }
}

# --------------------------------------------------------------------------
# Collector: personalization
# --------------------------------------------------------------------------

function Get-UserFontDirectory {
    [OutputType([string])]
    param()
    return (Join-Path ([string]$env:LOCALAPPDATA) 'Microsoft\Windows\Fonts')
}

function Get-PersonalizationInfo {
    [OutputType([object])]
    param()

    $wallpaper = [string](Get-RegValue -Path 'HKCU:\Control Panel\Desktop' -Name 'WallPaper' -Default '')
    if ($wallpaper -and -not (Test-Path -LiteralPath $wallpaper -PathType Leaf)) { $wallpaper = '' }

    $theme = 'unknown'
    $appsLight = Get-RegValue -Path 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize' -Name 'AppsUseLightTheme'
    if ($null -ne $appsLight) {
        if ([int]$appsLight -eq 0) { $theme = 'dark' } else { $theme = 'light' }
    }

    $accent = $null
    $accentRaw = Get-RegValue -Path 'HKCU:\Software\Microsoft\Windows\DWM' -Name 'AccentColor'
    if ($null -ne $accentRaw) {
        try {
            # DWM stores the accent as 0xAABBGGRR - the byte order is the
            # reverse of the one everybody writes HTML colours in.
            $value = [int64]$accentRaw
            if ($value -lt 0) { $value += 4294967296 }
            $red = [int]($value -band 0xFF)
            $green = [int](($value -shr 8) -band 0xFF)
            $blue = [int](($value -shr 16) -band 0xFF)
            $accent = ('#{0:X2}{1:X2}{2:X2}' -f $red, $green, $blue)
        } catch {
            $accent = $null
        }
    }

    $fonts = New-Object 'System.Collections.Generic.List[string]'
    $fontDir = Get-UserFontDirectory
    if (Test-Path -LiteralPath $fontDir) {
        try {
            foreach ($font in @(Get-ChildItem -LiteralPath $fontDir -File -ErrorAction Stop)) {
                if ($font.Extension -match '^\.(ttf|otf|ttc|fon|pfb)$') { $fonts.Add($font.Name) }
            }
        } catch {
            Add-HopWarning 'personalization: the user font folder could not be listed.'
        }
    }

    $result = [ordered]@{
        wallpaper    = $null
        theme        = $theme
        accent_color = $accent
        fonts_user   = @($fonts.ToArray())
    }
    if ($wallpaper) { $result['wallpaper'] = $wallpaper }
    return $result
}

# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------

function Get-ImageExtension {
    # The active wallpaper is usually %APPDATA%\...\Themes\TranscodedWallpaper,
    # which has no extension at all. Sniff the magic bytes instead.
    [OutputType([string])]
    param([Parameter(Mandatory = $true)][string]$Path)

    $buffer = New-Object -TypeName 'byte[]' -ArgumentList 8
    $read = 0
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        try { $read = $stream.Read($buffer, 0, 8) } finally { $stream.Dispose() }
    } catch {
        return ''
    }
    if ($read -ge 3 -and $buffer[0] -eq 0xFF -and $buffer[1] -eq 0xD8) { return '.jpg' }
    if ($read -ge 8 -and $buffer[0] -eq 0x89 -and $buffer[1] -eq 0x50) { return '.png' }
    if ($read -ge 2 -and $buffer[0] -eq 0x42 -and $buffer[1] -eq 0x4D) { return '.bmp' }
    if ($read -ge 4 -and $buffer[0] -eq 0x52 -and $buffer[1] -eq 0x49) { return '.webp' }
    return ''
}

function Export-HopPayload {
    # Copies the things worth carrying over into $script:PayloadRoot and
    # indexes every one of them in payload.entries. Wi-Fi profiles are handled
    # by the network collector, which has to export them anyway.
    # (mandatory is deliberately not used here: an empty browser list or an
    # empty $dev is a perfectly normal machine, and a mandatory parameter
    # refuses to bind an empty collection.)
    param($Dev = $null, $Browsers = @(), $Personalization = $null)

    if (-not $script:PayloadEnabled -or -not $script:PayloadRoot) { return }
    $userHome = [string]$env:USERPROFILE

    # --- git ---------------------------------------------------------------
    Copy-PayloadFile -SourcePath (Join-Path $userHome '.gitconfig') -RelativePath 'git/gitconfig' `
        -Kind 'gitconfig' -RestoreTo '~/.gitconfig' -Mode '0644' | Out-Null
    Copy-PayloadFile -SourcePath (Join-Path $userHome '.gitignore_global') -RelativePath 'git/gitignore_global' `
        -Kind 'gitconfig' -RestoreTo '~/.gitignore_global' -Mode '0644' | Out-Null

    # --- ssh ---------------------------------------------------------------
    $sshDir = Join-Path $userHome '.ssh'
    Copy-PayloadFile -SourcePath (Join-Path $sshDir 'config') -RelativePath 'ssh/config' `
        -Kind 'ssh' -RestoreTo '~/.ssh/config' -Mode '0600' | Out-Null
    foreach ($key in @(Get-Prop -Object $Dev -Name 'ssh_keys' -Default @())) {
        $fileName = [string](Get-Prop -Object $key -Name 'file' -Default '')
        if (-not $fileName) { continue }
        Copy-PayloadFile -SourcePath (Join-Path $sshDir ($fileName + '.pub')) -RelativePath ('ssh/' + $fileName + '.pub') `
            -Kind 'ssh' -RestoreTo ('~/.ssh/' + $fileName + '.pub') -Mode '0644' | Out-Null
        if ($script:WantSecrets) {
            Copy-PayloadFile -SourcePath (Join-Path $sshDir $fileName) -RelativePath ('ssh/' + $fileName) `
                -Kind 'ssh' -RestoreTo ('~/.ssh/' + $fileName) -Mode '0600' | Out-Null
        }
    }

    # --- gpg ---------------------------------------------------------------
    $gpg = Get-Prop -Object $Dev -Name 'gpg'
    if ([bool](Get-Prop -Object $gpg -Name 'present' -Default $false)) {
        $gpgPath = Get-CommandPath -Name 'gpg'
        if ($gpgPath) {
            $public = @(Invoke-Native -FilePath $gpgPath -ArgumentList @('--armor', '--export'))
            if ($public.Count -gt 0) {
                New-PayloadTextFile -Content (($public -join "`n") + "`n") -RelativePath 'gpg/public-keys.asc' `
                    -Kind 'gpg' -RestoreTo $null -Mode '0644' | Out-Null
            }
            if ($script:WantSecrets) {
                $secret = @(Invoke-Native -FilePath $gpgPath -ArgumentList @('--armor', '--export-secret-keys'))
                if ($secret.Count -gt 0 -and ($secret -join '') -match 'BEGIN PGP PRIVATE KEY') {
                    New-PayloadTextFile -Content (($secret -join "`n") + "`n") -RelativePath 'gpg/secret-keys.asc' `
                        -Kind 'gpg' -RestoreTo $null -Mode '0600' | Out-Null
                } else {
                    Add-HopWarning 'payload: gpg refused to export the secret keys unattended - export them by hand with `gpg --armor --export-secret-keys`.'
                }
            }
        }
    }

    # --- terminal and shell ------------------------------------------------
    $terminalCandidates = @(
        (Join-Path ([string]$env:LOCALAPPDATA) 'Packages\Microsoft.WindowsTerminal_8wekyb3d8bbwe\LocalState\settings.json'),
        (Join-Path ([string]$env:LOCALAPPDATA) 'Packages\Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe\LocalState\settings.json'),
        (Join-Path ([string]$env:LOCALAPPDATA) 'Microsoft\Windows Terminal\settings.json')
    )
    foreach ($candidate in $terminalCandidates) {
        if (Copy-PayloadFile -SourcePath $candidate -RelativePath 'terminal/windows-terminal.settings.json' -Kind 'terminal' -RestoreTo $null -Mode '0644') { break }
    }

    $documents = Get-KnownFolderPath -RegistryName 'Personal' -SpecialFolder 'MyDocuments'
    if (-not $documents) { $documents = Join-Path $userHome 'Documents' }
    foreach ($profileFile in @(
        (Join-Path $documents 'WindowsPowerShell\Microsoft.PowerShell_profile.ps1'),
        (Join-Path $documents 'PowerShell\Microsoft.PowerShell_profile.ps1')
    )) {
        if (Test-Path -LiteralPath $profileFile -PathType Leaf) {
            $leaf = Split-Path -Path $profileFile -Leaf
            $parent = Split-Path -Path (Split-Path -Path $profileFile -Parent) -Leaf
            Copy-PayloadFile -SourcePath $profileFile -RelativePath ('shell/' + $parent + '.' + $leaf) `
                -Kind 'other' -RestoreTo $null -Mode '0644' | Out-Null
        }
    }

    # --- vscode ------------------------------------------------------------
    $vscode = Get-Prop -Object $Dev -Name 'vscode'
    if ([bool](Get-Prop -Object $vscode -Name 'present' -Default $false)) {
        $flavor = [string](Get-Prop -Object $vscode -Name 'flavor' -Default 'code')
        $userDir = 'Code'
        if ($flavor -eq 'codium') { $userDir = 'VSCodium' }
        elseif ($flavor -eq 'code-insiders') { $userDir = 'Code - Insiders' }
        $vscodeUser = Join-Path (Join-Path ([string]$env:APPDATA) $userDir) 'User'
        Copy-PayloadFile -SourcePath (Join-Path $vscodeUser 'settings.json') -RelativePath 'vscode/settings.json' `
            -Kind 'vscode' -RestoreTo '~/.config/Code/User/settings.json' -Mode '0644' | Out-Null
        Copy-PayloadFile -SourcePath (Join-Path $vscodeUser 'keybindings.json') -RelativePath 'vscode/keybindings.json' `
            -Kind 'vscode' -RestoreTo '~/.config/Code/User/keybindings.json' -Mode '0644' | Out-Null
    }

    # --- bookmarks ---------------------------------------------------------
    foreach ($browser in @($Browsers)) {
        $id = [string](Get-Prop -Object $browser -Name 'id' -Default 'other')
        $index = 0
        foreach ($browserProfile in @(Get-Prop -Object $browser -Name 'profiles' -Default @())) {
            $index++
            $path = [string](Get-Prop -Object $browserProfile -Name 'path' -Default '')
            if (-not $path) { continue }
            $label = [string](Get-Prop -Object $browserProfile -Name 'name' -Default ('profile' + $index))
            # Keep the file name predictable and shell-safe.
            $slug = ($label -replace '[^A-Za-z0-9_.-]+', '-').Trim('-')
            if (-not $slug) { $slug = ('profile' + $index) }

            if ($id -eq 'firefox') {
                # places.sqlite is live and locked; the newest JSONLZ4 backup
                # holds the same bookmarks and copies cleanly.
                $backupDir = Join-Path $path 'bookmarkbackups'
                if (-not (Test-Path -LiteralPath $backupDir)) { continue }
                $backup = @(Get-ChildItem -LiteralPath $backupDir -File -ErrorAction SilentlyContinue |
                    Sort-Object -Property LastWriteTime -Descending)
                if ($backup.Count -eq 0) { continue }
                Copy-PayloadFile -SourcePath $backup[0].FullName `
                    -RelativePath ('browsers/firefox-' + $slug + '-' + $backup[0].Name) `
                    -Kind 'bookmarks' -RestoreTo $null -Mode '0644' | Out-Null
            } else {
                $bookmarks = Read-ChromiumBookmarks -ProfilePath $path -AsHtml
                $html = [string](Get-Prop -Object $bookmarks -Name 'html' -Default '')
                if (-not $html) { continue }
                New-PayloadTextFile -Content $html -RelativePath ('browsers/' + $id + '-' + $slug + '.html') `
                    -Kind 'bookmarks' -RestoreTo $null -Mode '0644' | Out-Null
            }
        }
    }

    # --- wallpaper ---------------------------------------------------------
    $wallpaper = [string](Get-Prop -Object $Personalization -Name 'wallpaper' -Default '')
    if ($wallpaper -and (Test-Path -LiteralPath $wallpaper -PathType Leaf)) {
        $extension = [System.IO.Path]::GetExtension($wallpaper)
        if (-not $extension) { $extension = Get-ImageExtension -Path $wallpaper }
        if (-not $extension) { $extension = '.img' }
        Copy-PayloadFile -SourcePath $wallpaper -RelativePath ('personalization/wallpaper' + $extension) `
            -Kind 'wallpaper' -RestoreTo ('~/Pictures/wallpaper' + $extension) -Mode '0644' | Out-Null
    }

    # --- fonts -------------------------------------------------------------
    $fontDir = Get-UserFontDirectory
    foreach ($font in @(Get-Prop -Object $Personalization -Name 'fonts_user' -Default @())) {
        $fontName = [string]$font
        if (-not $fontName) { continue }
        Copy-PayloadFile -SourcePath (Join-Path $fontDir $fontName) -RelativePath ('fonts/' + $fontName) `
            -Kind 'font' -RestoreTo ('~/.local/share/fonts/' + $fontName) -Mode '0644' | Out-Null
    }
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

$scanStarted = Get-Date

Write-Note ''
Write-Note '  hop2arch scanner' 'Cyan'
Write-Note '  reads this machine, writes two local paths, sends nothing anywhere.' 'DarkGray'
Write-Note ''

# --- 1/12 -----------------------------------------------------------------
Write-Step 'Environment'
$script:IsElevated = Invoke-Collector -Name 'environment/elevation' -Fallback $false -Body {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

$OutFile = Resolve-FullPath -Path $OutFile
$PayloadDir = Resolve-FullPath -Path $PayloadDir

if ($script:PayloadEnabled) {
    try {
        New-Item -ItemType Directory -Path $PayloadDir -Force | Out-Null
        $script:PayloadRoot = $PayloadDir
    } catch {
        Add-HopWarning ('payload: could not create "{0}" ({1}) - running as if -NoPayload had been given.' -f $PayloadDir, $_.Exception.Message)
        $script:PayloadEnabled = $false
        $script:PayloadRoot = $null
    }
} elseif ($script:WantSecrets) {
    Add-HopWarning '-WithSecrets does nothing together with -NoPayload: secrets only ever live in the payload directory.'
}
if (-not $script:IsElevated) {
    Add-HopWarning 'Not running as administrator: BitLocker, TPM and Wi-Fi passwords may be reported as unknown. Re-run from an elevated shell if you need them.'
}

$elevationNote = 'standard user'
if ($script:IsElevated) { $elevationNote = 'elevated' }
Complete-Step -Status 'ok' -Detail ('{0}, PowerShell {1}' -f $elevationNote, $PSVersionTable.PSVersion.ToString())

# --- 2/12 -----------------------------------------------------------------
Write-Step 'System and hardware'
$system = Invoke-Collector -Name 'system' -Body { Get-SystemInfo } -Fallback ([ordered]@{})
$systemDetail = ''
if ($system) {
    $systemDetail = ('{0} / {1}' -f `
        [string](Get-Prop -Object (Get-Prop -Object $system -Name 'windows') -Name 'caption' -Default 'Windows'),
        [string](Get-Prop -Object (Get-Prop -Object $system -Name 'cpu') -Name 'name' -Default 'unknown CPU'))
}
Complete-Step -Detail $systemDetail

# --- 3/12 -----------------------------------------------------------------
Write-Step 'Disks and volumes'
$disks = @(Invoke-Collector -Name 'disks' -Body { Get-DiskInventory } -Fallback @())
$partitionCount = 0
foreach ($disk in $disks) { $partitionCount += @(Get-Prop -Object $disk -Name 'partitions' -Default @()).Count }
Complete-Step -Detail ('{0} disk(s), {1} partition(s)' -f $disks.Count, $partitionCount)

# --- 4/12 -----------------------------------------------------------------
Write-Step 'User profile'
$user = Invoke-Collector -Name 'user' -Body { Get-UserInfo } -Fallback ([ordered]@{})
$userFolders = Get-Prop -Object $user -Name 'folders'
$userDataBytes = $null
if ($userFolders -is [System.Collections.IDictionary]) {
    foreach ($folderName in @($userFolders.Keys)) {
        $size = Get-Prop -Object $userFolders[$folderName] -Name 'size_bytes'
        if ($null -ne $size) {
            if ($null -eq $userDataBytes) { $userDataBytes = [int64]0 }
            $userDataBytes += [int64]$size
        }
    }
}
$userDetail = [string](Get-Prop -Object $user -Name 'name' -Default '')
if ($script:SkipSizes) {
    $userDetail += ' (sizes skipped)'
} elseif ($null -ne $userDataBytes) {
    $userDetail += (' ({0} in the known folders)' -f (Format-Bytes $userDataBytes))
}
Complete-Step -Detail $userDetail

# --- 5/12 -----------------------------------------------------------------
Write-Step 'Installed software'
$software = @(Invoke-Collector -Name 'software' -Body { Get-SoftwareInventory } -Fallback @())
$realSoftware = @($software | Where-Object { -not [bool](Get-Prop -Object $_ -Name 'system_component' -Default $false) })
Complete-Step -Detail ('{0} programs ({1} system components)' -f $realSoftware.Count, ($software.Count - $realSoftware.Count))

# --- 6/12 -----------------------------------------------------------------
Write-Step 'Developer environment'
$documentsPath = ''
if ($userFolders -is [System.Collections.IDictionary] -and $userFolders.Contains('Documents')) {
    $documentsPath = [string](Get-Prop -Object $userFolders['Documents'] -Name 'path' -Default '')
}
$dev = Invoke-Collector -Name 'dev' -Body { Get-DevInfo -DocumentsPath $documentsPath } -Fallback ([ordered]@{})
$devBits = New-Object 'System.Collections.Generic.List[string]'
if ([bool](Get-Prop -Object (Get-Prop -Object $dev -Name 'git') -Name 'present' -Default $false)) { $devBits.Add('git') }
$sshKeyCount = @(Get-Prop -Object $dev -Name 'ssh_keys' -Default @()).Count
if ($sshKeyCount -gt 0) { $devBits.Add(('{0} ssh key(s)' -f $sshKeyCount)) }
if ([bool](Get-Prop -Object (Get-Prop -Object $dev -Name 'wsl') -Name 'present' -Default $false)) { $devBits.Add('wsl') }
if ([bool](Get-Prop -Object (Get-Prop -Object $dev -Name 'vscode') -Name 'present' -Default $false)) { $devBits.Add('vscode') }
Complete-Step -Detail ($devBits -join ', ')

# --- 7/12 -----------------------------------------------------------------
Write-Step 'Browsers'
$browsers = @(Invoke-Collector -Name 'browsers' -Body { Get-BrowserInventory } -Fallback @())
$browserNames = New-Object 'System.Collections.Generic.List[string]'
foreach ($browser in $browsers) { $browserNames.Add([string](Get-Prop -Object $browser -Name 'name' -Default '?')) }
Complete-Step -Detail ($browserNames -join ', ')

# --- 8/12 -----------------------------------------------------------------
Write-Step 'Network and Wi-Fi'
$network = Invoke-Collector -Name 'network' -Body { Get-NetworkInfo } -Fallback ([ordered]@{})
$wifiProfiles = @(Get-Prop -Object $network -Name 'wifi_profiles' -Default @())
$wifiSecrets = @($wifiProfiles | Where-Object { [bool](Get-Prop -Object $_ -Name 'has_secret' -Default $false) })
Complete-Step -Detail ('{0} Wi-Fi profile(s), {1} with a password' -f $wifiProfiles.Count, $wifiSecrets.Count)

# --- 9/12 -----------------------------------------------------------------
Write-Step 'Games'
$gaming = Invoke-Collector -Name 'gaming' -Body { Get-GamingInfo } -Fallback ([ordered]@{})
$gameCount = 0
foreach ($storeName in @('steam', 'epic', 'gog')) {
    $gameCount += @(Get-Prop -Object (Get-Prop -Object $gaming -Name $storeName) -Name 'games' -Default @()).Count
}
Complete-Step -Detail ('{0} game(s)' -f $gameCount)

# --- 10/12 ----------------------------------------------------------------
Write-Step 'Personalization'
$personalization = Invoke-Collector -Name 'personalization' -Body { Get-PersonalizationInfo } -Fallback ([ordered]@{})
$fontCount = @(Get-Prop -Object $personalization -Name 'fonts_user' -Default @()).Count
Complete-Step -Detail ('{0} theme, {1} user font(s)' -f [string](Get-Prop -Object $personalization -Name 'theme' -Default 'unknown'), $fontCount)

# --- 11/12 ----------------------------------------------------------------
Write-Step 'Payload files'
if ($script:PayloadEnabled) {
    Invoke-Collector -Name 'payload' -Body {
        Export-HopPayload -Dev $dev -Browsers $browsers -Personalization $personalization
    } | Out-Null
    Complete-Step -Detail ('{0} file(s) in {1}' -f $script:PayloadEntries.Count, $PayloadDir)
} else {
    Complete-Step -Status 'skipped' -Detail 'manifest only (-NoPayload)'
}

# --- 12/12 ----------------------------------------------------------------
Write-Step 'Writing hopfile'
$payloadRelative = $null
if ($script:PayloadEnabled -and $script:PayloadRoot) {
    $payloadRelative = Get-RelativePath -From (Split-Path -Path $OutFile -Parent) -To $script:PayloadRoot
    # Two different drives have no path between them that stays relative, and
    # the spec says payload_dir is resolved against the hopfile's own folder.
    # Say so out loud rather than write a path the Arch side would misread.
    if ($payloadRelative -match '^[A-Za-z]:' -or $payloadRelative.StartsWith('/')) {
        $payloadRelative = ($script:PayloadRoot -replace '\\', '/')
        Add-HopWarning ('The payload directory is not below the hopfile, so payload_dir holds the absolute path "{0}". Keep the two together, or point "hop land" at the payload yourself.' -f $payloadRelative)
    }
}

$hopfile = [ordered]@{
    hopfile_version = $script:HopfileVersion
    generated_at    = ((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))
    generator       = $script:Generator
    payload_dir     = $payloadRelative
    system          = $system
    disks           = @($disks)
    user            = $user
    software        = @($software)
    dev             = $dev
    browsers        = @($browsers)
    network         = $network
    gaming          = $gaming
    personalization = $personalization
    payload         = [ordered]@{ entries = @($script:PayloadEntries.ToArray()) }
    warnings        = @($script:Warnings.ToArray())
}

$written = $false
try {
    # -Depth 12 covers the deepest nesting we build (disks -> partitions).
    $json = ConvertTo-Json -InputObject $hopfile -Depth 12
    $outDirectory = Split-Path -Path $OutFile -Parent
    if ($outDirectory -and -not (Test-Path -LiteralPath $outDirectory)) {
        New-Item -ItemType Directory -Path $outDirectory -Force | Out-Null
    }
    # Out-File -Encoding utf8 writes a BOM in Windows PowerShell 5.1 and a
    # leading BOM breaks every strict JSON parser, Python's included.
    [System.IO.File]::WriteAllText($OutFile, $json, (New-Object System.Text.UTF8Encoding($false)))
    $written = $true
    Complete-Step -Detail ('{0} KB' -f [int]([math]::Ceiling($json.Length / 1024)))
} catch {
    Complete-Step -Status 'failed' -Detail $_.Exception.Message
    Write-Error ('hop-scan could not write "{0}": {1}' -f $OutFile, $_.Exception.Message) -ErrorAction Continue
}

# --- summary ---------------------------------------------------------------
if (-not $script:QuietMode) {
    $elapsed = (Get-Date) - $scanStarted
    $userDataText = 'not measured'
    if ($null -ne $userDataBytes) { $userDataText = Format-Bytes $userDataBytes }

    Write-Note ''
    Write-Note ('  scanned in {0:N1} s' -f $elapsed.TotalSeconds) 'DarkGray'
    Write-Note ''
    Write-Note ('  {0,-16}{1}' -f 'programs', $realSoftware.Count)
    Write-Note ('  {0,-16}{1}' -f 'games', $gameCount)
    Write-Note ('  {0,-16}{1}' -f 'wi-fi profiles', $wifiProfiles.Count)
    Write-Note ('  {0,-16}{1}' -f 'payload files', $script:PayloadEntries.Count)
    Write-Note ('  {0,-16}{1}' -f 'user data', $userDataText)
    if ($script:Warnings.Count -gt 0) {
        Write-Note ('  {0,-16}{1}' -f 'warnings', $script:Warnings.Count) 'Yellow'
    }
    Write-Note ''
    if ($written) {
        Write-Note ('  hopfile   {0}' -f $OutFile) 'White'
    } else {
        Write-Note ('  hopfile   NOT WRITTEN - {0}' -f $OutFile) 'Red'
    }
    if ($script:PayloadEnabled -and $script:PayloadRoot) {
        $payloadColour = 'White'
        if ($script:WantSecrets) { $payloadColour = 'Yellow' }
        Write-Note ('  payload   {0}' -f $script:PayloadRoot) $payloadColour
        if ($script:WantSecrets) {
            Write-Note '            contains private keys and Wi-Fi passwords - guard it like ~/.ssh' 'Yellow'
        }
    }
    Write-Note ''
    Write-Note ('  next      hop plan {0}' -f (Split-Path -Path $OutFile -Leaf)) 'Cyan'
    Write-Note ''
}

