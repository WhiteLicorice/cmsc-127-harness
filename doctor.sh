#!/usr/bin/env bash
# Check the tools that CMSC 127 uses.
#
# Run this file in Git Bash, WSL, or macOS:
#   bash doctor.sh
#
# It reads tool versions and connects to the database in
# CMSC127_DATABASE_URL. It does not install software or change settings.

set -uo pipefail

passed=0
failed=0
total=0

ok() {
    printf 'ok    %s\n' "$1"
    passed=$((passed + 1))
    total=$((total + 1))
}

bad() {
    printf 'FAIL  %s\n' "$1"
    printf '      %s\n' "$2"
    failed=$((failed + 1))
    total=$((total + 1))
}

case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
        os=windows
        python_prefix=(py -3.15)
        python_expected='Python 3.15.2'
        postgres_expected='18.6'
        ;;
    Linux*)
        os=linux
        python_prefix=(python3)
        postgres_expected='18.6'
        if grep -qi microsoft /proc/version 2>/dev/null; then
            python_expected='Python 3.12.3'
            postgres_expected='16.15'
        else
            python_expected='Python 3.15.2'
        fi
        ;;
    Darwin*)
        os=macos
        python_prefix=(python3)
        python_expected='Python 3.15.2'
        postgres_expected='18.6'
        ;;
    *)
        os=unknown
        python_prefix=(python3)
        python_expected='unsupported operating system'
        postgres_expected='unsupported operating system'
        ;;
esac

if command -v python >/dev/null 2>&1 \
    && [ "$(python --version 2>&1)" = "$python_expected" ]; then
    python_prefix=(python)
fi

case "$os" in
    windows)
        python_fix='Install the python.org manager with Add-AppxPackage -AppInstallerFile https://www.python.org/ftp/python/pymanager/pymanager.appinstaller. Open a new PowerShell. Run pymanager install --refresh. Run pymanager install 3.15.'
        ;;
    linux)
        python_fix='On Ubuntu, run sudo apt install python3 python3-venv. Open a new terminal.'
        ;;
    macos)
        python_fix='Install Python 3.15.2 from python.org. Put its bin folder on PATH and open a new terminal.'
        ;;
    *)
        python_fix='Run this doctor in Git Bash, WSL, Linux, or macOS.'
        ;;
esac

case "$os" in
    windows)
        postgres_fix='Run export PATH="/c/Program Files/PostgreSQL/18/bin:$PATH". Open a new Git Bash.'
        ;;
    linux)
        postgres_fix='Run export PATH="/usr/lib/postgresql/16/bin:$PATH". On Ubuntu, install PostgreSQL with sudo apt install postgresql postgresql-client.'
        ;;
    macos)
        postgres_fix='Run brew install postgresql@18. Run export PATH="$(brew --prefix postgresql@18)/bin:$PATH".'
        ;;
    *)
        postgres_fix='Run this doctor in Git Bash, WSL, Linux, or macOS.'
        ;;
esac

case "$os" in
    windows)
        git_fix='Install Git for Windows. Run winget install --id Git.Git --exact --source winget --accept-package-agreements --accept-source-agreements. If WinGet says Git is in use, close Git Bash, bash.exe, and sleep.exe. Run the install command again.'
        ;;
    linux)
        git_fix='On Ubuntu, run sudo apt install git.'
        ;;
    macos)
        git_fix='Run brew install git.'
        ;;
    *)
        git_fix='Run this doctor in Git Bash, WSL, Linux, or macOS.'
        ;;
esac

run_python() {
    "${python_prefix[@]}" "$@"
}

python_ok=false
if command -v "${python_prefix[0]}" >/dev/null 2>&1; then
    python_version=$(run_python --version 2>&1)
    if [ "$python_version" = "$python_expected" ]; then
        ok "Python $python_version runs on $os."
        python_ok=true
    else
        bad "Python reports $python_version on $os." \
            "$python_fix"
    fi
else
    bad 'The Python command is not on PATH.' \
        "$python_fix"
fi

drivers_ok=false
if [ "$python_ok" = true ]; then
    if run_python -B "$(dirname "$0")/doctor_db.py" --drivers; then
        drivers_ok=true
    fi
fi
if [ "$drivers_ok" = true ]; then
    ok 'A pinned course database driver is installed.'
else
    bad 'No pinned course database driver passed.' \
        'Run python -m pip install -r requirements.txt from the harness folder.'
fi

for tool in psql pg_dump pg_ctl; do
    if command -v "$tool" >/dev/null 2>&1; then
        tool_version=$($tool --version 2>&1 | head -n 1)
        if [[ "$tool_version" == *"$postgres_expected"* ]]; then
            ok "$tool reports PostgreSQL $postgres_expected."
        else
            bad "$tool reports $tool_version." \
                "$postgres_fix"
        fi
    else
        bad "$tool is not on PATH." \
            "$postgres_fix"
    fi
done

if command -v git >/dev/null 2>&1 && git --version >/dev/null 2>&1; then
    ok "Git runs on $os."
else
    bad 'Git is not on PATH.' \
        "$git_fix"
fi

database_ok=false
if [ "$python_ok" = true ] && run_python -B "$(dirname "$0")/doctor_db.py"; then
    database_ok=true
fi
if [ "$database_ok" = true ]; then
    ok 'A course driver connects to the configured database.'
else
    bad 'A course driver did not connect to the configured database.' \
        'Set CMSC127_DATABASE_URL to the PostgreSQL database for this course. Check its password. Check the host and port. Check the server state.'
fi

printf '%s of %s steps complete\n' "$passed" "$total"
[ "$failed" -eq 0 ]
