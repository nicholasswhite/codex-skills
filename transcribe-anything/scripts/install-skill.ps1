[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Source = $ProjectRoot
$CodexHomePath = if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
    Join-Path $env:USERPROFILE '.codex'
} else {
    $env:CODEX_HOME
}
$SkillRoot = Join-Path $CodexHomePath 'skills'
$Target = Join-Path $SkillRoot 'transcribe-anything'

New-Item -ItemType Directory -Path $SkillRoot -Force | Out-Null
if (Test-Path -LiteralPath $Target) {
    $Existing = Get-Item -Force -LiteralPath $Target
    $ExistingTarget = @($Existing.Target) | Select-Object -First 1
    if ($Existing.LinkType -eq 'Junction' -and
        [string]::Equals($ExistingTarget, $Source, [StringComparison]::OrdinalIgnoreCase)) {
        Write-Host "Codex skill already linked: $Target"
        return
    }
    throw "A different skill already exists at $Target. Move or remove it, then rerun this script."
}

New-Item -ItemType Junction -Path $Target -Target $Source | Out-Null
Write-Host "Installed Codex skill: $Target -> $Source"
