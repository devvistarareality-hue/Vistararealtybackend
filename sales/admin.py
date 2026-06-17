from django.contrib import admin
from .models import LeadSource, Project, Lead, FollowUp, SiteVisit, Closure, LeadStatusHistory

admin.site.register(LeadSource)
admin.site.register(Project)
admin.site.register(Lead)
admin.site.register(FollowUp)
admin.site.register(SiteVisit)
admin.site.register(Closure)
admin.site.register(LeadStatusHistory)
