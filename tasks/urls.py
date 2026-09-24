from django.urls import path

from . import views

urlpatterns = [
    path('lists/', views.TaskListsView.as_view()),
    path('lists/<int:pk>/', views.TaskListDetailView.as_view()),
    path('tags/', views.TaskTagsView.as_view()),
    path('assignees/', views.TaskAssigneesView.as_view()),
    path('stats/', views.TaskStatsView.as_view()),
    path('checklist/<int:iid>/', views.TaskChecklistItemView.as_view()),
    path('<int:pk>/comments/', views.TaskCommentsView.as_view()),
    path('<int:pk>/checklist/', views.TaskChecklistView.as_view()),
    path('<int:pk>/', views.TaskDetailView.as_view()),
    path('', views.TasksView.as_view()),
]
