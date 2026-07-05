"""Tests for the LLM schema-description helper and its management command.

``describe_schema_for_llm`` turns a DjangoQLSchema into a self-contained,
machine-readable description of the whole search space (fields, types,
relations, allowed operators, examples) suitable for an LLM prompt. The
``djangoql_describe_schema_for_llm`` command prints that description as JSON.
"""

import json
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from djangoql.extras import AutocompleteSchemaMixin
from djangoql.llm import describe_schema_for_llm
from djangoql.parser import DjangoQLParser
from djangoql.schema import DjangoQLSchema

from ..models import Book


class AuthorPickerSchema(AutocompleteSchemaMixin, DjangoQLSchema):
    include = (Book, User)
    autocomplete = {
        Book: {
            'author': {
                'queryset': lambda s: User.objects.filter(
                    username__icontains=s,
                ),
                'search_fields': ['username'],
            },
        },
    }


class ExcludeUserSchema(DjangoQLSchema):
    exclude = ()

    def get_fields(self, model):
        if model == Book:
            return [
                'name',
                'rating',
                'is_published',
                'author',
                'published_date',
            ]
        return super().get_fields(model)


class DescribeSchemaForLLMTest(TestCase):
    def setUp(self):
        self.bundle = describe_schema_for_llm(DjangoQLSchema(Book))

    def test_top_level_keys(self):
        for key in (
            'start_model',
            'grammar',
            'operators_by_type',
            'models',
            'examples',
        ):
            self.assertIn(key, self.bundle)

    def test_start_model_is_the_root(self):
        self.assertEqual('core.book', self.bundle['start_model'])

    def test_models_contains_root_and_related(self):
        models = self.bundle['models']
        self.assertIn('core.book', models)
        self.assertIn('auth.user', models)

    def test_operator_legend_is_emitted_once_by_type(self):
        legend = self.bundle['operators_by_type']
        self.assertIn('>', legend['float']['operators'])
        self.assertIn('~', legend['str']['operators'])
        self.assertIn('startswith', legend['str']['operators'])
        self.assertEqual({'=', '!='}, set(legend['bool']['operators']))
        self.assertIn('relation', legend)
        self.assertIn('object_reference', legend)

    def test_grammar_documents_operator_lookup(self):
        self.assertIn('operators_by_type', self.bundle['grammar']['operators'])

    def test_fields_never_repeat_operators_or_examples(self):
        for field in self.bundle['models']['core.book'].values():
            if isinstance(field, dict):
                self.assertNotIn('operators', field)
                self.assertNotIn('example', field)

    def test_plain_scalar_field_is_a_bare_type_string(self):
        # is_published is a non-null bool with no metadata -> bare "bool"
        self.assertEqual(
            'bool', self.bundle['models']['core.book']['is_published']
        )

    def test_nullable_field_uses_question_mark_suffix(self):
        # published_date is null=True and has no metadata -> "date?"
        self.assertEqual(
            'date?', self.bundle['models']['core.book']['published_date']
        )
        # rating is a nullable float with no metadata -> "float?"
        self.assertEqual('float?', self.bundle['models']['core.book']['rating'])

    def test_field_with_metadata_is_an_object_with_type(self):
        # name carries verbose_name/help_text -> object, type is 'str'
        name = self.bundle['models']['core.book']['name']
        self.assertEqual('str', name['type'])
        self.assertEqual('Title', name['label'])
        self.assertEqual('The title of the book', name['help_text'])

    def test_choice_field_object_lists_labels(self):
        genre = self.bundle['models']['core.book']['genre']
        self.assertEqual(['Drama', 'Comics', 'Other'], genre['choices'])

    def test_relation_field_object_points_at_related_model(self):
        author = self.bundle['models']['core.book']['author']
        self.assertEqual('auth.user', author['relates_to'])
        self.assertTrue(author['type'].startswith('relation'))

    def test_object_reference_uses_its_operator_class(self):
        bundle = describe_schema_for_llm(AuthorPickerSchema(Book))
        author = bundle['models']['core.book']['author']
        self.assertTrue(author['object_reference'])
        self.assertEqual(
            ['=', '!=', 'in', 'not in'],
            bundle['operators_by_type']['object_reference']['operators'],
        )

    def test_grammar_warns_there_is_no_standalone_not(self):
        self.assertIn('negation', self.bundle['grammar'])

    def test_examples_actually_parse(self):
        parser = DjangoQLParser()
        for query in self.bundle['examples']:
            parser.parse(query)


