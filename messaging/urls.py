from django.urls import path
from . import views

urlpatterns = [
    path("contacts/", views.ContactsView.as_view(), name="msg-contacts"),
    path("conversations/", views.ConversationsListView.as_view(), name="msg-conversations"),
    path("thread/<int:user_id>/", views.ThreadMessagesView.as_view(), name="msg-thread"),
    path("thread/<int:user_id>/read/", views.MarkThreadReadView.as_view(), name="msg-thread-read"),
    path("send/", views.SendMessageView.as_view(), name="msg-send"),

    # Calls (audio/video, signalled over the same per-user websocket
    # messages already use — see messaging/consumers.py + views.py).
    path("calls/start/", views.CallStartView.as_view(), name="call-start"),
    path("calls/<int:call_id>/respond/", views.CallRespondView.as_view(), name="call-respond"),
    path("calls/<int:call_id>/end/", views.CallEndView.as_view(), name="call-end"),
    path("calls/<int:call_id>/signal/", views.CallSignalView.as_view(), name="call-signal"),
    path("calls/thread/<int:user_id>/", views.CallHistoryView.as_view(), name="call-history"),
    path("calls/active/", views.ActiveIncomingCallView.as_view(), name="call-active"),

    # Web Push subscriptions (see messaging/push_utils.py) — lets a call
    # still ring a user when their tab/browser is fully closed.
    path("push/subscribe/", views.PushSubscribeView.as_view(), name="push-subscribe"),
    path("push/unsubscribe/", views.PushUnsubscribeView.as_view(), name="push-unsubscribe"),
]