# CMSC 127 Harness

The harness is the course's local toolchain. It has a read-only page that shows
a PostgreSQL database, a comparator that checks an entity-relationship diagram
(ERD) against the live schema, a runner that checks a pair's work, and doctor
scripts that report missing tools. Python holds the parser and the comparator.
The page uses Python and TypeScript.

## The Harness Page

The harness page shows a PostgreSQL database while you work. It is the visual
tool for the course. Keep it open beside your terminal.

### Start the Page

Start PostgreSQL first. Install the Python packages in `requirements.txt` and
activate the Python environment (see Install and Test). Open Git Bash. Go to
the bundle root, the folder that holds `start.sh`. Run this command:

```bash
./start.sh
```

The script starts the server and opens the default browser. It prints the
address, usually `http://127.0.0.1:55440/`. If the browser does not open, open
the address that the script prints. Press Ctrl+C in the Git Bash window to stop
the server.

The server binds to `127.0.0.1`, the loopback address. It accepts connections
from this computer only.

The page reads the PostgreSQL connection URL from `CMSC127_DATABASE_URL`. Set
this variable in the same shell before you start the page. For example:

```bash
export CMSC127_DATABASE_URL='postgresql://your_user@127.0.0.1:5432/your_database'
```

Replace `your_user` and `your_database` with your PostgreSQL role and database.
The `check.sh` script, which runs the course checks, reads the same variable.

### The Seven Panels

The page has seven panels. Use the tabs at the top of the page to switch
panels. A schema is a named group of tables. The schema selector sets the
schema for the Tables, Schema, and Roles panels.

| Panel | What it shows |
|---|---|
| Tables | Every table and view in the selected schema, with its rows. A view is a saved query that acts like a table. Each row that is new, changed, or gone since the last poll carries a mark. Rows compare by primary key, the column or columns that identify a row, or by the whole row when the table has no primary key. The panel shows at most 100 rows per table and states the cap. |
| Changes | Each change that the polls observed, in time order: the time, the table, the kind of change, and the key. The feed keeps the last 100 observations. It needs no hook in your code. |
| Schema | The live schema as a diagram, beside the ERD from the `README.md` in the folder where you started the page. The panel lists each difference, each relationship in words, each `NOT NULL` column, and each table constraint. The diagram shows each column type. |
| Sessions | Each other open connection to the database (a session), whether it holds an open transaction (a group of statements that either all take effect or none do), and the session it waits for. |
| Checks | The last result from `check.sh`: each case, pass or fail, and the expected rows beside the actual rows for a failed case. The panel reads `.cmsc127/last-run.json`. It does not start a run. |
| Roles | Each database role, which is a user or a group of users, and its privileges on each table, such as `SELECT` or `INSERT`. |
| Status | The server, the database, the connected role, and the time of the last poll. A plain message appears when the server is down or the password is wrong. |

### What the Page Reads

The page is read-only. It sends `SELECT` statements and reads PostgreSQL's
catalog. It has no query box and no edit control. Every write comes from your
own Python or `psql`.

The page reads committed data. A transaction that another session has open
stays invisible until that session runs `COMMIT`. Lab 3 uses this behavior to
show isolation.

The page polls the database every second. It shows at most 100 rows per table
and keeps the last 100 change events. A change outside the row cap may not
appear in the feed.

### Limits

The Windows firewall result is unverified. The server binds to `127.0.0.1`,
so a prompt is unlikely. No test has confirmed that a prompt is absent.

The default browser on macOS and Linux is unverified. The page ran in Edge on
Windows. The GitHub workflow loads the page in Chromium without a visible
window on Ubuntu, macOS, and Windows. That check does not prove that each
system's default browser opens and draws the page.

### Build the Bundle

This section is for the harness developer. A student needs no Node.js and no
compiler.

The release bundle holds these files:

- `server.py`, the Python server.
- `comparator.py`, the ERD comparator.
- `erd_parser.py`, the ERD parser.
- `start.sh`, the start script.
- `public/index.html` and `public/style.css`, the page shell and its style.
- `public/app.js`, `public/panelRegistry.js`, `public/panelRequest.js`, and
  `public/panels/*.js`, the compiled TypeScript modules.
