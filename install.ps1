$ErrorActionPreference = 'Stop'

$gitsvnDirectory = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')

foreach ($gitsvnFile in @(
    'main.py',
    'gitsvn.cmd',
    'gitsvn\__init__.py',
    'gitsvn\models.py',
    'gitsvn\config.py',
    'gitsvn\terminal.py',
    'gitsvn\working_copy.py',
    'gitsvn\snapshots.py',
    'gitsvn\inspection.py',
    'gitsvn\conflicts.py',
    'gitsvn\conflict_ui.py',
    'gitsvn\merge.py',
    'gitsvn\branches.py',
    'gitsvn\app.py',
    'gitsvn\setup.py',
    'gitsvn\cli.py'
)) {
    if (-not (Test-Path -LiteralPath (Join-Path $gitsvnDirectory $gitsvnFile) -PathType Leaf)) {
        throw "Missing gitsvn file: $gitsvnFile"
    }
}

Get-Command python -ErrorAction Stop | Out-Null
Get-Command svn -ErrorAction Stop | Out-Null

$gitsvnUserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$gitsvnAlreadyInstalled = @(
    $gitsvnUserPath -split ';' | Where-Object {
        [Environment]::ExpandEnvironmentVariables($_).Trim().Trim('"').TrimEnd('\') -ieq $gitsvnDirectory
    }
).Count -gt 0

if (-not $gitsvnAlreadyInstalled) {
    $gitsvnNewPath = if ([string]::IsNullOrEmpty($gitsvnUserPath)) {
        $gitsvnDirectory
    } else {
        $gitsvnUserPath + ';' + $gitsvnDirectory
    }
    [Environment]::SetEnvironmentVariable('Path', $gitsvnNewPath, 'User')
}

Write-Output "gitsvn is registered in your user PATH: $gitsvnDirectory"
Write-Output 'Open a new terminal to run gitsvn from any directory.'
