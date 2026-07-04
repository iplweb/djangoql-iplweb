# Describing a schema for an LLM

Want a language model to *write* DjangoQL for your users — turn "books rated
over 4.5 that are still in stock" into `rating > 4.5 and in_stock = True`? The
model needs to know three things: **what** it can query (the fields and the
relations between them), **how** each field may be compared (the operators legal
for its type), and the handful of **grammar** rules that aren't obvious.

`describe_schema_for_llm()` produces exactly that, as a single self-contained
JSON document, by reusing DjangoQL's own schema introspection — the same walk
over related models that powers auto-completion. Drop the JSON into a system
prompt and the model has everything it needs to generate valid queries.

## Library primitive: `describe_schema_for_llm`

```python
from djangoql.llm import describe_schema_for_llm
from djangoql.schema import DjangoQLSchema

from library.models import Book

bundle = describe_schema_for_llm(DjangoQLSchema(Book))
```

`bundle` is a plain, JSON-serializable dict:

```json
{
  "start_model": "library.book",
  "grammar": {
    "shape": "<field> <operator> <value>, combined with `and` / `or` and grouped with parentheses",
    "relations": "cross model boundaries with a dot: author.country.name = \"Poland\"",
    "lists": "membership uses a parenthesized list: x in (\"a\", \"b\")",
    "null": "a nullable field or a relation can be compared to None",
    "strings": "string values are double-quoted; ~ means contains",
    "negation": "there is NO standalone `not` operator. Negate with the operator itself: != , !~ , not in , not startswith , not endswith. Example: publisher != None (NOT: not publisher = None)"
  },
  "models": {
    "library.book": {
      "author": {
        "type": "relation",
        "nullable": false,
        "operators": ["= None", "!= None", "<relation>.<field> (traverse with a dot)"],
        "relates_to": "library.author",
        "match_field": "name",
        "related_values": ["Isaac Asimov", "J.R.R. Tolkien", "Ursula K. Le Guin"],
        "note": "match by traversal: author.name = <value>"
      },
      "genre": {
        "type": "int",
        "nullable": true,
        "operators": ["=", "!=", ">", ">=", "<", "<=", "in", "not in"],
        "example": "genre = 42",
        "choices": ["Science Fiction", "Fantasy", "Non-Fiction"],
        "note": "value should be one of the listed choices"
      },
      "rating": {
        "type": "float",
        "nullable": false,
        "operators": ["=", "!=", ">", ">=", "<", "<=", "in", "not in"],
        "example": "rating = 4.5"
      },
      "title": {
        "type": "str",
        "nullable": false,
        "operators": ["=", "!=", "~", "!~", "startswith", "endswith", "not startswith", "not endswith", "in", "not in"],
        "example": "title ~ \"text\"",
        "label": "Book Title",
        "help_text": "The full title as printed on the cover."
      }
    },
    "library.author": { "...": "..." }
  },
  "examples": [
    "id = 1",
    "id > 10 and id < 100",
    "id in (1, 2, 3)",
    "id = 1 or id = 2",
    "(id > 1 and id < 5) or id = 10"
  ]
}
```

What each part carries:

- **`start_model`** — the root model the query is rooted at.
- **`models`** — every model reachable from the root, keyed by label. This is the
  field *graph*: a field with `"type": "relation"` names the model it
  `relates_to`, so the LLM can traverse `author.country.name` the same way
  introspection does. Only fields that are actually suggested in
  auto-completion are included, so the description matches what a user sees.
- **`operators`** — the operators legal for each field's type. Strings get `~`
  (contains) and `startswith`/`endswith`; booleans get only `=` / `!=`;
  relations are traversed with a dot or compared to `None`.
- **`suggested_values`** — for fields that expose concrete options (choices or an
  autocomplete picker), a sample of real values, so the model can pick valid
  ones rather than guess.
- **`label`** / **`help_text`** — human-readable metadata copied from the
  underlying model field, when it adds information beyond the field name.
- **`choices`** — for a field defined with `choices=`, the closed set of labels
  DjangoQL accepts.
- **`match_field`** / **`related_values`** (or `match_fields` / `related_examples`)
  — for a relation, concrete values from the related model that the LLM can
  match on.
