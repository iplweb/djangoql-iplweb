import json
import re
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from django.db import models
from django.db.models import Avg, Count, Max, Min, OuterRef, Q, Subquery, Sum
from django.db.models import FloatField as ORMFloatField
from django.db.models import IntegerField as ORMIntegerField
from django.db.models.constants import LOOKUP_SEP
from django.db.models.fields.related import ForeignObjectRel
from django.db.models.functions import Coalesce
from django.urls import resolve, reverse
from django.urls.exceptions import NoReverseMatch
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _

from .exceptions import DjangoQLSchemaError
from .schema import DateField, DjangoQLField, DjangoQLSchema, IntField, StrField


#: Matches a trailing ``[<int>]`` token used to embed an object id in a
#: suggestion string, e.g. ``"Jan Kowalski [42]"``.
_ID_RE = re.compile(r'\s*\[(\d+)\]\s*$')


class DatePartField(IntField):
    """
    Virtual integer field for a date/time part extracted via a Django ORM
    transform, e.g. ``written__year`` -> ``written__year`` lookup.

    The field name IS the ORM lookup, so no get_lookup_name() override is
    needed: get_lookup joins ``path + [name]`` with ``__``.
    """

    def __init__(self, base_field, part, model=None, nullable=False):
        self.base_field = base_field
        self.part = part
        super().__init__(
            model=model,
            name=f'{base_field}__{part}',
            nullable=nullable,
        )


class DateExtractField(DateField):
    """``<datetime>__date`` -> compares to a date in YYYY-MM-DD format."""

    def __init__(self, base_field, model=None, nullable=False):
        self.base_field = base_field
        super().__init__(
            model=model,
            name=f'{base_field}__date',
            nullable=nullable,
        )


class TimeExtractField(DjangoQLField):
    """``<datetime>__time`` -> compares to a time in HH:MM[:SS] format."""

    type = 'time'
    value_types = [str]
    value_types_description = _('times in "HH:MM[:SS]" format')

    def __init__(self, base_field, model=None, nullable=False):
        self.base_field = base_field
        super().__init__(
            model=model,
            name=f'{base_field}__time',
            nullable=nullable,
        )

    def get_lookup_value(self, value):
        if isinstance(value, list):
            return [self._parse_time(v) for v in value]
        return self._parse_time(value)

    @staticmethod
    def _parse_time(value):
        if not value:
            return None
        mask = '%H:%M:%S' if value.count(':') > 1 else '%H:%M'
        return datetime.strptime(value, mask).time()

    def validate(self, value):
        super().validate(value)
        try:
            self.get_lookup_value(value)
        except ValueError:
            raise DjangoQLSchemaError(
                _(
                    'Field "{field}" can be compared to times in "HH:MM[:SS]" '
                    'format, but not to {value}',
                ).format(field=self.name, value=repr(value)),
            )


def _owner_lookup(relation_field):
    """
    Given a to-many relation entry from ``model._meta.get_fields()``, return the
    lookup used to filter the *related* model's rows by the owning instance,
    for building a correlated subquery.

    - Reverse relations (ForeignObjectRel: reverse FK / reverse M2M): the
      forward field lives on the related model -> use its name.
    - Forward M2M (ManyToManyField on the searched model): use the reverse
      query name back to the owner.
    """
    if isinstance(relation_field, ForeignObjectRel):
        return relation_field.field.name
    return relation_field.related_query_name()


class AggregateField(IntField):
    """
    Base class for subquery-backed relation aggregates. Subclasses set
    ``aggregate`` (an aggregate class) and ``aggregate_name``.

    The user-facing field name (e.g. ``book__count``) maps to a collision-safe
    annotation alias (``djangoql_book_count``); the filter is applied to the
    alias, while the path is used to correlate the subquery.
    """

    aggregate = None
    aggregate_name = None

    def __init__(
        self,
        model,
        relation_name,
        related_model,
        owner_lookup,
        name,
        source_field=None,
        # None preserves the subclass-level `nullable` class attr
        nullable=None,
        suggested=True,
    ):
        self.relation_name = relation_name
        self.related_model = related_model
        self.owner_lookup = owner_lookup
        self.source_field = source_field
        super().__init__(
            model=model,
            name=name,
            nullable=nullable,
            suggested=suggested,
        )

    def annotation_alias(self, path):
        # Django resolves annotation aliases by scanning LOOKUP_SEP-prefixes of
        # a filter key, so aliases containing "__" work. The "djangoql" prefix
        # prevents collisions with real model fields or other annotations.
        return LOOKUP_SEP.join(['djangoql', *path, self.name])

    def output_field(self):
        return ORMIntegerField()

    def build_expression(self, path):
        return self._subquery(path)

    def _subquery(self, path):
        outer = LOOKUP_SEP.join(list(path) + ['pk'])
        rel_qs = (
            self.related_model._base_manager.order_by()
            .filter(**{self.owner_lookup: OuterRef(outer)})
            .values(self.owner_lookup)
            .annotate(_agg=self.aggregate(self.source_field or 'pk'))
            .values('_agg')
        )
        return Subquery(rel_qs, output_field=self.output_field())

    def get_annotations(self, path):
        return {self.annotation_alias(path): self.build_expression(path)}

    def get_lookup(self, path, operator, value):
        alias = self.annotation_alias(path)
        op, invert = self.get_operator(operator)
        q = Q(**{f'{alias}{op}': self.get_lookup_value(value)})
        return ~q if invert else q


