from datetime import datetime
from decimal import Decimal

from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import Avg, Count, Max, Min, OuterRef, Q, Subquery, Sum
from django.db.models import FloatField as ORMFloatField
from django.db.models import IntegerField as ORMIntegerField
from django.db.models.constants import LOOKUP_SEP
from django.db.models.fields.related import ForeignObjectRel
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _

from .exceptions import DjangoQLSchemaError
from .schema import DateField, DjangoQLField, DjangoQLSchema, IntField


class DatePartField(IntField):
    """
    Virtual integer field for a date/time part extracted via a Django ORM
    transform, e.g. ``written__year`` -> ``written__year`` lookup.

    The field name IS the ORM lookup, so no get_lookup_name() override is
    needed: get_lookup joins ``path + [name]`` with ``__``.
    """

    suggested = False  # hidden from autocomplete; surfaced via error hint

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

    suggested = False  # hidden from autocomplete; surfaced via error hint

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
    suggested = False  # hidden from autocomplete; surfaced via error hint

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
        # Derived fields are hidden from autocomplete by default. Relation
        # count keeps a flat name and is surfaced via the error hint; numeric
        # aggregates are synthesized on demand and never serialized.
        suggested=False,
        # When True the field is addressed via dot syntax (e.g.
        # ``book.rating__sum``), so the trailing path element is the relation
        # itself and subquery correlation must use ``path[:-1]``.
        relation_hop_in_path=False,
    ):
        self.relation_name = relation_name
        self.related_model = related_model
        self.owner_lookup = owner_lookup
        self.source_field = source_field
        self.relation_hop_in_path = relation_hop_in_path
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
        # For dot-addressed aggregates the trailing path element is the
        # relation itself (already encoded in owner_lookup), so correlate on
        # the owning model reached via path[:-1]. Flat fields correlate on the
        # full path.
        correlation = (
            list(path[:-1]) if self.relation_hop_in_path else list(path)
        )
        outer = LOOKUP_SEP.join(correlation + ['pk'])
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
            # Only the flat relation count is registered as a real (hidden)
            # field. Numeric aggregates use dot syntax (e.g. book.rating__sum)
            # and are synthesized on demand in resolve_unknown(), so they never
            # bloat the field list or autocomplete.
            fields.append(
                CountField(
                    model=model,
                    relation_name=rel,
                    related_model=related,
                    owner_lookup=owner,
                    name='%s__count' % rel,
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

    def resolve_unknown(self, model_cls, prev_relation, name_part):
        """
        Synthesize a numeric aggregate field for dot syntax such as
        ``book.rating__sum`` (``prev_relation`` is the ``book`` relation just
        traversed, ``name_part`` is ``rating__sum``). Falls back to super() for
        anything we don't recognize.
        """
        field = self._synthesize_aggregate(prev_relation, name_part)
        if field is not None:
            return field
        return super().resolve_unknown(model_cls, prev_relation, name_part)

    def _synthesize_aggregate(self, prev_relation, name_part):
        if prev_relation is None or LOOKUP_SEP not in name_part:
            return None
        source_field, _sep, agg_name = name_part.rpartition(LOOKUP_SEP)
        agg_cls = dict(self.AGGREGATE_FIELDS).get(agg_name)
        if agg_cls is None or not source_field:
            return None
        owner_model = prev_relation.model
        related_model = prev_relation.related_model
        try:
            rel_f = owner_model._meta.get_field(prev_relation.name)
        except FieldDoesNotExist:
            return None
        if not (
            rel_f.is_relation and (rel_f.one_to_many or rel_f.many_to_many)
        ):
            return None
        owner_lookup = self._aggregate_owner_lookup(rel_f)
        if owner_lookup is None:
            return None
        if not self._is_aggregatable_numeric(related_model, source_field):
            return None
        return agg_cls(
            model=related_model,
            relation_name=prev_relation.name,
            related_model=related_model,
            owner_lookup=owner_lookup,
            source_field=source_field,
            name=name_part,
            relation_hop_in_path=True,
        )

    def _is_aggregatable_numeric(self, model, field_name):
        try:
            nf = model._meta.get_field(field_name)
        except FieldDoesNotExist:
            return False
        return (
            isinstance(nf, self.NUMERIC_FIELDS)
            and not nf.is_relation
            and not getattr(nf, 'primary_key', False)
            and getattr(nf, 'editable', True)  # skip GFK/internal cols
        )

    def _first_numeric_field(self, model):
        """First aggregatable numeric field name (prefer one without choices,
        for a cleaner example), or None."""
        fallback = None
        for nf in model._meta.get_fields():
            if (
                isinstance(nf, self.NUMERIC_FIELDS)
                and not nf.is_relation
                and not getattr(nf, 'primary_key', False)
                and getattr(nf, 'editable', True)
            ):
                if not getattr(nf, 'choices', None):
                    return nf.name
                if fallback is None:
                    fallback = nf.name
        return fallback

    def _aggregate_hint_examples(self, model_cls):
        first_rel = None
        for f in model_cls._meta.get_fields():
            if not (f.is_relation and (f.one_to_many or f.many_to_many)):
                continue
            if (
                f.related_model is None
                or self._aggregate_owner_lookup(f) is None
            ):
                continue
            if first_rel is None:
                first_rel = f.name
            numeric = self._first_numeric_field(f.related_model)
            if numeric:
                return ['%s__count' % f.name, '%s.%s__sum' % (f.name, numeric)]
        if first_rel is not None:
            return ['%s__count' % first_rel]
        return []

    def unknown_field_hint(self, model_cls):
        hint = super().unknown_field_hint(model_cls)
        examples = self._aggregate_hint_examples(model_cls)
        if examples:
            sentence = _(
                'Relation aggregates are hidden from suggestions: use '
                '<relation>__count and '
                '<relation>.<numeric_field>__{{sum,avg,min,max}} — e.g. '
                '{examples}.'
            ).format(examples=', '.join(examples))
            hint = ('%s %s' % (hint, sentence)).strip()
        return hint


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

    def _date_hint_example(self, model_cls):
        for f in model_cls._meta.get_fields():
            if isinstance(f, models.TimeField):
                return '%s__hour' % f.name
            if isinstance(f, models.DateField):  # also matches DateTimeField
                return '%s__year' % f.name
        return None

    def unknown_field_hint(self, model_cls):
        hint = super().unknown_field_hint(model_cls)
        example = self._date_hint_example(model_cls)
        if example:
            sentence = _(
                'Date/time parts are hidden too: use '
                '<field>__{{year,month,day,hour,...}} — e.g. {example}.'
            ).format(example=example)
            hint = ('%s %s' % (hint, sentence)).strip()
        return hint


class ExtrasSchema(DatePartsSchemaMixin, AggregateSchemaMixin, DjangoQLSchema):
    """Opt-in schema with date/time parts and relation aggregates enabled."""