- **`grammar`** / **`examples`** — the non-obvious rules and a few worked queries.

!!! note "Pass the schema *you* expose"
    `describe_schema_for_llm()` takes a schema **instance**, so pass the exact
    `DjangoQLSchema` subclass your admin or view uses (e.g. one that restricts
    fields via `get_fields()` or adds
    [autocomplete pickers](integrating-django-autocomplete-light.md)). The
    description then covers precisely the search space your users have — no more,
    no less. Object-picker fields correctly advertise only `= / != / in / not in`
    despite their string type.

### Field labels and help text

Model fields often carry human-readable metadata that the field *name* alone
doesn't convey — a `verbose_name` like "Book Title", or a `help_text`
explaining what the field means. `describe_schema_for_llm()` copies both onto
the field entry whenever they add information:

- **`label`** comes from the field's `verbose_name`. It is omitted when it
  would only restate the field name (e.g. a field called `title` whose
  auto-generated verbose name is `"title"`) — no point spending prompt tokens
  on a label that says nothing the field name doesn't already say.
- **`help_text`** is copied verbatim whenever the underlying field defines
  one.

Both are hints, not machine-readable constraints: they help the model guess
what a field is *for* before it ever sees a row of data.

### Choice fields

A field defined with Django's `choices=` is a closed set — there's no
guessing a valid value, only the list. `describe_schema_for_llm()` always
emits that list as `choices` (capped at 100 entries), using the
human-readable label side of each `(value, label)` pair — the same label
DjangoQL's own value matching accepts and translates back to the stored
value. A `note` on the entry spells out the constraint: the value should be
one of the listed choices. Because choices live on the field definition, this
costs no database query and needs no opt-in.

### Related-model values

Knowing that `author` is a `library.author` isn't enough for a model to write
`author.name = "J.R.R. Tolkien"` — it also has to know what a real `name`
looks like. `describe_schema_for_llm()` can embed real, matchable values for
a relation directly in its entry, controlled by `max_fk_options`:

```python
from djangoql.llm import describe_schema_for_llm

bundle = describe_schema_for_llm(schema, max_fk_options=50)  # the default
```

or from the command line:

```shell
$ python manage.py djangoql_describe_schema_for_llm library.Book --max-fk-options 50
```

`max_fk_options` is a cardinality gate, not a sample size: a relation's
values are only embedded when the number of *distinct* values is at or under
the threshold, so the model sees the whole domain rather than an arbitrary
slice. Pass `0` to turn this off entirely — relations then fall back to
"traverse with a dot or compare to `None`".

#### Choosing what a relation reveals: `fk_options`

Without any configuration (**auto mode**, below), `describe_schema_for_llm()`
guesses one identifying field per relation. To be explicit — reveal several
fields, a computed string, or nothing at all — set `fk_options` on the
schema:

```python
class BookSchema(DjangoQLSchema):
    fk_options = {
        Book: {
            'author': ['name', 'country'],  # several fields
            'publisher': '__str__',         # fall back to str(obj)
            'editor': False,                # never reveal editor values
        },
    }
```

`fk_options` is keyed by the **model that owns the relation** (here `Book`,
which has the `author`, `publisher` and `editor` foreign keys), then by the
**relation's field name** on that model. Each entry's value — the *spec* —
controls what gets embedded for that relation:

| Spec | Meaning |
| --- | --- |
| `'field_name'` | Emit that field's distinct values, gated by `max_fk_options`. Produces `match_field` + `related_values`. |
| `['field_a', 'field_b']` | Emit each field's distinct values, gated by `max_fk_options`. Produces `match_fields` + a per-field `related_values` dict. |
| `'__str__'` | Emit up to `max_fk_options` rows' `str(obj)`, gated by row count rather than distinct-value count. Produces `related_examples`. |
| `True` | Force the relation's default identifying field (see auto mode), ignoring the `max_fk_options` threshold. Falls back to `'__str__'`-style examples if the related model has no string field. |
| `False` | Never reveal values for this relation, regardless of `max_fk_options`. |
| *(no entry)* | **Auto mode** — see below. |

#### Auto mode

