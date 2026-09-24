"""
ASGI config for hopenix project.

Routes plain HTTP to Django as usual, and `/ws/...` paths to Channels
consumers for websocket connections (real-time chat push).

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'hopenix.settings')

# get_asgi_application() must be called BEFORE importing anything that
# touches Django models (routing/consumers import serializers/models),
# otherwise you hit the classic "Apps aren't loaded yet" error.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from messaging.routing import websocket_urlpatterns  # noqa: E402
from messaging.token_auth import TokenAuthMiddleware  # noqa: E402

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": TokenAuthMiddleware(
        URLRouter(websocket_urlpatterns)
    ),
})