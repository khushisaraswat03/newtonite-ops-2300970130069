"""
Idempotency keys: protection against double-clicks and automatic retries.

The client sends a random key with a write request (API: `Idempotency-Key`
header; HTML forms: a hidden field), and the SAME key if it retries. The first
request runs normally and its result is stored; any repeat gets the stored
result instead of running again.

How it stays correct when the two copies arrive at the same moment:
the key row is INSERTed at the start, in the same transaction as the action.
(user, key) is unique, so Postgres makes the second INSERT wait until the
first transaction finishes; then it fails, and we return the stored result.

Only successful results are stored: if the action raises an error, the whole
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
MAX_KEY_LENGTH = 100


class KeyReused(Exception):
    """The key was already used for a different request."""


def fingerprint(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def run_once(user, key, request_hash, action):
    """
    Run `action()` at most once per (user, key).
    `action` returns (status, body); on a repeat the stored (status, body) is returned
    with replayed=True. Returns (status, body, replayed).
    """
    with transaction.atomic():
        try:
            with transaction.atomic():  # savepoint, so a failed INSERT doesn't break the outer transaction
                record = IdempotencyKey.objects.create(
                    user=user, key=key, request_hash=request_hash, response_status=0, response_body={},
                )
        except IntegrityError:
            # Seen this key before (possibly after waiting for the first request to commit).
            stored = IdempotencyKey.objects.get(user=user, key=key)
            if stored.request_hash != request_hash:
                raise KeyReused()
            return stored.response_status, stored.response_body, True

        status, body = action()  # an exception here rolls back the key row too
        record.response_status = status
        record.response_body = body
        record.save(update_fields=["response_status", "response_body"])
        return status, body, False


def idempotent(view):
    """Decorator for API views. Put it below @api_view so request.user and request.data are ready."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        key = request.META.get(HEADER)
        if not key or request.method not in WRITE_METHODS:
            return view(request, *args, **kwargs)
        if len(key) > MAX_KEY_LENGTH:
            return Response({"detail": f"Idempotency-Key must be at most {MAX_KEY_LENGTH} characters."}, status=400)

        def action():
            response = view(request, *args, **kwargs)
            return response.status_code, response.data

        try:
            status, body, replayed = run_once(
                request.user, key, fingerprint(request.method, request.path, request.data), action,
            )
        except KeyReused:
            return Response({"detail": "This Idempotency-Key was already used for a different request."}, status=422)

        response = Response(body, status=status)
        if replayed:
            response["Idempotent-Replayed"] = "true"
        return response

    return wrapper
