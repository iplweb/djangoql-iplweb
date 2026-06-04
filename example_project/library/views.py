"""Standalone (non-admin) showcase views.

These call the DjangoQL primitives directly — ``apply_search`` (run a query),
``format_query`` (pretty-print), and ``explain`` (per-branch counts) — to show
that the features work outside the admin too. The query box on these pages uses
the library's own front-end primitives (multiline.js, highlight.js) with a
custom palette; styling here is deliberately "turned up" — it is a demo.

The API endpoints are ``csrf_exempt`` purely to keep this self-contained demo
simple. Do not copy that into a real project.
"""

import json

from django.core.exceptions import FieldError, ValidationError
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from djangoql.breakdown import explain
from djangoql.exceptions import DjangoQLError
from djangoql.formatter import format_query
from djangoql.queryset import apply_search

from .models import Book


QUERY_ERRORS = (DjangoQLError, ValueError, FieldError, ValidationError)

EXAMPLES = [
    'rating > 4.5 and in_stock = True',
    'author.country.name = "Poland" or publisher.name ~ "Press"',
    'year >= 2000 and (genres.name = "Science Fiction" or rating > 4)',
    'pages > 400 and author.alive = True and price < 30',
]


def index(request):
    return render(request, 'library/demo.html', {'examples': EXAMPLES})


def codemirror(request):
    return render(request, 'library/codemirror.html', {'examples': EXAMPLES})


def _query(request):
    if request.method == 'POST':
        return request.POST.get('q', '')
    return request.GET.get('q', '')


@csrf_exempt
def api_format(request):
    query = _query(request)
    if not query.strip():
        return JsonResponse({'formatted': ''})
    try:
        return JsonResponse({'formatted': format_query(query)})
    except DjangoQLError as e:
        return JsonResponse({'error': str(e)}, status=400)


@csrf_exempt
def api_explain(request):
    query = _query(request)
    if not query.strip():
        return JsonResponse({'tree': None})
    try:
        tree = explain(Book.objects.all(), query)
    except QUERY_ERRORS as e:
        return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'tree': tree})


@csrf_exempt
def api_search(request):
    query = _query(request)
    qs = Book.objects.all().select_related('author', 'publisher')
    if query.strip():
        try:
            qs = apply_search(qs, query)
        except QUERY_ERRORS as e:
            return JsonResponse({'error': str(e)}, status=400)
    total = qs.count()
    rows = [
        {
            'title': b.title,
            'author': b.author.name,
            'publisher': b.publisher.name if b.publisher else '',
            'year': b.year,
            'rating': b.rating,
            'price': str(b.price),
            'in_stock': b.in_stock,
        }
        for b in qs[:100]
    ]
    return JsonResponse({'total': total, 'shown': len(rows), 'rows': rows})


def api_introspect(request):
    """Schema JSON for the completion widget (used by the CodeMirror page)."""
    from djangoql.schema import DjangoQLSchema
    from djangoql.serializers import DjangoQLSchemaSerializer

    schema = DjangoQLSchema(Book)
    data = DjangoQLSchemaSerializer().serialize(schema)
    return JsonResponse(json.loads(json.dumps(data)))
