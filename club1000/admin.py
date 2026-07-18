from django.contrib import admin
from .models import Scheme, Investor, Payout, ReferralReward

admin.site.register(Scheme)
admin.site.register(Investor)
admin.site.register(Payout)
admin.site.register(ReferralReward)
