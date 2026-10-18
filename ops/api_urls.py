from django.urls import path
from rest_framework.authtoken.views import obtain_auth_token

from . import api

urlpatterns = [
    path("auth/token/", obtain_auth_token),
    path("me/", api.me),
    path("items/", api.item_list),
    path("items/<int:pk>/", api.item_detail),
    path("items/<int:pk>/claim/", api.claim),
    path("items/<int:pk>/release/", api.release),
    path("items/<int:pk>/unassign/", api.unassign),
    path("items/<int:pk>/transition/", api.transition),
    path("items/<int:pk>/handoff/", api.handoff),
    path("items/<int:pk>/comments/", api.comments),
    path("items/<int:pk>/activity/", api.activity),
]