- `public/vendor/mermaid.min.js` and `public/vendor/MERMAID-LICENSE.txt`, the
  diagram script and its license.

The Python server uses the packages in `requirements.txt`. Build the bundle
with Node.js 24.9.x and npm 11.6.0:

```bash
npm ci
npm run build
```

`npm run build` runs the strict type check, compiles the TypeScript, and copies
the assets into `dist/bundle`.

The page project uses these tool versions:

| Tool | Version |
|---|---|
| Node.js | 24.9.x |
| npm | 11.6.0 |
| TypeScript | 5.9.3 |
| Vitest | 5.1.0 |
| Vite | 6.4.6 |
| Pyright | 1.1.414 |
| Playwright | 1.64.1 |
| Python | 3.15 or newer |
| Mermaid | 12.3.0 |

Node.js is pinned to 24.9.x in `.nvmrc` and `package.json`. TypeScript 5.9.3
passed the strict type check. Vitest 5.1.0 declares an engine range that
includes Node 24. Vite 6.4.6 satisfies the Vite 6 peer requirement of Vitest.
Pyright 1.1.414 runs the strict Python check. Playwright 1.64.1 runs the
headless browser smoke. Mermaid 12.3.0 is stored with its MIT license, so the
page needs no network.

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
Create `.venv` and install the requirements first. The command checks the
application and test files in strict mode. Pyright excludes `.venv` and Python
bytecode caches. There is no untyped test fixture.

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

A pair adds its own cases in `cases/pair/manifest.json`. The pair manifest uses
the same fields. Its files stay inside `cases/pair/`. The published
manifest stays unchanged, so the protected-file check still passes.

Result data uses a JSON object with `columns` and `rows` fields. SQL `NULL`
uses JSON `null`. Numeric values compare by numeric value, so `1`, `1.0`, and
`1.00` match. Column names and column order must match. Row order must match
unless the manifest sets `ordered` to `false`. The runner compares duplicate
rows. A failed comparison prints the expected rows and the actual rows.

The runner supports six case kinds. `output` calls the pair's `./run` command.
`schema` compares ERD text with PostgreSQL. `reject` checks that SQL fails.
`state` checks rows after an operation. `race` runs SQL steps on two sessions
in the manifest order. `query` runs a SQL file directly.

The invariant list runs after the cases. `work_sql` runs report files as SQL.
Pair Python is not in the path. It cannot filter their rows. `normal_form`
checks every mapped table and column against the catalog, then checks that a
primary or unique key enforces the determinant. `feature` looks for a named SQL
feature in a view definition from the PostgreSQL catalog. It can miss a feature
that the query text hides. It can also accept a feature that does no useful
work. `index` reads the structured plan from `EXPLAIN`. It compares the index
identity with the catalog. It requires that a migration created the index. The
check disables sequential scans, so the plan does not prove the optimizer's
ordinary choice.
`backup_restore` calls the pair's commands and compares table rows before and
after restore. `protected` checks published file hashes. `written` checks that
required headings have content. It does not judge the content. `contribution`
prints commits and changed lines by member and week. The instructor reads that
table. It does not set a score. `pyright` runs strict Pyright and rejects a
student `# pyright: ignore` directive. When Pyright is absent, the check fails
with the Node.js and npm repair.

Each result line identifies a case or invariant. A failed group does not hide the
other group results. A `SKIP` line gives a reason and does not count as a pass.
An `INFO` line carries the contribution table and does not count. The final
count follows `== result ==` and includes the Pyright result. The runner writes
the last result to `.cmsc127/last-run.json` for the course page. A form fault
stops the run before it creates a database and reports the file, line, and
column.

Run one case with `./check.sh --case CASE_ID`. Run the form check without a
database with `./check.sh --form-lint`. Both commands exit 0 on success, 1 on a
failed check, and 2 on a form fault or a usage error. The form check prints no
expected answer. Set `CMSC127_RUNNER_DIR` to a local harness checkout to test
the runner before a release.

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
npm install --global pyright@1.1.414
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
npm install --global pyright@1.1.414
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
