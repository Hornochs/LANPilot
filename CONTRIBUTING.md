# Contributing to LANPilot

Thanks for your interest. LANPilot is built so that support for additional games can be added
by people who know those games; adapters are the most valuable kind of contribution.

Before you start, read [docs/architecture.md](docs/architecture.md) and check
[docs/ROADMAP.md](docs/ROADMAP.md) to see what is planned and what is in progress.

## Development setup

Python 3.11 or newer is required. No Node.js toolchain is needed.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Run the checks that CI runs:

```bash
.venv/bin/ruff check .            # lint, including security rules
.venv/bin/ruff format --check .   # formatting
.venv/bin/mypy                    # strict type checking
.venv/bin/pytest --cov            # tests
```

`ruff format .` fixes formatting, `ruff check --fix .` fixes most lint findings.

Run the application locally:

```bash
export LANPILOT_SERVER__PUBLIC_URL=http://127.0.0.1:9030
export LANPILOT_DATA_DIR=./data
export LANPILOT_SECRET_KEY_FILE=./data/secret.key
export LANPILOT_ENABLE_DEMO_ADAPTER=true
mkdir -p data && .venv/bin/lanpilot secret generate-key --output data/secret.key   # once
.venv/bin/lanpilot user create admin     # once; asks for a password
.venv/bin/lanpilot serve
```

### Database changes

Every change to `src/lanpilot/db/models.py` needs a migration; a test fails otherwise.

```bash
mkdir -p data && .venv/bin/lanpilot db upgrade        # bring the dev database to the newest schema
.venv/bin/alembic revision --autogenerate -m "add servers"
```

Review the generated file in `src/lanpilot/db/migrations/versions/`, give it the next number
(`0002_…`, revision id `"0002"`) and keep `downgrade()` working.

### Frontend

There is no build step. Templates are in `src/lanpilot/web/templates/`, styles in
`static/css/app.css`, behaviour in `static/js/app.js`. The Content-Security-Policy forbids
inline scripts, inline styles and `on…=` attributes; a test enforces this. Third-party
libraries are vendored: change the table in `scripts/vendor_frontend.py`, run the script and
update `NOTICE.md`.

### Packages

`packaging/build.sh` builds a package for the distribution it runs on:

```bash
.venv/bin/python -m build --wheel
sh packaging/fetch-nfpm.sh /tmp/nfpm
NFPM=/tmp/nfpm/nfpm sh packaging/build.sh deb dist/lanpilot-*.whl /tmp/packages
```

`packaging/test-install.sh` installs, upgrades and removes a package and checks the result.
It changes the system it runs on, so use it in a throw-away container only; CI does that
for every supported distribution.

## Ground rules

- **One topic per pull request.** Describe what changed and how you tested it.
- **Tests are part of the change.** New behaviour needs tests; bug fixes need a test that
  fails without the fix.
- **Keep the documentation current.** If you change structure, interfaces or the data model,
  update `docs/architecture.md` in the same pull request. Add user-visible changes to
  `CHANGELOG.md` under "Unreleased".
- **English** for code, comments, documentation and commit messages.
- **No secrets** in commits, issues or logs: no passwords, RCON passwords, tokens or
  packet captures that contain them.

## Licensing of contributions

LANPilot is MIT licensed. By contributing you agree that your contribution is released under
the same license.

- Dependencies must have an MIT-compatible license. GPL-licensed libraries (for example
  `rcon`, `pygbx`, PyPlanet) cannot be used.
- Do not copy code from GPL projects, including Discord-Gameserver-Notifier, unless you are
  its sole author and state that you release it under MIT.
- Record vendored files and code of external origin in [NOTICE.md](NOTICE.md).

## Adding support for a game

A game is supported through an *adapter*: a class that declares its capabilities (status,
map list, map change, restart, players, console) and a connection schema from which the
"add server" form is generated. The interface is described in
[docs/architecture.md](docs/architecture.md#adapter-interface). A step-by-step guide follows
with roadmap step 1.9.

What a good adapter contribution contains:

1. The adapter and, if the protocol is new, a transport client without game logic.
   `src/lanpilot/games/demo.py` is a complete, small example.
2. A protocol simulator or recorded responses so the tests run without a real server, and a
   test that calls `lanpilot.testing.contract.check_adapter()`.
3. `docs/games/<game>.md` with the facts you verified against a real server: ports, commands,
   quirks, and how to enable remote control on the server.
4. No user or server supplied text interpolated into console commands without validation.

If a game has no remote-control protocol, a monitoring-only adapter is still welcome.

## Reporting bugs and requesting features

Use the issue templates. For security problems follow [SECURITY.md](SECURITY.md) instead of
opening a public issue.

## Conduct

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
