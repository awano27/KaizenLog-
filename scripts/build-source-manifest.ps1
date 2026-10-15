[CmdletBinding()]
param(
    [string]$Manifest = (Join-Path $PSScriptRoot "..\packaging\source-manifest.json"),
    [string]$OutputDir = (Join-Path $PSScriptRoot "..\artifacts\source"),
    [string]$BaseRef = "HEAD",
    [string]$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot ".."))
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-Sha256([byte[]]$Bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-FileSha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Missing file: $Path" }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-GitText([string[]]$Arguments) {
    $value = & git -C $RepositoryRoot @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "git $($Arguments -join ' ') failed: $($value -join ' ')" }
    return ($value -join "`n")
}

function Normalize-Relative([string]$Path) {
    return $Path.Replace("\", "/").TrimStart("/")
}

function Get-RelativePath([string]$Base, [string]$Target) {
    $baseUri = New-Object System.Uri(($Base.TrimEnd("\") + "\"))
    $targetUri = New-Object System.Uri($Target)
    return [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($targetUri).ToString()).Replace("/", "\")
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Test-Pattern([string]$Relative, [string]$Pattern, [switch]$Rooted) {
    $path = Normalize-Relative $Relative
    $normalized = Normalize-Relative $Pattern
    $pathParts = @($path -split "/")
    $patternParts = @($normalized -split "/")
    if ($Rooted) {
        if ($normalized.EndsWith("/**")) {
            $prefixParts = @($patternParts[0..($patternParts.Count - 2)])
            if ($pathParts.Count -le $prefixParts.Count) { return $false }
            for ($i = 0; $i -lt $prefixParts.Count; $i++) {
                if ($pathParts[$i] -notlike $prefixParts[$i]) { return $false }
            }
            return $true
        }
        if ($pathParts.Count -ne $patternParts.Count) { return $false }
        for ($i = 0; $i -lt $patternParts.Count; $i++) {
            if ($pathParts[$i] -notlike $patternParts[$i]) { return $false }
        }
        return $true
    }
    if ($patternParts.Count -eq 1) {
        foreach ($component in $pathParts) { if ($component -like $patternParts[0]) { return $true } }
        return $false
    }
    if ($normalized.EndsWith("/**")) {
        $prefixParts = @($patternParts[0..($patternParts.Count - 2)])
        if ($pathParts.Count -lt $prefixParts.Count) { return $false }
        if ($prefixParts.Count -eq 1) {
            foreach ($component in $pathParts) { if ($component -like $prefixParts[0]) { return $true } }
            return $false
        }
        for ($start = 0; $start -le ($pathParts.Count - $prefixParts.Count); $start++) {
            $matched = $true
            for ($i = 0; $i -lt $prefixParts.Count; $i++) { if ($pathParts[$start + $i] -notlike $prefixParts[$i]) { $matched = $false; break } }
            if ($matched) { return $true }
        }
        return $false
    }
    if ($pathParts.Count -ne $patternParts.Count) { return $false }
    for ($i = 0; $i -lt $patternParts.Count; $i++) { if ($pathParts[$i] -notlike $patternParts[$i]) { return $false } }
    return $true
}

function Test-MatchesAny([string]$Relative, $Patterns, [switch]$Rooted) {
    foreach ($pattern in @($Patterns)) {
        if (Test-Pattern $Relative ([string]$pattern) -Rooted:$Rooted) { return $true }
    }
    return $false
}

function Add-DeterministicZipFile($Archive, [string]$Source, [string]$EntryName) {
    $entry = $Archive.CreateEntry($EntryName, [System.IO.Compression.CompressionLevel]::Optimal)
    $entry.LastWriteTime = [DateTimeOffset]::new(1980, 1, 1, 0, 0, 0, [TimeSpan]::Zero)
    $stream = $entry.Open()
    try {
        $bytes = [System.IO.File]::ReadAllBytes($Source)
        $stream.Write($bytes, 0, $bytes.Length)
    } finally { $stream.Dispose() }
}

function Remove-SafeOutputFile([string]$Output, [string]$Name) {
    $outputFull = [System.IO.Path]::GetFullPath($Output).TrimEnd("\")
    $target = [System.IO.Path]::GetFullPath((Join-Path $outputFull $Name))
    if (-not $target.StartsWith($outputFull + "\", [System.StringComparison]::OrdinalIgnoreCase)) { throw "Output cleanup escaped OutputDir: $target" }
    if (-not (Test-Path -LiteralPath $target)) { return }
    $item = Get-Item -LiteralPath $target -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Refusing to remove reparse-point output: $target" }
    Remove-Item -LiteralPath $target -Force
}

try {
    $root = (Resolve-Path -LiteralPath $RepositoryRoot).Path
    $manifestPath = (Resolve-Path -LiteralPath $Manifest).Path
    $manifestData = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([int]$manifestData.schema_version -ne 1) { throw "Unsupported source manifest schema" }
    if (-not $manifestData.include) { throw "Source manifest has no include allowlist" }
    $includePatterns = @($manifestData.include)
    $excludePatterns = @($manifestData.exclude)
    $required = @($manifestData.required)

    foreach ($requiredPath in $required) {
        $requiredAbsolute = Join-Path $root (Normalize-Relative ([string]$requiredPath))
        if (-not (Test-Path -LiteralPath $requiredAbsolute -PathType Leaf)) { throw "Required manifest file is missing: $requiredPath" }
    }

    $files = @(
        Get-ChildItem -LiteralPath $root -Recurse -File -Force | ForEach-Object {
            $relative = Normalize-Relative (Get-RelativePath $root $_.FullName)
            if ((Test-MatchesAny $relative $includePatterns -Rooted) -and -not (Test-MatchesAny $relative $excludePatterns)) {
                [pscustomobject]@{ Path = $relative; FullName = $_.FullName }
            }
        }
    ) | Sort-Object Path
    if ($files.Count -eq 0) { throw "Source manifest selected no files" }

    $output = [System.IO.Path]::GetFullPath($OutputDir)
    New-Item -ItemType Directory -Force -Path $output | Out-Null
    $head = (Invoke-GitText @("rev-parse", $BaseRef)).Trim()
    $headTree = Invoke-GitText @("ls-tree", "-r", "--full-tree", $head)
    $statusText = Invoke-GitText @("status", "--porcelain=v1", "--untracked-files=all")
    $diffText = Invoke-GitText @("diff", "--no-ext-diff", "--binary", $head, "--")

    $entries = @(
        $files | ForEach-Object {
            $trackedText = (& git -C $root ls-files -- $_.Path 2>$null | Out-String).Trim()
            $isTracked = $trackedText -eq $_.Path
            $isDirty = $false
            if ($isTracked) {
                & git -C $root diff --quiet -- $_.Path 2>$null
                $isDirty = $LASTEXITCODE -ne 0
            }
            [ordered]@{
                path = $_.Path
                sha256 = Get-FileSha256 $_.FullName
                state = if (-not $isTracked) { "untracked-reviewed" } elseif ($isDirty) { "tracked-dirty-reviewed" } else { "tracked-clean" }
            }
        }
    )
    $workingParts = @($entries | ForEach-Object { "$($_.path)`t$($_.sha256)" }) -join "`n"
    $provenance = [ordered]@{
        schema_version = 1
        manifest = Normalize-Relative (Get-RelativePath $root $manifestPath)
        base_ref = $BaseRef
        head = $head
        head_manifest_sha256 = Get-Sha256 ([System.Text.Encoding]::UTF8.GetBytes($headTree))
        working_tree_manifest_sha256 = Get-Sha256 ([System.Text.Encoding]::UTF8.GetBytes($workingParts))
        uncommitted_diff_sha256 = Get-Sha256 ([System.Text.Encoding]::UTF8.GetBytes($statusText + "`n" + $diffText))
        files = $entries
    }
    $provenancePath = Join-Path $output "source-provenance.json"
    Write-Utf8NoBom $provenancePath ($provenance | ConvertTo-Json -Depth 8)

    $version = "working-tree"
    $projectFile = Join-Path $root "pyproject.toml"
    if (Test-Path -LiteralPath $projectFile) {
        $projectText = Get-Content -LiteralPath $projectFile -Raw -Encoding UTF8
        $match = [regex]::Match($projectText, '(?m)^version\s*=\s*["'']([^"'']+)["'']')
        if ($match.Success) { $version = $match.Groups[1].Value }
    }
    $zipPath = Join-Path $output ("kaizenlog-source-{0}.zip" -f $version)
    Remove-SafeOutputFile $output ([System.IO.Path]::GetFileName($zipPath))
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $fileStream = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    $archive = New-Object System.IO.Compression.ZipArchive($fileStream, [System.IO.Compression.ZipArchiveMode]::Create, $false)
    try {
        foreach ($file in $files) { Add-DeterministicZipFile $archive $file.FullName $file.Path }
    } finally {
        $archive.Dispose()
        $fileStream.Dispose()
    }
    $zipHash = Get-FileSha256 $zipPath
    $provenance.zip = [ordered]@{ path = [System.IO.Path]::GetFileName($zipPath); sha256 = $zipHash }
    Write-Utf8NoBom $provenancePath ($provenance | ConvertTo-Json -Depth 8)
    Write-Output ("Created {0} ({1} files)" -f $zipPath, $files.Count)
    exit 0
} catch {
    Write-Error $_
    exit 1
}
