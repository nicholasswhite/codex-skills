[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'The project environment is missing. Run .\scripts\setup.ps1 first.'
}

Push-Location $ProjectRoot
$ExitCode = 0
try {
    & $Python -m transcribe_anything.web.app
    $ExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $ExitCode
