# CMSC 127 Harness

The harness is a local toolchain for PostgreSQL work.

## Quick Start

The page shows your PostgreSQL database. It talks to the harness server on your
computer. That server reads the database. The steps use Git Bash, the shell that
comes with Git for Windows.

### Get the Folder

Run `git clone https://github.com/WhiteLicorice/cmsc-127-harness.git`, then
`cd cmsc-127-harness`. Run every later step in that folder.

### Before You Start

Each line gives one requirement, why the page needs it, and how to check it.
Skip a line when the check passes. A failed check states its repair on the same
line.

- Git, which clones the folder. Check it with `git --version`. If the command
  is missing, install Git from <https://git-scm.com/downloads>.
- PostgreSQL 18, the database server that holds your tables. The harness server
  reads it every second. Check it with `pg_isready -h 127.0.0.1`. If your server
  uses another port, add `-p` and that number. The reply is
  `accepting connections`. If the command is missing, add the PostgreSQL `bin`
  folder to `PATH`. The database doctor reads the server version and fails when
  the server is older than 18.
- Python 3.15.x, which runs the harness server. The doctors and the Block 1
  checker accept the 3.15 series and reject a later series, such as 3.16. On
  macOS and Linux, check it with `python3.15 --version`, the command that
  Block 1 installs. On Windows, check it with `py -3.15 --version`, which
  selects an installed 3.15. The reply starts with `Python 3.15`. If the command
  is missing or the reply shows another version, install Python 3.15 from
  <https://www.python.org/downloads/>. Step 1 uses `python3.15` or `py -3.15`
  to create the environment. After step 2 activates it, `python` means the
  environment's interpreter. If `python` still reports the system version, the
  environment isn't active. Rerun the activation command from step 2.

In PowerShell, replace the activation command with
`.\.venv\Scripts\Activate.ps1` and the `export` command with
`$env:CMSC127_DATABASE_URL = '...'`. An activated environment provides
`python` in PowerShell too. Run `bash start.sh` from Git Bash, since
PowerShell may find a different `bash`.

### Steps

1. Create the Python environment. On macOS and Linux, run
   `python3.15 -m venv .venv`. On Windows, run `py -3.15 -m venv .venv`. Skip
   this step when the folder already holds `.venv`.
2. Activate it. Run `source .venv/Scripts/activate`. On macOS and Linux, run
   `source .venv/bin/activate`.
3. Install the Python packages into the environment. Run
   `python -m pip install -r requirements.txt`. The file holds the PostgreSQL
   driver.
4. Set the database URL. Run
   `export CMSC127_DATABASE_URL='postgresql://your_user:your_password@127.0.0.1:5432/your_database'`.
   Replace `your_user`, `your_password`, and `your_database` with your
   PostgreSQL role, its password, and your database. The password is the one
   you set for the role when you installed PostgreSQL.
   If the password contains URL punctuation, percent-encode it before putting
   it in the URL. For example, write `@` as `%40`.
5. Start the harness server. Run `bash start.sh`. The script prints the address
   and opens the local page in your browser. Then open
   <https://whitelicorice.github.io/cmsc-127-harness/> in Chrome or Edge, or in
   Firefox 151 or later. If the harness server printed another port, add `?port=`
   and that number to the link. Click `Connect` in the Status panel. The page is
   ready when the Status panel lists the harness server and the database.

## The Details

The harness has a web page that shows your database while you work. It also
compares your entity-relationship diagram (ERD) with the live schema, shows the
last result from `bash check.sh`, and reports a missing tool. A pair is two
students who work together.

### The Harness Page

The page shows a PostgreSQL database while you work. Keep it open beside your
terminal.

#### Start the Harness Server

The Quick Start sets up and starts the harness server. This section covers what
the script does.

The folder holds the built page, so `start.sh` doesn't build it. If
`public/app.js` is missing, the script prints `The page files are missing from
this bundle. Download the bundle again and unzip the whole folder.` and stops
with a nonzero status.

The script starts the harness server and opens the local page in the default
browser. It prints the address, usually `http://127.0.0.1:55440/`. The hosted
page is the one to use, so open
<https://whitelicorice.github.io/cmsc-127-harness/> after the script starts the
server. Open the printed address in the three cases under "The Local Page"
below. Press Ctrl+C in the terminal to stop the harness server. A file from a
zip archive may have no executable bit. Run the script through `bash` in that
case.