class CountField(AggregateField):
    aggregate = Count
    aggregate_name = 'count'
    nullable = False  # Coalesce(..., 0) guarantees a non-null integer

    def build_expression(self, path):
        # Coalesce to 0 so "<rel>__count = 0" matches rows with no relations.
        return Coalesce(self._subquery(path), 0)


class NumericAggregateField(AggregateField):
    """Sum/Avg/Min/Max over a numeric field of the related model.

    Unlike CountField, these return SQL NULL for an empty related set, so they
    are nullable and accept numeric comparison values.
    """

    nullable = True
    value_types = [int, float, Decimal]
    value_types_description = _('numbers')

    def output_field(self):
        # Float output is used for v1 simplicity. For a DecimalField source,
        # very large sums could lose sub-unit precision; introduce a
        # Decimal-typed output_field in future if exact typing is needed.
        return ORMFloatField()


class SumField(NumericAggregateField):
    aggregate = Sum
    aggregate_name = 'sum'


class AvgField(NumericAggregateField):
    aggregate = Avg
    aggregate_name = 'avg'


class MinField(NumericAggregateField):
    aggregate = Min
    aggregate_name = 'min'


class MaxField(NumericAggregateField):
    aggregate = Max
    aggregate_name = 'max'


class AggregateSchemaMixin:
    """
    Schema mixin: for every to-many relation (reverse FK + M2M, both
    directions) of a model, add a ``<rel>__count`` field and
    ``<rel>__<numfield>__{sum,avg,min,max}`` for each numeric field on the
    related model (excluding primary keys and FK ids). Relations whose reverse
    accessor is hidden (e.g. related_name='+') are skipped, since a correlated
    subquery needs a usable reverse lookup.
    """

    NUMERIC_FIELDS = (
        models.IntegerField,
        models.FloatField,
        models.DecimalField,
    )
    AGGREGATE_FIELDS = (
        ('sum', SumField),
        ('avg', AvgField),
        ('min', MinField),
        ('max', MaxField),
    )

    def get_fields(self, model):
        fields = list(super().get_fields(model))
        for f in model._meta.get_fields():
            if not (f.is_relation and (f.one_to_many or f.many_to_many)):
                continue
            related = f.related_model
            if related is None:
                continue
            owner = self._aggregate_owner_lookup(f)
            if owner is None:  # hidden/unusable reverse -> skip
                continue
            rel = f.name
            fields.append(
                CountField(
                    model=model,
                    relation_name=rel,
                    related_model=related,
                    owner_lookup=owner,
                    name='%s__count' % rel,
                )
            )
            for nf in related._meta.get_fields():
                if (
                    isinstance(nf, self.NUMERIC_FIELDS)
                    and not nf.is_relation
                    and not getattr(nf, 'primary_key', False)
                    and getattr(nf, 'editable', True)  # skip GFK/internal cols
                ):
                    for agg_name, agg_cls in self.AGGREGATE_FIELDS:
                        fields.append(
                            agg_cls(
                                model=model,
                                relation_name=rel,
                                related_model=related,
                                owner_lookup=owner,
                                source_field=nf.name,
                                name='{}__{}__{}'.format(
                                    rel, nf.name, agg_name
                                ),
                            )
                        )
        return fields

    @staticmethod
    def _aggregate_owner_lookup(f):
        """
        Return the reverse lookup to filter the related model by the owning
        instance (for building a correlated subquery), or None if the
        relation's reverse accessor is hidden/unusable.

        A forward M2M with ``related_name='+'`` sets ``f.remote_field.hidden``
        to True and generates an internal accessor name that cannot be used as
        a real Django lookup. We detect this case and return None so that the
        caller skips the relation entirely.
        """
        if (
            hasattr(f, 'remote_field')
            and f.remote_field is not None
            and getattr(f.remote_field, 'hidden', False)
        ):
            return None
        return _owner_lookup(f)


