from django.urls import path
from .views import VerifyCompanyView, CompanyListView, CompanyDetailView

urlpatterns = [
    path('verify/',    VerifyCompanyView.as_view(), name='company-verify'),
    path('all/',       CompanyListView.as_view(),   name='company-list'),
    path('<int:pk>/',  CompanyDetailView.as_view(), name='company-detail'),
]
