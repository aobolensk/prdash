from django.urls import path
from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.home, name='home'),
    path(
        'plugins/<slug:plugin_id>/assets/<str:asset_name>/',
        views.plugin_asset,
        name='plugin_asset',
    ),
    path(
        'plugins/<slug:plugin_id>/<slug:route>/',
        views.plugin_route,
        name='plugin_route',
    ),
    path(
        'plugins/<slug:plugin_id>/<slug:route>/stream/',
        views.plugin_route_stream,
        name='plugin_route_stream',
    ),
    path('prs/', views.pr_list, name='pr_list'),
    path('prs/merged/', views.merged_pr_list, name='merged_pr_list'),
    path('prs/by-author/', views.author_pr_list, name='author_pr_list'),
    path('prs/by-author/merged/', views.author_merged_pr_list, name='author_merged_pr_list'),
    path('prs/review-requests/', views.review_requests_list, name='review_requests_list'),
    path('prs/review-requests/reviewed/', views.review_reviewed_list, name='review_reviewed_list'),
    path('prs/review-requests/approved/', views.review_approved_list, name='review_approved_list'),
    path('prs/assigned/', views.assigned_list, name='assigned_list'),
    # GitHub owner names cannot contain underscores, so this never shadows a repo URL.
    path('prs/_plugins/<slug:plugin_id>/<slug:name>/', views.plugin_pr_list, name='plugin_pr_list'),
    path('prs/_plugins/<slug:plugin_id>/<slug:name>/fetch/', views.plugin_pr_list_fetch,
         name='plugin_pr_list_fetch'),
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
    path('repos/<int:repo_id>/toggle/', views.toggle_repo, name='toggle_repo'),
    path('settings/', views.settings, name='settings'),
    path('settings/pat/save/', views.save_pat, name='save_pat'),
    path('settings/pat/delete/', views.delete_pat, name='delete_pat'),
    path('settings/preferences/save/', views.save_preferences, name='save_preferences'),
    path('settings/plugins/save/', views.save_plugins, name='save_plugins'),
    path('settings/plugins/<slug:plugin_id>/', views.plugin_settings, name='plugin_settings'),
]
