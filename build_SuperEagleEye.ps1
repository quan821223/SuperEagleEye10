$ErrorActionPreference = "Stop"

function Invoke-Step
{
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command,
        [Parameter(Mandatory = $true)]
        [string]$ErrorMessage
    )

    & $Command
    if ($LASTEXITCODE -ne 0)
    {
        throw "$ErrorMessage (exit code: $LASTEXITCODE)"
    }
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = $root
$buildRoot = Join-Path $root "build"
$tempDistRoot = Join-Path $buildRoot "__dist"
$packageRoot = Join-Path $buildRoot "__package"
$specPath = Join-Path $root "SuperEagleEye.spec"
$distRoot = Join-Path $projectRoot "dist"
$legacyPublishRuntimeRoot = Join-Path $projectRoot "dist\SuperEagleEye_dist"
$legacyToolsRuntimeRoot = Join-Path $projectRoot "tools\SuperEagleEye_dist"
$versionPath = Join-Path $root "version.json"

if (-not (Test-Path $versionPath))
{
    throw "version.json not found."
}

$runtimeVersion = (Get-Content -Raw -Path $versionPath | ConvertFrom-Json).version
if ([string]::IsNullOrWhiteSpace($runtimeVersion))
{
    throw "version.json does not contain a valid version."
}
$safeRuntimeVersion = $runtimeVersion -replace '[^0-9A-Za-z._-]', '_'
$versionedExeName = "SuperEagleEye_v$safeRuntimeVersion.exe"
$versionedDistFolderName = "SuperEagleEye_v${safeRuntimeVersion}_dist"
$publishRuntimeRoot = Join-Path $distRoot $versionedDistFolderName
$versionedArchiveName = "$versionedDistFolderName.7z"
$versionedArchivePath = Join-Path $distRoot $versionedArchiveName
$legacyArchivePath = Join-Path $distRoot "SuperEagleEye_dist.7z"

if (-not $env:UV_CACHE_DIR)
{
    $env:UV_CACHE_DIR = Join-Path $projectRoot ".uv-cache"
}

Push-Location $root

Invoke-Step -Command { py -3 --version } -ErrorMessage "Python 3 is required but was not found by the Windows py launcher"
Invoke-Step -Command { py -3 -m uv --version } -ErrorMessage "uv is required but was not found. Install it with: py -3 -m pip install uv"
Invoke-Step -Command { py -3 -m uv sync --frozen } -ErrorMessage "Failed to synchronize the locked Python environment"

foreach ($path in @($buildRoot, $publishRuntimeRoot, $legacyPublishRuntimeRoot, $legacyToolsRuntimeRoot))
{
    if (Test-Path $path)
    {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}

if (Test-Path $specPath)
{
    Remove-Item -LiteralPath $specPath -Force
}

Invoke-Step -Command {
    py -3 -m uv run pyinstaller `
      --noconfirm `
      --clean `
      --onedir `
      --console `
      --distpath "$tempDistRoot" `
      --name SuperEagleEye `
      --add-data "SC_communication_gRPC.proto;." `
      --add-data "SC_communication_gRPC_pb2.py;." `
      --add-data "SC_communication_gRPC_pb2_grpc.py;." `
      --add-data "camera_map.json;." `
      --add-data "version.json;." `
      SuperEagleEye.py
} -ErrorMessage "PyInstaller build failed"

$tempRuntimeRoot = Join-Path $tempDistRoot "SuperEagleEye"
$tempExePath = Join-Path $tempRuntimeRoot "SuperEagleEye.exe"
$tempVersionedExePath = Join-Path $tempRuntimeRoot $versionedExeName

if (-not (Test-Path $tempExePath))
{
    throw "PyInstaller completed without producing build\__dist\SuperEagleEye\SuperEagleEye.exe"
}

Move-Item $tempExePath $tempVersionedExePath -Force
Copy-Item $versionPath (Join-Path $tempRuntimeRoot "version.json") -Force
New-Item -ItemType Directory -Path $publishRuntimeRoot -Force | Out-Null
Copy-Item (Join-Path $tempRuntimeRoot "*") $publishRuntimeRoot -Recurse -Force

if (-not (Test-Path (Join-Path $publishRuntimeRoot $versionedExeName)))
{
    throw "Versioned executable was not produced: $versionedExeName"
}

if (Test-Path $legacyArchivePath)
{
    Remove-Item -LiteralPath $legacyArchivePath -Force
}

if (Test-Path $packageRoot)
{
    Remove-Item -LiteralPath $packageRoot -Recurse -Force
}

$versionedPackageRoot = Join-Path $packageRoot $versionedDistFolderName
New-Item -ItemType Directory -Path $versionedPackageRoot -Force | Out-Null
Copy-Item (Join-Path $tempRuntimeRoot "*") $versionedPackageRoot -Recurse -Force

if (Test-Path $versionedArchivePath)
{
    Remove-Item -LiteralPath $versionedArchivePath -Force
}

Invoke-Step -Command {
    py -3 -m uv run python -c "import sys; from pathlib import Path; import py7zr; archive = Path(sys.argv[1]); source = Path(sys.argv[2]); z = py7zr.SevenZipFile(archive, 'w', filters=[{'id': py7zr.FILTER_COPY}]); z.writeall(source, arcname=source.name); z.close()" $versionedArchivePath $versionedPackageRoot
} -ErrorMessage "Failed to create versioned 7z package"

if (-not (Test-Path $versionedArchivePath))
{
    throw "Versioned 7z package was not produced: $versionedArchiveName"
}

Pop-Location

