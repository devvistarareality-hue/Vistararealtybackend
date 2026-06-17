from django.urls import path
from . import views

urlpatterns = [
    path('stats/',                     views.StatsView.as_view()),
    path('leads/',                     views.LeadListView.as_view()),
    path('leads/bulk-delete/',         views.BulkDeleteLeadsView.as_view()),
    path('leads/import/',              views.BulkImportLeadsView.as_view()),
    path('leads/<int:pk>/',            views.LeadDetailView.as_view()),
    path('projects/',                  views.ProjectListView.as_view()),
    path('projects/<int:pk>/',         views.ProjectDetailView.as_view()),
    path('sources/',                   views.LeadSourceListView.as_view()),
    path('follow-ups/',                views.FollowUpListView.as_view()),
    path('follow-ups/<int:pk>/',       views.FollowUpDetailView.as_view()),
    path('site-visits/',               views.SiteVisitListView.as_view()),
    path('site-visits/<int:pk>/',      views.SiteVisitDetailView.as_view()),
    path('closures/',                  views.ClosureListView.as_view()),
    path('users/telecallers/',         views.TelecallerListView.as_view()),
    path('team/',                      views.SalesTeamView.as_view()),
    path('team/<int:pk>/',             views.SalesTeamMemberDetailView.as_view()),
    path('distribute/',                views.DistributeView.as_view()),
    path('distribution-log/',          views.DistributionLogView.as_view()),
    path('reports/',                   views.ReportsView.as_view()),
]
