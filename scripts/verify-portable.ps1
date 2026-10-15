[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InputDir,
    [string]$PythonlessEvidencePath = "",
    [string]$GuiEvidencePath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-FileSha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Missing file: $Path" }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Get-RelativePath([string]$Base, [string]$Target) {
    $baseUri = New-Object System.Uri(($Base.TrimEnd("\") + "\"))
    $targetUri = New-Object System.Uri($Target)
    return [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($targetUri).ToString()).Replace("/", "\")
}

function Normalize-SafeRelativeName([string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Name)) { return $null }
    $normalized = $Name.Replace("\", "/")
    if ($normalized.StartsWith("/") -or $normalized.Contains(":") -or $normalized.Contains("`0")) { return $null }
    $parts = $normalized -split "/"
    if (@($parts | Where-Object { $_ -eq "" -or $_ -eq "." -or $_ -eq ".." }).Count -gt 0) { return $null }
    return $normalized
}

function Get-SafeContainedPath([string]$Base, [string]$Relative) {
    $normalized = Normalize-SafeRelativeName $Relative
    if ($null -eq $normalized) { throw "Unsafe relative path: $Relative" }
    $baseFull = [System.IO.Path]::GetFullPath($Base).TrimEnd("\") + "\"
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $baseFull ($normalized -replace "/", "\")))
    if (-not $candidate.StartsWith($baseFull, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Relative path escaped base directory: $Relative" }
    return $candidate
}

function Has-Property($Object, [string]$Name) {
    return $null -ne $Object -and ($Object.PSObject.Properties.Name -contains $Name)
}

function Add-Reason([System.Collections.Generic.List[string]]$Reasons, [string]$Message) {
    [void]$Reasons.Add($Message)
}

function Is-ActualString($Value) {
    return $null -ne $Value -and $Value -is [string]
}

function Is-ActualBoolean($Value) {
    return $Value -is [bool]
}

function Is-ActualInteger($Value) {
    if ($null -eq $Value -or $Value -is [bool]) { return $false }
    return $Value -is [sbyte] -or $Value -is [byte] -or $Value -is [int16] -or $Value -is [uint16] -or $Value -is [int32] -or $Value -is [uint32] -or $Value -is [int64] -or $Value -is [uint64]
}

function Is-NonEmptyStringArray($Value) {
    if ($null -eq $Value -or -not ($Value -is [System.Array]) -or $Value.Count -eq 0) { return $false }
    foreach ($item in $Value) {
        if (-not (Is-ActualString $item) -or [string]::IsNullOrWhiteSpace($item)) { return $false }
    }
    return $true
}

function Get-ZipEntryHash($Entry) {
    $stream = $Entry.Open()
    $memory = New-Object System.IO.MemoryStream
    try {
        $stream.CopyTo($memory)
        $bytes = $memory.ToArray()
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try { return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant() }
        finally { $sha.Dispose() }
    } finally { $memory.Dispose(); $stream.Dispose() }
}

function Validate-Evidence($Evidence, [string]$Kind, [string]$ArtifactHash, [string]$Entrypoint, [string[]]$ArchiveNames, [System.Collections.Generic.List[string]]$Reasons) {
    if ($null -eq $Evidence) { return "NOT_RUN" }
    if ((Has-Property $Evidence "status") -and (Is-ActualString $Evidence.status) -and $Evidence.status -eq "FAIL") {
        Add-Reason $Reasons "$Kind evidence explicitly reports FAIL"
        return "FAIL"
    }
    if (-not (Has-Property $Evidence "status") -or -not (Is-ActualString $Evidence.status) -or $Evidence.status -ne "PASS") { return "NOT_RUN" }
    if (-not (Has-Property $Evidence "schema_version") -or -not (Is-ActualInteger $Evidence.schema_version) -or $Evidence.schema_version -ne 1) { return "NOT_RUN" }
    if (-not (Has-Property $Evidence "artifact_sha256") -or -not (Is-ActualString $Evidence.artifact_sha256) -or $Evidence.artifact_sha256 -notmatch '^[0-9a-fA-F]{64}$' -or $Evidence.artifact_sha256.ToLowerInvariant() -ne $ArtifactHash) { return "NOT_RUN" }
    if (-not (Has-Property $Evidence "entrypoint") -or -not (Is-ActualString $Evidence.entrypoint) -or [string]::IsNullOrWhiteSpace($Evidence.entrypoint) -or $Evidence.entrypoint -ne $Entrypoint) { return "NOT_RUN" }
    if (-not (Has-Property $Evidence "host") -or -not (Is-ActualString $Evidence.host) -or [string]::IsNullOrWhiteSpace($Evidence.host)) { return "NOT_RUN" }
    if (-not (Has-Property $Evidence "inventory") -or -not (Is-NonEmptyStringArray $Evidence.inventory) -or $Evidence.inventory -notcontains $Entrypoint) { return "NOT_RUN" }
    if ($ArchiveNames -notcontains $Entrypoint) { return "NOT_RUN" }
    if (-not (Has-Property $Evidence "checks")) { return "NOT_RUN" }
    if ($Kind -eq "pythonless") {
        if (-not (Has-Property $Evidence.checks "python_present") -or -not (Is-ActualBoolean $Evidence.checks.python_present) -or $Evidence.checks.python_present) { return "NOT_RUN" }
        if (-not (Has-Property $Evidence.checks "py_launcher_present") -or -not (Is-ActualBoolean $Evidence.checks.py_launcher_present) -or $Evidence.checks.py_launcher_present) { return "NOT_RUN" }
        if (-not (Has-Property $Evidence "cli_execution")) { return "NOT_RUN" }
        if (-not (Has-Property $Evidence.cli_execution "launched") -or -not (Is-ActualBoolean $Evidence.cli_execution.launched) -or -not $Evidence.cli_execution.launched) { return "NOT_RUN" }
        if (-not (Has-Property $Evidence.cli_execution "exit_code") -or -not (Is-ActualInteger $Evidence.cli_execution.exit_code) -or $Evidence.cli_execution.exit_code -ne 0) { return "NOT_RUN" }
        return "PASS"
    }
    if (-not (Has-Property $Evidence "gui_execution")) { return "NOT_RUN" }
    foreach ($field in @("launched", "displayed", "actions", "closed")) {
        if (-not (Has-Property $Evidence.gui_execution $field) -or -not (Is-ActualBoolean $Evidence.gui_execution.$field) -or -not $Evidence.gui_execution.$field) { return "NOT_RUN" }
    }
    return "PASS"
}

function Read-Evidence([string]$Path, [System.Collections.Generic.List[string]]$Reasons, [string]$Kind) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Add-Reason $Reasons "$Kind evidence file is missing"; return $null }
    try { return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { Add-Reason $Reasons "$Kind evidence is invalid JSON"; return $null }
}

$inputPath = [System.IO.Path]::GetFullPath($InputDir).TrimEnd("\")
$verificationPath = Join-Path $inputPath "verification.json"
$reasons = [System.Collections.Generic.List[string]]::new()
$checks = [ordered]@{}
$entryInfo = [ordered]@{ cli = "cli/kaizenlog.exe"; gui = "gui/kaizenlog-home.exe"; cli_parent = "cli"; gui_parent = "gui" }
$pythonlessStatus = "NOT_RUN"
$guiStatus = "NOT_RUN"

try {
    if (-not (Test-Path -LiteralPath $inputPath -PathType Container)) { throw "Portable input directory is missing: $inputPath" }
    $manifestPath = Join-Path $inputPath "manifest.json"
    $zipPath = Join-Path $inputPath "kaizenlog-portable-windows-x64.zip"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { Add-Reason $reasons "manifest.json is missing" }
    if (-not (Test-Path -LiteralPath $zipPath -PathType Leaf)) { Add-Reason $reasons "portable zip is missing" }
    $manifest = $null
    if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
        try { $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { Add-Reason $reasons "manifest.json is invalid JSON" }
    }
    if ($null -eq $manifest) { throw "No usable portable manifest" }
    if (-not (Has-Property $manifest "schema_version") -or [int]$manifest.schema_version -ne 1) { Add-Reason $reasons "unsupported portable manifest schema" }
    if (-not (Has-Property $manifest "zip_sha256")) { Add-Reason $reasons "portable manifest has no zip_sha256" }
    if (-not (Has-Property $manifest "files")) { Add-Reason $reasons "portable manifest has no files hash list" }
    if (Has-Property $manifest "entrypoints") {
        if (Has-Property $manifest.entrypoints "cli") { $entryInfo.cli = [string]$manifest.entrypoints.cli }
        if (Has-Property $manifest.entrypoints "gui") { $entryInfo.gui = [string]$manifest.entrypoints.gui }
    }
    $entryInfo.cli_parent = [System.IO.Path]::GetDirectoryName($entryInfo.cli)
    $entryInfo.gui_parent = [System.IO.Path]::GetDirectoryName($entryInfo.gui)
    $zipHash = $null
    $archive = $null
    $zipStream = $null
    $archiveEntries = @()
    $archiveNames = @()
    if (Test-Path -LiteralPath $zipPath -PathType Leaf) {
        $zipHash = Get-FileSha256 $zipPath
        $checks.zip_sha256 = $zipHash
        if (-not (Has-Property $manifest "zip_sha256") -or ([string]$manifest.zip_sha256).ToLowerInvariant() -ne $zipHash) { Add-Reason $reasons "portable zip SHA256 does not match manifest" }
        Add-Type -AssemblyName System.IO.Compression
        $zipStream = [System.IO.File]::OpenRead($zipPath)
        $archive = New-Object System.IO.Compression.ZipArchive($zipStream, [System.IO.Compression.ZipArchiveMode]::Read, $false)
        $archiveEntries = @($archive.Entries)
        $archiveNames = @($archiveEntries | ForEach-Object { $_.FullName.Replace("\", "/") })
        $duplicates = @($archiveNames | Group-Object | Where-Object Count -gt 1)
        if ($duplicates.Count -gt 0) { Add-Reason $reasons ("duplicate ZIP entries: " + (($duplicates | ForEach-Object Name) -join ", ")) }
        foreach ($name in $archiveNames) {
            $nameForValidation = if ($name.EndsWith("/")) { $name.TrimEnd("/") } else { $name }
            if ($null -eq (Normalize-SafeRelativeName $nameForValidation)) { Add-Reason $reasons "unsafe ZIP entry path: $name" }
            $lower = $name.ToLowerInvariant()
            if ($lower.Contains(".git/") -or $lower.Contains(".venv/") -or $lower.Contains(".kaizenlog/") -or $lower.Contains(".grok/") -or $lower.Contains("userlogs/") -or $lower.Contains("secrets/") -or $lower.Contains("credentials/") -or $lower -match '(^|/)\.env($|\.)' -or $lower -match '(^|/)(src|tests|browser-extension|pyproject\.toml)(/|$)') { Add-Reason $reasons "source or private path is present in portable zip: $name" }
        }
    }
    try {
        if (Has-Property $manifest "files") {
            $manifestFiles = @($manifest.files)
            $manifestPaths = @{}
            foreach ($file in $manifestFiles) {
                if (-not (Has-Property $file "path") -or -not (Has-Property $file "sha256")) { Add-Reason $reasons "portable manifest contains an incomplete file hash entry"; continue }
                $relative = Normalize-SafeRelativeName ([string]$file.path)
                if ($null -eq $relative) { Add-Reason $reasons ("unsafe manifest file path: " + [string]$file.path); continue }
                if ($manifestPaths.ContainsKey($relative)) { Add-Reason $reasons "duplicate manifest file path: $relative"; continue }
                $manifestPaths[$relative] = $true
                $localPath = Get-SafeContainedPath $inputPath $relative
                if (-not (Test-Path -LiteralPath $localPath -PathType Leaf)) { Add-Reason $reasons "manifest file missing from unpacked tree: $relative"; continue }
                $expectedHash = ([string]$file.sha256).ToLowerInvariant()
                if ((Get-FileSha256 $localPath) -ne $expectedHash) { Add-Reason $reasons "unpacked file SHA256 mismatch: $relative" }
                if ($archiveNames -notcontains $relative) { Add-Reason $reasons "manifest file missing from ZIP: $relative" }
                else {
                    $zipEntry = $archiveEntries | Where-Object { $_.FullName.Replace("\", "/") -eq $relative } | Select-Object -First 1
                    if ((Get-ZipEntryHash $zipEntry) -ne $expectedHash) { Add-Reason $reasons "ZIP file SHA256 mismatch: $relative" }
                }
            }
            foreach ($archiveEntry in @($archiveEntries | Where-Object { -not $_.FullName.Replace("\", "/").EndsWith("/") })) {
                $archiveRelative = Normalize-SafeRelativeName $archiveEntry.FullName
                if ($null -ne $archiveRelative -and -not $manifestPaths.ContainsKey($archiveRelative)) { Add-Reason $reasons "ZIP contains unlisted file: $archiveRelative" }
            }
            foreach ($actual in @(Get-ChildItem -LiteralPath $inputPath -Recurse -File -Force | ForEach-Object { (Get-RelativePath $inputPath $_.FullName).Replace("\", "/") } | Where-Object { $_ -notin @("manifest.json", "SHA256SUMS.txt", "verification.json", "kaizenlog-portable-windows-x64.zip") })) {
                if (-not $manifestPaths.ContainsKey($actual)) { Add-Reason $reasons "unlisted unpacked file: $actual" }
            }
            foreach ($required in @("README.md", "LICENSE", "portable-resources.json")) {
                if (-not $manifestPaths.ContainsKey($required)) { Add-Reason $reasons "required bundle resource is not listed: $required" }
            }
            $resourceManifestPath = Join-Path $inputPath "portable-resources.json"
            $resourceManifest = $null
            if (Test-Path -LiteralPath $resourceManifestPath -PathType Leaf) {
                try { $resourceManifest = Get-Content -LiteralPath $resourceManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json }
                catch { Add-Reason $reasons "portable-resources.json is invalid JSON" }
            }
            $requiredModules = @()
            if ($null -ne $resourceManifest -and (Has-Property $resourceManifest "embedded_module_inventory") -and (Has-Property $resourceManifest.embedded_module_inventory "required_modules")) {
                if (Is-NonEmptyStringArray $resourceManifest.embedded_module_inventory.required_modules) {
                    $requiredModules = @($resourceManifest.embedded_module_inventory.required_modules)
                } else { Add-Reason $reasons "portable resource manifest has invalid required module list" }
            } else { Add-Reason $reasons "portable resource manifest has no embedded module requirements" }
            $moduleInventoryChecks = [ordered]@{}
            foreach ($inventorySpec in @(
                [ordered]@{ path = "cli/embedded-modules.json"; entrypoint = $entryInfo.cli },
                [ordered]@{ path = "gui/embedded-modules.json"; entrypoint = $entryInfo.gui }
            )) {
                $inventoryPath = [string]$inventorySpec.path
                if (-not $manifestPaths.ContainsKey($inventoryPath)) { Add-Reason $reasons "embedded module inventory is not listed: $inventoryPath"; continue }
                $inventoryFile = Join-Path $inputPath ($inventoryPath -replace "/", "\")
                try { $inventory = Get-Content -LiteralPath $inventoryFile -Raw -Encoding UTF8 | ConvertFrom-Json }
                catch { Add-Reason $reasons "embedded module inventory is invalid JSON: $inventoryPath"; continue }
                if (-not (Has-Property $inventory "schema_version") -or -not (Is-ActualInteger $inventory.schema_version) -or $inventory.schema_version -ne 1) { Add-Reason $reasons "embedded module inventory schema is invalid: $inventoryPath"; continue }
                if (-not (Has-Property $inventory "analysis") -or -not (Is-ActualString $inventory.analysis) -or $inventory.analysis -ne "PyInstaller.Analysis.pure") { Add-Reason $reasons "embedded module inventory analysis marker is invalid: $inventoryPath" }
                if (-not (Has-Property $inventory "entrypoint") -or -not (Is-ActualString $inventory.entrypoint) -or $inventory.entrypoint -ne $inventorySpec.entrypoint) { Add-Reason $reasons "embedded module inventory entrypoint mismatch: $inventoryPath" }
                if (-not (Has-Property $inventory "executable_sha256") -or -not (Is-ActualString $inventory.executable_sha256) -or $inventory.executable_sha256 -notmatch '^[0-9a-fA-F]{64}$') { Add-Reason $reasons "embedded module inventory executable hash is invalid: $inventoryPath" }
                else {
                    $executablePath = Join-Path $inputPath ($inventorySpec.entrypoint -replace "/", "\")
                    if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf) -or $inventory.executable_sha256.ToLowerInvariant() -ne (Get-FileSha256 $executablePath)) { Add-Reason $reasons "embedded module inventory is not bound to its executable: $inventoryPath" }
                }
                if (-not (Has-Property $inventory "modules") -or -not (Is-NonEmptyStringArray $inventory.modules)) { Add-Reason $reasons "embedded module inventory has no typed modules: $inventoryPath" }
                else {
                    foreach ($requiredModule in $requiredModules) {
                        if ($inventory.modules -notcontains $requiredModule) { Add-Reason $reasons "embedded module is absent from PyInstaller analysis: $requiredModule ($inventoryPath)" }
                    }
                    $moduleInventoryChecks[$inventoryPath] = [ordered]@{ executable_sha256 = $inventory.executable_sha256.ToLowerInvariant(); module_count = @($inventory.modules).Count }
                }
            }
            $checks.embedded_module_inventory = $moduleInventoryChecks
            $runtimeNames = $archiveNames | ForEach-Object { $_.ToLowerInvariant() }
            $guiRuntimePrefix = $entryInfo.gui_parent.ToLowerInvariant() + "/"
            $guiRuntimeNames = @($runtimeNames | Where-Object { $_.StartsWith($guiRuntimePrefix, [System.StringComparison]::Ordinal) })
            $runtimeChecks = [ordered]@{
                python = [bool](@($runtimeNames | Where-Object { $_ -match '(^|/)python\d*\.dll$' }).Count)
                tcl_data = [bool](@($guiRuntimeNames | Where-Object { $_ -match '(^|/)_tcl_data/(tcl[^/]*/)?init\.tcl$' }).Count)
                tk_data = [bool](@($guiRuntimeNames | Where-Object { $_ -match '(^|/)_tk_data/(tk[^/]*/)?tk\.tcl$' }).Count)
                tcl_dll = [bool](@($guiRuntimeNames | Where-Object { $_ -match '(^|/)tcl\d+[a-z]*\.dll$' }).Count)
                tk_dll = [bool](@($guiRuntimeNames | Where-Object { $_ -match '(^|/)tk\d+[a-z]*\.dll$' }).Count)
            }
            $checks.runtime = $runtimeChecks
            foreach ($key in $runtimeChecks.Keys) { if (-not $runtimeChecks[$key]) { Add-Reason $reasons "portable runtime dependency is absent: $key" } }
        }
    } finally {
        if ($null -ne $archive) { $archive.Dispose() }
        if ($null -ne $zipStream) { $zipStream.Dispose() }
    }
    $sumsPath = Join-Path $inputPath "SHA256SUMS.txt"
    if (-not (Test-Path -LiteralPath $sumsPath -PathType Leaf)) { Add-Reason $reasons "SHA256SUMS.txt is missing" }
    else {
        $sumEntries = @{}
        foreach ($line in Get-Content -LiteralPath $sumsPath -Encoding UTF8) {
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            if ($line -notmatch '^([0-9a-fA-F]{64})\s+\*?(.+)$') { Add-Reason $reasons "invalid SHA256SUMS line"; continue }
            $sumHash = $Matches[1].ToLowerInvariant(); $sumPath = $Matches[2].Replace("\", "/")
            $safeSumPath = Normalize-SafeRelativeName $sumPath
            if ($null -eq $safeSumPath) { Add-Reason $reasons "unsafe SHA256SUMS path: $sumPath"; continue }
            $sumPath = $safeSumPath
            if ($sumEntries.ContainsKey($sumPath)) { Add-Reason $reasons "duplicate SHA256SUMS path: $sumPath" }
            $sumEntries[$sumPath] = $sumHash
            $sumAbsolute = Get-SafeContainedPath $inputPath $sumPath
            if (-not (Test-Path -LiteralPath $sumAbsolute -PathType Leaf)) { Add-Reason $reasons "SHA256SUMS file is missing: $sumPath" }
            elseif ((Get-FileSha256 $sumAbsolute) -ne $sumHash) { Add-Reason $reasons "SHA256SUMS mismatch: $sumPath" }
        }
        foreach ($requiredSum in @(Get-ChildItem -LiteralPath $inputPath -Recurse -File -Force | ForEach-Object { (Get-RelativePath $inputPath $_.FullName).Replace("\", "/") } | Where-Object { $_ -notin @("SHA256SUMS.txt", "verification.json") })) {
            if (-not $sumEntries.ContainsKey($requiredSum)) { Add-Reason $reasons "SHA256SUMS omits file: $requiredSum" }
        }
    }
    if ($entryInfo.cli -ne "cli/kaizenlog.exe") { Add-Reason $reasons "CLI entrypoint path is not cli/kaizenlog.exe" }
    if ($entryInfo.gui -ne "gui/kaizenlog-home.exe") { Add-Reason $reasons "GUI entrypoint path is not gui/kaizenlog-home.exe" }
    if ($entryInfo.cli_parent -eq $entryInfo.gui_parent) { Add-Reason $reasons "CLI and GUI entrypoints share a folder" }
    if ([System.IO.Path]::GetFileName($entryInfo.cli) -ne "kaizenlog.exe") { Add-Reason $reasons "CLI entrypoint name is not kaizenlog.exe" }
    if ([System.IO.Path]::GetFileName($entryInfo.gui) -ne "kaizenlog-home.exe") { Add-Reason $reasons "GUI entrypoint name is not kaizenlog-home.exe" }
    if ($archiveNames -notcontains $entryInfo.cli) { Add-Reason $reasons "CLI entrypoint is absent from portable ZIP" }
    if ($archiveNames -notcontains $entryInfo.gui) { Add-Reason $reasons "GUI entrypoint is absent from portable ZIP" }
    $artifactHashForEvidence = if ($null -ne $zipHash) { $zipHash } else { "" }
    $pythonlessStatus = Validate-Evidence (Read-Evidence $PythonlessEvidencePath $reasons "Pythonless") "pythonless" $artifactHashForEvidence $entryInfo.cli $archiveNames $reasons
    $guiStatus = Validate-Evidence (Read-Evidence $GuiEvidencePath $reasons "GUI") "gui" $artifactHashForEvidence $entryInfo.gui $archiveNames $reasons
    $checks.pythonless_status = $pythonlessStatus
    $checks.gui_status = $guiStatus
    $result = [ordered]@{ schema_version = 1; artifact_pass = if ($reasons.Count -eq 0) { "PASS" } else { "FAIL" }; reasons = @($reasons); checks = $checks; entrypoints = $entryInfo; pythonless_status = $pythonlessStatus; gui_status = $guiStatus }
    Write-Utf8NoBom $verificationPath ($result | ConvertTo-Json -Depth 10)
    if ($reasons.Count -gt 0) { throw ($reasons -join "; ") }
    Write-Output ("Portable verification: {0}; Pythonless: {1}; GUI: {2}" -f $result.artifact_pass, $pythonlessStatus, $guiStatus)
    exit 0
} catch {
    $failureReasons = @($reasons)
    if ($failureReasons -notcontains $_.Exception.Message) { $failureReasons += $_.Exception.Message }
    $failure = [ordered]@{ schema_version = 1; artifact_pass = "FAIL"; reasons = $failureReasons; checks = $checks; entrypoints = $entryInfo; pythonless_status = $pythonlessStatus; gui_status = $guiStatus }
    if (Test-Path -LiteralPath (Split-Path -Parent $verificationPath)) { Write-Utf8NoBom $verificationPath ($failure | ConvertTo-Json -Depth 10) }
    Write-Error $_
    exit 1
}