The harness server binds to `127.0.0.1`, the loopback address. It accepts
connections from this computer only.

The Schema panel shows the ERD from the `README.md` in the folder where you
started the harness server. The Checks panel shows the last result from
`.cmsc127/last-run.json` in that folder.

#### Open the Page

The hosted page loads from <https://whitelicorice.github.io/cmsc-127-harness/>.
That page is the one to use. A page fix reaches you with no new bundle. The new
page loads the next time you open it. The harness server still runs on your
computer and reads your own database.

Start the harness server first, as the previous section describes. Then open
the link in Chrome or Edge, or in Firefox 151 or later.

The page talks to the harness server at `http://127.0.0.1:55440`. When it
prints another port, add it to the link as `?port=` and load the link again, for
example <https://whitelicorice.github.io/cmsc-127-harness/?port=55441>.

Click `Connect` in the Status panel. If the browser asks for permission to
connect to your computer, allow it.

The page works offline after the Status panel shows "Saved for offline use."
Check for it before you go offline.

#### The Local Page

The local page is the fallback. Open the address that the harness server prints
in three cases:

- The browser is Safari, which does not support the hosted page.
- The harness server is older than v1.33.
- The machine is offline before the hosted page saves its copy.

The hosted page requires harness server v1.33 or later. A harness server the
page can read, but that has no supported version, shows `This harness server is
too old for the hosted page. Use the local page that the server prints.` A
harness server the page cannot read shows the connection message, which gives
the v1.33 rule and the local address. v1.32 and earlier, and a stopped harness
server, take that path.

#### The Seven Panels

The page has seven panels. Use the tabs at the top of the page to switch
panels. A schema is a named group of tables. The schema selector sets the
schema for the Tables, Schema, and Roles panels.

| Panel | What it shows |
|---|---|
| Tables | Every table and view in the selected schema, with its rows. A view is a saved query that acts like a table. Each row that is new, changed, or gone since the last poll carries a mark. Rows compare by primary key, the column or columns that identify a row, or by the whole row when the table has no primary key. The panel shows at most 100 rows per table and states the cap. |
| Changes | Each change that the polls observed, in time order: the time, the table, the kind of change, and the key. The feed keeps the last 100 observations. It needs no hook in your code. |
| Schema | The live schema as a diagram, beside the ERD from the `README.md` in the folder where you started the harness server. The panel lists each difference, each relationship in words, each `NOT NULL` column, and each table constraint. The diagram shows each column type. |
| Sessions | Each other open connection to the database (a session), whether it holds an open transaction (a group of statements that either all take effect or none do), and the session it waits for. |
| Checks | The last result from `check.sh`: each case, pass or fail, and the expected rows beside the actual rows for a failed case. The panel shows `.cmsc127/last-run.json`. It does not start a run. |
| Roles | Each database role, which is a user or a group of users, and its privileges on each table, such as `SELECT` or `INSERT`. |
| Status | The harness server, the database, the connected role, and the time of the last poll. A plain message appears when the harness server is down or the password is wrong. |

#### What the Page Looks Like

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

![The Status panel reporting the harness server and the last poll](docs/screenshots/status.png)

The Status panel reports the harness server, the database, the connected role,
and the time of the last poll.

#### What the Page Shows

The page is read-only. The harness server sends `SELECT` statements and reads
PostgreSQL's catalog. No query box or edit control sits on the page. Every write
comes from your own Python or `psql`.

The page shows committed data. A transaction that another session has open
stays invisible until that session runs `COMMIT`.

The page polls the harness server every second. It shows at most 100 rows per
table and keeps the last 100 change events. A change outside the row cap may not
appear in the feed.

#### Limits

No test has checked the Windows firewall. The harness server binds to `127.0.0.1`,
so it accepts connections from this computer only.

The local page loads its assets from its own folder and needs no network. No
test has started it on Windows with the network disconnected. The hosted page
needs the network on its first load. It runs offline after the Status panel
saves a copy.

