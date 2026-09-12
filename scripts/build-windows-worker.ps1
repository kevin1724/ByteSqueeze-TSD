[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$HandBrakeVersion = "1.11.2",
    [switch]$UseInstalledTools
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).ProviderPath
$VendorRoot = Join-Path $RepoRoot "worker\vendor\windows"
$ToolsRoot = Join-Path $VendorRoot "tools"
$Downloads = Join-Path $VendorRoot "downloads"
$BuildRoot = Join-Path $env:LOCALAPPDATA "ByteSqueezeWorkerBuild"
$DistRoot = Join-Path $RepoRoot "dist\windows-worker"
$LocalDistRoot = Join-Path $BuildRoot "dist"

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

$LocalExe = Join-Path $LocalDistRoot "ByteSqueezeWorker.exe"
$Exe = Join-Path $DistRoot "ByteSqueezeWorker.exe"
Copy-Item -LiteralPath $LocalExe -Destination $Exe -Force
if (-not (Test-Path -LiteralPath $Exe)) { throw "Build completed without producing $Exe" }
$Hash = (Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$Exe.sha256" -Encoding ascii -Value "$Hash  ByteSqueezeWorker.exe"
Write-Host "Built: $Exe"
Write-Host "SHA256: $Hash"
