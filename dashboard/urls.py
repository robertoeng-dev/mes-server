from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard_home, name="dashboard_home"),

    path("api/summary/", views.api_summary, name="api_summary"),
    path("api/top-failures/", views.api_top_failures, name="api_top_failures"),
    path("api/channels/", views.api_channels, name="api_channels"),
    path("api/hourly-yield/", views.api_hourly_yield, name="api_hourly_yield"),
    path("api/channel-hourly/", views.api_channel_hourly, name="api_channel_hourly"),
    path("api/debug/", views.api_debug, name="api_debug"),
    path("api/filters/", views.api_filters, name="api_filters"),
    path("api/channel-matrix/", views.api_channel_matrix, name="api_channel_matrix"),
    path("api/carrier-matrix/", views.api_carrier_matrix, name="api_carrier_matrix"),
    path("api/carrier-cycles/", views.api_carrier_cycles, name="api_carrier_cycles"),
    path("api/carriers/reset/", views.api_carrier_reset, name="api_carrier_reset"),
    path("api/carriers/limit/", views.api_carrier_limit, name="api_carrier_limit"),
    path("api/spc/distribution/", views.api_spc_distribution, name="api_spc_distribution"),
]
