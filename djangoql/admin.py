import json

from django.contrib import messages
from django.contrib.admin.views.main import ChangeList
from django.core.exceptions import FieldError, ValidationError
from django.db import DataError, NotSupportedError
from django.forms import Media
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.urls import path, reverse
from django.views.generic import TemplateView

from .breakdown import explain_empty
from .exceptions import DjangoQLError
from .queryset import apply_search
from .schema import DjangoQLSchema
from .serializers import SuggestionsAPISerializer
from .views import SuggestionsAPIView


DJANGOQL_SEARCH_MARKER = 'q-l'


class DjangoQLChangeList(ChangeList):
    def get_filters_params(self, *args, **kwargs):
        params = super().get_filters_params(*args, **kwargs)
        if DJANGOQL_SEARCH_MARKER in params:
            del params[DJANGOQL_SEARCH_MARKER]
        return params


class DjangoQLSearchMixin:
    search_fields = ('_djangoql',)  # just a stub to have search input displayed
    djangoql_completion = True
    djangoql_completion_enabled_by_default = True
    djangoql_schema = DjangoQLSchema
    djangoql_syntax_help_template = 'djangoql/syntax_help.html'
    # When a valid DjangoQL search returns zero rows, explain *where* in the
    # query the data runs out and surface it as a warning. Set to False to
    # disable the extra (lazy, count()-per-node) queries.
    djangoql_explain_empty = True
    # Cost guard for the empty-result breakdown: max AST nodes counted before
    # the breakdown is truncated to the top-level conjuncts.
    djangoql_explain_empty_max_nodes = 50

    def search_mode_toggle_enabled(self):
        # If search fields were defined on a child ModelAdmin instance,
        # we suppose that the developer wants two search modes and therefore
        # enable search mode toggle
        return self.search_fields != DjangoQLSearchMixin.search_fields

    def djangoql_search_enabled(self, request):
        return request.GET.get(DJANGOQL_SEARCH_MARKER, '').lower() == 'on'

    def get_changelist(self, *args, **kwargs):
        return DjangoQLChangeList

    def get_search_results(self, request, queryset, search_term):
        if (
            self.search_mode_toggle_enabled()
            and not self.djangoql_search_enabled(request)
        ):
            return super().get_search_results(
                request=request,
                queryset=queryset,
                search_term=search_term,
            )
        use_distinct = False
        if not search_term:
            return queryset, use_distinct

        try:
            qs = apply_search(queryset, search_term, self.djangoql_schema)
        except (DjangoQLError, ValueError, FieldError, ValidationError) as e:
            msg = self.djangoql_error_message(e)
            messages.add_message(request, messages.WARNING, msg)
            qs = queryset.none()
        else:
            # Hack to handle 'inet' comparison errors in Postgres. If you
            # know a better way to check for such an error, please submit a PR.
            try:
                # Django >= 2.1 has built-in .explain() method
                explain = getattr(qs, 'explain', None)
                if callable(explain):
                    try:
                        explain()
                    except NotSupportedError:
                        list(qs[:1])
                else:
                    list(qs[:1])
            except DataError as e:
                if 'inet' not in str(e):
                    raise
                msg = self.djangoql_error_message(e)
                messages.add_message(request, messages.WARNING, msg)
                qs = queryset.none()
            else:
                self.djangoql_add_empty_breakdown(
                    request,
                    queryset,
                    qs,
                    search_term,
                )

        return qs, use_distinct

    def djangoql_add_empty_breakdown(self, request, queryset, qs, search_term):
        """If a valid DjangoQL search returned zero rows, compute and surface
        a breakdown of *where* the query runs out of data as a warning.

        Lazy: this is only attempted when ``djangoql_explain_empty`` is on and
        the result set is empty, so the extra count() queries never run on a
        non-empty search.
        """
        if not self.djangoql_explain_empty:
            return
        exists = getattr(qs, 'exists', None)
        # Only real querysets support a cheap emptiness check; anything else
        # (e.g. a test double) is left untouched.
        if not callable(exists) or exists():
            return
        try:
            breakdown = explain_empty(
                queryset,
                search_term,
                self.djangoql_schema,
                max_nodes=self.djangoql_explain_empty_max_nodes,
            )
        except (DjangoQLError, ValueError, FieldError, ValidationError):
            # Never let the breakdown break the changelist; it's a best-effort
            # diagnostic on top of an already-empty result.
            return
        if breakdown is None:
            return
        msg = render_to_string(
            'djangoql/empty_breakdown.html',
            context={'node': breakdown},
        )
        messages.add_message(request, messages.WARNING, msg)

    def djangoql_error_message(self, exception):
        if isinstance(exception, ValidationError):
            msg = exception.messages[0]
        else:
            msg = str(exception)
        return render_to_string(
            'djangoql/error_message.html',
            context={
                'error_message': msg,
                'djangoql_syntax_help_url': reverse(
                    '%s:djangoql_syntax_help' % self.admin_site.name,
                ),
            },
        )

    @property
    def media(self):
        media = super().media
        if self.djangoql_completion:
            js = [
                'djangoql/js/completion.js',
            ]
            if self.search_mode_toggle_enabled():
                js.append('djangoql/js/completion_admin_toggle.js')
                if not self.djangoql_completion_enabled_by_default:
                    js.append('djangoql/js/completion_admin_toggle_off.js')
            js.append('djangoql/js/completion_admin.js')
            # Shift+Enter -> newline (Enter still submits). Loaded last so the
            # admin textarea created by completion_admin.js already exists; the
            # script itself is framework-agnostic and delegates on document.
            js.append('djangoql/js/multiline.js')
            media += Media(
                css={
                    '': (
                        'djangoql/css/completion.css',
                        'djangoql/css/completion_admin.css',
                    )
                },
                js=js,
            )
        return media

    def get_urls(self):
        custom_urls = []
        if self.djangoql_completion:
            custom_urls += [
                path(
                    'introspect/',
                    self.admin_site.admin_view(self.introspect),
                    name='{}_{}_djangoql_introspect'.format(
                        self.model._meta.app_label,
                        self.model._meta.model_name,
                    ),
                ),
                path(
                    'suggestions/',
                    self.admin_site.admin_view(self.suggestions),
                    name='{}_{}_djangoql_suggestions'.format(
                        self.model._meta.app_label,
                        self.model._meta.model_name,
                    ),
                ),
                path(
                    'djangoql-syntax/',
                    self.admin_site.admin_view(
                        TemplateView.as_view(
                            template_name=self.djangoql_syntax_help_template,
                        )
                    ),
                    name='djangoql_syntax_help',
                ),
            ]
        return custom_urls + super().get_urls()

    def introspect(self, request):
        suggestions_url = reverse(
            '{}:{}_{}_djangoql_suggestions'.format(
                self.admin_site.name,
                self.model._meta.app_label,
                self.model._meta.model_name,
            )
        )
        serializer = SuggestionsAPISerializer(suggestions_url)
        response = serializer.serialize(self.djangoql_schema(self.model))
        return HttpResponse(
            content=json.dumps(response, indent=2),
            content_type='application/json; charset=utf-8',
        )

    def suggestions(self, request):
        view = SuggestionsAPIView.as_view(
            schema=self.djangoql_schema(self.model),
        )
        return view(request)
