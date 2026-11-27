# Check the Windows tools for CMSC 127.
#
# Run this file in PowerShell before Git Bash is available:
#   powershell -ExecutionPolicy Bypass -File .\doctor.ps1
#
# It reads tool versions and connects to the database in
# CMSC127_DATABASE_URL. It does not install software or change settings.
# It checks the port in CMSC127_DATABASE_URL. The port is 5432 when the
# variable is absent or holds none.

$script:passed = 0
$script:failed = 0
$script:total = 0

function Report-Result($passed, $successMessage, $failureMessage, $repair) {
    $script:total += 1
    if ($passed) {
        Write-Host "ok    $successMessage"
        $script:passed += 1
    } else {
        Write-Host "FAIL  $failureMessage" -ForegroundColor Yellow
        Write-Host "      $repair"
        $script:failed += 1
    }
}

function Find-Application($name) {
    return Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Get-DatabasePort {
    $url = $env:CMSC127_DATABASE_URL
    if ([string]::IsNullOrWhiteSpace($url)) {
        return 5432
    }

    $authority = $url
    $schemeEnd = $url.IndexOf('://')
    if ($schemeEnd -ge 0) {
        $authority = $url.Substring($schemeEnd + 3)
    }
    $authority = [regex]::Split($authority, '[/?#]')[0]
    $hostPort = $authority.Substring($authority.LastIndexOf('@') + 1)
    $portMatch = [regex]::Match($hostPort, ':(\d+)$')
    if (-not $portMatch.Success) {
        return 5432
    }

    $port = [int]$portMatch.Groups[1].Value
    if ($port -lt 1 -or $port -gt 65535) {
        return 5432
    }
    return $port
}

Write-Host "Checking the Windows toolchain."

$pythonOk = $false
$pythonPath = $null
$virtualEnvironment = -not [string]::IsNullOrWhiteSpace($env:VIRTUAL_ENV)
if ($virtualEnvironment) {
    $environmentPython = Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe'
    if (Test-Path -LiteralPath $environmentPython -PathType Leaf) {
        $pythonPath = $environmentPython
    }
} else {
    $python = Find-Application 'py'
    if ($null -ne $python) {
        $pythonPath = $python.Source
    }
}
if ($null -ne $pythonPath) {
    if ($virtualEnvironment) {
        $pythonOutput = @(& $pythonPath --version 2>&1)
    } else {
        $pythonOutput = @(& $pythonPath -3.15 --version 2>&1)
    }
    $pythonExitCode = $LASTEXITCODE
    $pythonVersionOutput = ($pythonOutput | ForEach-Object { "$_" }) -join ' '
    if ([string]::IsNullOrWhiteSpace($pythonVersionOutput)) {
        $pythonVersion = ''
    } else {
        $pythonVersion = [regex]::Match(
            $pythonVersionOutput, '\d+(?:\.\d+)+'
        ).Value
    }
    $pythonOk = $pythonExitCode -eq 0 -and $pythonVersion -match '^3\.15\.'
}
$activateRepair = 'Run .\.venv\Scripts\Activate.ps1, then run .\doctor.ps1 in the same window.'
$createRepair = 'Run py -3.15 -m venv .venv, then .\.venv\Scripts\Activate.ps1, then python -m pip install -r requirements.txt, then run .\doctor.ps1 in the same window.'
$recreateRepair = 'Run py -3.15 -m venv --clear .venv, then .\.venv\Scripts\Activate.ps1, then python -m pip install -r requirements.txt, then run .\doctor.ps1 in the same window.'
$installRepair = 'Install the python.org manager with Add-AppxPackage -AppInstallerFile https://www.python.org/ftp/python/pymanager/pymanager.appinstaller. Open a new PowerShell. Run pymanager install --refresh. Run pymanager install 3.15.'
$pythonRepair = $installRepair
if ($null -eq $pythonPath) {
    if ($virtualEnvironment) {
        $pythonFailure = 'The environment has no Python.'
        $pythonRepair = $createRepair
    } else {
        $pythonFailure = 'The Python command is not on PATH.'
    }
} elseif ([string]::IsNullOrWhiteSpace($pythonVersionOutput)) {
    $pythonFailure = 'The Python 3.15 command did not run.'
} else {
    $pythonFailureOutput = $pythonVersionOutput.TrimEnd()
    if ($pythonFailureOutput.EndsWith('.')) {
        $pythonFailureOutput = $pythonFailureOutput.Substring(
            0, $pythonFailureOutput.Length - 1
        )
    }
    $pythonFailure = "Python reports $pythonFailureOutput."
    if (-not $pythonOk) {
        if ($virtualEnvironment) {
            $pythonRepair = $recreateRepair
        } elseif (Test-Path -LiteralPath '.venv\Scripts\python.exe' -PathType Leaf) {
            $localVersion = @(& '.\.venv\Scripts\python.exe' --version 2>&1) -join ' '
            if ($LASTEXITCODE -eq 0 -and $localVersion -match '3\.15\.') {
                $pythonRepair = $activateRepair
            }
        }
    }
}
Report-Result $pythonOk "Python $pythonVersion runs." $pythonFailure $pythonRepair

$driversOk = $false
if ($pythonOk) {
    if ($virtualEnvironment) {
        $driverOutput = & $pythonPath -B (Join-Path $PSScriptRoot 'doctor_db.py') --drivers 2>&1
    } else {
        $driverOutput = & $pythonPath -3.15 -B (Join-Path $PSScriptRoot 'doctor_db.py') --drivers 2>&1
    }
    $driversOk = $LASTEXITCODE -eq 0
    $driverOutput | ForEach-Object { Write-Host $_ }
    if ($virtualEnvironment) {
        $driverRepair = "Run & `"$pythonPath`" -m pip install -r requirements.txt from this folder."
    } elseif (Test-Path -LiteralPath '.venv\Scripts\Activate.ps1' -PathType Leaf) {
        $driverRepair = 'Run .\.venv\Scripts\Activate.ps1, then run python -m pip install -r requirements.txt from this folder.'
    } else {
        $driverRepair = 'Run py -3.15 -m venv .venv, then .\.venv\Scripts\Activate.ps1, then run python -m pip install -r requirements.txt from this folder.'
    }
    Report-Result $driversOk 'The pinned PostgreSQL driver is installed.' `
        'No pinned PostgreSQL driver passed.' `
        $driverRepair
} else {
    Write-Host 'Skipped the driver and connection checks because Python did not run.'
}

foreach ($tool in @('psql', 'pg_dump', 'pg_ctl')) {
    $application = Find-Application $tool
    $repair = 'Add C:\Program Files\PostgreSQL\18\bin to PATH in Windows settings. Open a new PowerShell. Run the installer if that folder is missing.'
    if ($null -eq $application) {
        Report-Result $false '' "$tool is not on PATH." $repair
        continue
    }

    $toolOutput = @(& $application.Source --version 2>&1)
    $toolExitCode = $LASTEXITCODE
    $toolVersionOutput = ($toolOutput | ForEach-Object { "$_" }) -join ' '
    if ([string]::IsNullOrWhiteSpace($toolVersionOutput)) {
        $toolVersion = ''
        $toolFailure = "$tool did not report a version."
    } else {
        $toolVersion = [regex]::Match(
            $toolVersionOutput, '\d+(?:\.\d+)+'
        ).Value
        $toolFailure = "$tool reports $toolVersionOutput."
    }
    $toolOk = $toolExitCode -eq 0 -and $toolVersion -match '^18\.'
    Report-Result $toolOk "$tool reports PostgreSQL 18.x." $toolFailure $repair
}

$git = Find-Application 'git'
if ($null -eq $git) {
    $gitOk = $false
    $gitFailure = 'Git is not on PATH.'
} else {
    $gitOutput = @(& $git.Source --version 2>&1)
    $gitExitCode = $LASTEXITCODE
    $gitVersionOutput = ($gitOutput | ForEach-Object { "$_" }) -join ' '
    if ([string]::IsNullOrWhiteSpace($gitVersionOutput)) {
        $gitOk = $false
        $gitFailure = 'Git did not report a version.'
    } else {
        $gitOk = $gitExitCode -eq 0
        $gitFailure = "Git reports $gitVersionOutput."
    }
}
Report-Result $gitOk 'Git runs from PATH.' $gitFailure `
    'Install Git for Windows with WinGet. Run winget install --id Git.Git --exact --source winget --accept-package-agreements --accept-source-agreements. If WinGet says Git is in use, close Git Bash, bash.exe, and sleep.exe. Run the install command again.'

$databasePort = Get-DatabasePort
$listener = Get-NetTCPConnection -LocalPort $databasePort -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
$portOk = $null -ne $listener
Report-Result $portOk "A server listens on port $databasePort." `
    "No server listens on port $databasePort." `
    "Run Get-NetTCPConnection -LocalPort $databasePort. If WSL holds the port, run pg_lsclusters in WSL. Select a free port for one server."

if ($pythonOk -and $driversOk) {
    $databaseOk = $false
    if ($virtualEnvironment) {
        $databaseOutput = & $pythonPath -B (Join-Path $PSScriptRoot 'doctor_db.py') 2>&1
    } else {
        $databaseOutput = & $pythonPath -3.15 -B (Join-Path $PSScriptRoot 'doctor_db.py') 2>&1
    }
    $databaseOk = $LASTEXITCODE -eq 0
    $databaseOutput | ForEach-Object { Write-Host $_ }
    Report-Result $databaseOk 'The PostgreSQL driver connects to the configured database.' `
        'The PostgreSQL driver did not connect to the configured database.' `
        'Set CMSC127_DATABASE_URL to a PostgreSQL database that the installed driver can read. Check its password. Check the host and port. Check the server state.'
} elseif ($pythonOk) {
    Write-Host 'Skipped the connection check because the driver did not pass.'
}

Write-Host "$script:passed of $script:total steps complete."
exit $(if ($script:failed -eq 0) { 0 } else { 1 })
