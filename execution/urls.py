from django.urls import path
from .views import (
    PRListCreateView, PRDetailView, pr_transition, pr_approve,
    MaterialIssueListCreateView, MaterialIssueDetailView,
    MeasurementBookListCreateView, MeasurementBookDetailView, mb_submit, mb_certify,
    RABillListCreateView, RABillDetailView,
)

urlpatterns = [
    # Purchase Requisitions
    path('prs/',                         PRListCreateView.as_view(),              name='pr-list'),
    path('prs/<int:pk>/',                PRDetailView.as_view(),                  name='pr-detail'),
    path('prs/<int:pr_id>/transition/',  pr_transition,                           name='pr-transition'),
    path('prs/<int:pr_id>/approve/',     pr_approve,                              name='pr-approve'),

    # Material Issues
    path('issues/',                      MaterialIssueListCreateView.as_view(),   name='issue-list'),
    path('issues/<int:pk>/',             MaterialIssueDetailView.as_view(),       name='issue-detail'),

    # Measurement Books
    path('mbs/',                         MeasurementBookListCreateView.as_view(), name='mb-list'),
    path('mbs/<int:pk>/',                MeasurementBookDetailView.as_view(),     name='mb-detail'),
    path('mbs/<int:mb_id>/submit/',      mb_submit,                               name='mb-submit'),
    path('mbs/<int:mb_id>/certify/',     mb_certify,                              name='mb-certify'),

    # RA Bills
    path('ra-bills/',                    RABillListCreateView.as_view(),          name='ra-bill-list'),
    path('ra-bills/<int:pk>/',           RABillDetailView.as_view(),              name='ra-bill-detail'),
]
