# Tests for the core `suggested` flag and djangoql.extras derived fields.
from django.contrib.auth.models import User
from django.db.models import Count
from django.test import TestCase

from djangoql.queryset import apply_search
from djangoql.schema import DjangoQLSchema, IntField
from djangoql.serializers import DjangoQLSchemaSerializer

from ..models import Book


class HiddenFieldSchema(DjangoQLSchema):
    def get_fields(self, model):
        fields = list(super().get_fields(model))
        if model == Book:
            fields.append(IntField(name='secret', suggested=False))
        return fields


class SuggestedFlagTest(TestCase):
    def test_default_field_is_suggested(self):
        self.assertTrue(IntField(name='x').suggested)

    def test_unsuggested_field_hidden_from_serializer(self):
        data = DjangoQLSchemaSerializer().serialize(HiddenFieldSchema(Book))
        book_fields = data['models']['core.book']
        self.assertIn('name', book_fields)
        self.assertNotIn('secret', book_fields)

    def test_unsuggested_field_still_validates(self):
        # suggested=False hides a field from autocomplete but it must remain
        # usable in actual queries (validation/resolution still see it).
        from djangoql.parser import DjangoQLParser

        ast = DjangoQLParser().parse('secret > 0')
        HiddenFieldSchema(Book).validate(ast)  # must not raise


class _ProbeCountField(IntField):
    """Minimal annotation-backed field for testing the lazy hook."""

    name = 'probe'

    def get_annotations(self, path):
        return {'probe': Count('book')}

    def get_lookup(self, path, operator, value):
        from django.db.models import Q

        op, invert = self.get_operator(operator)
        q = Q(**{'probe%s' % op: value})
        return ~q if invert else q


class ProbeSchema(DjangoQLSchema):
    def get_fields(self, model):
        fields = list(super().get_fields(model))
        if model == User:
            fields.append(_ProbeCountField(model=User))
        return fields


class CollectAnnotationsTest(TestCase):
    def test_annotation_applied_only_when_field_used(self):
        used = apply_search(User.objects.all(), 'probe > 1', schema=ProbeSchema)
        self.assertIn('probe', used.query.annotations)

    def test_annotation_absent_when_field_unused(self):
        unused = apply_search(
            User.objects.all(),
            'username = "x"',
            schema=ProbeSchema,
        )
        self.assertNotIn('probe', unused.query.annotations)


class DatePartsTest(TestCase):
    def _where(self, search):
        from djangoql.extras import DatePartsSchemaMixin

        class S(DatePartsSchemaMixin, DjangoQLSchema):
            pass

        qs = apply_search(Book.objects.all(), search, schema=S)
        return str(qs.query).split('WHERE')[1].strip()

    def test_datetime_year(self):
        # SQLite expands __year on a DateTimeField to a BETWEEN range rather
        # than calling django_datetime_extract, so we verify the transform
        # works correctly by checking the filtered results instead.
        from django.contrib.auth.models import User
        from django.utils.timezone import make_aware

        author = User.objects.create_user('testauthor_year')
        Book.objects.create(
            name='Y2020',
            author=author,
            written=make_aware(
                __import__('datetime').datetime(2020, 6, 15, 12, 0)
            ),
        )
        Book.objects.create(
            name='Y2021',
            author=author,
            written=make_aware(
                __import__('datetime').datetime(2021, 6, 15, 12, 0)
            ),
        )
        from djangoql.extras import DatePartsSchemaMixin

        class S(DatePartsSchemaMixin, DjangoQLSchema):
            pass

        qs = apply_search(Book.objects.all(), 'written__year = 2020', schema=S)
        names = list(qs.values_list('name', flat=True))
        self.assertIn('Y2020', names)
        self.assertNotIn('Y2021', names)

    def test_datetime_hour(self):
        where = self._where('written__hour >= 9').lower()
        self.assertIn('hour', where)

    def test_date_field_month(self):
        where = self._where('published_date__month = 6').lower()
        self.assertIn('month', where)

    def test_time_field_minute(self):
        where = self._where('published_time__minute = 30').lower()
        self.assertIn('minute', where)

    def test_datetime_date_extract(self):
        where = self._where('written__date = "2020-01-01"').lower()
        self.assertIn('date', where)