class DatePartsSchemaMixin:
    """
    Schema mixin: expands every Date/DateTime/Time model field into virtual
    part fields (year, month, ..., hour, minute, second) plus __date/__time
    extraction for DateTimeField.
    """

    DATE_PARTS = (
        'year',
        'month',
        'day',
        'week_day',
        'quarter',
        'week',
        'iso_year',
        'iso_week_day',
    )
    TIME_PARTS = ('hour', 'minute', 'second')

    def get_fields(self, model):
        fields = list(super().get_fields(model))
        for f in model._meta.get_fields():
            # DateTimeField is a subclass of DateField — test it first.
            if isinstance(f, models.DateTimeField):
                parts = self.DATE_PARTS + self.TIME_PARTS
                fields += [
                    DatePartField(f.name, p, model=model, nullable=f.null)
                    for p in parts
                ]
                fields += [
                    DateExtractField(f.name, model=model, nullable=f.null),
                    TimeExtractField(f.name, model=model, nullable=f.null),
                ]
            elif isinstance(f, models.DateField):
                fields += [
                    DatePartField(f.name, p, model=model, nullable=f.null)
                    for p in self.DATE_PARTS
                ]
            elif isinstance(f, models.TimeField):
                fields += [
                    DatePartField(f.name, p, model=model, nullable=f.null)
                    for p in self.TIME_PARTS
                ]
        return fields


class AutocompleteField(StrField):
    """
    A value field whose suggestions come from a pluggable provider, and which
    filters by the embedded object id rather than by a string column.

    Suggestions are formatted ``"<label> [<id>]"``; ``get_lookup_value`` parses
    the trailing ``[<int>]`` back to a primary key and the field filters
    ``<name> = pk``. Typically used to expose a ForeignKey as a *picker*: under
    this field name you filter by the related object, you do not traverse into
    the related model's own fields.

    Three providers supply suggestions (priority high -> low):

    1. ``url`` -- an existing autocomplete endpoint (a url name or local path).
       It is resolved and called *in-process* with the current request, whose
       ``GET[search_param]`` is set to the search term. The endpoint must return
       Select2 JSON: ``{"results": [{"id": .., "text": ..}], ...}``.
    2. ``queryset`` / ``get_queryset`` -- a queryset or a ``search -> queryset``
       callable (DAL-agnostic, full control).
    3. a subclass override of :meth:`get_options` / :meth:`format_label` /
       :meth:`get_id`.

    Config kwargs: ``url``, ``queryset``/``get_queryset``, ``search_fields``,
    ``view``, ``label`` (callable obj->str, default ``str``), ``id_of``
    (callable obj->id, default ``obj.pk``), ``search_param`` (default ``'q'``),
    ``limit`` (default 50).
    """

    suggest_options = True

    def __init__(
        self,
        model=None,
        name=None,
        nullable=None,
        suggested=None,
        url=None,
        queryset=None,
        get_queryset=None,
        search_fields=None,
        view=None,
        label=None,
        id_of=None,
        search_param='q',
        limit=50,
    ):
        super().__init__(
            model=model,
            name=name,
            nullable=nullable,
            suggest_options=True,
            suggested=suggested,
        )
        self.url = url
        self.queryset = queryset
        self._get_queryset = get_queryset
        self.search_fields = list(search_fields) if search_fields else []
        self.view = view
        self.label = label
        self.id_of = id_of
        self.search_param = search_param
        self.limit = limit
        self.request = None

    # -- request threading -------------------------------------------------

    def set_request(self, request):
        """Receive the current request (called by ``SuggestionsAPIView``)."""
        self.request = request

    # -- id parsing --------------------------------------------------------

    def parse_id(self, value):
        """
        Parse a trailing ``[<int>]`` id out of a suggestion string.

        - ``"X [42]"`` -> ``42``
        - ``["A [1]", "B [2]"]`` -> ``[1, 2]``
        - ``"plain"`` (no bracket) -> ``"plain"`` (free-text fallback)
        """
        if isinstance(value, list):
            return [self.parse_id(v) for v in value]
        if isinstance(value, str):
            match = _ID_RE.search(value)
            if match:
                return int(match.group(1))
        return value

    # -- suggestion options ------------------------------------------------

    def format_label(self, obj):
        if self.label is not None:
            return str(self.label(obj))
        return str(obj)

    def get_id(self, obj):
        if self.id_of is not None:
            return self.id_of(obj)
        return obj.pk

    def get_queryset(self, search):
        if self._get_queryset is not None:
            return self._get_queryset(search)
        if self.queryset is not None:
            qs = self.queryset
            if callable(qs):
                qs = qs(search)
            elif self.search_fields and search:
                qs = qs.filter(self._search_fields_q(search))
            return qs
        raise NotImplementedError(
            'AutocompleteField needs a url, a queryset/get_queryset, or a '
            'get_options()/get_queryset() override to provide suggestions.'
        )

    def _search_fields_q(self, search):
        q = Q()
        for field in self.search_fields:
            q |= Q(**{f'{field}__icontains': search})
        return q

    def get_options(self, search):
        # Strip a trailing "[id]" so re-editing "Label [42]" searches by Label.
        match = _ID_RE.search(search or '')
        if match:
            search = (search or '')[: match.start()]
        if self.url:
            return self._options_from_url(search)
        objects = list(self.get_queryset(search)[: self.limit])
        return [
            f'{self.format_label(obj)} [{self.get_id(obj)}]' for obj in objects
        ]

    def _options_from_url(self, search):
        url = self.url
        try:
            path = urlsplit(reverse(url)).path
        except NoReverseMatch:
            path = urlsplit(url).path
        match = resolve(path)
        request = self._clone_request(search)
        response = match.func(request, *match.args, **match.kwargs)
        data = json.loads(response.content)
        results = data.get('results', [])
        return [
            f'{strip_tags(str(item.get("text", "")))} [{item.get("id")}]'
            for item in results[: self.limit]
        ]

    def _clone_request(self, search):
        request = self.request
        if request is None:
            from django.test import RequestFactory

            request = RequestFactory().get('/')
        # Shallow-clone the GET QueryDict with the search param overridden, so
        # we don't mutate the live request shared with the rest of the view.
        get = request.GET.copy()
        get[self.search_param] = search
        request.GET = get
        return request

    # -- lookup / filtering ------------------------------------------------

    def get_lookup_value(self, value):
        return self.parse_id(value)

    def get_lookup(self, path, operator, value):
        parsed = self.parse_id(value)
        has_id = self._has_id(parsed)
        if has_id:
            search = LOOKUP_SEP.join(path + [self.get_lookup_name()])
            op, invert = self.get_operator(operator)
            q = Q(**{f'{search}{op}': parsed})
            return ~q if invert else q
        # Free-text fallback: icontains over search_fields (prefixed by path).
        return self._free_text_lookup(path, operator, value)

    @staticmethod
    def _has_id(parsed):
        if isinstance(parsed, list):
            return bool(parsed) and all(isinstance(v, int) for v in parsed)
        return isinstance(parsed, int)

    def _free_text_lookup(self, path, operator, value):
        # search_fields live on the *related* model, so they must be reached
        # through this field's relation name, e.g. ``author__username`` for an
        # ``author`` FK with ``search_fields=['username']``. Without configured
        # search_fields, fall back to the field's own name (a plain string col).
        if self.search_fields:
            prefix = list(path) + [self.get_lookup_name()]
            fields = self.search_fields
        else:
            prefix = list(path)
            fields = [self.get_lookup_name()]
        keys = [LOOKUP_SEP.join(prefix + [field]) for field in fields]

        op, invert = self.get_operator(operator)
        terms = value if isinstance(value, list) else [value]
        q = Q()
        for term in terms:
            for key in keys:
                q |= Q(**{f'{key}__icontains': term})
        return ~q if invert else q

    def validate(self, value):
        if value is not None and not isinstance(value, str):
            raise DjangoQLSchemaError(
                _(
                    'Field "{field}" expects a string value, but got {value}'
                ).format(field=self.name, value=repr(value)),
            )


