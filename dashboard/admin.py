from django.contrib import admin
from .models import RepositoryProfile, TrackedRepository


@admin.register(RepositoryProfile)
class RepositoryProfileAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'created_at']
    list_filter = ['user', 'created_at']
    search_fields = ['name', 'user__username']


@admin.register(TrackedRepository)
class TrackedRepositoryAdmin(admin.ModelAdmin):
    list_display = ['full_name', 'profile', 'user', 'created_at']
    list_filter = ['user', 'profile', 'created_at']
    search_fields = ['owner', 'name', 'profile__name', 'user__username']
