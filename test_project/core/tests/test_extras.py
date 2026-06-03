# Tests for the core `suggested` flag and djangoql.extras derived fields.
from datetime import datetime

from django.contrib.auth.models import Group, User
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
            written=make_aware(datetime(2020, 6, 15, 12, 0)),
        )
        Book.objects.create(
            name='Y2021',
            author=author,
            written=make_aware(datetime(2021, 6, 15, 12, 0)),
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

    def test_datetime_time_extract(self):
        from django.utils.timezone import make_aware

        author = User.objects.create_user('testauthor_time')
        morning = Book.objects.create(
            name='morn',
            author=author,
            written=make_aware(datetime(2020, 1, 1, 9, 30)),
        )
        evening = Book.objects.create(
            name='eve',
            author=author,
            written=make_aware(datetime(2020, 1, 1, 18, 45)),
        )
        from djangoql.extras import DatePartsSchemaMixin

        class S(DatePartsSchemaMixin, DjangoQLSchema):
            pass

        qs = apply_search(
            Book.objects.all(), 'written__time = "09:30"', schema=S
        )
        names = set(qs.values_list('name', flat=True))
        self.assertIn('morn', names)
        self.assertNotIn('eve', names)
        # Clean up to avoid leaking into other tests
        morning.delete()
        evening.delete()
        author.delete()


class _CountSchemaFK(DjangoQLSchema):
    """Hand-built schema adding only User.book__count (reverse FK)."""

    def get_fields(self, model):
        from djangoql.extras import CountField, _owner_lookup

        fields = list(super().get_fields(model))
        if model == User:
            rel = User._meta.get_field('book')  # ManyToOneRel
            fields.append(
                CountField(
                    model=User,
                    relation_name='book',
                    related_model=rel.related_model,
                    owner_lookup=_owner_lookup(rel),
                    name='book__count',
                )
            )
        return fields


class CountFKTest(TestCase):
    def setUp(self):
        self.prolific = User.objects.create(username='prolific')
        self.quiet = User.objects.create(username='quiet')
        for i in range(3):
            Book.objects.create(name='b%d' % i, author=self.prolific)

    def _usernames(self, search):
        qs = apply_search(User.objects.all(), search, schema=_CountSchemaFK)
        return set(qs.values_list('username', flat=True))

    def test_count_gt(self):
        self.assertEqual(self._usernames('book__count > 2'), {'prolific'})

    def test_count_zero_matches_empty(self):
        # Coalesce(...,0) makes "= 0" match users with no books.
        self.assertIn('quiet', self._usernames('book__count = 0'))
        self.assertNotIn('prolific', self._usernames('book__count = 0'))

    def test_count_lazy(self):
        qs = apply_search(
            User.objects.all(), 'username = "x"', schema=_CountSchemaFK
        )
        self.assertEqual(qs.query.annotations, {})

    def test_count_not_nullable(self):
        from djangoql.exceptions import DjangoQLSchemaError

        with self.assertRaises(DjangoQLSchemaError):
            apply_search(
                User.objects.all(),
                'book__count = None',
                schema=_CountSchemaFK,
            )


class _CountSchemaM2M(DjangoQLSchema):
    def get_fields(self, model):
        from djangoql.extras import CountField, _owner_lookup

        fields = list(super().get_fields(model))
        if model == User:
            rel = User._meta.get_field('groups')  # ManyToManyField (forward)
            fields.append(
                CountField(
                    model=User,
                    relation_name='groups',
                    related_model=rel.related_model,
                    owner_lookup=_owner_lookup(rel),
                    name='groups__count',
                )
            )
        return fields


class CountM2MTest(TestCase):
    def setUp(self):
        self.g1 = Group.objects.create(name='g1')
        self.g2 = Group.objects.create(name='g2')
        self.member = User.objects.create(username='member')
        self.member.groups.add(self.g1, self.g2)
        self.loner = User.objects.create(username='loner')

    def _usernames(self, search):
        qs = apply_search(User.objects.all(), search, schema=_CountSchemaM2M)
        return set(qs.values_list('username', flat=True))

    def test_m2m_count_gt(self):
        self.assertEqual(self._usernames('groups__count > 1'), {'member'})

    def test_m2m_count_zero(self):
        self.assertIn('loner', self._usernames('groups__count = 0'))


class _AggSchema(DjangoQLSchema):
    def get_fields(self, model):
        from djangoql.extras import (
            AvgField,
            MaxField,
            MinField,
            SumField,
            _owner_lookup,
        )

        fields = list(super().get_fields(model))
        if model == User:
            rel = User._meta.get_field('book')
            common = dict(
                model=User,
                relation_name='book',
                related_model=rel.related_model,
                owner_lookup=_owner_lookup(rel),
                source_field='rating',
            )
            fields += [
                SumField(name='book__rating__sum', **common),
                AvgField(name='book__rating__avg', **common),
                MinField(name='book__rating__min', **common),
                MaxField(name='book__rating__max', **common),
            ]
        return fields


class AggregatesTest(TestCase):
    def setUp(self):
        self.u = User.objects.create(username='u')
        Book.objects.create(name='a', author=self.u, rating=2.0)
        Book.objects.create(name='b', author=self.u, rating=4.0)
        self.v = User.objects.create(username='v')
        Book.objects.create(name='c', author=self.v, rating=10.0)

    def _usernames(self, search):
        qs = apply_search(User.objects.all(), search, schema=_AggSchema)
        return set(qs.values_list('username', flat=True))

    def test_avg(self):
        # u avg = 3.0, v avg = 10.0
        self.assertEqual(self._usernames('book__rating__avg > 5'), {'v'})

    def test_sum(self):
        # u sum = 6.0, v sum = 10.0
        self.assertEqual(self._usernames('book__rating__sum < 8'), {'u'})

    def test_min_max(self):
        self.assertEqual(self._usernames('book__rating__min >= 10'), {'v'})
        self.assertEqual(self._usernames('book__rating__max <= 4'), {'u'})

    def test_numeric_aggregate_none_validates(self):
        # avg/sum/min/max return SQL NULL for an empty set, so "= None" is a
        # valid, non-raising query (unlike count which is non-nullable).
        from djangoql.parser import DjangoQLParser

        ast = DjangoQLParser().parse('book__rating__avg = None')
        _AggSchema(User).validate(ast)  # must not raise

    def test_avg_none_matches_user_with_no_books(self):
        User.objects.create(username='no_books')
        result = self._usernames('book__rating__avg = None')
        self.assertIn('no_books', result)
        self.assertNotIn('u', result)
