[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonExecutable = $null
$PythonArgs = @()
$VersionCheck = 'import sys; raise SystemExit(0 if (3, 12) <= sys.version_info[:2] < (3, 15) else 1)'
$PythonLauncher = Get-Command py -ErrorAction SilentlyContinue

if ($null -ne $PythonLauncher) {
    foreach ($Version in @('3.12', '3.13', '3.14')) {
        $CandidateArgs = @("-$Version")
        & $PythonLauncher.Source @CandidateArgs -c $VersionCheck 2>$null
        if ($LASTEXITCODE -eq 0) {
            $PythonExecutable = $PythonLauncher.Source
            $PythonArgs = $CandidateArgs
            break
        }
    }
}

if ($null -eq $PythonExecutable) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $PythonCommand) {
        & $PythonCommand.Source -c $VersionCheck 2>$null
        if ($LASTEXITCODE -eq 0) {
            $PythonExecutable = $PythonCommand.Source
        }
    }
}

if ($null -eq $PythonExecutable) {
    throw 'Python 3.12, 3.13, or 3.14 is required. Install a supported version, then rerun this script.'
}

$VenvPath = Join-Path $ProjectRoot '.venv'
if (-not (Test-Path -LiteralPath $VenvPath -PathType Container)) {
    & $PythonExecutable @PythonArgs -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) {
        throw "Python failed to create the virtual environment (exit $LASTEXITCODE)."
    }
}

$VenvPython = Join-Path $VenvPath 'Scripts\python.exe'
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "pip upgrade failed (exit $LASTEXITCODE)."
}
& $VenvPython -m pip install --editable "${ProjectRoot}[dev]"
if ($LASTEXITCODE -ne 0) {
    throw "Project dependency installation failed (exit $LASTEXITCODE)."
}
& (Join-Path $PSScriptRoot 'install-skill.ps1')

Write-Host "Ready. Copy .env.example to .env and configure OpenAI or audio.cpp."
Write-Host "Then run: .\scripts\run-web.ps1"
