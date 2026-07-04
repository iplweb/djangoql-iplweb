"""Describe a DjangoQL schema for a Large Language Model.

:func:`describe_schema_for_llm` turns any
:class:`~djangoql.schema.DjangoQLSchema`
into a compact, self-contained, machine-readable description of the entire
search space -- every model, every field, its type, whether it is nullable,
what it relates to (the "dependent" fields), and which operators are legal for
that field type -- plus a grammar cheat-sheet and a few worked examples.

Drop the JSON straight into an LLM system prompt and the model has everything it
needs to generate valid DjangoQL: the field graph tells it *what* it can query,
the per-type operator lists tell it *how*, and the grammar notes cover the few
non-obvious rules (relations traversed with a dot, no standalone ``not``).

The introspection itself is reused from the schema (the same BFS over related
models that powers autocomplete); this module only layers the operator matrix,
examples and grammar notes on top.
"""

from django.core.exceptions import FieldDoesNotExist

from .schema import RelationField


#: Operators that make sense per field ``type``. Derived from
#: :meth:`DjangoQLField.get_operator` combined with the per-type notes in its
#: docstring: ``~`` / ``!~`` (LIKE) apply to strings (and datetimes, which have
#: an explicit LIKE override); BoolField is limited to equality. Negation is not
#: a standalone operator in DjangoQL -- it is baked into ``!=``, ``!~`` and the
#: ``not in`` / ``not startswith`` / ``not endswith`` forms.
OPERATORS_BY_TYPE = {
    'int': ['=', '!=', '>', '>=', '<', '<=', 'in', 'not in'],
    'float': ['=', '!=', '>', '>=', '<', '<=', 'in', 'not in'],
    'date': ['=', '!=', '>', '>=', '<', '<=', 'in', 'not in'],
    'datetime': ['=', '!=', '>', '>=', '<', '<=', '~', '!~', 'in', 'not in'],
    'str': [
        '=',
        '!=',
        '~',
        '!~',
        'startswith',
        'endswith',
        'not startswith',
        'not endswith',
        'in',
        'not in',
    ],
    'bool': ['=', '!='],
}

#: One illustrative right-hand-side value per scalar type, used to render a
#: ready-to-copy example expression for each field.
_EXAMPLE_VALUE_BY_TYPE = {
    'int': '42',
    'float': '4.5',
    'date': '"2021-06-01"',
    'datetime': '"2021-06-01 14:30"',
    'str': '"text"',
    'bool': 'True',
}

#: How many concrete suggestion values (choices / autocomplete options) to embed
#: per field. Enough to teach the LLM the shape of the values without bloating
#: the prompt.
MAX_SUGGESTED_VALUES = 20


def operators_for(field):
    """Return the legal DjangoQL operators for ``field``.

    Relations are special: they are either traversed with a dot or compared to
    ``None``, so they get a descriptive list rather than raw operator tokens.
    Object-picker fields (``object_reference``) match a related row by primary
    key, so despite their string ``type`` they only accept equality/membership.
    """
    if isinstance(field, RelationField):
        return ['= None', '!= None', '<relation>.<field> (traverse with a dot)']
    if getattr(field, 'object_reference', False):
        return ['=', '!=', 'in', 'not in']
    return list(OPERATORS_BY_TYPE.get(field.type, ['=', '!=', 'in', 'not in']))


def _field_metadata(field):
    """`label` (verbose_name) and `help_text` from the underlying model field.

    Skips the auto-generated verbose_name (the field name with underscores
    turned into spaces) so we only emit labels that add information. Returns an
    empty dict for custom fields with no backing model field, and for reverse
    relations whose ``_meta`` entry has no ``verbose_name``.
    """
    model = getattr(field, 'model', None)
    name = getattr(field, 'name', None)
    if not model or not name:
        return {}
    try:
        model_field = model._meta.get_field(name)
    except (FieldDoesNotExist, AttributeError):
        return {}
    meta = {}
    verbose = getattr(model_field, 'verbose_name', None)
    if verbose:
        verbose = str(verbose).strip()
        default = name.replace('_', ' ').strip()
        if verbose.lower() != default.lower():
            meta['label'] = verbose
    help_text = getattr(model_field, 'help_text', None)
    if help_text and str(help_text).strip():
        meta['help_text'] = str(help_text).strip()
    return meta


def describe_field(name, field):
    """Describe a single schema field as a plain, JSON-serializable dict."""
    entry = {
        'type': field.type,
        'nullable': bool(field.nullable),
        'operators': operators_for(field),
    }
    entry.update(_field_metadata(field))
    if isinstance(field, RelationField):
        entry['relates_to'] = field.relation
        entry['note'] = (
            'traverse into the related model with a dot, e.g. '
            '%s.<field>; or compare the relation itself to None' % name
        )
    else:
        op = '~' if field.type == 'str' else '='
        entry['example'] = '{} {} {}'.format(
            name,
            op,
            _EXAMPLE_VALUE_BY_TYPE.get(field.type, '?'),
        )
    if getattr(field, 'object_reference', False):
        # Object-picker fields accept only = / != / in / not in against a pk.
        entry['object_reference'] = True
    options = _field_options(field)
    if options:
        entry['suggested_values'] = options
    return entry


def _field_options(field):
    """Concrete suggestion values for a field, if the schema exposes any.

    Mirrors the autocomplete serializer's guard: only fields flagged with
    ``suggest_options`` are queried, so this never hits the database for an
    ordinary field.
    """
    if not getattr(field, 'suggest_options', False):
        return None
    try:
        options = list(field.get_options(''))
    except Exception:
        # A custom get_options() may need request context we don't have here;
        # a missing value list must never break schema description.
        return None
    return [str(o) for o in options[:MAX_SUGGESTED_VALUES]] or None


def describe_schema_for_llm(schema):
    """Return a JSON-serializable description of ``schema`` for an LLM prompt.

    ``schema`` is an *instance* of a :class:`~djangoql.schema.DjangoQLSchema`
    subclass (e.g. ``MySchema(MyModel)``). Only fields that are actually
    suggested in autocomplete are included, so the description matches what a
    user sees.
    """
    models = {}
    for model_label, fields in schema.models.items():
        models[model_label] = {
            name: describe_field(name, field)
            for name, field in fields.items()
            if field.suggested
        }
    return {
        'start_model': schema.model_label(schema.current_model),
        'grammar': {
            'shape': (
                '<field> <operator> <value>, combined with `and` / `or` '
                'and grouped with parentheses'
            ),
            'relations': (
                'cross model boundaries with a dot: '
                'author.country.name = "Poland"'
            ),
            'lists': 'membership uses a parenthesized list: x in ("a", "b")',
            'null': 'a nullable field or a relation can be compared to None',
            'strings': 'string values are double-quoted; ~ means contains',
            'negation': (
                'there is NO standalone `not` operator. Negate with the '
                'operator itself: != , !~ , not in , not startswith , '
                'not endswith. Example: publisher != None '
                '(NOT: not publisher = None)'
            ),
        },
        'models': models,
        'examples': _examples(schema),
    }


def _examples(schema):
    """A few generic, always-valid example queries.

    Deliberately schema-agnostic so they parse regardless of the model. They
    teach shape (and/or, grouping, lists, contains, None), not specific fields.
    """
    return [
        'id = 1',
        'id > 10 and id < 100',
        'id in (1, 2, 3)',
        'id = 1 or id = 2',
        '(id > 1 and id < 5) or id = 10',
    ]
