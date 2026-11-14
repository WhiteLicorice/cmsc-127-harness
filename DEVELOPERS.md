# CMSC 127 Harness for Developers

This file is for the harness developer. The student route is in
[README.md](README.md).

## Build the Page

The parser and the comparator are Python files. The page uses Python and
TypeScript.

`public/` holds the page and its assets:

- `index.html` and `style.css`, the page shell and its style.
- `vendor/mermaid.min.js` and `vendor/MERMAID-LICENSE.txt`, the diagram script
  and its license.
- `app.js`, `panelRegistry.js`, `panelRequest.js`, and `panels/*.js`, the
  compiled TypeScript modules.

The Python server uses the packages in `requirements.txt`. Install Node.js
26.13.1 and npm 11.20.0 before the build. Then run two commands from the
repository root:

```bash
npm ci
npm run build
```

`npm ci` installs the exact package versions in `package-lock.json`.
`npm run build` runs the strict type check and compiles `src/` into `public/`.
The compiled files land beside `index.html`. Git leaves them out of the
repository, so the first `start.sh` run builds them.

The page loads `/app.js`, `/style.css`, and `/vendor/mermaid.min.js`. The server
serves `public/` as the asset root. The page needs no network because the
Mermaid script sits in `public/vendor/` with its license.

The page project uses these tool versions:

| Tool | Version | Selection reason |
|---|---|---|
| Node.js | 26.13.1 | Pinned in `.nvmrc` and `package.json`. |
| npm | 11.20.0 | No separate pin. |
| TypeScript | 5.9.3 | Passes the strict type check. |
| Vitest | 5.1.0 | Declares an engine range that includes Node 26. |
| Vite | 6.4.6 | Satisfies the Vite 6 peer requirement of Vitest. |
| Pyright | 1.1.414 | Runs the strict Python check. |
| Playwright | 1.64.1 | Runs the headless browser smoke. |
| Python | 3.15 or newer | Runs the server and the tests. |
| Mermaid | 12.3.0 | Stored with its MIT license, so the page needs no network. |

## Install and Test

Start PostgreSQL first. Use a disposable database and a role that can create
schemas and tables. The tests drop the schemas `t04_compare`, `t04_empty`, and
`t04_search_first`. They also drop the table `public.t04_default`.

The Python tests read the compiled page in `public/`. Run `npm ci` and
`npm run build` before the Python tests.

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

Run `python -m pyright` from the repository root to check the Python files. Run
`npm test` to run the page tests.

`start.sh` builds the page on the first run, when `public/app.js` is missing.
A student runs no build step.

The browser checks need a disposable database. `npm run smoke:browser` creates
a `public.smoke_student` table, loads the page in Chromium without a window,
and drops the table when it finishes. `npm run smoke:schema` needs
`CMSC127_PAIR_DATABASE_URL`, a database whose only schema is `pair` with a
`student` table. The workflow creates that database before the check.

The WSL route uses Ubuntu 24.04.5, Python 3.15.2, and PostgreSQL 18.7. The
harness requires Python 3.15 or newer and PostgreSQL 18 on every platform. On
WSL, `psql` and `pg_isready` are on `PATH`.

## Test the Runner

Set `CMSC127_RUNNER_DIR` to a local harness checkout to test the runner before
a release.

The harness self-tests use a separate scratch database. Run them with
`./check.sh --self-test`. The GitHub workflow runs the self-tests and the same
staged checks from a clean checkout on Ubuntu, macOS, and Windows.
