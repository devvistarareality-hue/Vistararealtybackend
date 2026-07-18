from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from django.conf import settings
from django.conf.urls.static import static

def health(request):
    return JsonResponse({'status': 'ok'})

urlpatterns = [
    path('health/', health),
    path('admin/', admin.site.urls),
    path('api/company/', include('companies.urls')),
    path('api/auth/', include('accounts.urls')),
    path('api/attendance/', include('attendance.urls')),
    path('api/sales/', include('sales.urls')),
    path('api/club1000/', include('club1000.urls')),
]

# Serve uploaded media (signed LOIs) in development.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
