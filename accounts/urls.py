from django.urls import path
from .views import (
    LoginView, MeView,
    UserListCreateView, UserDetailView,
    DesignationListCreateView, DesignationDetailView,
    NotificationTestView, NotificationListView, NotificationReadView,
    SessionTokenRefreshView,
)

urlpatterns = [
    path('login/',                    LoginView.as_view(),                name='login'),
    path('token/refresh/',            SessionTokenRefreshView.as_view(),  name='token-refresh'),
    path('me/',                       MeView.as_view(),                   name='me'),
    path('users/',                    UserListCreateView.as_view(),        name='user-list'),
    path('users/<int:pk>/',           UserDetailView.as_view(),            name='user-detail'),
    path('designations/',             DesignationListCreateView.as_view(), name='designation-list'),
    path('designations/<int:pk>/',    DesignationDetailView.as_view(),     name='designation-detail'),
    path('notifications/test/',       NotificationTestView.as_view(),      name='notification-test'),
    path('notifications/',            NotificationListView.as_view(),      name='notification-list'),
    path('notifications/read/',       NotificationReadView.as_view(),      name='notification-read-all'),
    path('notifications/<int:pk>/read/', NotificationReadView.as_view(),   name='notification-read'),
]
