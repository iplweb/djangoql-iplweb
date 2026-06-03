from django.test import TestCase

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
