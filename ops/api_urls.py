from django.urls import path
from rest_framework.authtoken.views import obtain_auth_token

from . import api

urlpatterns = [
    # POST {"username", "password"} -> {"token"}; then send "Authorization: Token <token>".
    path("auth/token/", obtain_auth_token),
    path("me/", api.me),
    path("items/", api.item_list),
]
