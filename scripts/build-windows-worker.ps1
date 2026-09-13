[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$HandBrakeVersion = "1.11.2",
    [switch]$UseInstalledTools,
    [string]$SigningCertificatePath = "",
    [string]$SigningCertificatePassword = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).ProviderPath
$VendorRoot = Join-Path $RepoRoot "worker\vendor\windows"
$ToolsRoot = Join-Path $VendorRoot "tools"
$Downloads = Join-Path $VendorRoot "downloads"
$BuildRoot = Join-Path $env:LOCALAPPDATA "ByteSqueezeWorkerBuild"
$DistRoot = Join-Path $RepoRoot "dist\windows-worker"
$LocalDistRoot = Join-Path $BuildRoot "dist"
$BundleName = "ByteSqueezeWorker"

New-Item -ItemType Directory -Force -Path $ToolsRoot, $Downloads, $BuildRoot, $DistRoot, $LocalDistRoot | Out-Null

function Copy-InstalledTool([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) {
        Copy-Item -LiteralPath $command.Source -Destination (Join-Path $ToolsRoot $Name) -Force
        return $true
    }
    return $false
}

$HandBrakePath = Join-Path $ToolsRoot "HandBrakeCLI.exe"
if (-not (Test-Path -LiteralPath $HandBrakePath)) {
    if (-not $UseInstalledTools -or -not (Copy-InstalledTool "HandBrakeCLI.exe")) {
        $Archive = Join-Path $Downloads "HandBrakeCLI-$HandBrakeVersion-win-x86_64.zip"
        $Url = "https://github.com/HandBrake/HandBrake/releases/download/$HandBrakeVersion/HandBrakeCLI-$HandBrakeVersion-win-x86_64.zip"
        Write-Host "Downloading official HandBrakeCLI $HandBrakeVersion..."
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Archive
        $Extract = Join-Path $Downloads "handbrake"
        if (Test-Path -LiteralPath $Extract) { Remove-Item -LiteralPath $Extract -Recurse -Force }
        Expand-Archive -LiteralPath $Archive -DestinationPath $Extract -Force
        $Found = Get-ChildItem -LiteralPath $Extract -Filter HandBrakeCLI.exe -Recurse | Select-Object -First 1
        if (-not $Found) { throw "HandBrakeCLI.exe was not found in the official archive." }
        Copy-Item -LiteralPath $Found.FullName -Destination $HandBrakePath -Force
    }
}

$FfmpegPath = Join-Path $ToolsRoot "ffmpeg.exe"
$FfprobePath = Join-Path $ToolsRoot "ffprobe.exe"
if (-not (Test-Path -LiteralPath $FfmpegPath) -or -not (Test-Path -LiteralPath $FfprobePath)) {
    $Copied = $false
    if ($UseInstalledTools) {
        $Copied = (Copy-InstalledTool "ffmpeg.exe") -and (Copy-InstalledTool "ffprobe.exe")
    }
    if (-not $Copied) {
        $Archive = Join-Path $Downloads "ffmpeg-release-essentials.zip"
        Write-Host "Downloading the current Gyan FFmpeg essentials build..."
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $Archive
        $Extract = Join-Path $Downloads "ffmpeg"
        if (Test-Path -LiteralPath $Extract) { Remove-Item -LiteralPath $Extract -Recurse -Force }
        Expand-Archive -LiteralPath $Archive -DestinationPath $Extract -Force
        foreach ($Name in @("ffmpeg.exe", "ffprobe.exe")) {
            $Found = Get-ChildItem -LiteralPath $Extract -Filter $Name -Recurse | Select-Object -First 1
            if (-not $Found) { throw "$Name was not found in the FFmpeg archive." }
            Copy-Item -LiteralPath $Found.FullName -Destination (Join-Path $ToolsRoot $Name) -Force
        }
    }
}