A relation with no `fk_options` entry at all is handled automatically: its
identifying field is picked from the related model's *schema-visible*
fields — so it only ever surfaces a field a user could already search on — a
field literally named `name` if there is one, otherwise the first string
field. If that field's distinct-value count is at or under
`max_fk_options`, its values are embedded exactly as with an explicit
`'field_name'` spec. If the related model exposes no string field, or its
value count exceeds the threshold, auto mode emits nothing for that
relation.

Auto mode also skips any relation whose target model belongs to one of
Django's own sensitive apps — `auth`, `admin`, `contenttypes`, `sessions` —
so schema description never auto-dumps usernames, permission codenames, or
session data into a prompt. An explicit `fk_options` entry for such a
relation overrides the exclusion; the skip only applies to auto mode.

!!! warning "Privacy: auto mode queries your data"
    Every other value in the schema description is either static (field
    names, types, choices) or opt-in (`suggested_values` requires
    `suggest_options` on the field). Auto-mode related values are the one
    exception: by default, any relation under the `max_fk_options` threshold
    has its distinct values queried and embedded, with no explicit opt-in
    beyond calling `describe_schema_for_llm()` at all. If a relation's
    target holds data you don't want surfaced this way — anything not
    already meant to be user-searchable — disable it explicitly with
    `fk_options = {Model: {relation: False}}`, or disable auto mode globally
    with `max_fk_options=0`.

#### Output shape

Depending on the spec (or auto mode's equivalent), a relation's entry gains
one of three shapes:

- **`match_field` + `related_values`** — a single field's distinct values, as
  a list (`'field_name'` spec, auto mode, or `True`).
- **`match_fields` + `related_values`** — several fields' distinct values, as
  a dict keyed by field name (`['a', 'b']` spec).
- **`related_examples`** — `str(obj)` rows, when there's no single field to
  match on (`'__str__'` spec, or as `True`'s fallback when the related model
  has no string field).

Each shape also carries a `note` spelling out how to use it in a query, e.g.
`"match by traversal: author.name = <value>"`.

## Management command: `djangoql_describe_schema_for_llm`

The same description is available from the command line for any model — handy
for building prompts, fixtures, or eval sets:

```shell
# Default schema (all introspectable fields)
$ python manage.py djangoql_describe_schema_for_llm library.Book

# The exact schema your admin/view exposes
$ python manage.py djangoql_describe_schema_for_llm library.Book \
    --schema library.schema.BookSchema

# Compact output, redirected to a file
$ python manage.py djangoql_describe_schema_for_llm library.Book \
    --indent 0 > book_schema.json

# Lower the related-values threshold, or disable auto mode with 0
$ python manage.py djangoql_describe_schema_for_llm library.Book \
    --max-fk-options 10
```

| Argument | Meaning |
| --- | --- |
| `app_label.ModelName` | The model to describe (required). |
| `--schema` | Dotted path to a `DjangoQLSchema` subclass to use instead of the default. |
| `--indent` | JSON indentation (default `2`; `0` for the most compact multi-line output). |
| `--max-fk-options` | Max distinct related-model values to embed per relation (default `50`). `0` disables auto mode; explicit `fk_options` on the schema still apply. See [Related-model values](#related-model-values). |

The command is available in any project that has `'djangoql'` in
`INSTALLED_APPS`.

## Closing the loop: generate → validate → repair

The description teaches the model *what* to write; DjangoQL's own parser and
schema tell you whether it got it right — **without touching the database**:

```python
from djangoql.parser import DjangoQLParser
from djangoql.exceptions import DjangoQLError
from djangoql.schema import DjangoQLSchema

from library.models import Book


def validate_query(query):
    schema = DjangoQLSchema(Book)
    try:
        schema.validate(DjangoQLParser().parse(query))
        return None            # valid
    except DjangoQLError as e:
        return str(e)          # feed this back to the model and ask it to fix
```

Validation errors are written to be actionable — an unknown field yields
`Unknown field: ratng. Did you mean: rating?` — so they make excellent feedback
for a repair step: generate a query, validate it, and on failure hand the error
back to the model for another attempt. Only once `validate_query()` returns
`None` do you run the query against real data.
