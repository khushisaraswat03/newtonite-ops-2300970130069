from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("login/", views.OpsLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("items/", views.item_list, name="item_list"),
    path("notifications/", views.notifications, name="notifications"),
    path("items/new/", views.item_create, name="item_create"),
    path("items/<int:pk>/", views.item_detail, name="item_detail"),
    path("items/<int:pk>/<str:action>/", views.item_action, name="item_action"),
]
