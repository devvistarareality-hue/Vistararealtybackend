from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse

def health(request):
    return JsonResponse({'status': 'ok'})

urlpatterns = [
    path('health/', health),
    path('admin/', admin.site.urls),
    path('api/company/', include('companies.urls')),
    path('api/auth/', include('accounts.urls')),
    path('api/attendance/', include('attendance.urls')),
    path('api/sales/', include('sales.urls')),
    # ERP
    path('api/erp/master/',     include('erp_master.urls')),
    path('api/erp/execution/',  include('execution.urls')),
    path('api/erp/purchase/',   include('purchase.urls')),
    path('api/erp/inventory/',  include('inventory.urls')),
    path('api/erp/finance/',    include('finance.urls')),
]
