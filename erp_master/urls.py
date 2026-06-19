from django.urls import path
from .views import (
    VendorListCreateView, VendorDetailView,
    GLAccountListCreateView, GLAccountDetailView,
    MaterialListCreateView, MaterialDetailView,
    ERPProjectListCreateView, ERPProjectDetailView,
    WBSActivityListCreateView, WBSActivityDetailView,
    WBSTreeView, DocumentTrailListView,
)

urlpatterns = [
    path('vendors/',                   VendorListCreateView.as_view(),    name='vendor-list'),
    path('vendors/<int:pk>/',          VendorDetailView.as_view(),        name='vendor-detail'),
    path('gl-accounts/',               GLAccountListCreateView.as_view(), name='gl-account-list'),
    path('gl-accounts/<int:pk>/',      GLAccountDetailView.as_view(),     name='gl-account-detail'),
    path('materials/',                 MaterialListCreateView.as_view(),  name='material-list'),
    path('materials/<int:pk>/',        MaterialDetailView.as_view(),      name='material-detail'),
    path('erp-projects/',              ERPProjectListCreateView.as_view(),name='erp-project-list'),
    path('erp-projects/<int:pk>/',     ERPProjectDetailView.as_view(),    name='erp-project-detail'),
    path('wbs/',                       WBSActivityListCreateView.as_view(),name='wbs-list'),
    path('wbs/<int:pk>/',              WBSActivityDetailView.as_view(),   name='wbs-detail'),
    path('wbs/tree/<int:project_id>/', WBSTreeView.as_view(),             name='wbs-tree'),
    path('document-trail/',            DocumentTrailListView.as_view(),   name='document-trail'),
]
