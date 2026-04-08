from django.urls import path
from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.home, name='home'),
    path('prs/', views.pr_list, name='pr_list'),
    path('prs/merged/', views.merged_pr_list, name='merged_pr_list'),
    path('prs/review-requests/', views.review_requests_list, name='review_requests_list'),
    path('prs/review-requests/reviewed/', views.review_reviewed_list, name='review_reviewed_list'),
    path('prs/review-requests/approved/', views.review_approved_list, name='review_approved_list'),
    path('prs/assigned/', views.assigned_list, name='assigned_list'),
    path('prs/<str:owner>/<str:repo>/', views.repo_pr_list, name='repo_pr_list'),
    path('prs/<str:owner>/<str:repo>/merged/', views.repo_merged_pr_list, name='repo_merged_pr_list'),
    path('prs/<str:owner>/<str:repo>/review-requests/',
         views.repo_review_requests_list, name='repo_review_requests_list'),
    path('prs/<str:owner>/<str:repo>/review-requests/reviewed/',
         views.repo_review_reviewed_list, name='repo_review_reviewed_list'),
    path('prs/<str:owner>/<str:repo>/review-requests/approved/',
         views.repo_review_approved_list, name='repo_review_approved_list'),
    path('prs/<str:owner>/<str:repo>/assigned/', views.repo_assigned_list, name='repo_assigned_list'),
    path('repos/add/', views.add_repo, name='add_repo'),
    path('repos/<int:repo_id>/remove/', views.remove_repo, name='remove_repo'),
    path('stats/', views.stats, name='stats'),
    path('settings/', views.settings, name='settings'),
    path('settings/pat/save/', views.save_pat, name='save_pat'),
    path('settings/pat/delete/', views.delete_pat, name='delete_pat'),
]
