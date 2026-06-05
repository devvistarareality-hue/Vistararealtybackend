from django.contrib import admin
from .models import Lead, LeadActivity, Project


class LeadActivityInline(admin.TabularInline):
    model      = LeadActivity
    extra      = 0
    readonly_fields = ('type', 'note', 'created_by', 'created_at')


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display  = ('name', 'location', 'type', 'status', 'units', 'price_range', 'lead_count', 'created_at')
    list_filter   = ('type', 'status')
    search_fields = ('name', 'location')


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display  = ('name', 'phone', 'project', 'source', 'status', 'assigned_to', 'created_at')
    list_filter   = ('status', 'source', 'project')
    search_fields = ('name', 'phone', 'email')
    inlines       = [LeadActivityInline]


@admin.register(LeadActivity)
class LeadActivityAdmin(admin.ModelAdmin):
    list_display  = ('lead', 'type', 'created_by', 'created_at')
    list_filter   = ('type',)
