from datetime import datetime
from decimal import Decimal

from django.db import models
from django.db.models import Avg, Count, Max, Min, OuterRef, Q, Subquery, Sum
from django.db.models import FloatField as ORMFloatField
from django.db.models import IntegerField as ORMIntegerField
from django.db.models.constants import LOOKUP_SEP
from django.db.models.fields.related import ForeignObjectRel
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _

from .exceptions import DjangoQLSchemaError
from .schema import DateField, DjangoQLField, IntField


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
        # Django allows "__" in annotation aliases but forbids it inside field
        # names, so joining path + name with "__" is collision-free. The
        # "djangoql" prefix avoids clashing with real field/relation lookups.
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
