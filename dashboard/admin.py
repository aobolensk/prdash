from django.contrib import admin
from .models import TrackedRepository


@admin.register(TrackedRepository)
class TrackedRepositoryAdmin(admin.ModelAdmin):
    list_display = ['full_name', 'user', 'created_at']
    list_filter = ['user', 'created_at']
    search_fields = ['owner', 'name', 'user__username']
