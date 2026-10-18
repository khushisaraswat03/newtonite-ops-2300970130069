from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from . import services
from .idempotency import idempotent
from .models import WorkItem
from .permissions import check
from .queries import BadQuery, filter_items, paginate
from .serializers import (
    ActivityEventSerializer, CommentInput, CommentSerializer, CreateItemInput, EditItemInput,
    HandoffInput, ReasonInput, TransitionInput, WorkItemDetailSerializer, WorkItemListSerializer,
)
from .workflow import TransitionError

ACTIVITY_PAGE_SIZE = 50


def exception_handler(exc, context):
    if isinstance(exc, services.ConflictError):
        body = {"detail": str(exc)}
        if exc.current is not None:
            body["current"] = WorkItemDetailSerializer(exc.current, context={"user": context["request"].user}).data
        return Response(body, status=status.HTTP_409_CONFLICT)
    if isinstance(exc, (TransitionError, BadQuery)):
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    if isinstance(exc, DjangoValidationError):
        errors = exc.message_dict if hasattr(exc, "error_dict") else {"non_field_errors": exc.messages}
        return Response({"detail": _first_message(errors), "errors": errors}, status=status.HTTP_400_BAD_REQUEST)
    if isinstance(exc, exceptions.ValidationError):
        errors = exc.detail if isinstance(exc.detail, dict) else {"non_field_errors": exc.detail}
        return Response({"detail": _first_message(errors), "errors": errors}, status=status.HTTP_400_BAD_REQUEST)
    return drf_exception_handler(exc, context)


def _first_message(errors):
    field, messages = next(iter(errors.items()))
    message = messages[0] if isinstance(messages, list) else messages
    return message if field == "non_field_errors" else f"{field}: {message}"


def _validated(serializer_class, request):
    s = serializer_class(data=request.data)
    s.is_valid(raise_exception=True)
    return s.validated_data


def _get_item(request, pk):
    item = WorkItem.objects.select_related("team", "assignee", "created_by").filter(pk=pk).first()
    if item is None:
        raise Http404("Item not found.")
    check(request.user, "view", item)
    return item


def _detail(request, item, status_code=200):
    return Response(WorkItemDetailSerializer(item, context={"user": request.user}).data, status=status_code)


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


@api_view(["GET", "POST"])
@idempotent
def item_list(request):
    if request.method == "POST":
        item = services.create_item(request.user, **_validated(CreateItemInput, request))
        return _detail(request, item, status.HTTP_201_CREATED)

    qs = filter_items(request.user, request.query_params)
    items, next_cursor = paginate(qs, request.query_params.get("cursor"), request.query_params.get("page_size"))
    return Response({
        "results": WorkItemListSerializer(items, many=True).data,
        "next_cursor": next_cursor,
    })


@api_view(["GET", "PATCH"])
@idempotent
def item_detail(request, pk):
    item = _get_item(request, pk)
    if request.method == "PATCH":
        item = services.update_item(request.user, item, **_validated(EditItemInput, request))
    return _detail(request, item)


@api_view(["POST"])
@idempotent
def claim(request, pk):
    return _detail(request, services.claim_item(request.user, _get_item(request, pk)))


@api_view(["POST"])
@idempotent
def release(request, pk):
    return _detail(request, services.release_item(request.user, _get_item(request, pk)))


@api_view(["POST"])
@idempotent
def unassign(request, pk):
    data = _validated(ReasonInput, request)
    return _detail(request, services.unassign_item(request.user, _get_item(request, pk), data["reason"]))


@api_view(["POST"])
@idempotent
def transition(request, pk):
    data = _validated(TransitionInput, request)
    item = services.transition_item(
        request.user, _get_item(request, pk), data["status"], version=data["version"], reason=data["reason"],
    )
    return _detail(request, item)


@api_view(["POST"])
@idempotent
def handoff(request, pk):
    data = _validated(HandoffInput, request)
    item = services.handoff_item(
        request.user, _get_item(request, pk), data["team"], version=data["version"], reason=data["reason"],
    )
    return _detail(request, item)


@api_view(["GET", "POST"])
@idempotent
def comments(request, pk):
    item = _get_item(request, pk)
    if request.method == "POST":
        comment = services.add_comment(request.user, item, _validated(CommentInput, request)["body"])
        return Response(CommentSerializer(comment).data, status=status.HTTP_201_CREATED)
    return _paged_by_id(request, item.comments.select_related("author"), CommentSerializer)


@api_view(["GET"])
def activity(request, pk):
    item = _get_item(request, pk)
    return _paged_by_id(request, item.events.select_related("actor"), ActivityEventSerializer)


def _paged_by_id(request, qs, serializer_class):
    before = request.query_params.get("before")
    if before:
        if not before.isdigit():
            raise BadQuery("before must be an id.")
        qs = qs.filter(id__lt=int(before))
    rows = list(qs.order_by("-id")[: ACTIVITY_PAGE_SIZE + 1])
    page = rows[:ACTIVITY_PAGE_SIZE]
    return Response({
        "results": serializer_class(page, many=True).data,
        "next_before": page[-1].id if len(rows) > ACTIVITY_PAGE_SIZE else None,
    })
