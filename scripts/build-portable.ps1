[CmdletBinding()]
param(
    [string]$OutputDir = (Join-Path $PSScriptRoot "..\artifacts\portable"),
    [string]$SourceManifest = (Join-Path $PSScriptRoot "..\packaging\source-manifest.json"),
    [string]$PyInstallerVersion = "6.22.3",
    [string]$ExistingBuildPython = "",
    [string]$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot ".."))
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-FileSha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Missing file: $Path" }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
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

function Has-Property($Object, [string]$Name) {
    return $null -ne $Object -and ($Object.PSObject.Properties.Name -contains $Name)
}

function Invoke-Checked([string]$File, [string[]]$Arguments) {
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$File $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

function Assert-SafeOutputDirectory([string]$Output, [string]$Root) {
    $fullOutput = [System.IO.Path]::GetFullPath($Output).TrimEnd("\")
    $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd("\")
    if ($fullOutput -eq $fullRoot -or $fullOutput.Length -lt 8) { throw "Refusing broad or repository-root output directory: $fullOutput" }
    if ([System.IO.Path]::GetFileName($fullOutput) -ne "portable") { throw "OutputDir must name the dedicated portable artifact directory: $fullOutput" }
    if (Test-Path -LiteralPath $fullOutput) {
        $item = Get-Item -LiteralPath $fullOutput -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Refusing reparse-point output directory: $fullOutput" }
    }
    $ancestor = $fullOutput
    while (-not [string]::IsNullOrWhiteSpace($ancestor)) {
        if (Test-Path -LiteralPath $ancestor) {
            $ancestorItem = Get-Item -LiteralPath $ancestor -Force
            if (($ancestorItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Refusing output path through reparse point: $ancestor" }
        }
        $parent = Split-Path -Parent $ancestor
        if ($parent -eq $ancestor) { break }
        $ancestor = $parent
    }
    $rootPrefix = $fullRoot + "\"
    $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd("\") + "\"
    $insideRepositoryArtifacts = $false
    if ($fullOutput.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        $relative = (Get-RelativePath $fullRoot $fullOutput).Replace("/", "\")
        $insideRepositoryArtifacts = $relative.StartsWith("artifacts\", [System.StringComparison]::OrdinalIgnoreCase)
    }
    if (-not $insideRepositoryArtifacts -and -not $fullOutput.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "OutputDir must be inside repository artifacts or the system temporary directory: $fullOutput"
    }
}

function Remove-SafeArtifact([string]$Output, [string]$Name) {
    $target = [System.IO.Path]::GetFullPath((Join-Path $Output $Name))
    $outputPrefix = [System.IO.Path]::GetFullPath($Output).TrimEnd("\") + "\"
    if (-not $target.StartsWith($outputPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Artifact cleanup escaped OutputDir: $target" }
    if (-not (Test-Path -LiteralPath $target)) { return }
    $item = Get-Item -LiteralPath $target -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Refusing to remove reparse-point artifact: $target" }
    Remove-Item -LiteralPath $target -Recurse -Force
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

function New-DeterministicZip([string]$SourceDir, [string]$Destination, [string]$RootName) {
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $fileStream = [System.IO.File]::Open($Destination, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    $archive = New-Object System.IO.Compression.ZipArchive($fileStream, [System.IO.Compression.ZipArchiveMode]::Create, $false)
    try {
        $sourceRoot = (Resolve-Path -LiteralPath $SourceDir).Path
        Get-ChildItem -LiteralPath $sourceRoot -Recurse -File -Force | Sort-Object FullName | ForEach-Object {
            $relative = (Get-RelativePath $sourceRoot $_.FullName).Replace("\", "/")
            $entryName = if ([string]::IsNullOrWhiteSpace($RootName)) { $relative } else { "$RootName/$relative" }
            Add-DeterministicZipFile $archive $_.FullName $entryName
        }
    } finally {
        $archive.Dispose()
        $fileStream.Dispose()
    }
}

function Write-EmbeddedModuleInventory([string]$SourcePath, [string]$DestinationPath, [string]$Entrypoint, [string]$ExecutablePath) {
    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) { throw "PyInstaller analysis inventory is missing: $SourcePath" }
    $analysis = Get-Content -LiteralPath $SourcePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not (Has-Property $analysis "schema_version") -or [int]$analysis.schema_version -ne 1) { throw "Unsupported PyInstaller analysis inventory schema: $SourcePath" }
    if (-not (Has-Property $analysis "modules") -or @($analysis.modules).Count -eq 0) { throw "PyInstaller analysis inventory has no modules: $SourcePath" }
    $modules = @($analysis.modules | ForEach-Object { [string]$_ } | Sort-Object -Unique)
    if ($modules -contains "") { throw "PyInstaller analysis inventory contains an empty module name: $SourcePath" }
    $inventory = [ordered]@{
        schema_version = 1
        analysis = "PyInstaller.Analysis.pure"
        entrypoint = $Entrypoint
        executable_sha256 = Get-FileSha256 $ExecutablePath
        modules = $modules
    }
    Write-Utf8NoBom $DestinationPath ($inventory | ConvertTo-Json -Depth 8)
}

try {
    $root = (Resolve-Path -LiteralPath $RepositoryRoot).Path
    $output = [System.IO.Path]::GetFullPath($OutputDir)
    Assert-SafeOutputDirectory $output $root
    $manifestPath = (Resolve-Path -LiteralPath $SourceManifest).Path
    $manifestHash = Get-FileSha256 $manifestPath
    $manifestData = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([int]$manifestData.schema_version -ne 1) { throw "Unsupported source manifest schema" }
    New-Item -ItemType Directory -Force -Path $output | Out-Null
    foreach ($name in @("cli", "gui", "manifest.json", "SHA256SUMS.txt", "kaizenlog-portable-windows-x64.zip")) { Remove-SafeArtifact $output $name }

    $buildPython = $ExistingBuildPython
    $buildRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-build-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
    if ([string]::IsNullOrWhiteSpace($buildPython)) {
        $buildVenv = Join-Path $buildRoot "build-venv"
        $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
        if (-not $pyLauncher) { throw "Python launcher 'py' is required to create the external build venv" }
        Invoke-Checked $pyLauncher.Source @("-3.11", "-m", "venv", $buildVenv)
        $buildPython = Join-Path $buildVenv "Scripts\python.exe"
    } else {
        $buildPython = (Resolve-Path -LiteralPath $buildPython).Path
    }
    if (-not (Test-Path -LiteralPath $buildPython -PathType Leaf)) { throw "Build Python does not exist: $buildPython" }
    $buildPythonFull = [System.IO.Path]::GetFullPath($buildPython)
    $rootPrefix = [System.IO.Path]::GetFullPath($root).TrimEnd("\") + "\"
    if ($buildPythonFull.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Build Python must be outside the source repository" }
    $venvConfig = Join-Path (Split-Path -Parent (Split-Path -Parent $buildPythonFull)) "pyvenv.cfg"
    if (-not (Test-Path -LiteralPath $venvConfig -PathType Leaf)) { throw "Build Python is not a verified external venv: $buildPythonFull" }
    # Build tooling and backend requirements stay in the dedicated external
    # environment.  Runtime dependencies are installed from the wheel's
    # metadata below, so pyproject.toml remains the single dependency source.
    Invoke-Checked $buildPython @("-m", "pip", "install", "--upgrade", "build", "setuptools>=68", "wheel", "pyinstaller==$PyInstallerVersion")
    $wheelOutput = Join-Path $buildRoot "wheel"
    New-Item -ItemType Directory -Force -Path $wheelOutput | Out-Null
    # Build from an external staging copy.  Setuptools may emit egg-info while
    # building; keeping that generated metadata outside the checkout prevents
    # the product source tree from being mutated.
    $wheelSource = Join-Path $buildRoot "wheel-source"
    New-Item -ItemType Directory -Force -Path $wheelSource | Out-Null
    foreach ($sourceName in @("src", "pyproject.toml", "README.md", "LICENSE")) {
        $sourcePath = Join-Path $root $sourceName
        if (-not (Test-Path -LiteralPath $sourcePath)) { throw "Missing wheel source member: $sourcePath" }
        $destinationPath = Join-Path $wheelSource $sourceName
        if ((Get-Item -LiteralPath $sourcePath).PSIsContainer) {
            Copy-Item -LiteralPath $sourcePath -Destination $destinationPath -Recurse -Force
        } else {
            Copy-Item -LiteralPath $sourcePath -Destination $destinationPath -Force
        }
    }
    # --no-isolation is deliberate only after installing the exact build
    # backend floor declared by pyproject.toml and wheel support above.
    Invoke-Checked $buildPython @("-m", "build", "--wheel", "--no-isolation", "--outdir", $wheelOutput, $wheelSource)
    $wheel = Get-ChildItem -LiteralPath $wheelOutput -Filter "kaizenlog-*.whl" -File | Select-Object -First 1
    if ($null -eq $wheel) { throw "Local wheel build did not produce a kaizenlog wheel" }
    # Install according to the wheel METADATA dependencies; do not duplicate
    # requests/tzdata here or bypass dependency resolution with --no-deps.
    Invoke-Checked $buildPython @("-m", "pip", "install", "--force-reinstall", $wheel.FullName)
    $reportedVersion = (& $buildPython -m PyInstaller --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller is unavailable in the dedicated build environment: $reportedVersion" }
    if ($reportedVersion -ne $PyInstallerVersion) { throw "Expected PyInstaller $PyInstallerVersion but found $reportedVersion" }

    $temporaryBuild = Join-Path ([System.IO.Path]::GetTempPath()) ("kaizenlog-pyinstaller-" + [guid]::NewGuid().ToString("N"))
    $cliDist = Join-Path $temporaryBuild "dist-cli"
    $guiDist = Join-Path $temporaryBuild "dist-gui"
    $cliWork = Join-Path $temporaryBuild "work-cli"
    $guiWork = Join-Path $temporaryBuild "work-gui"
    $cliInventory = Join-Path $temporaryBuild "cli-analysis-inventory.json"
    $guiInventory = Join-Path $temporaryBuild "gui-analysis-inventory.json"
    New-Item -ItemType Directory -Force -Path $temporaryBuild, $cliDist, $guiDist, $cliWork, $guiWork | Out-Null
    $cliSpec = Join-Path $root "packaging\kaizenlog-cli.spec"
    $guiSpec = Join-Path $root "packaging\kaizenlog-gui.spec"
    foreach ($spec in @($cliSpec, $guiSpec)) { if (-not (Test-Path -LiteralPath $spec -PathType Leaf)) { throw "Missing PyInstaller spec: $spec" } }
    $previousInventoryPath = $env:KAIZENLOG_MODULE_INVENTORY_PATH
    try {
        $env:KAIZENLOG_MODULE_INVENTORY_PATH = $cliInventory
        Invoke-Checked $buildPython @("-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", $cliDist, "--workpath", $cliWork, $cliSpec)
        $env:KAIZENLOG_MODULE_INVENTORY_PATH = $guiInventory
        Invoke-Checked $buildPython @("-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", $guiDist, "--workpath", $guiWork, $guiSpec)
    } finally {
        if ($null -eq $previousInventoryPath) { Remove-Item Env:KAIZENLOG_MODULE_INVENTORY_PATH -ErrorAction SilentlyContinue }
        else { $env:KAIZENLOG_MODULE_INVENTORY_PATH = $previousInventoryPath }
    }

    $cliBundle = Join-Path $cliDist "kaizenlog"
    $guiBundle = Join-Path $guiDist "kaizenlog-home"
    if (-not (Test-Path -LiteralPath (Join-Path $cliBundle "kaizenlog.exe") -PathType Leaf)) { throw "CLI executable was not produced" }
    if (-not (Test-Path -LiteralPath (Join-Path $guiBundle "kaizenlog-home.exe") -PathType Leaf)) { throw "GUI executable was not produced" }
    Copy-Item -LiteralPath $cliBundle -Destination (Join-Path $output "cli") -Recurse
    Copy-Item -LiteralPath $guiBundle -Destination (Join-Path $output "gui") -Recurse
    Write-EmbeddedModuleInventory $cliInventory (Join-Path $output "cli\embedded-modules.json") "cli/kaizenlog.exe" (Join-Path $output "cli\kaizenlog.exe")
    Write-EmbeddedModuleInventory $guiInventory (Join-Path $output "gui\embedded-modules.json") "gui/kaizenlog-home.exe" (Join-Path $output "gui\kaizenlog-home.exe")
    foreach ($support in @("README.md", "LICENSE", "docs\USAGE.md")) {
        $supportPath = Join-Path $root $support
        if (-not (Test-Path -LiteralPath $supportPath -PathType Leaf)) { throw "Missing portable support file: $support" }
        $supportDestination = Join-Path $output $support
        $supportParent = Split-Path -Parent $supportDestination
        New-Item -ItemType Directory -Force -Path $supportParent | Out-Null
        Copy-Item -LiteralPath $supportPath -Destination $supportDestination
    }
    $readmeAssets = @(
        "hero.svg",
        "slide-overview.png",
        "section-start.svg",
        "slide-pipeline.png",
        "section-loop.svg",
        "slide-benefits.png"
    )
    $readmeAssetsSource = Join-Path $root "assets\readme"
    $readmeAssetsDestination = Join-Path $output "assets\readme"
    foreach ($asset in $readmeAssets) {
        $assetPath = Join-Path $readmeAssetsSource $asset
        if (-not (Test-Path -LiteralPath $assetPath -PathType Leaf)) { throw "Missing portable README asset: $assetPath" }
        New-Item -ItemType Directory -Force -Path $readmeAssetsDestination | Out-Null
        Copy-Item -LiteralPath $assetPath -Destination (Join-Path $readmeAssetsDestination $asset)
    }
    $resourceManifest = Join-Path $root "src\kaizenlog\resources\portable-resources.json"
    if (-not (Test-Path -LiteralPath $resourceManifest -PathType Leaf)) { throw "Missing portable resource manifest: $resourceManifest" }
    Copy-Item -LiteralPath $resourceManifest -Destination (Join-Path $output "portable-resources.json")

    $stage = Join-Path $temporaryBuild "portable-stage"
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    Copy-Item -LiteralPath (Join-Path $output "cli") -Destination (Join-Path $stage "cli") -Recurse
    Copy-Item -LiteralPath (Join-Path $output "gui") -Destination (Join-Path $stage "gui") -Recurse
    Copy-Item -LiteralPath (Join-Path $output "README.md") -Destination (Join-Path $stage "README.md")
    Copy-Item -LiteralPath (Join-Path $output "LICENSE") -Destination (Join-Path $stage "LICENSE")
    New-Item -ItemType Directory -Force -Path (Join-Path $stage "docs"), (Join-Path $stage "assets") | Out-Null
    Copy-Item -LiteralPath (Join-Path $output "docs\USAGE.md") -Destination (Join-Path $stage "docs\USAGE.md")
    Copy-Item -LiteralPath (Join-Path $output "assets\readme") -Destination (Join-Path $stage "assets\readme") -Recurse
    Copy-Item -LiteralPath (Join-Path $output "portable-resources.json") -Destination (Join-Path $stage "portable-resources.json")
    $zipPath = Join-Path $output "kaizenlog-portable-windows-x64.zip"
    New-DeterministicZip $stage $zipPath ""

    $bundleFiles = @(
        Get-ChildItem -LiteralPath $output -Recurse -File -Force | Where-Object { $_.Name -notin @("manifest.json", "SHA256SUMS.txt", "verification.json", "kaizenlog-portable-windows-x64.zip") } | ForEach-Object {
            [ordered]@{ path = (Get-RelativePath $output $_.FullName).Replace("\", "/"); sha256 = Get-FileSha256 $_.FullName }
        }
    ) | Sort-Object path
    $manifestOut = [ordered]@{
        schema_version = 1
        platform = "windows-x64"
        pyinstaller_version = $reportedVersion
        source_manifest_sha256 = $manifestHash
        entrypoints = [ordered]@{ cli = "cli/kaizenlog.exe"; gui = "gui/kaizenlog-home.exe" }
        files = $bundleFiles
        zip = "kaizenlog-portable-windows-x64.zip"
        zip_sha256 = Get-FileSha256 $zipPath
    }
    Write-Utf8NoBom (Join-Path $output "manifest.json") ($manifestOut | ConvertTo-Json -Depth 8)
    $sumLines = @(
        Get-ChildItem -LiteralPath $output -Recurse -File -Force | Where-Object { $_.Name -ne "SHA256SUMS.txt" -and $_.Name -ne "verification.json" } | Sort-Object FullName | ForEach-Object {
            "{0}  {1}" -f (Get-FileSha256 $_.FullName), ((Get-RelativePath $output $_.FullName).Replace("\", "/"))
        }
    )
    Write-Utf8NoBom (Join-Path $output "SHA256SUMS.txt") ($sumLines -join "`n")
    Write-Output ("Created portable artifact: {0}" -f $zipPath)
    exit 0
} catch {
    Write-Error $_
    exit 1
}
