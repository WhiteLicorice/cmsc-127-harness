# CMSC 127 Harness

The harness reads an ERD block, compares it with PostgreSQL, and checks a
student's toolchain. Python holds the parser and the comparator. The doctor
scripts report missing tools and give a repair for each failure.

## Compare an ERD

Set `CMSC127_DATABASE_URL` to a PostgreSQL database. Install the requirements
first (see Install and Test). Run this example from the repository root.

Pass a Psycopg or pg8000 connection, the lines inside a Mermaid fence, and an
optional schema:

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
The message includes the schema identifier and asks whether your migrations
(SQL files that create and change your tables) ran against this database.

## Check Python Types

The repository requires Python 3.15 or newer. Pyright 1.1.414 installs through
npm. Install Node.js and npm first, from the official guide at
<https://docs.npmjs.com/downloading-and-installing-node-js-and-npm>. Then run
`npm install --global pyright@1.1.414` and `pyright` from the repository root.
You do not need npm if you only check types with Pylance in VS Code. Create
`.venv` and install the requirements first. The command checks the application
and test files in strict mode. Pyright excludes `.venv` and Python bytecode
caches. There is no untyped test fixture.

The Pylance extension (`ms-python.vscode-pylance`) reads `.vscode/settings.json`
and checks the whole workspace in strict mode.

## Run Staged Checks

Each pair repository has one `./check.sh` entry point. The script fetches a
fixed runner tag on the first run. It keeps that version in the local cache.
Later runs need no network access. The script fetches the tag by its full Git
reference. It does not fetch a branch.

Set `CMSC127_DATABASE_URL` in the shell before you run the checks. Use a local
PostgreSQL role that can create and drop databases. The runner creates a new
scratch database for each run. It applies migrations in filename order, then
calls `./run prepare` to load data. It drops the scratch database after the
run. It does not use or change the database named in the URL.

Use `NNNN_lowercase_description.sql` for migration files. Start with `0001` and
use consecutive numbers. The runner stops when a number is missing or a file
does not use this form. Keep schema changes in these committed SQL
files. Keep the loader in `load.sql`.

The runner reads `cases/manifest.json`. Each listed case has an `.expected`
JSON sidecar and may have an `.exit` sidecar. A case file that the manifest
does not list fails before the runner creates a database. The runner reads
all cases up to the requested stage. Stage 2 includes stage 1.

Result data uses a JSON object with `columns` and `rows` fields. SQL `NULL`
uses JSON `null`. Numeric values compare by numeric value, so `1`, `1.0`, and
`1.00` match. Column names and column order must match. Row order must match
unless the manifest sets `ordered` to `false`. The runner compares duplicate
rows. A failed comparison prints the expected rows and the actual rows.

The runner supports six case kinds. `output` calls the pair's `./run` command.
`schema` compares ERD text with PostgreSQL. `reject` checks that SQL fails.
`state` checks rows after an operation. `race` runs SQL steps on two sessions
in the manifest order. `query` runs a SQL file directly.

The invariant list runs after the cases. `work_sql` runs report files as SQL,
so pair Python cannot filter their rows. `normal_form` checks that each
determinant and dependent column set shares a table and that a primary or
unique key enforces the determinant. `feature` looks for a named SQL feature
in a view definition from the PostgreSQL catalog. It can miss a feature that
the query text hides, or accept a feature that does no useful work. `index`
checks the plan from `EXPLAIN`. `backup_restore` calls the pair's commands and
compares table rows before and after restore. `protected` checks published
file hashes. `written` checks that required headings have content. It does
not judge the content. `contribution` prints changed lines by Git author and
week. The instructor reads that table. It does not set a score.

Each result line identifies a case or invariant. A failed group does not hide the
other group results. A `SKIP` line gives a reason and does not count as a pass.
The final count follows `== result ==`. The runner writes the last result to
`.cmsc127/last-run.json` for the course page. A form fault stops scoring and
reports its line and column.

The harness self-tests use a separate scratch database. Run them with
`./check.sh --self-test`. The GitHub workflow runs the self-tests and the same
staged checks from a clean checkout on Ubuntu, macOS, and Windows.

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
