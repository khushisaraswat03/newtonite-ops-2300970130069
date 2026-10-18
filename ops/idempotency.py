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
    pass


def fingerprint(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def run_once(user, key, request_hash, action):
    with transaction.atomic():
        try:
            with transaction.atomic():
                record = IdempotencyKey.objects.create(
                    user=user, key=key, request_hash=request_hash, response_status=0, response_body={},
                )
        except IntegrityError:
            stored = IdempotencyKey.objects.get(user=user, key=key)
            if stored.request_hash != request_hash:
                raise KeyReused()
            return stored.response_status, stored.response_body, True

        status, body = action()
        record.response_status = status
        record.response_body = body
        record.save(update_fields=["response_status", "response_body"])
        return status, body, False


def idempotent(view):
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
