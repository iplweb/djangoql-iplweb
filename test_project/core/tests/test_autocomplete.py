# Tests for djangoql.extras.AutocompleteField / AutocompleteSchemaMixin.
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from djangoql.extras import AutocompleteField, AutocompleteSchemaMixin
from djangoql.parser import DjangoQLParser
from djangoql.queryset import apply_search
from djangoql.schema import DjangoQLSchema, RelationField
from djangoql.serializers import SuggestionsAPISerializer

from ..models import Book


class QuerysetAutocompleteSchema(AutocompleteSchemaMixin, DjangoQLSchema):
    include = (Book, User)
    autocomplete = {
        Book: {
            'author': {
                'queryset': lambda s: User.objects.filter(
                    username__icontains=s
                ).order_by('username'),
                'search_fields': ['username'],
                'label': lambda u: u.username,
            },
        },
    }


class UrlAutocompleteSchema(AutocompleteSchemaMixin, DjangoQLSchema):
    include = (Book, User)
    autocomplete = {
        Book: {
            'author': {
                'url': 'user-autocomplete',
                'search_fields': ['username'],
            },
        },
    }


class ParseIdTest(TestCase):
    def setUp(self):
        self.field = AutocompleteField(
            model=Book, name='author', search_fields=['username']
        )

    def test_parse_single_id(self):
        self.assertEqual(self.field.parse_id('Jan Kowalski [42]'), 42)

    def test_parse_id_list(self):
        self.assertEqual(
            self.field.parse_id(['A [1]', 'B [2]']),
            [1, 2],
        )

    def test_parse_plain_string_returns_raw(self):
        self.assertEqual(self.field.parse_id('plain'), 'plain')


class QuerysetProviderTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.kow = User.objects.create(username='Jan Kowalski')
        cls.nowak = User.objects.create(username='Anna Nowak')

    def _field(self, **kw):
        kw.setdefault('model', Book)
        kw.setdefault('name', 'author')
        kw.setdefault('search_fields', ['username'])
        return AutocompleteField(**kw)

    def test_get_options_formats_label_and_id(self):
        field = self._field(
            queryset=lambda s: User.objects.filter(
                username__icontains=s
            ).order_by('username'),
            label=lambda u: u.username,
        )
        options = list(field.get_options('kow'))
        self.assertEqual(options, ['Jan Kowalski [%d]' % self.kow.pk])

    def test_get_options_respects_limit(self):
        for i in range(5):
            User.objects.create(username='dup%d' % i)
        field = self._field(
            queryset=lambda s: User.objects.filter(
                username__icontains=s
            ).order_by('username'),
            label=lambda u: u.username,
            limit=2,
        )
        options = list(field.get_options('dup'))
        self.assertEqual(len(options), 2)

    def test_get_options_strips_trailing_id_from_search(self):
        field = self._field(
            queryset=lambda s: User.objects.filter(
                username__icontains=s
            ).order_by('username'),
            label=lambda u: u.username,
        )
        options = list(field.get_options('Jan Kowalski [%d]' % self.kow.pk))
        self.assertEqual(options, ['Jan Kowalski [%d]' % self.kow.pk])


class QueryByPkTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.kow = User.objects.create(username='Jan Kowalski')
        cls.nowak = User.objects.create(username='Anna Nowak')
        cls.b1 = Book.objects.create(name='b1', author=cls.kow)
        cls.b2 = Book.objects.create(name='b2', author=cls.nowak)

    def _search(self, q):
        return apply_search(
            Book.objects.all(), q, schema=QuerysetAutocompleteSchema
        )

    def test_equals_filters_by_pk(self):
        qs = self._search('author = "Jan Kowalski [%d]"' % self.kow.pk)
        sql = str(qs.query)
        self.assertIn('author_id', sql)
        self.assertIn('= %d' % self.kow.pk, sql)
        self.assertNotIn('username', sql)
        self.assertEqual(list(qs), [self.b1])

    def test_not_equals_filters_by_pk(self):
        qs = self._search('author != "Jan Kowalski [%d]"' % self.kow.pk)
        self.assertEqual(list(qs), [self.b2])

    def test_in_filters_by_pks(self):
        qs = self._search(
            'author in ("Jan Kowalski [%d]", "Anna Nowak [%d]")'
            % (self.kow.pk, self.nowak.pk)
        )
        self.assertEqual(sorted(b.pk for b in qs), [self.b1.pk, self.b2.pk])

    def test_free_text_fallback_uses_icontains(self):
        qs = self._search('author = "kowal"')
        self.assertEqual(list(qs), [self.b1])
        sql = str(qs.query).lower()
        self.assertIn('username', sql)
        self.assertIn('like', sql)


class UrlProviderTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.kow = User.objects.create(username='Jan Kowalski')
        cls.b1 = Book.objects.create(name='b1', author=cls.kow)

    def test_url_provider_calls_view_in_process(self):
        request = RequestFactory().get('/')
        field = AutocompleteField(
            model=Book,
            name='author',
            url='user-autocomplete',
            search_fields=['username'],
        )
        field.set_request(request)
        options = list(field.get_options('kow'))
        self.assertEqual(options, ['Jan Kowalski [%d]' % self.kow.pk])

    def test_url_provider_query_filters_by_pk(self):
        qs = apply_search(
            Book.objects.all(),
            'author = "Jan Kowalski [%d]"' % self.kow.pk,
            schema=UrlAutocompleteSchema,
        )
        self.assertEqual(list(qs), [self.b1])

    def test_request_reaches_view(self):
        # The bound request must reach the in-process view: the view reads
        # request.GET['q'], so a sentinel query carried on the bound request
        # only filters results if the same request object is threaded through.
        request = RequestFactory().get('/?q=zzz-nomatch')
        field = AutocompleteField(
            model=Book,
            name='author',
            url='user-autocomplete',
            search_fields=['username'],
        )
        field.set_request(request)
        # get_options overrides q with its own search term; the view sees that.
        options = list(field.get_options('kow'))
        self.assertEqual(options, ['Jan Kowalski [%d]' % self.kow.pk])
        # And the bound request's GET param is what the view reads as 'q':
        self.assertEqual(request.GET.get('q'), 'kow')


class SchemaMixinTest(TestCase):
    def test_field_is_autocomplete_instance(self):
        schema = QuerysetAutocompleteSchema(Book)
        field = schema.models['core.book']['author']
        self.assertIsInstance(field, AutocompleteField)

    def test_field_is_not_a_relation(self):
        schema = QuerysetAutocompleteSchema(Book)
        field = schema.models['core.book']['author']
        self.assertNotIsInstance(field, RelationField)
        self.assertEqual(field.type, 'str')

    def test_serialized_schema_has_options_true(self):
        # The async suggestions serializer emits options: true (the widget then
        # fetches values via the suggestions endpoint) and no 'relation' key,
        # because the FK is exposed as a value field, not a relation.
        data = SuggestionsAPISerializer('/suggestions/').serialize(
            QuerysetAutocompleteSchema(Book)
        )
        author = data['models']['core.book']['author']
        self.assertEqual(author['options'], True)
        self.assertNotIn('relation', author)
        self.assertIn('suggestions_api_url', data)

    def test_async_options_true(self):
        field = AutocompleteField(
            model=Book, name='author', search_fields=['username']
        )
        self.assertTrue(field.async_options)
        self.assertTrue(field.suggest_options)


class ValidationTest(TestCase):
    def test_validate_accepts_str(self):
        schema = QuerysetAutocompleteSchema(Book)
        ast = DjangoQLParser().parse('author = "Jan [1]"')
        schema.validate(ast)  # must not raise


class RelAndPickerSchema(AutocompleteSchemaMixin, DjangoQLSchema):
    """`author` zostaje relacją (trawersacja z kropką), a `author__rel` to
    picker filtrujący realny FK `author` przez lookup_name."""

    include = (Book, User)
    autocomplete = {
        Book: {
            'author__rel': {
                'lookup_name': 'author',
                'queryset': lambda s: User.objects.filter(
                    username__icontains=s
                ).order_by('username'),
                'search_fields': ['username'],
                'label': lambda u: u.username,
            },
        },
    }

    def get_fields(self, model):
        fields = list(super().get_fields(model))
        if model is Book:
            fields.append('author__rel')
        return fields


class LookupNameTest(TestCase):
    def test_lookup_name_defaults_to_field_name(self):
        field = AutocompleteField(model=Book, name='author__rel')
        self.assertEqual(field.get_lookup_name(), 'author__rel')

    def test_lookup_name_overrides_lookup(self):
        field = AutocompleteField(
            model=Book, name='author__rel', lookup_name='author'
        )
        self.assertEqual(field.get_lookup_name(), 'author')


class RelAndPickerCoexistTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.kow = User.objects.create(username='Jan Kowalski')
        cls.nowak = User.objects.create(username='Anna Nowak')
        cls.b1 = Book.objects.create(name='b1', author=cls.kow)
        cls.b2 = Book.objects.create(name='b2', author=cls.nowak)

    def _search(self, q):
        return apply_search(Book.objects.all(), q, schema=RelAndPickerSchema)

    def test_picker_filters_real_fk_by_pk(self):
        qs = self._search('author__rel = "Jan Kowalski [%d]"' % self.kow.pk)
        sql = str(qs.query)
        self.assertIn('author_id', sql)
        self.assertNotIn('author__rel', sql)
        self.assertEqual(list(qs), [self.b1])

    def test_picker_in_filters_by_pks(self):
        qs = self._search(
            'author__rel in ("Jan Kowalski [%d]", "Anna Nowak [%d]")'
            % (self.kow.pk, self.nowak.pk)
        )
        self.assertEqual(sorted(b.pk for b in qs), [self.b1.pk, self.b2.pk])

    def test_picker_free_text_fallback_targets_real_fk(self):
        qs = self._search('author__rel = "kowal"')
        sql = str(qs.query).lower()
        self.assertIn('username', sql)
        self.assertIn('like', sql)
        self.assertEqual(list(qs), [self.b1])

    def test_dot_traversal_still_works(self):
        qs = self._search('author.username = "Jan Kowalski"')
        self.assertEqual(list(qs), [self.b1])

    def test_both_idioms_in_one_query(self):
        qs = self._search(
            'author.username = "Jan Kowalski" '
            'and author__rel = "Jan Kowalski [%d]"' % self.kow.pk
        )
        self.assertEqual(list(qs), [self.b1])

    def test_picker_is_value_field_relation_is_relation(self):
        schema = RelAndPickerSchema(Book)
        fields = schema.models['core.book']
        self.assertIsInstance(fields['author__rel'], AutocompleteField)
        self.assertIsInstance(fields['author'], RelationField)
        self.assertEqual(fields['author'].type, 'relation')