class AutocompleteSchemaMixin:
    """
    Schema mixin that turns configured fields into :class:`AutocompleteField`
    pickers.

    Declare an ``autocomplete`` map of ``{Model: {field_name: config}}`` where
    each config is a dict of :class:`AutocompleteField` kwargs, an
    ``AutocompleteField`` instance, or a callable ``(model, field_name) ->
    AutocompleteField``::

        class RecordSchema(AutocompleteSchemaMixin, DjangoQLSchema):
            autocomplete = {
                Record: {
                    'autor': {'url': 'autocomplete-autor',
                              'search_fields': ['last_name']},
                },
            }
    """

    autocomplete = {}

    def get_field_instance(self, model, field_name):
        config = self.autocomplete.get(model, {})
        if field_name in config:
            return self._build_autocomplete_field(
                model, field_name, config[field_name]
            )
        return super().get_field_instance(model, field_name)

    def _build_autocomplete_field(self, model, field_name, config):
        try:
            db_field = model._meta.get_field(field_name)
            nullable = getattr(db_field, 'null', False)
        except Exception:
            nullable = False
        if isinstance(config, AutocompleteField):
            field = config
        elif callable(config):
            field = config(model, field_name)
        else:
            field = AutocompleteField(**config)
        if field.model is None:
            field.model = model
        if field.name is None:
            field.name = field_name
        if not field.nullable and nullable:
            field.nullable = nullable
        return field


class ExtrasSchema(
    DatePartsSchemaMixin,
    AggregateSchemaMixin,
    AutocompleteSchemaMixin,
    DjangoQLSchema,
):
    """Opt-in schema with date/time parts and relation aggregates enabled."""
