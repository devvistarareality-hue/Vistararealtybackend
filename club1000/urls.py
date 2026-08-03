from django.urls import path
from . import views

urlpatterns = [
    path('stats/',                    views.StatsView.as_view()),
    path('users/',                    views.Club1000UsersView.as_view()),
    path('schemes/',                  views.SchemeListCreateView.as_view()),
    path('schemes/<int:pk>/',         views.SchemeDetailView.as_view()),
    path('investors/references/',     views.ReferenceSuggestionsView.as_view()),
    path('investors/next-loi-no/',    views.InvestorNextLoiNoView.as_view()),
    path('investors/',                views.InvestorListCreateView.as_view()),
    path('investors/<int:pk>/',       views.InvestorDetailView.as_view()),
    path('investors/<int:pk>/redeem/', views.InvestorRedeemView.as_view()),
    path('investors/<int:pk>/action/', views.InvestorActionView.as_view()),
    path('investors/<int:pk>/revise/', views.InvestorReviseView.as_view()),
    path('investors/<int:pk>/renew/', views.InvestorRenewView.as_view()),
    path('investors/<int:pk>/mature-payout/', views.InvestorMaturityPayoutView.as_view()),
    path('investors/<int:pk>/loi-no/', views.InvestorLoiNoView.as_view()),
    path('investors/<int:pk>/upload-loi/', views.InvestorUploadLoiView.as_view()),
    path('investors/<int:pk>/loi-url/', views.InvestorLoiUrlView.as_view()),
    path('payouts/',                  views.PayoutListView.as_view()),
    path('payouts/<int:pk>/mark-paid/', views.PayoutMarkPaidView.as_view()),
    path('referral-rewards/',                  views.ReferralRewardListView.as_view()),
    path('referral-rewards/<int:pk>/mark-paid/', views.ReferralRewardMarkPaidView.as_view()),
    path('leads/',                    views.LeadListCreateView.as_view()),
    path('leads/<int:pk>/',           views.LeadDetailView.as_view()),
    path('follow-ups/',               views.FollowUpListCreateView.as_view()),
    path('follow-ups/<int:pk>/',      views.FollowUpDetailView.as_view()),
]
