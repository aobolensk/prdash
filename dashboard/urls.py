from django.urls import path
from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.home, name='home'),
    path('prs/', views.pr_list, name='pr_list'),
    path('prs/merged/', views.merged_pr_list, name='merged_pr_list'),
    path('prs/<str:owner>/<str:repo>/', views.repo_pr_list, name='repo_pr_list'),
    path('prs/<str:owner>/<str:repo>/merged/', views.repo_merged_pr_list, name='repo_merged_pr_list'),
    path('repos/add/', views.add_repo, name='add_repo'),
    path('repos/<int:repo_id>/remove/', views.remove_repo, name='remove_repo'),
]
