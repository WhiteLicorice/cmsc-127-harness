# CMSC 127 Harness

The harness is the course's local toolchain. Start with the page. The toolchain
also checks your entity-relationship diagram (ERD) against the live schema, runs
your pair's checks, and reports a missing tool.

## The Harness Page

The page shows a PostgreSQL database while you work. Keep it open beside your
terminal.

### Start the Page

Start PostgreSQL first. Install the Python packages in `requirements.txt` and
activate the Python environment, as the first block of your course shows.

Open Git Bash. Go to your project directory. This directory holds `start.sh`,
your `README.md`, and the `.cmsc127` folder. The Schema panel reads the ERD from
the `README.md` in this directory. The Checks panel reads the last result from
`.cmsc127/last-run.json` in this directory.

The page reads the PostgreSQL connection URL from `CMSC127_DATABASE_URL`. Set
this variable in the same shell before you start the page. For example:

```bash
export CMSC127_DATABASE_URL='postgresql://your_user@127.0.0.1:5432/your_database'
```

Replace `your_user` and `your_database` with your PostgreSQL role and database.
The `check.sh` script, which runs the course checks, reads the same variable.

Run this command from the project directory:

```bash
./start.sh
```

The script starts the server and opens the default browser. It prints the
address, usually `http://127.0.0.1:55440/`. If the browser does not open, open
the address that the script prints. Press Ctrl+C in the Git Bash window to stop
the server.

The server binds to `127.0.0.1`, the loopback address. It accepts connections
from this computer only.

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

### What the Page Looks Like

![The Tables panel listing the campus club tables and their rows](docs/screenshots/tables.png)

The Tables panel lists the event, member, and signup tables, with their rows,
in the campus club domain.

![The Changes panel listing an update and an insert](docs/screenshots/changes.png)

The Changes panel lists an update and an insert that the last two polls
observed.

![The Schema panel with the ERD beside the live schema](docs/screenshots/schema.png)

The Schema panel shows the ERD from `README.md` beside the live schema. Both
match, then the panel lists the relationships and the columns and constraints.

![The Sessions panel listing an idle session in a transaction](docs/screenshots/sessions.png)

The Sessions panel lists a second connection to the database, which holds an
open transaction.

![The Checks panel listing one failed case](docs/screenshots/checks.png)

The Checks panel lists five cases, and shows the expected rows and the actual
rows for the failed case.

![The Roles panel listing a role and its privileges](docs/screenshots/roles.png)

The Roles panel lists the role and its privileges on each table.

![The Status panel reporting the server and the last poll](docs/screenshots/status.png)

The Status panel reports the server, the database, the connected role, and the
time of the last poll.

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

No test has checked the Windows firewall. The server binds to `127.0.0.1`,
so it accepts connections from this computer only.

The page loads its assets from the bundle and needs no network. No test has
started it on Windows with the network disconnected.

The page ran in Edge on Windows. The GitHub workflow loads it in Chromium
without a visible window on Ubuntu, macOS, and Windows. The default browser on
macOS and Linux remains unchecked. The workflow does not prove that the default
browser opens and draws the page.

## Compare an ERD

Set `CMSC127_DATABASE_URL` to a PostgreSQL database. Install the requirements
first, as the first block of your course shows. Run this example from the
repository root.

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

The repository requires Python 3.15 or newer. Create `.venv` and install the
requirements first, as the first block of your course shows. Pyright 1.1.414
installs through npm. Install Node.js and npm first, from the official guide at
<https://docs.npmjs.com/downloading-and-installing-node-js-and-npm>. Then run
`npm install --global pyright@1.1.414` and `pyright` from the repository root.
The command checks the application and test files in strict mode. Pyright
excludes `.venv` and Python bytecode caches.

The Pylance extension (`ms-python.vscode-pylance`) reads `.vscode/settings.json`
and checks the whole workspace in strict mode.

## Run Staged Checks

Each pair repository has one `./check.sh` entry point. The first run fetches a
fixed runner tag by its full Git reference and keeps it in the local cache.
Later runs use the cache and need no network access.

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

The invariant list runs after the cases. Each invariant checks one property.

| Invariant | What it checks | Limit |
|---|---|---|
| `work_sql` | Runs report files as SQL. Pair Python is not in the path, so it cannot filter their rows. | |
| `normal_form` | Checks every mapped table and column against the catalog, then checks that a primary or unique key enforces the determinant, the column or columns that the other columns depend on. | |
| `feature` | Looks for a named SQL feature in a view definition from the PostgreSQL catalog. | It can miss a feature that the query text hides. It can accept a feature that does no useful work. |
| `index` | Reads the structured plan from `EXPLAIN`, compares the index identity with the catalog, and requires that a migration created the index. | The check disables sequential scans, so the plan does not prove the optimizer's ordinary choice. |
| `backup_restore` | Calls the pair's commands and compares table rows before and after restore. | |
| `protected` | Checks published file hashes. | |
| `written` | Checks that required headings have content. | It does not judge the content. |
| `contribution` | Prints commits and changed lines by member and week. | The instructor reads the table. It does not set a score. |
| `pyright` | Runs strict Pyright and rejects a student `# pyright: ignore` directive. | When Pyright is absent, the check fails with the Node.js and npm repair. |

Each result line identifies a case or invariant. A failed group does not hide the
other group results. A `SKIP` line gives a reason and does not count as a pass.
An `INFO` line carries the contribution table and does not count. The final
count follows `== result ==` and includes the Pyright result.

The runner writes the last result to `.cmsc127/last-run.json` for the course
page. A form fault stops the run before it creates a database and reports the
file, line, and column.

Run one case with `./check.sh --case CASE_ID`. Run the form check without a
database with `./check.sh --form-lint`. Both commands exit 0 on success, 1 on a
failed check, and 2 on a form fault or a usage error. The form check prints no
expected answer.

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

## For Developers

The build, install, test, and release steps are in [DEVELOPERS.md](DEVELOPERS.md).
