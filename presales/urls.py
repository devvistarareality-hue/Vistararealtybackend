from django.urls import path
from .views import (
    PreSalesDashboardView,
    TeamMembersView,
    ProjectListCreateView,
    ProjectDetailView,
    LeadListCreateView,
    LeadDetailView,
    LeadStatusChangeView,
    LeadTransferView,
    LeadFollowupView,
)

urlpatterns = [
    path('dashboard/',               PreSalesDashboardView.as_view(),  name='presales-dashboard'),
    path('team/',                    TeamMembersView.as_view(),         name='presales-team'),
    path('projects/',                ProjectListCreateView.as_view(),   name='project-list'),
    path('projects/<int:pk>/',       ProjectDetailView.as_view(),       name='project-detail'),
    path('leads/',                   LeadListCreateView.as_view(),      name='lead-list'),
    path('leads/<int:pk>/',          LeadDetailView.as_view(),          name='lead-detail'),
    path('leads/<int:pk>/status/',   LeadStatusChangeView.as_view(),    name='lead-status'),
    path('leads/<int:pk>/transfer/', LeadTransferView.as_view(),        name='lead-transfer'),
    path('leads/<int:pk>/followup/', LeadFollowupView.as_view(),        name='lead-followup'),
]
