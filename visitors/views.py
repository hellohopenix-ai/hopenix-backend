from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from users.access import ModuleAccess

from .models import Visitor, VisitorStatus
from .permissions import can_approve_visitors, can_delete_visitor, can_edit_visitor
from .serializers import VisitorSerializer

User = get_user_model()


def _notify_admins_of_visitor(visitor, request):
    """New visitor request -> every admin gets alerted, the same
    "online tab gets a live socket push, offline device gets a real
    Web Push" split messaging.views.CallStartView already uses for calls
    (see messaging/push_utils.py's module docstring). This is what makes
    the approval request reach the admin's phone/laptop even with the
    Hopenix tab or browser fully closed, instead of only showing up the
    next time someone happens to open the Visitors page.

    Imported inside the function (not at module level) so this app
    doesn't hard-fail to load if `messaging` isn't installed/migrated
    yet in some environment — a missing notification is far less
    disruptive than the whole Visitors API refusing to start.
    """
    try:
        from messaging.consumers import is_user_online
        from messaging.push_utils import send_web_push
        from messaging.views import push_to_user
    except Exception:
        return

    data = VisitorSerializer(visitor, context={"request": request}).data
    title = "New Visitor Approval Request"
    body = f"{visitor.name}{f' — {visitor.company}' if visitor.company else ''} is waiting to meet {visitor.meeting_with or 'you'}."

    # Every admin except the one who just registered this visitor
    # themselves (they already know — they just submitted the form).
    admins = User.objects.filter(role="admin").exclude(id=getattr(request.user, "id", None))
    for admin in admins:
        payload = {"type": "visitor.request", "visitor": data, "title": title, "body": body}
        try:
            if is_user_online(admin.id):
                push_to_user(admin.id, payload)
            else:
                send_web_push(admin, payload)
        except Exception:
            # Never let a notification failure break the actual
            # registration — the visitor record itself is already saved.
            continue


class VisitorViewSet(viewsets.ModelViewSet):
    """Full CRUD for VisitorsPage.jsx, plus the decide/wait/reopen actions
    the Host Approval panel needs beyond plain field edits — mirrors
    expenses.ExpenseViewSet's shape (custom actions alongside standard
    ModelViewSet CRUD) elsewhere in this project.

    Looked up by `code` (the "HV-..." id) rather than the numeric pk, so
    every place the frontend already has a visitor's `id` can call these
    endpoints directly with zero remapping.
    """

    # Server-side enforcement of the Page Access / Module Access tables for
    # the "Visitors" page (see users/access.py): the caller must be allowed
    # to open the Visitors page AND hold the matching view/create/edit/
    # delete flag. Client-role tokens are always refused.
    module_name = "Visitors"
    serializer_class = VisitorSerializer
    permission_classes = [permissions.IsAuthenticated, ModuleAccess]
    lookup_field = "code"

    def get_queryset(self):
        qs = Visitor.objects.select_related("created_by", "reviewed_by").all()
        params = self.request.query_params

        status_param = params.get("status")
        if status_param and status_param != "All":
            qs = qs.filter(status=status_param)

        search = params.get("search")
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(company__icontains=search)
                | Q(phone__icontains=search)
                | Q(meeting_with__icontains=search)
            )

        return qs

    def perform_create(self, serializer):
        phone = serializer.validated_data.get("phone", "").strip()
        prior_visits = Visitor.objects.filter(phone=phone).count()
        visitor = serializer.save(created_by=self.request.user, visits=prior_visits + 1)
        _notify_admins_of_visitor(visitor, self.request)

    def perform_update(self, serializer):
        if not can_edit_visitor(self.request.user, self.get_object()):
            raise PermissionDenied("You can't edit this visitor record.")
        serializer.save()

    def perform_destroy(self, instance):
        if not can_delete_visitor(self.request.user, instance):
            raise PermissionDenied("You can't delete this visitor record.")
        instance.delete()

    def _require_admin(self):
        if not can_approve_visitors(self.request.user):
            raise PermissionDenied("Only an admin can decide on a visitor request.")

    # -- Host Approval actions --------------------------------------------
    # Mirror decide(id, status) / askToWait(id) / reopenForReview(id) on
    # the frontend exactly, one endpoint each, so the eventual API wiring
    # is a straight swap for the current local setVisitors(...) calls.

    @action(detail=True, methods=["post"])
    def decide(self, request, code=None):
        """POST /api/visitors/visitors/<code>/decide/   Body: { status: "Approved" | "Rejected" }"""
        self._require_admin()
        new_status = request.data.get("status")
        if new_status not in (VisitorStatus.APPROVED, VisitorStatus.REJECTED):
            return Response(
                {"error": "status must be \"Approved\" or \"Rejected\"."}, status=status.HTTP_400_BAD_REQUEST
            )
        visitor = self.get_object()
        visitor.status = new_status
        visitor.reviewed = True
        visitor.reviewed_by = request.user
        visitor.reviewed_at = timezone.now()
        visitor.save(update_fields=["status", "reviewed", "reviewed_by", "reviewed_at"])
        return Response(VisitorSerializer(visitor, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def wait(self, request, code=None):
        """POST /api/visitors/visitors/<code>/wait/ — "Ask to wait": drops
        out of the live queue without approving or rejecting yet."""
        self._require_admin()
        visitor = self.get_object()
        visitor.reviewed = True
        visitor.reviewed_by = request.user
        visitor.reviewed_at = timezone.now()
        visitor.save(update_fields=["reviewed", "reviewed_by", "reviewed_at"])
        return Response(VisitorSerializer(visitor, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def reopen(self, request, code=None):
        """POST /api/visitors/visitors/<code>/reopen/ — brings an
        already-reviewed Waiting record back into the live Host Approval
        queue (the Records table's "Review" action)."""
        self._require_admin()
        visitor = self.get_object()
        visitor.reviewed = False
        visitor.reviewed_by = None
        visitor.reviewed_at = None
        visitor.save(update_fields=["reviewed", "reviewed_by", "reviewed_at"])
        return Response(VisitorSerializer(visitor, context={"request": request}).data)