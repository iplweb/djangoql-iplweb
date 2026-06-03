from datetime import datetime

from django.db import models
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
