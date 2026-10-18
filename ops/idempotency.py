"""
Idempotency keys: protection against double-clicks and automatic retries.

The client sends an `Idempotency-Key: <random id>` header with a write request,
and the SAME key if it retries. The first request runs normally and its
response is stored; any repeat gets the stored response instead of running again.

How it stays correct when the two copies arrive at the same moment:
the key row is INSERTed at the start, in the same transaction as the action.
(user, key) is unique, so Postgres makes the second INSERT wait until the
first transaction finishes; then it fails, and we return the stored response.

Only successful responses are stored: if the action raises an error, the whole
transaction (key row included) rolls back, so the user can fix the input and retry.
"""

import functools
import hashlib
import json

from django.db import IntegrityError, transaction
from rest_framework.response import Response

from .models import IdempotencyKey

HEADER = "HTTP_IDEMPOTENCY_KEY"
WRITE_METHODS = {"POST", "PATCH"}


def _request_hash(request):
    body = json.dumps(request.data, sort_keys=True, default=str)
    return hashlib.sha256(f"{request.method} {request.path} {body}".encode()).hexdigest()


def _replay(stored):
    response = Response(stored.response_body, status=stored.response_status)
    response["Idempotent-Replayed"] = "true"
    return response


def idempotent(view):
    """Decorator for API views. Put it below @api_view so request.user and request.data are ready."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        key = request.META.get(HEADER)
        if not key or request.method not in WRITE_METHODS:
            return view(request, *args, **kwargs)
        if len(key) > 100:
            return Response({"detail": "Idempotency-Key must be at most 100 characters."}, status=400)

        request_hash = _request_hash(request)
        with transaction.atomic():
            try:
                with transaction.atomic():  # savepoint, so a failed INSERT doesn't break the outer transaction
                    record = IdempotencyKey.objects.create(
                        user=request.user, key=key, request_hash=request_hash,
                        response_status=0, response_body={},
                    )
            except IntegrityError:
                # Seen this key before (possibly waited for the first request to commit).
                stored = IdempotencyKey.objects.get(user=request.user, key=key)
                if stored.request_hash != request_hash:
                    return Response(
                        {"detail": "This Idempotency-Key was already used for a different request."},
                        status=422,
                    )
                return _replay(stored)

            response = view(request, *args, **kwargs)  # an exception here rolls back the key too
            record.response_status = response.status_code
            record.response_body = response.data
            record.save(update_fields=["response_status", "response_body"])
            return response

    return wrapper
