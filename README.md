# MovieLens Analytics

Data Engineering, IIT Palakkad.

## Install

Tested with Python 3.12 on Linux x86_64. The bootstrap downloads MongoDB 8.0.38 and the official MovieLens 25M archive into this project. Nothing is installed system-wide.

From this directory:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

## Set up the database

```sh
.venv/bin/python scripts/manage.py setup
```

This creates a private `.env` if needed, downloads and verifies the runtime/data, starts a single-node MongoDB replica set, imports the complete dataset, then stops the MongoDB process it started. Let the import finish before starting the app.

Open `.env` locally for the generated administrator and regular-user credentials. They are never committed to this repository. `.env.example` lists the fields. A normal setup rerun preserves completed imports, accounts and app ratings.

## Run

```sh
.venv/bin/python scripts/manage.py run
```

Open `http://127.0.0.1:5008`. Regular users can explore movies and rate them. Administrators can open `/admin` to inspect accounts, change another account's role and view rating history.

Press Ctrl+C to stop the app and any MongoDB process started by this command. A MongoDB instance that was already running is not stopped.

The Flask server is for the local course demonstration. Keep it bound to localhost. A replica-set MongoDB deployment is required for rating transactions.

## Tests

```sh
uv pip install --python .venv/bin/python -r requirements-dev.txt
```

While `scripts/manage.py run` is running in another terminal:

```sh
env MONGO_URI='mongodb://127.0.0.1:27118/?replicaSet=movielens-rs' TEST_MONGO_URI='mongodb://127.0.0.1:27118/?replicaSet=movielens-rs' .venv/bin/python -m pytest -q tests
```

The verified suite has 51 passing tests, including real MongoDB integration. Tests create and remove isolated `movielens_test_*` or `movielens_loader_test_*` databases. Tiny loader fixtures are explicitly synthetic and are not used for dashboard screenshots.

Read-only full-dataset verification:

```sh
.venv/bin/python scripts/verify_dataset.py
```

Browser checks and screenshot regeneration, with Chromium installed locally:

```sh
.venv/bin/python scripts/capture_dashboard.py
```

The browser check uses normal login, submits and replaces a demo rating, changes and restores the regular user's role, and saves screenshots. It changes the local demo database. Credentials and cookies are not written to its evidence files.

## Data and queries

- All 25,000,095 original ratings and 1,093,360 source user tags are retained.
- Movie documents store exact rating sums, counts, averages, histograms, external IDs and the five highest genome relevance scores. The complete original ZIP is retained.
- Genre/tag counts are prepared during ingestion. Rankings and genre views are queried from MongoDB.
- Ranking uses the mean rating. Minimum counts are 500 overall and 20 for a release year, falling to 5 if fewer than five movies qualify for that year.
- The popularity distribution includes unrated movies. Genre coverage counts movie–genre memberships.
- App accounts are separate from the dataset's numeric user IDs. A transaction updates the account's current vote, movie summary and rating-change event together.
- Passwords are hashed. Protected routes reload the role from MongoDB. Forms and JSON submissions carry CSRF tokens. There is no query-parameter login shortcut.
- Chart.js is vendored locally. The dashboard needs no font or chart-library CDN.

## Screenshots

### User dashboard

Movie search, personal ratings, genome tags and the rating distribution.

![User dashboard](docs/screenshots/user-dashboard.png)

### Administrator dashboard

Dataset totals, account roles and rating history. The screenshot shows local demo accounts.

![Administrator dashboard](docs/screenshots/admin-dashboard.png)

## Files

- `load_data.py`: chunked ingestion, cleaning, summaries, indexes and account seeding.
- `app.py`: login, permissions, analytics APIs and rating transactions.
- `templates/`, `static/`: user/admin views and charts.
- `scripts/manage.py`: local setup, startup and shutdown.
- `tests/`: regression tests and labelled synthetic loader fixtures.
- `docs/screenshots/`: user and administrator dashboard screenshots.
- `verification/`: locally generated dataset and browser-check results, excluded from Git.

To intentionally replace dataset collections, stop the app and run `load_data.py --zip data/ml-25m.zip --reset`. This also removes current app ratings and rating events. User accounts are preserved. Normal setup does not pass `--reset`.
