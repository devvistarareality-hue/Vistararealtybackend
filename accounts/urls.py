from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from .views import (
    LoginView, MeView,
    UserListCreateView, UserDetailView,
    DesignationListCreateView, DesignationDetailView,
    PushTokenView,
)

urlpatterns = [
    path('login/',                    LoginView.as_view(),                name='login'),
    path('token/refresh/',            TokenRefreshView.as_view(),         name='token-refresh'),
    path('me/',                       MeView.as_view(),                   name='me'),
    path('users/',                    UserListCreateView.as_view(),        name='user-list'),
    path('users/<int:pk>/',           UserDetailView.as_view(),            name='user-detail'),
    path('designations/',             DesignationListCreateView.as_view(), name='designation-list'),
    path('designations/<int:pk>/',    DesignationDetailView.as_view(),     name='designation-detail'),
    path('notifications/token/',      PushTokenView.as_view(),             name='push-token'),
]
