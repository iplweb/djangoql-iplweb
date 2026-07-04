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

#: Cap on emitted choice labels. Larger than MAX_SUGGESTED_VALUES because
#: choices are a closed set -- the LLM should see the whole domain, not a
#: sample -- and they are read from the field definition without a query.
MAX_CHOICE_VALUES = 100

#: Target app-labels whose rows must never be auto-dumped into a prompt.
#: An explicit fk_options entry overrides this exclusion.
SENSITIVE_TARGET_APP_LABELS = frozenset(
    {'auth', 'admin', 'contenttypes', 'sessions'},
)

#: Sentinel: a relation with no fk_options entry falls back to auto mode.
_AUTO = object()


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


def _choice_labels(field):
    """Closed-set choice labels for a field, or None.

    DjangoQL matches choice fields by their human label and translates it back
    to the stored value (``schema.py`` get_lookup_value), so the labels are the
    exact tokens an LLM should put in a query. Choices live on the model field
    and are read without a database query.
    """
    model = getattr(field, 'model', None)
    name = getattr(field, 'name', None)
    if not model or not name:
        return None
    try:
        choices = model._meta.get_field(name).choices
    except (FieldDoesNotExist, AttributeError):
        return None
    if not choices:
        return None
    labels = [str(c[1]) for c in choices]
    return labels[:MAX_CHOICE_VALUES] or None


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
    if verbose is not None:
        verbose = str(verbose).strip()
        default = name.replace('_', ' ').strip()
        if verbose and verbose.lower() != default.lower():
            meta['label'] = verbose
    help_text = getattr(model_field, 'help_text', None)
    if help_text and str(help_text).strip():
        meta['help_text'] = str(help_text).strip()
    return meta


def _default_match_field(schema, related_label):
    """Pick an identifying field among the related model's schema-visible
    fields.

    Restricting to schema fields inherits DjangoQL's own exclusions (e.g. the
    password field is never exposed), so we never surface a sensitive column.
    Also skips fields with ``suggested`` set to False, since those are hidden
    from the emitted schema description too. Prefers a field literally named
    ``name``, else the first string field. Returns None when the related
    model exposes no (suggested) string field.
    """
    fields = schema.models.get(related_label, {})
    name_field = fields.get('name')
    if (
        name_field is not None
        and getattr(name_field, 'suggested', True)
        and getattr(name_field, 'type', None) == 'str'
    ):
        return 'name'
    for fname, f in fields.items():
        if getattr(f, 'suggested', True) and getattr(f, 'type', None) == 'str':
            return fname
    return None


def _distinct_values(related_model, field_name, limit):
    """Distinct string values of ``field_name``; None when over the limit.

    ``limit=None`` forces emission (no cardinality gate), capped at
    MAX_SUGGESTED_VALUES. Any DB/field error yields None so schema description
    never breaks.
    """
    try:
        qs = (
            related_model.objects.order_by(field_name)
            .values_list(field_name, flat=True)
            .distinct()
        )
        if limit is None:
            rows = list(qs[:MAX_SUGGESTED_VALUES])
        else:
            rows = list(qs[: limit + 1])
            if len(rows) > limit:
                return None
        return [str(v) for v in rows if v is not None] or None
    except Exception:
        return None


def _match_field_entry(related_model, relation_name, match_field, limit):
    """Enrichment dict for a single identifying field, or {} if nothing fits."""
    if match_field is None:
        return {}
    values = _distinct_values(related_model, match_field, limit)
    if not values:
        return {}
    return {
        'match_field': match_field,
        'related_values': values,
        'note': 'match by traversal: {}.{} = <value>'.format(
            relation_name,
            match_field,
        ),
    }


def _fk_spec(schema, field, name):
    """Resolve the fk_options entry for ``name`` on ``field.model``.

    Returns the sentinel ``_AUTO`` when there is no entry (auto mode).
    """
    fk_options = getattr(schema, 'fk_options', None) or {}
    return fk_options.get(field.model, {}).get(name, _AUTO)


