import json
import re

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import UserFlag

KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
MAX_VALUE_BYTES = 64 * 1024  # one flag is a tiny UI marker, never a document store
MAX_FLAGS_PER_USER = 200


class FlagListView(APIView):
    """GET /api/flags/ -> { "flags": { "<key>": <value>, ... } } for the logged-in user only."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = UserFlag.objects.filter(user=request.user)
        return Response({"flags": {r.key: r.value for r in rows}})


class FlagDetailView(APIView):
    """PUT /api/flags/<key>/ {"value": ...} -> create or replace one flag.
    DELETE /api/flags/<key>/ -> remove it. Always scoped to request.user, so
    nobody can read or change another account's flags."""

    permission_classes = [IsAuthenticated]

    def put(self, request, key):
        if not KEY_RE.match(key):
            return Response({"error": "Invalid flag name."}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(request.data, dict) or "value" not in request.data:
            return Response({"error": "Send a JSON body like {\"value\": ...}."}, status=status.HTTP_400_BAD_REQUEST)
        value = request.data["value"]
        if len(json.dumps(value)) > MAX_VALUE_BYTES:
            return Response({"error": "Flag value is too large."}, status=status.HTTP_400_BAD_REQUEST)

        existing = UserFlag.objects.filter(user=request.user, key=key).first()
        if existing is None and UserFlag.objects.filter(user=request.user).count() >= MAX_FLAGS_PER_USER:
            return Response({"error": "Too many flags saved for this account."}, status=status.HTTP_400_BAD_REQUEST)

        flag, _ = UserFlag.objects.update_or_create(user=request.user, key=key, defaults={"value": value})
        return Response({"key": flag.key, "value": flag.value})

    def delete(self, request, key):
        UserFlag.objects.filter(user=request.user, key=key).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
