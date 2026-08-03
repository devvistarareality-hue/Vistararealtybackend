from django.contrib import admin
from .models import Scheme, Investor, Payout, ReferralReward, Lead, FollowUp, LeadStatusHistory

admin.site.register(Scheme)
admin.site.register(Investor)
admin.site.register(Payout)
admin.site.register(ReferralReward)
admin.site.register(Lead)
admin.site.register(FollowUp)
admin.site.register(LeadStatusHistory)
