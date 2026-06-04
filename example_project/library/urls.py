from django.urls import path

from . import views


urlpatterns = [
    path('', views.index, name='demo'),
    path('codemirror/', views.codemirror, name='demo-codemirror'),
    path('api/format/', views.api_format, name='demo-api-format'),
    path('api/explain/', views.api_explain, name='demo-api-explain'),
    path('api/search/', views.api_search, name='demo-api-search'),
    path('api/introspect/', views.api_introspect, name='demo-api-introspect'),
]