Set-Content -LiteralPath (Join-Path $ToolsRoot "THIRD-PARTY-NOTICES.txt") -Encoding utf8 -Value @"
ByteSqueeze Worker bundles HandBrakeCLI and FFmpeg.
HandBrake: https://handbrake.fr/ and https://github.com/HandBrake/HandBrake
FFmpeg: https://ffmpeg.org/ (Windows binary supplied by https://www.gyan.dev/ffmpeg/builds/)
These programs retain their respective licenses and are invoked as separate executables.
"@

& $Python -m pip install --disable-pip-version-check --upgrade pyinstaller pystray pillow flask
if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }
& $Python (Join-Path $RepoRoot "worker\make_windows_icon.py") (Join-Path $ToolsRoot "ByteSqueezeWorker.ico")
if ($LASTEXITCODE -ne 0) { throw "Worker icon generation failed." }

$env:BYTESQUEEZE_WINDOWS_TOOLS = $ToolsRoot
Push-Location $BuildRoot
try {
    & $Python -m PyInstaller --noconfirm --clean --distpath $LocalDistRoot --workpath (Join-Path $BuildRoot "work") (Join-Path $RepoRoot "worker\windows_worker.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
} finally {
    Pop-Location
}

$LocalBundle = Join-Path $LocalDistRoot $BundleName
$LocalExe = Join-Path $LocalBundle "ByteSqueezeWorker.exe"
if (-not (Test-Path -LiteralPath $LocalExe)) { throw "Build completed without producing $LocalExe" }

function Find-SignTool {
    $command = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $kitRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    if (Test-Path -LiteralPath $kitRoot) {
        $found = Get-ChildItem -LiteralPath $kitRoot -Filter "signtool.exe" -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '[\\/]x64[\\/]signtool\.exe$' } |
            Sort-Object -Property FullName -Descending |
            Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return ""
}

if ($SigningCertificatePath) {
    if (-not (Test-Path -LiteralPath $SigningCertificatePath)) {
        throw "Signing certificate was not found: $SigningCertificatePath"
    }
    $SignTool = Find-SignTool
    if (-not $SignTool) { throw "signtool.exe is required for an Authenticode release." }
    & $SignTool sign /fd SHA256 /td SHA256 /tr $TimestampUrl /f $SigningCertificatePath /p $SigningCertificatePassword $LocalExe
    if ($LASTEXITCODE -ne 0) { throw "Authenticode signing failed." }
    & $SignTool verify /pa /v $LocalExe
    if ($LASTEXITCODE -ne 0) { throw "Authenticode verification failed." }
} else {
    Write-Warning "No Authenticode certificate supplied. The portable layout reduces antivirus false positives, but Microsoft may still show an unknown-publisher warning."
}

$Bundle = Join-Path $DistRoot $BundleName
if (Test-Path -LiteralPath $Bundle) { Remove-Item -LiteralPath $Bundle -Recurse -Force }
Copy-Item -LiteralPath $LocalBundle -Destination $Bundle -Recurse -Force
$Exe = Join-Path $Bundle "ByteSqueezeWorker.exe"
$Archive = Join-Path $DistRoot "ByteSqueeze-Windows-Worker.zip"
if (Test-Path -LiteralPath $Archive) { Remove-Item -LiteralPath $Archive -Force }
Compress-Archive -Path (Join-Path $Bundle "*") -DestinationPath $Archive -CompressionLevel Optimal

$ExeHash = (Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash.ToLowerInvariant()
$ArchiveHash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath (Join-Path $DistRoot "ByteSqueezeWorker.exe.sha256") -Encoding ascii -Value "$ExeHash  ByteSqueezeWorker.exe"
Set-Content -LiteralPath "$Archive.sha256" -Encoding ascii -Value "$ArchiveHash  ByteSqueeze-Windows-Worker.zip"
Write-Host "Built portable worker: $Bundle"
Write-Host "Built release archive: $Archive"
Write-Host "EXE SHA256: $ExeHash"
Write-Host "ZIP SHA256: $ArchiveHash"