def _match_fields_entry(related_model, relation_name, match_fields, limit):
    """Enrichment dict for several identifying fields, or {} if none fit."""
    values = {}
    for f in match_fields:
        v = _distinct_values(related_model, f, limit)
        if v:
            values[f] = v
    if not values:
        return {}
    emitted_fields = [f for f in match_fields if f in values]
    return {
        'match_fields': emitted_fields,
        'related_values': values,
        'note': 'match by traversal, e.g. {}.{} = <value>'.format(
            relation_name,
            emitted_fields[0],
        ),
    }


def _str_examples(related_model, limit):
    """Up to ``limit`` ``str(obj)`` rows of the related model, or None.

    ``limit=None`` forces emission capped at MAX_SUGGESTED_VALUES. Gated by
    row count (str() cannot be made distinct in SQL). Any error yields None.
    """
    try:
        if limit is None:
            rows = related_model.objects.all()[:MAX_SUGGESTED_VALUES]
        else:
            if related_model.objects.count() > limit:
                return None
            rows = related_model.objects.all()[:limit]
        return [str(o) for o in rows] or None
    except Exception:
        return None


def _examples_entry(related_model, relation_name, limit):
    """Enrichment dict of str(obj) examples for a relation, or {}."""
    examples = _str_examples(related_model, limit)
    if not examples:
        return {}
    return {
        'related_examples': examples,
        'note': (
            'these are example rows of the related model; match by '
            'traversing to an identifying field, e.g. %s.<field> = <value>'
            % relation_name
        ),
    }


def _relation_values(schema, field, name, max_fk_options):
    """Concrete match values for a relation, honouring fk_options.

    Dispatch on the schema's fk_options entry:
      - False        -> nothing (no query)
      - True         -> force the default field, ignore the threshold
      - 'field'      -> that field's distinct values, gated by threshold
      - ['a', 'b']   -> each field's distinct values, gated by threshold
      - '__str__'    -> str(obj) examples, gated by row count
      - no entry     -> auto: default field, skip sensitive models / over-limit
    Returns {} when nothing should be emitted; never raises.
    """
    related_model = field.related_model
    spec = _fk_spec(schema, field, name)

    if spec is False:
        return {}
    if spec is True:
        match_field = _default_match_field(schema, field.relation)
        if match_field is None:
            return _examples_entry(related_model, name, limit=None)
        return _match_field_entry(related_model, name, match_field, limit=None)
    if isinstance(spec, str) and spec != '__str__':
        return _match_field_entry(
            related_model,
            name,
            spec,
            limit=max_fk_options,
        )
    if isinstance(spec, (list, tuple)):
        return _match_fields_entry(
            related_model,
            name,
            list(spec),
            limit=max_fk_options,
        )
    if spec == '__str__':
        return _examples_entry(related_model, name, limit=max_fk_options)

    # spec is _AUTO
    if max_fk_options <= 0:
        return {}
    if related_model._meta.app_label in SENSITIVE_TARGET_APP_LABELS:
        return {}
    match_field = _default_match_field(schema, field.relation)
    if match_field is None:
        return {}
    return _match_field_entry(
        related_model,
        name,
        match_field,
        limit=max_fk_options,
    )


def describe_field(name, field, schema=None, max_fk_options=50):
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
        if schema is not None:
            entry.update(_relation_values(schema, field, name, max_fk_options))
    else:
        op = '~' if field.type == 'str' else '='
        entry['example'] = '{} {} {}'.format(
            name,
            op,
            _EXAMPLE_VALUE_BY_TYPE.get(field.type, '?'),
        )
        choices = _choice_labels(field)
        if choices:
            entry['choices'] = choices
            entry['note'] = 'value should be one of the listed choices'
        else:
            options = _field_options(field)
            if options:
                entry['suggested_values'] = options
    if getattr(field, 'object_reference', False):
        # Object-picker fields accept only = / != / in / not in against a pk.
        entry['object_reference'] = True
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


def describe_schema_for_llm(schema, max_fk_options=50):
    """Return a JSON-serializable description of ``schema`` for an LLM prompt.

    ``schema`` is an *instance* of a :class:`~djangoql.schema.DjangoQLSchema`
    subclass (e.g. ``MySchema(MyModel)``). Only fields that are actually
    suggested in autocomplete are included, so the description matches what a
    user sees.
    """
    models = {}
    for model_label, fields in schema.models.items():
        models[model_label] = {
            name: describe_field(
                name,
                field,
                schema=schema,
                max_fk_options=max_fk_options,
            )
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
