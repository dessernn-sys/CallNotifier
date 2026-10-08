# ============================================================================
# make_source_zip.ps1 — сборка архива исходников для релиза
# Использование: powershell -NoProfile -File make_source_zip.ps1 <release_dir>
# ============================================================================

param(
    [Parameter(Mandatory=$true)]
    [string]$ReleaseDir
)

$ErrorActionPreference = "Stop"
$targetZip = Join-Path $ReleaseDir "source.zip"

if (Test-Path $targetZip) {
    Remove-Item -Force $targetZip
}

$excludeDirs  = @('venv','venv_client','.venv','.venv_client','env','.env',
                  'build','dist','__pycache__',
                  '.git','.idea','.vscode','.vs',
                  'audio_cache','client_cache',
                  'docs','.pytest_cache','.mypy_cache','.ruff_cache',
                  'node_modules','.tox','.eggs','.cache','.coverage')
$excludeExts  = @('.log','.db','.bak','.pyc','.exe','.sha256','.tmp')
$excludeFiles = @('config_admin.json','config_client.json','config_server.json',
                  '.env','secrets.json','make_source_zip.ps1')

function Get-FilesRecursive {
    param([string]$Path)
    $result = @()
    $result += Get-ChildItem -Path $Path -File -ErrorAction SilentlyContinue
    $dirs = Get-ChildItem -Path $Path -Directory -ErrorAction SilentlyContinue
    foreach ($dir in $dirs) {
        if ($excludeDirs -contains $dir.Name) { continue }
        $result += Get-FilesRecursive -Path $dir.FullName
    }
    return $result
}

$allFiles = Get-FilesRecursive -Path (Get-Location).Path

$files = $allFiles | Where-Object {
    ($excludeExts  -notcontains $_.Extension) -and
    ($excludeFiles -notcontains $_.Name)
}

Write-Host "Files to archive:" $files.Count

$files | Compress-Archive -DestinationPath $targetZip -Force

Write-Host "source.zip created:" (Resolve-Path $targetZip).Path