# Example project

The repository ships a runnable demo under [`example_project/`][ep] that puts
all the query-UX features together on a realistic, richly related dataset
(books → authors → countries, publishers, genres):

- multi-line queries (`Shift+Enter`),
- pretty-print (a *Format* button → the [`format`](pretty-print.md) endpoint),
- per-branch [record counts](query-breakdown.md) (an *Explain* button →
  the `explain` endpoint),
- [syntax highlighting](syntax-highlighting.md) — a restyled overlay **and** a
  CodeMirror 6 page driven by `DjangoQLHighlight.tokenize()`,
- the Django admin with completion + multi-line + the opt-in highlight overlay.

It deliberately "goes wild" with styling — that part is the demo's own, not the
library's. DjangoQL ships the primitives; the example shows ways to use them.

[ep]: https://github.com/iplweb/djangoql-iplweb/tree/master/example_project

## Run it

```bash
cd example_project
uv run python manage.py migrate
uv run python manage.py seed_demo          # lots of related demo data
uv run python manage.py createsuperuser    # optional, for the admin
uv run python manage.py runserver
```

Then open:

- <http://127.0.0.1:8000/> — overlay demo (multi-line, highlight, Format,
  Explain counts, results)
- <http://127.0.0.1:8000/codemirror/> — CodeMirror 6 variant
- <http://127.0.0.1:8000/admin/> — Django admin

See [`example_project/README.md`][ep] for the full walkthrough, sample queries,
and a map of how each piece is wired.

!!! note "The CodeMirror page needs internet"
    It loads CodeMirror 6 from a CDN. Everything else runs fully offline.
