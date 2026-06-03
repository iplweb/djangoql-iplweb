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
