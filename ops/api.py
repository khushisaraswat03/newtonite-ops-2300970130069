"""
JSON API. Views only translate HTTP <-> Python: permissions, rules and
database work live in permissions.py, queries.py and services.py.
"""

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .permissions import team_roles
from .queries import BadQuery, filter_items, paginate
from .serializers import WorkItemListSerializer


@api_view(["GET"])
def me(request):
    user = request.user
    return Response({
        "id": user.id,
        "username": user.username,
        "is_admin": user.is_superuser,
        "teams": [
            {"id": m.team_id, "name": m.team.name, "role": m.role}
            for m in user.memberships.select_related("team")
        ],
    })


@api_view(["GET"])
def item_list(request):
    """
    GET /api/items/?view=&status=&priority=&type=&team=&assignee=&q=&cursor=&page_size=
    """
    try:
        qs = filter_items(request.user, request.query_params)
        items, next_cursor = paginate(qs, request.query_params.get("cursor"), request.query_params.get("page_size"))
    except BadQuery as e:
        return Response({"detail": str(e)}, status=400)

    return Response({
        "results": WorkItemListSerializer(items, many=True).data,
        "next_cursor": next_cursor,
    })