class DjangoqlSchemaCommandTest(TestCase):
    def _run(self, *args):
        out = StringIO()
        call_command('djangoql_describe_schema_for_llm', *args, stdout=out)
        return out.getvalue()

    def test_outputs_valid_json_for_a_model(self):
        data = json.loads(self._run('core.Book'))
        self.assertEqual('core.book', data['start_model'])
        self.assertIn('rating', data['models']['core.book'])

    def test_unknown_model_raises_command_error(self):
        with self.assertRaises(CommandError):
            self._run('core.Nonexistent')

    def test_bad_model_format_raises_command_error(self):
        with self.assertRaises(CommandError):
            self._run('BookWithoutAppLabel')

    def test_custom_schema_limits_the_fields(self):
        data = json.loads(
            self._run(
                'core.Book',
                '--schema',
                'core.tests.test_llm.ExcludeUserSchema',
            ),
        )
        book_fields = set(data['models']['core.book'].keys())
        self.assertEqual(
            {'name', 'rating', 'is_published', 'author', 'published_date'},
            book_fields,
        )

    def test_bad_schema_path_raises_command_error(self):
        with self.assertRaises(CommandError):
            self._run('core.Book', '--schema', 'no.such.Schema')

    def test_indent_option_is_respected(self):
        # indent=0 -> newlines but no leading spaces; default -> indented
        compact = self._run('core.Book', '--indent', '0')
        self.assertNotIn('\n  "start_model"', compact)

    def test_max_fk_options_flag_is_passed_through(self):
        # With the gate at 0, auto FK values are disabled; similar_books
        # (a non-sensitive relation) must carry no related_values.
        Book.objects.create(
            name='Dune',
            author=User.objects.create(username='ada'),
        )
        data = json.loads(self._run('core.Book', '--max-fk-options', '0'))
        similar = data['models']['core.book']['similar_books']
        self.assertNotIn('related_values', similar)


