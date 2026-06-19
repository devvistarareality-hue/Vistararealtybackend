from django.urls import path
from .views import (
    GRNListCreateView, GRNDetailView, grn_qc_update,
    StockLedgerListView, stock_balance,
)

urlpatterns = [
    path('grns/',                           GRNListCreateView.as_view(),  name='grn-list'),
    path('grns/<int:pk>/',                  GRNDetailView.as_view(),      name='grn-detail'),
    path('grns/<int:grn_id>/qc/',           grn_qc_update,                name='grn-qc'),
    path('stock-ledger/',                   StockLedgerListView.as_view(),name='stock-ledger'),
    path('stock-balance/<int:project_id>/', stock_balance,                name='stock-balance'),
]
