# Packs Job Applier for the server. Run in PowerShell from the project folder:
#   First time (code + your data):   powershell -ExecutionPolicy Bypass -File deploy\make_bundle.ps1
#   Later updates (code only):       powershell -ExecutionPolicy Bypass -File deploy\make_bundle.ps1 -CodeOnly
# After the first move, the SERVER holds your tracker data - never send data again or you'd overwrite it.
param([switch]$CodeOnly)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $CodeOnly) {
    # Two bots can't share one Telegram token: stop the laptop copy before moving to the server.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*main.py*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}

$stage = Join-Path $env:TEMP "job-applier-bundle"
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory $stage | Out-Null

$items = @("jobapplier", "deploy", "main.py", "requirements.txt", "profile.example.yaml", ".env.example", "README.md")
if (-not $CodeOnly) { $items += @("profile.yaml", ".env", "resumes") }
foreach ($i in $items) { Copy-Item -Recurse (Join-Path $root $i) $stage }

if (-not $CodeOnly) {
    New-Item -ItemType Directory (Join-Path $stage "data") | Out-Null
    foreach ($f in @("tracker.db", "secret.key")) {   # secret.key is needed to read your saved site passwords
        if (Test-Path "data\$f") { Copy-Item "data\$f" (Join-Path $stage "data") }
    }
    if (Test-Path "data\posts") { Copy-Item -Recurse "data\posts" (Join-Path $stage "data") }
}
Get-ChildItem -Recurse -Directory -Filter __pycache__ $stage | Remove-Item -Recurse -Force

$out = Join-Path $root "job-applier-bundle.tar.gz"
Remove-Item $out -ErrorAction SilentlyContinue
& "$env:SystemRoot\System32\tar.exe" -czf $out -C $stage .   # Windows' built-in tar (not Git's)
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $out)) { throw "Packing failed" }
Write-Host "Created $out" -ForegroundColor Green