The page ran in Edge on Windows. The GitHub workflow loads it in Chromium
without a visible window on Ubuntu, macOS, and Windows. It loads the local page
in Firefox and in the WebKit engine that Safari uses. It also opens the
published page in Chromium after a deploy from `main`. That run uses a harness
server on the same machine. The default browser on macOS and Linux remains
unchecked. The workflow doesn't prove that the default browser opens and draws
the page.

### Compare an ERD

Run the Quick Start first. Then run this example from the folder that holds
`comparator.py`.

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
from PostgreSQL's catalog. It doesn't use the connection's `search_path`.
Each `Difference` has a property, subject, ERD value, schema value, and message.

The comparator checks table identifiers, column identifiers, primary keys,
foreign keys, and relationship endpoints. It reads cardinality, how many rows
sit on each side of a relationship, from foreign keys, column nullability, and
unique constraints. It doesn't compare types, indexes, defaults, or `CHECK`
constraints. It doesn't count stored rows.

When the selected schema has no tables, the comparator returns one message.
The message includes the schema identifier and asks whether your migrations
(SQL files that create and change your tables) ran against this database.

### Check Python Types

The harness requires Python 3.15.x. Run the Quick Start first.

Pyright is a type checker for Python. Pyright 1.1.414 installs with the Python
packages: run `python -m pip install -r requirements.txt`. Then run `pyright`
from the folder that holds `pyrightconfig.json`. The command checks the
application and test files in strict mode. Pyright excludes `.venv` and Python
bytecode caches.

The Pylance extension (`ms-python.vscode-pylance`) reads `.vscode/settings.json`
and checks the whole workspace in strict mode.

### Run Staged Checks

Each pair's project folder has one `./check.sh` entry point. The first run
fetches a fixed runner tag by its full Git reference and keeps it in the local
cache. Later runs use the cache and need no network access.

Set `CMSC127_DATABASE_URL` in the shell before you run the checks. Use a local
PostgreSQL role that can create and drop databases. The runner creates a new
scratch database for each run. A scratch database is a temporary database for
one run. The runner applies migrations in filename order, then calls
`./run prepare` to load data. It drops the scratch database after the run. It
doesn't use or change the database named in the URL.

Use `NNNN_lowercase_description.sql` for migration files. Start with `0001` and
use consecutive numbers. The runner stops when a number is missing or a file
doesn't use this form. Keep schema changes in these committed SQL files. Keep
the loader in `load.sql`.

A stage is one group of checks, one for each lab. The runner reads
`cases/manifest.json`, each `cases/published/<lab>/manifest.json`, and
`cases/pair/manifest.json`. Each listed case has an `.expected` JSON sidecar
and may have an `.exit` sidecar. An unlisted case file fails before the runner
creates a database. The runner reads all cases up to the requested stage.
Stage 2 includes stage 1. `./check.sh --select-stage` prints the stage in
`.stage` at the repository root.

A pair adds its own cases in `cases/pair/manifest.json`. The pair manifest uses
the same fields. Its files stay inside `cases/pair/`. The starter holds each
lab's published files under `cases/published/<lab>/`. Those folders are
protected. Pair checks belong in the pair manifest.

Result data uses a JSON object with `columns` and `rows` fields. SQL `NULL`
uses JSON `null`. Numeric values compare by numeric value, so `1`, `1.0`, and
`1.00` match. Column names and column order must match. Row order must match
unless the manifest sets `ordered` to `false`. The runner compares duplicate
rows. A failed comparison prints the expected rows and the actual rows.

The runner supports ten case kinds. `output` calls the pair's `./run` command.
`schema` compares ERD text with PostgreSQL. `reject` checks that SQL fails.
`allow` checks that SQL succeeds under a named role, then rolls it back.
`state` checks rows after an operation. `race` runs SQL steps on two sessions
in the manifest order. `query` runs a SQL file directly. `document` checks the
structure of a Markdown file and the entries for each warning row. `report` runs
a report SQL file named in a JSON body. `view` reads the catalog to prove that a
report reads a required view.

For a restricted-role check, add a lowercase `role` field and set the expected
SQLSTATE. A `reject` case expects `42501`, the insufficient-privilege code. An
`allow` case expects `00000`, the success code, and the runner supplies that
code when the case omits it. The runner creates a temporary probe role, grants
it membership in the named role, and runs the insert as that probe. An `allow`
case rolls its insert back. The runner removes the probe role after the check.
The named role must exist in the database created by the pair's migrations.

