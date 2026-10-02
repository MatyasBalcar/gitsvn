$ErrorActionPreference = 'Stop'

$gitsvnDirectory = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')

foreach ($gitsvnFile in @('main.py', 'gitsvn.cmd')) {
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
