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
$specPath = Join-Path $root "SuperEagleEye.spec"
$publishRuntimeRoot = Join-Path $projectRoot "dist\SuperEagleEye_dist"
$legacyToolsRuntimeRoot = Join-Path $projectRoot "tools\SuperEagleEye_dist"

if (-not $env:UV_CACHE_DIR)
{
    $env:UV_CACHE_DIR = Join-Path $projectRoot ".uv-cache"
}

Push-Location $root

Invoke-Step -Command { py -3 --version } -ErrorMessage "Python 3 is required but was not found by the Windows py launcher"
Invoke-Step -Command { py -3 -m uv --version } -ErrorMessage "uv is required but was not found. Install it with: py -3 -m pip install uv"
Invoke-Step -Command { py -3 -m uv sync --frozen } -ErrorMessage "Failed to synchronize the locked Python environment"

foreach ($path in @($buildRoot, $publishRuntimeRoot, $legacyToolsRuntimeRoot))
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

if (-not (Test-Path (Join-Path $tempDistRoot "SuperEagleEye\SuperEagleEye.exe")))
{
    throw "PyInstaller completed without producing build\__dist\SuperEagleEye\SuperEagleEye.exe"
}

Copy-Item (Join-Path $root "version.json") (Join-Path $tempDistRoot "SuperEagleEye\version.json") -Force
New-Item -ItemType Directory -Path $publishRuntimeRoot -Force | Out-Null
Copy-Item (Join-Path $tempDistRoot "SuperEagleEye\*") $publishRuntimeRoot -Recurse -Force

Pop-Location
