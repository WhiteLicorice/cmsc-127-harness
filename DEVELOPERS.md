# CMSC 127 Harness for Developers

This file is for the harness developer. A student needs neither Node.js nor a
compiler. The student route is in [README.md](README.md).

## Build the Bundle

Python holds the parser and the comparator. The page uses Python and
TypeScript.

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

| Tool | Version | Selection reason |
|---|---|---|
| Node.js | 24.9.x | Pinned in `.nvmrc` and `package.json`. |
| npm | 11.6.0 | No separate pin. |
| TypeScript | 5.9.3 | Passed the strict type check. |
| Vitest | 5.1.0 | Declares an engine range that includes Node 24. |
| Vite | 6.4.6 | Satisfies the Vite 6 peer requirement of Vitest. |
| Pyright | 1.1.414 | Runs the strict Python check. |
| Playwright | 1.64.1 | Runs the headless browser smoke. |
| Python | 3.15 or newer | The repository requires Python 3.15 or newer. |
| Mermaid | 12.3.0 | Stored with its MIT license, so the page needs no network. |

The page uses TypeScript under the strict compiler setting and Vitest as its
test runner.

## Install and Test

Start PostgreSQL first. Use a disposable database and a role that can create
schemas and tables. The tests drop the schemas
`t04_compare`, `t04_empty`, and `t04_search_first`. They also drop the table
`public.t04_default`.

Install Node.js 24.9.x and npm 11.6.0 first (see Build the Bundle). The Python
tests read `dist/bundle`. Git leaves that directory out of the repository. Run
`npm ci` and `npm run build` before the Python tests. The Python tests have no
untyped fixture.

In PowerShell on Windows, run:

```powershell
py -3.15 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
npm ci
npm run build
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
npm ci
npm run build
npm install --global pyright@1.1.414
export CMSC127_DATABASE_URL='postgresql://postgres:password@127.0.0.1:55435/postgres'
export PGHOST=127.0.0.1
export PGPORT=55435
export PGUSER=postgres
export PGPASSWORD=password
export PGDATABASE=postgres
python -m unittest discover --start-directory tests --top-level-directory . --verbose
```

A student skips `npm ci` and `npm run build`. The release bundle already holds
`dist/bundle`, and `./start.sh` runs it.

The browser checks need a disposable database. `npm run smoke:browser` creates
a `public.smoke_student` table, loads the page in Chromium without a window,
and drops the table when it finishes. `npm run smoke:schema` needs
`CMSC127_PAIR_DATABASE_URL`, a database whose only schema is `pair` with a
`student` table. The workflow creates that database before the check.

The WSL route uses Python 3.12.3 and PostgreSQL 16.15. The Windows and macOS
routes use Python 3.15.2 and PostgreSQL 18.6.
On WSL, add `/usr/lib/postgresql/16/bin` to `PATH` before you run the doctor.

## Test the Runner

Set `CMSC127_RUNNER_DIR` to a local harness checkout to test the runner before
a release.

The harness self-tests use a separate scratch database. Run them with
`./check.sh --self-test`. The GitHub workflow runs the self-tests and the same
staged checks from a clean checkout on Ubuntu, macOS, and Windows.
