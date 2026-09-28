param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$buildPython = (Get-Command $Python -ErrorAction Stop).Source
$originalBuildPath = $env:PATH
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    # Avoid collecting unrelated DLLs from tools installed on the host PATH.
    $env:PATH = "$(Split-Path -Parent $buildPython);$env:SystemRoot\system32;$env:SystemRoot"
    & $buildPython -m PyInstaller --clean --noconfirm RA2Trainer.spec
    if ($LASTEXITCODE -ne 0) { throw "EXE build failed" }
    $smokeProcess = Start-Process -FilePath (Join-Path $PWD 'dist/RA2Trainer.exe') `
        -ArgumentList '--self-test release-smoke.json' -WorkingDirectory (Join-Path $PWD 'build') `
        -WindowStyle Hidden -PassThru
    if (-not $smokeProcess.WaitForExit(45000)) { throw "EXE self-test timed out" }
    if ($smokeProcess.ExitCode -ne 0) { throw "EXE self-test failed; see build/release-smoke.json" }
    $smokeReport = Get-Content -Raw -Encoding UTF8 -LiteralPath build/release-smoke.json | ConvertFrom-Json
    if (-not $smokeReport.ok) { throw "EXE self-test did not pass" }
    $releaseVersion = & $buildPython -c "from trainer.runtime import VERSION; print(VERSION)"
    if ($LASTEXITCODE -ne 0) { throw "Cannot read release version" }
    New-Item -ItemType Directory -Force release | Out-Null
    $releaseStem = "RA2Trainer-v$releaseVersion-windows-x64"
    Copy-Item -LiteralPath dist/RA2Trainer.exe -Destination "release/$releaseStem.exe"
    Compress-Archive -LiteralPath dist/RA2Trainer.exe, README.md -DestinationPath "release/$releaseStem.zip" -Force
    $releaseHashes = Get-FileHash -Algorithm SHA256 -LiteralPath "release/$releaseStem.exe", "release/$releaseStem.zip"
    $releaseHashes | ForEach-Object { "$($_.Hash.ToLower())  $(Split-Path -Leaf $_.Path)" } |
        Set-Content -Encoding ascii -LiteralPath release/SHA256SUMS.txt
    Write-Host "Release files: $((Resolve-Path release).Path)"
} finally {
    $env:PATH = $originalBuildPath
    Pop-Location
}
