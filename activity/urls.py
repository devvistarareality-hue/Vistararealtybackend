from django.urls import path

from .views import ActivityLogView

urlpatterns = [
    path('', ActivityLogView.as_view()),
]
