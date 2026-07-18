from django.urls import path
from . import views

urlpatterns = [
    path('stats/',                    views.StatsView.as_view()),
    path('schemes/',                  views.SchemeListCreateView.as_view()),
    path('schemes/<int:pk>/',         views.SchemeDetailView.as_view()),
    path('investors/references/',     views.ReferenceSuggestionsView.as_view()),
    path('investors/',                views.InvestorListCreateView.as_view()),
    path('investors/<int:pk>/',       views.InvestorDetailView.as_view()),
    path('investors/<int:pk>/redeem/', views.InvestorRedeemView.as_view()),
    path('payouts/',                  views.PayoutListView.as_view()),
    path('payouts/<int:pk>/mark-paid/', views.PayoutMarkPaidView.as_view()),
    path('referral-rewards/',                  views.ReferralRewardListView.as_view()),
    path('referral-rewards/<int:pk>/mark-paid/', views.ReferralRewardMarkPaidView.as_view()),
]
