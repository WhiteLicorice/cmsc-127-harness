# Check the Windows tools for CMSC 127.
#
# Run this file in PowerShell before Git Bash is available:
#   powershell -ExecutionPolicy Bypass -File .\doctor.ps1
#
# It reads tool versions and connects to the database in
# CMSC127_DATABASE_URL. It does not install software or change settings.

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

Write-Host "Checking the Windows toolchain."

$pythonOk = $false
$python = $null
$virtualEnvironment = -not [string]::IsNullOrWhiteSpace($env:VIRTUAL_ENV)
if ($virtualEnvironment) {
    $python = Find-Application 'python'
} else {
    $python = Find-Application 'py'
}
if ($null -ne $python) {
    if ($virtualEnvironment) {
        $pythonOutput = @(& $python.Source --version 2>&1)
    } else {
        $pythonOutput = @(& $python.Source -3.15 --version 2>&1)
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
    $pythonOk = $pythonExitCode -eq 0 -and $pythonVersion -eq '3.15.2'
}
if ($null -eq $python) {
    $pythonFailure = 'The Python command is not on PATH.'
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
}
Report-Result $pythonOk 'Python 3.15.2 runs.' $pythonFailure `
    'Install the python.org manager with Add-AppxPackage -AppInstallerFile https://www.python.org/ftp/python/pymanager/pymanager.appinstaller. Open a new PowerShell. Run pymanager install --refresh. Run pymanager install 3.15.'

if ($pythonOk) {
    $driversOk = $false
    if ($virtualEnvironment) {
        $driverOutput = & $python.Source -B (Join-Path $PSScriptRoot 'doctor_db.py') --drivers 2>&1
    } else {
        $driverOutput = & $python.Source -3.15 -B (Join-Path $PSScriptRoot 'doctor_db.py') --drivers 2>&1
    }
    $driversOk = $LASTEXITCODE -eq 0
    $driverOutput | ForEach-Object { Write-Host $_ }
    Report-Result $driversOk 'The pinned PostgreSQL driver is installed.' `
        'No pinned PostgreSQL driver passed.' `
        'Run py -3.15 -m venv .venv. Activate .venv. Run python -m pip install -r requirements.txt from the harness folder.'
} else {
    Write-Host 'Skipped the driver and connection checks because Python did not run.'
}

foreach ($tool in @('psql', 'pg_dump')) {
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
    $toolOk = $toolExitCode -eq 0 -and $toolVersion -eq '18.6'
    Report-Result $toolOk "$tool reports PostgreSQL 18.6." $toolFailure $repair
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

$service = Get-Service -Name 'postgresql-x64-18' -ErrorAction SilentlyContinue
$serviceOk = $null -ne $service -and $service.Status -eq 'Running'
Report-Result $serviceOk 'The PostgreSQL 18 service is running.' `
    'The PostgreSQL 18 service is not running.' `
    'Start the postgresql-x64-18 service. If the installer returned to the prompt at once, check the newest %TEMP%\installbuilder_installer*.log. Run the installer with Start-Process -Wait when the prompt must wait.'

$listener = Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
$portOk = $null -ne $listener
Report-Result $portOk 'A server listens on port 5432.' `
    'No server listens on port 5432.' `
    'Run Get-NetTCPConnection -LocalPort 5432. If WSL holds the port, run pg_lsclusters in WSL. Select a free port for one server.'

if ($pythonOk) {
    $databaseOk = $false
    if ($virtualEnvironment) {
        $databaseOutput = & $python.Source -B (Join-Path $PSScriptRoot 'doctor_db.py') 2>&1
    } else {
        $databaseOutput = & $python.Source -3.15 -B (Join-Path $PSScriptRoot 'doctor_db.py') 2>&1
    }
    $databaseOk = $LASTEXITCODE -eq 0
    $databaseOutput | ForEach-Object { Write-Host $_ }
    Report-Result $databaseOk 'The PostgreSQL driver connects to the configured database.' `
        'The PostgreSQL driver did not connect to the configured database.' `
        'Set CMSC127_DATABASE_URL to a PostgreSQL database that the installed driver can read. Check its password. Check the host and port. Check the server state.'
}

Write-Host "$script:passed of $script:total steps complete."
exit $(if ($script:failed -eq 0) { 0 } else { 1 })
