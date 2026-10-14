"""A lead follows its booking.

Whenever a booking is saved approved by both Sales and Accounts (and not cancelled),
its lead is set to Closed — status and STM status — whichever path approved it: the
Sales / CP approval, the Accounts approval, a revision, an import or a script. Before
this each path set it itself, and a booking approved some other way left its lead at
"sv done" or even "lost" while the deal was sold.

A note goes in the lead's history for each field changed. Runs after the save
commits, and never blocks or breaks the save.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_save

logger = logging.getLogger(__name__)
NOTE = 'Closed automatically: booking #%s is approved by Sales and Accounts'


def _approved(b):
    return (b.status == 'sold' and b.accounts_status == 'approved' and not b.cancelled_at
            and 'CANCEL' not in str(b.approval_status or '').upper())


def close_lead_for(booking_id):
    from .models import Booking, Lead, LeadStatusHistory
    b = Booking.objects.filter(pk=booking_id).only(
        'id', 'lead_id', 'status', 'accounts_status', 'approval_status', 'cancelled_at').first()
    if not b or not b.lead_id or not _approved(b):
        return False
    lead = Lead.objects.filter(pk=b.lead_id).only('id', 'status', 'stm_status').first()
    if not lead or (lead.status == 'closed' and lead.stm_status == 'closed'):
        return False
    for f in ('status', 'stm_status'):
        old = getattr(lead, f) or ''
        if old != 'closed':
            LeadStatusHistory.objects.create(lead_id=lead.id, field_changed=f, old_value=old,
                                             new_value='closed', remarks=NOTE % b.id)
    Lead.objects.filter(pk=lead.id).update(status='closed', stm_status='closed')
    return True


def _on_booking_saved(sender, instance, raw=False, **kw):
    if raw or not instance.lead_id or not _approved(instance):
        return
    bid = instance.pk

    def run():
        try:
            close_lead_for(bid)
        except Exception:
            logger.exception('lead_sync: could not close the lead of booking %s', bid)
    transaction.on_commit(run)


def connect():
    from .models import Booking
    post_save.connect(_on_booking_saved, sender=Booking, dispatch_uid='lead_follows_booking')
