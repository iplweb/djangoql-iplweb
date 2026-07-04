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
        "note": "traverse into the related model with a dot, e.g. author.<field>; or compare the relation itself to None"
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
        "example": "title ~ \"text\""
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
- **`grammar`** / **`examples`** — the non-obvious rules and a few worked queries.

!!! note "Pass the schema *you* expose"
    `describe_schema_for_llm()` takes a schema **instance**, so pass the exact
    `DjangoQLSchema` subclass your admin or view uses (e.g. one that restricts
    fields via `get_fields()` or adds
    [autocomplete pickers](integrating-django-autocomplete-light.md)). The
    description then covers precisely the search space your users have — no more,
    no less. Object-picker fields correctly advertise only `= / != / in / not in`
    despite their string type.

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
```

| Argument | Meaning |
| --- | --- |
| `app_label.ModelName` | The model to describe (required). |
| `--schema` | Dotted path to a `DjangoQLSchema` subclass to use instead of the default. |
| `--indent` | JSON indentation (default `2`; `0` for the most compact multi-line output). |

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