class RelationValuesTest(TestCase):
    def _book(self, name):
        author = User.objects.create(username='u-%s' % name)
        return Book.objects.create(name=name, author=author)

    def test_auto_emits_related_values_for_small_relation(self):
        # similar_books -> Book (non-sensitive). A handful of books means few
        # distinct names, so auto mode should surface them.
        for n in ('Dune', 'Solaris', 'It'):
            self._book(n)
        bundle = describe_schema_for_llm(DjangoQLSchema(Book))
        similar = bundle['models']['core.book']['similar_books']
        self.assertEqual('name', similar['match_field'])
        self.assertEqual(
            {'Dune', 'Solaris', 'It'}, set(similar['related_values'])
        )

    def test_auto_skips_relation_over_threshold(self):
        for n in ('a', 'b', 'c'):
            self._book(n)
        bundle = describe_schema_for_llm(DjangoQLSchema(Book), max_fk_options=2)
        similar = bundle['models']['core.book']['similar_books']
        self.assertNotIn('related_values', similar)

    def test_max_fk_options_zero_disables_auto(self):
        self._book('Dune')
        bundle = describe_schema_for_llm(DjangoQLSchema(Book), max_fk_options=0)
        similar = bundle['models']['core.book']['similar_books']
        self.assertNotIn('related_values', similar)

    def test_auto_never_touches_sensitive_target_model(self):
        # author -> auth.User is sensitive: no values, and never the password.
        self._book('Dune')
        bundle = describe_schema_for_llm(DjangoQLSchema(Book))
        author = bundle['models']['core.book']['author']
        self.assertNotIn('related_values', author)
        self.assertNotIn('password', json.dumps(author))

    def test_default_match_field_prefers_visible_str_field(self):
        # The heuristic must pick from schema-visible fields (never password).
        from djangoql.llm import _default_match_field

        schema = DjangoQLSchema(Book)
        self.assertEqual('name', _default_match_field(schema, 'core.book'))

    def test_default_match_field_skips_unsuggested_fields(self):
        from djangoql.llm import _default_match_field

        class _StubField:
            def __init__(self, ftype, suggested=True):
                self.type = ftype
                self.suggested = suggested

        class _StubSchema:
            models = {
                'x.y': {
                    'secret': _StubField('str', suggested=False),
                    'label': _StubField('str', suggested=True),
                },
            }

        # 'secret' comes first but is hidden -> must fall through to 'label'
        self.assertEqual('label', _default_match_field(_StubSchema(), 'x.y'))

    def test_fk_options_name_overrides_sensitive_exclusion(self):
        User.objects.create(username='ada')
        User.objects.create(username='alan')

        class ForcedSchema(DjangoQLSchema):
            fk_options = {Book: {'author': 'username'}}

        bundle = describe_schema_for_llm(ForcedSchema(Book))
        author = bundle['models']['core.book']['author']
        self.assertEqual('username', author['match_field'])
        self.assertEqual({'ada', 'alan'}, set(author['related_values']))

    def test_fk_options_list_emits_values_per_field(self):
        User.objects.create(username='ada', email='ada@x.io')

        class ListSchema(DjangoQLSchema):
            fk_options = {Book: {'author': ['username', 'email']}}

        bundle = describe_schema_for_llm(ListSchema(Book))
        author = bundle['models']['core.book']['author']
        self.assertEqual(['username', 'email'], author['match_fields'])
        self.assertEqual(['ada'], author['related_values']['username'])
        self.assertEqual(['ada@x.io'], author['related_values']['email'])

    def test_fk_options_str_emits_related_examples(self):
        User.objects.create(username='ada')

        class StrSchema(DjangoQLSchema):
            fk_options = {Book: {'author': '__str__'}}

        bundle = describe_schema_for_llm(StrSchema(Book))
        author = bundle['models']['core.book']['author']
        self.assertEqual(['ada'], author['related_examples'])

    def test_fk_options_false_emits_nothing(self):
        User.objects.create(username='ada')

        class OffSchema(DjangoQLSchema):
            fk_options = {Book: {'author': False}}

        bundle = describe_schema_for_llm(OffSchema(Book))
        author = bundle['models']['core.book']['author']
        self.assertNotIn('related_values', author)
        self.assertNotIn('related_examples', author)

    def test_fk_options_false_on_nonsensitive_relation(self):
        # author -> auth.User is sensitive, so auto mode would exclude it
        # regardless of the False spec; similar_books -> Book is not, so an
        # empty result here can only be explained by the False dispatch arm.
        for n in ('Dune', 'Solaris'):
            self._book(n)

        class OffSchema(DjangoQLSchema):
            fk_options = {Book: {'similar_books': False}}

        bundle = describe_schema_for_llm(OffSchema(Book))
        similar = bundle['models']['core.book']['similar_books']
        self.assertNotIn('related_values', similar)

    def test_fk_options_true_ignores_threshold(self):
        # Test on similar_books -> Book, whose default identifying field is
        # deterministically 'name'. (For auth.User the default would be the
        # first alphabetical string field, 'email', not 'username'.)
        for n in ('Dune', 'Solaris'):
            self._book(n)

        class ForceSchema(DjangoQLSchema):
            fk_options = {Book: {'similar_books': True}}

        # threshold of 1 would normally skip two distinct names
        bundle = describe_schema_for_llm(ForceSchema(Book), max_fk_options=1)
        similar = bundle['models']['core.book']['similar_books']
        self.assertEqual('name', similar['match_field'])
        self.assertEqual({'Dune', 'Solaris'}, set(similar['related_values']))
