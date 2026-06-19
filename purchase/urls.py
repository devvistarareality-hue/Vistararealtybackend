from django.urls import path
from .views import POListCreateView, PODetailView, po_update_status

urlpatterns = [
    path('pos/',                          POListCreateView.as_view(), name='po-list'),
    path('pos/<int:pk>/',                 PODetailView.as_view(),     name='po-detail'),
    path('pos/<int:po_id>/status/',       po_update_status,           name='po-status'),
]
