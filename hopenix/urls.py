from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('users.urls')),
    path('api/settings/', include('settings.urls')),
    path('api/dashboard/', include('dashboard.urls')),
    path('api/projects/', include('projects.urls')),
    path('api/tasks/', include('tasks.urls')),
    path('api/messages/', include('messaging.urls')),
    path('api/sales/', include('sales.urls')),
    path('api/expenses/', include('expenses.urls')), 
    path('api/meetings/', include('meetings.urls')),
   path('api/employees/', include('employees.urls')),
    path('api/reports/', include('reports.urls')),
   path('api/visitors/', include('visitors.urls')),
       path('api/coworking/', include('coworking.urls')),
]

# Serve uploaded files (CVs, etc.) in development. In production this
# should be handled by the web server / a proper storage backend instead.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)