# Contributing

Thanks for your interest in improving DjangoQL (iplweb fork)!

## Development setup

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

```shell
uv sync --all-extras      # create the virtualenv and install dev deps
uv run pre-commit install # install the git hook
```

## Running the tests

```shell
uv run pytest
```

The suite runs against SQLite by default.  To run the same suite against a
throwaway PostgreSQL container instead, set `DJANGOQL_TEST_DB=postgres`:

```shell
DJANGOQL_TEST_DB=postgres uv run pytest
```

This requires Docker to be running.  The first run will pull the
`postgres:16-alpine` image from Docker Hub; subsequent runs reuse the local
image.  The `testcontainers[postgres]` and `psycopg[binary]` packages are
included in the `dev` extra and are installed by `uv sync --all-extras`.

The suite runs against the bundled `test_project/`. To check translation
catalogs locally, run `django-admin compilemessages` from the `djangoql/`
directory first (requires the system `gettext` package).

## Linting and formatting

Linting and formatting are handled by [ruff](https://docs.astral.sh/ruff/),
wired through pre-commit.

**Hooks run on _staged files only_** — which is exactly the default behaviour
of `pre-commit` on commit. When you stage and commit a change, ruff
(`ruff check --fix` + `ruff-format`), `pyupgrade`, and `django-upgrade` run on
just the files you touched, so your diff stays scoped to your actual edits.

**Please do not run `pre-commit run --all-files`.** The existing codebase is
intentionally *not* bulk-reformatted: doing so would rewrite large amounts of
untouched, working code (quote styles, line wrapping, syntax modernization)
and bury real changes under mechanical churn. Formatting is applied
incrementally, file by file, as code is naturally edited.

For the same reason, the CI lint job is **informational only** (it never fails
the build). Correctness is enforced by the test matrix; style is enforced by
the pre-commit hook on the lines you change.

## Submitting changes

1. Branch off `master`.
2. Keep each commit focused; let the pre-commit hook lint your staged changes.
3. Make sure `uv run pytest` is green.
4. Open a pull request describing the change and why.

## Translations

Native-speaker review of the auto-translated locales is very welcome — see the
[Internationalization section of the README](README.md#internationalization-i18n)
for how the catalogs are organised.
