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

function Report-Result($passed, $message, $repair) {
    $script:total += 1
    if ($passed) {
        Write-Host "ok    $message"
        $script:passed += 1
    } else {
        Write-Host "FAIL  $message" -ForegroundColor Yellow
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
        $pythonVersion = & $python.Source --version 2>$null
    } else {
        $pythonVersion = & $python.Source -3.15 --version 2>$null
    }
    $pythonOk = $LASTEXITCODE -eq 0 -and $pythonVersion -eq 'Python 3.15.2'
}
Report-Result $pythonOk 'Python 3.15.2 runs.' `
    'Install the python.org manager with Add-AppxPackage -AppInstallerFile https://www.python.org/ftp/python/pymanager/pymanager.appinstaller. Open a new PowerShell. Run pymanager install --refresh. Run pymanager install 3.15.'

$driversOk = $false
if ($pythonOk) {
    if ($virtualEnvironment) {
        $driverOutput = & $python.Source -B (Join-Path $PSScriptRoot 'doctor_db.py') --drivers 2>&1
    } else {
        $driverOutput = & $python.Source -3.15 -B (Join-Path $PSScriptRoot 'doctor_db.py') --drivers 2>&1
    }
    $driversOk = $LASTEXITCODE -eq 0
    $driverOutput | ForEach-Object { Write-Host $_ }
}
Report-Result $driversOk 'A pinned course database driver is installed.' `
    'Run py -3.15 -m venv .venv. Activate .venv. Run python -m pip install -r requirements.txt from the harness folder.'

foreach ($tool in @('psql', 'pg_dump')) {
    $application = Find-Application $tool
    $toolVersion = $null
    if ($null -ne $application) {
        $toolVersion = & $application.Source --version 2>$null | Select-Object -First 1
    }
    $toolOk = $null -ne $toolVersion -and $toolVersion -match '18\.6'
    Report-Result $toolOk "$tool reports PostgreSQL 18.6." `
        'Add C:\Program Files\PostgreSQL\18\bin to PATH in Windows settings. Open a new PowerShell. Run the installer if that folder is missing.'
}

$git = Find-Application 'git'
$gitOk = $null -ne $git
if ($gitOk) {
    $gitVersion = git --version
    $gitOk = -not [string]::IsNullOrWhiteSpace($gitVersion)
}
Report-Result $gitOk 'Git runs from PATH.' `
    'Install Git for Windows with WinGet. Run winget install --id Git.Git --exact --source winget --accept-package-agreements --accept-source-agreements. If WinGet says Git is in use, close Git Bash, bash.exe, and sleep.exe. Run the install command again.'

$service = Get-Service -Name 'postgresql-x64-18' -ErrorAction SilentlyContinue
$serviceOk = $null -ne $service -and $service.Status -eq 'Running'
Report-Result $serviceOk 'The PostgreSQL 18 service is running.' `
    'Start the postgresql-x64-18 service. If the installer returned to the prompt at once, check the newest %TEMP%\installbuilder_installer*.log. Run the installer with Start-Process -Wait when the prompt must wait.'

$listener = Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
$portOk = $null -ne $listener
Report-Result $portOk 'A server listens on port 5432.' `
    'Run Get-NetTCPConnection -LocalPort 5432. If WSL holds the port, run pg_lsclusters in WSL. Select a free port for one server.'

$databaseOk = $false
if ($pythonOk) {
    if ($virtualEnvironment) {
        $databaseOutput = & $python.Source -B (Join-Path $PSScriptRoot 'doctor_db.py') 2>&1
    } else {
        $databaseOutput = & $python.Source -3.15 -B (Join-Path $PSScriptRoot 'doctor_db.py') 2>&1
    }
    $databaseOk = $LASTEXITCODE -eq 0
    $databaseOutput | ForEach-Object { Write-Host $_ }
}
Report-Result $databaseOk 'A course driver connects to the configured database.' `
    'Set CMSC127_DATABASE_URL to the PostgreSQL database for this course. Check its password. Check the host and port. Check the server state.'

Write-Host "$script:passed of $script:total steps complete."
exit $(if ($script:failed -eq 0) { 0 } else { 1 })
