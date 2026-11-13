# CMSC 127 Harness

The harness reads an ERD block, compares it with PostgreSQL, and checks a
student's toolchain. Python holds the parser and the comparator. The doctor
scripts report missing tools and give a repair for each failure.

## Compare an ERD

Pass a Psycopg or pg8000 connection, the lines inside a Mermaid fence, and an
optional schema:

Set `CMSC127_DATABASE_URL` to a PostgreSQL database. Install the requirements
first, then run this example from the repository root.

```python
import os
import psycopg

from comparator import compare_erd

erd_text = """erDiagram
    parent {
        int id PK
    }
"""
connection = psycopg.connect(os.environ["CMSC127_DATABASE_URL"])
differences = compare_erd(connection, erd_text)
connection.close()
for difference in differences:
    print(difference.message)
```

The schema argument defaults to `public`. The comparator reads that schema
from PostgreSQL's catalog. It does not use the connection's `search_path`.
Each `Difference` has a property, subject, ERD value, schema value, and message.

The comparator checks table identifiers, column identifiers, primary keys, foreign keys,
and relationship endpoints. It reads cardinality from foreign keys, column
nullability, and unique constraints. It does not compare types, indexes,
defaults, or `CHECK` constraints. It does not count stored rows.

When the selected schema has no tables, the comparator returns one message.
The message includes the schema identifier and asks whether migrations ran against this
database.

## Check Python Types

The repository requires Python 3.15 or newer. Install Pyright 1.1.414 with
`npm install --global pyright@1.1.414`, then run `pyright` from the repository
root. Create `.venv` and install the requirements first. The command checks the
application and test files in strict mode. Pyright excludes `.venv` and Python
bytecode caches. There is no untyped test fixture.

The Pylance extension (`ms-python.vscode-pylance`) reads `.vscode/settings.json`
and checks the whole workspace in strict mode.

## Install and Test

Start PostgreSQL first. Use a disposable database and a role that can create
schemas and tables. The tests drop the schemas
`t04_compare`, `t04_empty`, and `t04_search_first`. They also drop the table
`public.t04_default`.

In PowerShell on Windows, run:

```powershell
py -3.15 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:CMSC127_DATABASE_URL = 'postgresql://postgres:password@127.0.0.1:55435/postgres'
$env:PGHOST = '127.0.0.1'
$env:PGPORT = '55435'
$env:PGUSER = 'postgres'
$env:PGPASSWORD = 'password'
$env:PGDATABASE = 'postgres'
python -m unittest discover --start-directory tests --top-level-directory . --verbose
```

Replace `password` with the password for the disposable database. Use
`postgresql://postgres@127.0.0.1:55435/postgres` when local trust allows a
connection without a password.

In WSL, Linux, or macOS, install the drivers and set the connection variables:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export CMSC127_DATABASE_URL='postgresql://postgres:password@127.0.0.1:55435/postgres'
export PGHOST=127.0.0.1
export PGPORT=55435
export PGUSER=postgres
export PGPASSWORD=password
export PGDATABASE=postgres
python -m unittest discover --start-directory tests --top-level-directory . --verbose
```

The WSL route uses Python 3.12.3 and PostgreSQL 16.15. The Windows and macOS
routes use Python 3.15.2 and PostgreSQL 18.6.
On WSL, add `/usr/lib/postgresql/16/bin` to `PATH` before you run the doctor.

## Run a Doctor

Start PostgreSQL first. Set `CMSC127_DATABASE_URL` to a database that the
installed course driver can read. The doctor only opens and closes a
connection. It does not run a query or change files or settings.
Activate `.venv` before you run a doctor. This makes it use the installed drivers.

In PowerShell, run:

```powershell
$env:CMSC127_DATABASE_URL = 'postgresql://postgres:password@127.0.0.1:5432/postgres'
.\doctor.ps1
```

In Bash, run:

```bash
export CMSC127_DATABASE_URL='postgresql://postgres:password@127.0.0.1:5432/postgres'
bash doctor.sh
```

The PowerShell doctor checks Python 3.15.2, the pinned drivers, PostgreSQL
18.6 tools, Git, the PostgreSQL 18 service, port 5432, and a database
connection. The Bash doctor checks Python, the pinned drivers, PostgreSQL
tools, Git, and a database connection. Each doctor ends with a step count.
It returns zero only when every step passes.

The doctors read `CMSC127_DATABASE_URL`. They do not print its password.
Keep the password out of screenshots and shared logs.
