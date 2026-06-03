from datetime import date

from django.apps import apps
from django.contrib.auth.models import Group, User
from django.db import models
from django.test import TestCase

from djangoql.exceptions import DjangoQLSchemaError
from djangoql.parser import DjangoQLParser
from djangoql.schema import (
    DateField,
    DjangoQLField,
    DjangoQLSchema,
    IntField,
    StrField,
)
from djangoql.serializers import SuggestionsAPISerializer

from ..models import Book


serializer = SuggestionsAPISerializer('/suggestions/')


class ExcludeUserSchema(DjangoQLSchema):
    exclude = (User,)


class IncludeUserGroupSchema(DjangoQLSchema):
    include = (Group, User)


class IncludeExcludeSchema(DjangoQLSchema):
    include = (Group,)
    exclude = (Book,)


class BookCustomFieldsSchema(DjangoQLSchema):
    def get_fields(self, model):
        if model == Book:
            return ['name', 'is_published']
        return super().get_fields(model)


class WrittenInYearField(IntField):
    model = Book
    name = 'written_in_year'

    def get_lookup_name(self):
        return 'written__year'


class BookCustomSearchSchema(DjangoQLSchema):
    def get_fields(self, model):
        if model == Book:
            return [
                WrittenInYearField(),
            ]


class DjangoQLSchemaTest(TestCase):
    def all_models(self):
        models = []
        for app_label in apps.app_configs:
            models.extend(apps.get_app_config(app_label).get_models())
        return models

    def test_default(self):
        schema_dict = serializer.serialize(DjangoQLSchema(Book))
        self.assertIsInstance(schema_dict, dict)
        self.assertEqual('core.book', schema_dict.get('current_model'))
        models = schema_dict.get('models')
        self.assertIsInstance(models, dict)
        all_model_labels = sorted([str(m._meta) for m in self.all_models()])
        session_model = all_model_labels.pop()
        self.assertEqual('sessions.session', session_model)
        self.assertListEqual(all_model_labels, sorted(models.keys()))

    def test_exclude(self):
        schema_dict = serializer.serialize(ExcludeUserSchema(Book))
        self.assertEqual('core.book', schema_dict['current_model'])
        self.assertListEqual(
            sorted(schema_dict['models'].keys()),
            [
                'admin.logentry',
                'auth.group',
                'auth.permission',
                'contenttypes.contenttype',
                'core.book',
            ],
        )

    def test_include(self):
        schema_dict = serializer.serialize(IncludeUserGroupSchema(User))
        self.assertEqual('auth.user', schema_dict['current_model'])
        self.assertListEqual(
            sorted(schema_dict['models'].keys()),
            [
                'auth.group',
                'auth.user',
            ],
        )

    def test_get_fields(self):
        default_schema = DjangoQLSchema(Book)
        default = serializer.serialize(default_schema)['models']['core.book']
        custom_schema = BookCustomFieldsSchema(Book)
        custom = serializer.serialize(custom_schema)['models']['core.book']
        self.assertListEqual(
            list(default.keys()),
            [
                'author',
                'content_type',
                'genre',
                'id',
                'is_published',
                'name',
                'object_id',
                'price',
                'rating',
                'similar_books',
                'written',
            ],
        )
        self.assertListEqual(list(custom.keys()), ['name', 'is_published'])

    def test_circular_references(self):
        models = serializer.serialize(DjangoQLSchema(Book))['models']
        # If Book references Author then Author should also reference Book back
        book_author_field = models['core.book'].get('author')
        self.assertIsNotNone(book_author_field)
        self.assertEqual('relation', book_author_field['type'])
        self.assertEqual('auth.user', book_author_field['relation'])
        self.assertEqual('relation', models['auth.user']['book']['type'])

    def test_custom_search(self):
        models = serializer.serialize(BookCustomSearchSchema(Book))['models']
        self.assertListEqual(
            list(models['core.book'].keys()),
            ['written_in_year'],
        )

    def test_invalid_config(self):
        try:
            IncludeExcludeSchema(Group)
            self.fail('Invalid schema with include & exclude raises no error')
        except DjangoQLSchemaError:
            pass
        try:
            IncludeUserGroupSchema(Book)
            self.fail('Schema was initialized with a model excluded from it')
        except DjangoQLSchemaError:
            pass
        try:
            IncludeUserGroupSchema(User())
            self.fail('Schema was initialized with an instance of a model')
        except DjangoQLSchemaError:
            pass

    def test_validation_pass(self):
        samples = [
            'first_name = "Lolita"',
            'groups.id < 42',
            'groups = None',  # that's ok to compare a model to None
            'groups != None',
            'groups.name in ("Stoics") and is_staff = False',
            'date_joined > "1753-01-01"',
            'date_joined > "1753-01-01 01:24"',
            'date_joined > "1753-01-01 01:24:42"',
        ]
        for query in samples:
            ast = DjangoQLParser().parse(query)
            try:
                IncludeUserGroupSchema(User).validate(ast)
            except DjangoQLSchemaError as e:
                self.fail(e)

    def test_validation_fail(self):
        samples = [
            'gav = 1',  # unknown field
            'groups.gav > 1',  # unknown related field
            'groups = "lol"',  # can't compare model to a value
            'groups.name != 1',  # bad value type
            'is_staff = True and gav < 2',  # complex expression with valid part
            'date_joined < "1753-30-01"',  # bad timestamps
            'date_joined < "1753-01-01 12"',
            'date_joined < "1753-01-01 12AM"',
        ]
        for query in samples:
            ast = DjangoQLParser().parse(query)
            try:
                IncludeUserGroupSchema(User).validate(ast)
                self.fail("This query should't pass validation: %s" % query)
            except DjangoQLSchemaError:
                pass

    def test_get_field_cls_mapping(self):
        # Model field class -> DjangoQL field class resolution, including the
        # plain DateField branch (the test models only use DateTimeField) and
        # the fallthrough for an unmapped field type.
        schema = DjangoQLSchema(Book)
        self.assertIs(schema.get_field_cls(models.DateField()), DateField)
        self.assertIs(
            schema.get_field_cls(models.DurationField()),
            DjangoQLField,
        )

    def test_as_dict_is_deprecated(self):
        # as_dict() calls warnings.warn() without an explicit category, so the
        # warning is a UserWarning rather than a DeprecationWarning.
        with self.assertWarns(UserWarning) as ctx:
            result = DjangoQLSchema(Book).as_dict()
        self.assertIn('deprecated', str(ctx.warning))
        self.assertEqual('core.book', result['current_model'])


class StrFieldOptionsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        author = User.objects.create(username='author')
        for name in ('alpha', 'beta', 'gamma'):
            Book.objects.create(name=name, author=author)

    def test_get_options_returns_choices_when_present(self):
        # When the underlying model field defines choices, get_options returns
        # the matching choice labels instead of querying the column.
        field = StrField(model=Book, name='genre')
        self.assertEqual(list(field.get_options('dra')), ['Drama'])

    def test_get_options_falls_back_to_icontains(self):
        # A StrField with no choices should query the model with an icontains
        # lookup on its own column, ordered and de-duplicated.
        field = StrField(model=Book, name='name')
        self.assertEqual(
            list(field.get_options('a')),
            ['alpha', 'beta', 'gamma'],
        )
        self.assertEqual(list(field.get_options('ph')), ['alpha'])
        # Empty search drops the lookup and returns everything.
        self.assertEqual(
            list(field.get_options('')),
            ['alpha', 'beta', 'gamma'],
        )


class FieldValidateTest(TestCase):
    def test_non_nullable_field_rejects_none(self):
        with self.assertRaises(DjangoQLSchemaError):
            IntField(name='age', nullable=False).validate(None)

    def test_nullable_field_rejects_wrong_type(self):
        # A nullable field reports the "nullable <type>" variant of the error.
        with self.assertRaises(DjangoQLSchemaError) as ctx:
            IntField(name='age', nullable=True).validate('not-an-int')
        self.assertIn('nullable', str(ctx.exception))


class DateFieldTest(TestCase):
    def test_parse_date(self):
        self.assertIsNone(DateField.parse_date(''))
        self.assertEqual(DateField.parse_date('2017-01-30'), date(2017, 1, 30))

    def test_validate_rejects_non_date(self):
        field = DateField(model=Book, name='written')
        # A well-formed string that isn't a real date must raise, not crash.
        with self.assertRaises(DjangoQLSchemaError):
            field.validate('not-a-date')
