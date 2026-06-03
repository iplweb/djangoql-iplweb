# Schema & custom fields

## DjangoQL Schema

Schema defines limitations - what you can do with a DjangoQL query. If you don't specify any schema, DjangoQL will provide a default schema for you. This will walk recursively through all model fields and relations and include everything it finds in the schema, so users would be able to search through everything. Sometimes this is not what you want, either due to DB performance or security concerns. If you'd like to limit search models or fields, you should define a schema. Here's an example:

``` python
class UserQLSchema(DjangoQLSchema):
    exclude = (Book,)
    suggest_options = {
        Group: ['name'],
    }

    def get_fields(self, model):
        if model == Group:
            return ['name']
        return super(UserQLSchema, self).get_fields(model)


@admin.register(User)
class CustomUserAdmin(DjangoQLSearchMixin, UserAdmin):
    djangoql_schema = UserQLSchema
```

In the example above we created a schema that does 3 things:

- excludes the Book model from search via `exclude` option. Instead of `exclude` you may also use `include`, which limits a search to listed models only;
- limits available search fields for Group model to only the `name` field , in the `.get_fields()` method;
- enables completion options for Group names via `suggest_options`.

An important note about `suggest_options`: it looks for the `choices` model field parameter first, and if it's not specified - it will synchronously pull all values for given model fields, so you should avoid large querysets there. If you'd like to define custom suggestion options, see below.

## Custom search fields

Deeper search customization can be achieved with custom search fields. Custom search fields can be used to search by annotations, define custom suggestion options, or define fully custom search logic. In `djangoql.schema`, DjangoQL defines the following base field classes that you may subclass to define your own behavior:

- `IntField`
- `FloatField`
- `StrField`
- `BoolField`
- `DateField`
- `DateTimeField`
- `RelationField`

Here are examples for common use cases:

**Search by queryset annotations:**

``` python
from djangoql.schema import DjangoQLSchema, IntField


class UserQLSchema(DjangoQLSchema):
    def get_fields(self, model):
        fields = super(UserQLSchema, self).get_fields(model)
        if model == User:
            fields += [IntField(name='groups_count')]
        return fields


@admin.register(User)
class CustomUserAdmin(DjangoQLSearchMixin, UserAdmin):
    djangoql_schema = UserQLSchema

    def get_queryset(self, request):
        qs = super(CustomUserAdmin, self).get_queryset(request)
        return qs.annotate(groups_count=Count('groups'))
```

Let's take a closer look at what's happening in the example above. First, we add `groups_count` annotation to the queryset that is used by Django admin in the `CustomUserAdmin.get_queryset()` method. It would contain the number of groups a user belongs to. As our queryset now pulls this column, we can filter by it. It just needs to be included in the schema. In `UserQLSchema.get_fields()` we define a custom integer search field for the `User` model. Its name should match the name of the column in our queryset.

**Custom suggestion options**

``` python
from djangoql.schema import DjangoQLSchema, StrField


class GroupNameField(StrField):
    model = Group
    name = 'name'
    suggest_options = True

    def get_options(self, search):
        return super(GroupNameField, self)\
            .get_options(search)\
            .annotate(users_count=Count('user'))\
            .order_by('-users_count')


class UserQLSchema(DjangoQLSchema):
    def get_fields(self, model):
        if model == Group:
            return ['id', GroupNameField()]
        return super(UserQLSchema, self).get_fields(model)


@admin.register(User)
class CustomUserAdmin(DjangoQLSearchMixin, UserAdmin):
    djangoql_schema = UserQLSchema
```

In this example we've defined a custom GroupNameField that sorts suggestions for group names by popularity (no. of users in a group) instead of default alphabetical sorting.

**Custom search lookup**

DjangoQL base fields provide two basic methods that you can override to substitute either search column, search value, or both - `.get_lookup_name()` and `.get_lookup_value(value)`:

``` python
class UserDateJoinedYear(IntField):
    name = 'date_joined_year'

    def get_lookup_name(self):
        return 'date_joined__year'


class UserQLSchema(DjangoQLSchema):
    def get_fields(self, model):
        fields = super(UserQLSchema, self).get_fields(model)
        if model == User:
            fields += [UserDateJoinedYear()]
        return fields


@admin.register(User)
class CustomUserAdmin(DjangoQLSearchMixin, UserAdmin):
    djangoql_schema = UserQLSchema
```

In this example we've defined the custom `date_joined_year` search field for users, and used the built-in Django `__year` filter option in `.get_lookup_name()` to filter by date year only. Similarly you can use `.get_lookup_value(value)` hook to modify a search value before it's used in the filter.

**Fully custom search lookup**

`.get_lookup_name()` and `.get_lookup_value(value)` hooks cover many simple use cases, but sometimes they're not enough and you want a fully custom search logic. In such cases you can override main `.get_lookup()` method of a field. Example below demonstrates User `age` search:

``` python
from djangoql.schema import DjangoQLSchema, IntField


class UserAgeField(IntField):
    """
    Search by given number of full years
    """
    model = User
    name = 'age'

    def get_lookup_name(self):
        """
        We'll be doing comparisons vs. this model field
        """
        return 'date_joined'

    def get_lookup(self, path, operator, value):
        """
        The lookup should support with all operators compatible with IntField
        """
        if operator == 'in':
            result = None
            for year in value:
                condition = self.get_lookup(path, '=', year)
                result = condition if result is None else result | condition
            return result
        elif operator == 'not in':
            result = None
            for year in value:
                condition = self.get_lookup(path, '!=', year)
                result = condition if result is None else result & condition
            return result

        value = self.get_lookup_value(value)
        search_field = '__'.join(path + [self.get_lookup_name()])
        year_start = self.years_ago(value + 1)
        year_end = self.years_ago(value)
        if operator == '=':
            return (
                Q(**{'%s__gt' % search_field: year_start}) &
                Q(**{'%s__lte' % search_field: year_end})
            )
        elif operator == '!=':
            return (
                Q(**{'%s__lte' % search_field: year_start}) |
                Q(**{'%s__gt' % search_field: year_end})
            )
        elif operator == '>':
            return Q(**{'%s__lt' % search_field: year_start})
        elif operator == '>=':
            return Q(**{'%s__lte' % search_field: year_end})
        elif operator == '<':
            return Q(**{'%s__gt' % search_field: year_end})
        elif operator == '<=':
            return Q(**{'%s__gte' % search_field: year_start})

    def years_ago(self, n):
        timestamp = now()
        try:
            return timestamp.replace(year=timestamp.year - n)
        except ValueError:
            # February 29
            return timestamp.replace(month=2, day=28, year=timestamp.year - n)


class UserQLSchema(DjangoQLSchema):
    def get_fields(self, model):
        fields = super(UserQLSchema, self).get_fields(model)
        if model == User:
            fields += [UserAgeField()]
        return fields


@admin.register(User)
class CustomUserAdmin(DjangoQLSearchMixin, UserAdmin):
    djangoql_schema = UserQLSchema
```