A `state` body can set `operation` to `restore`. The runner calls the pair's
backup and restore commands, then compares `check_sql` rows with the expected
rows. Other `state` operations still run as SQL.

A `query` case may set `document` to `true` when it owns the `document` fact.
The runner then checks that the pair's `column_mapping` view maps that fact to a
`jsonb` column that holds at least one non-null value.

A row whose first cell starts with `warning:`, whatever the letter case, is a
warning. The runner prints it under its case and counts it on the count line, so
a case with only warnings passes.

An invariant is a rule that must hold for every correct database. The invariant
list runs after the cases. Each invariant checks one property.

| Invariant | What it checks | Limit |
|---|---|---|
| `work_sql` | Runs report files as SQL. Pair Python is not in the path, so it cannot filter their rows. | |
| `normal_form` | Checks every mapped table and column against the catalog, then checks that a primary or unique key enforces the determinant, the column or columns that the other columns depend on. | |
| `feature` | Looks for a named SQL feature in a view definition from the PostgreSQL catalog, or in a report SQL file. | It can miss a feature that the query text hides. It can accept a feature that does no useful work. |
| `index` | Reads the index's key columns from `pg_index` and `pg_attribute`, then reads the structured plan from `EXPLAIN`. It compares the index identity with the catalog, requires that a migration created the index, requires the stated leading key column, and requires the plan's `Index Cond` to name that column. | The check disables sequential scans, so the plan does not prove the optimizer's ordinary choice. |
| `backup_restore` | Calls the pair's commands and compares table rows before and after restore. | |
| `protected` | Checks the hashes of the protected files. | |
| `written` | Checks that required headings have content. | It does not judge the content. |
| `contribution` | Prints commits and changed lines by member and week. | The table is information and does not enter the count. |
| `pyright` | Runs strict Pyright and rejects a `# pyright: ignore` directive. | When Pyright is absent, the check fails with the pip repair. |

Each result line identifies a case or invariant. A failed group doesn't hide
the other group results. A `SKIP` line gives a reason and doesn't count as a
pass. An `INFO` line carries the contribution table and doesn't count. The
final count follows `== result ==` and includes the Pyright result.

The runner writes the last result to `.cmsc127/last-run.json` for the harness
page. A form fault, a file with the wrong structure, stops the run before it
creates a database and reports the file, line, and column.

Run one case with `bash check.sh STAGE --case CASE_ID`, where `STAGE` is the
stage that lists the case. Run the form check without a database with `./lint`.
`./lint` exits 0 when the form is clean and 1 when it finds a fault.
`bash check.sh` runs the same form checks first and prints them under a
`== form ==` heading. The form check prints no expected answer. It prints a
cached warning subject as advice. The next run checks the entry against its own
warnings.

### Run a Doctor

A doctor is a script that checks your setup and reports a missing tool. Start
PostgreSQL first. Set `CMSC127_DATABASE_URL` to a database that the installed
PostgreSQL driver can read. The doctor only opens and closes a connection. It
doesn't run a query or change files or settings. Activate the Python
environment before you run a doctor. This makes it use the installed drivers.

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

Both doctors check the same eight steps: Python 3.15.x, the pinned
`psycopg` 3.3.9 driver, `psql`, `pg_dump`, `pg_ctl`, Git, a listener on the
port in `CMSC127_DATABASE_URL`, and a database connection. `pg8000` 1.31.6 is
the spare driver. The connection check uses it when `psycopg` cannot connect.
The port is 5432 when the variable is absent or holds a port outside 1 to
65535.

The Bash doctor prints a wait line when a server is starting. Both doctors
pass the listener step in that state.
When the driver step fails, the doctors skip the connection check. An active
environment whose Python is not 3.15.x fails the Python step and prints
the recreate command. Each doctor prints the interpreter that ran the driver
and connection checks. Each doctor ends with a step count. It returns zero
only when every step passes.

The doctors read `CMSC127_DATABASE_URL`. They don't print its password.
Keep the password out of screenshots and shared logs.

### For Developers

The build, install, test, and release steps are in
[DEVELOPERS.md](DEVELOPERS.md).
