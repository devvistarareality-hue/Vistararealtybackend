"""Rebuild the closures that a past Data Reset destroyed.

An approved booking always mirrors itself into a Closure (see BookingApprovalView).
When Leads were cleared, the CASCADE on the old Closure.lead deleted those mirrors and
the booking's `closure` FK went NULL — leaving approved/sold bookings with no
conversion record at all. Re-create one closure per such booking from the booking's own
(fully self-contained) data. Idempotent: only touches sold bookings whose closure is
missing.
"""
from django.db import migrations


def rebuild(apps, schema_editor):
    Booking = apps.get_model('sales', 'Booking')
    Closure = apps.get_model('sales', 'Closure')

    qs = Booking.objects.filter(status='sold', closure__isnull=True).select_related('plot')
    for b in qs.iterator():
        unit = b.plot_numbers or (b.plot.number if b.plot_id else '') or b.area or ''
        c = Closure.objects.create(
            company_id=b.company_id,
            lead_id=b.lead_id,
            project_id=b.project_id,
            stm_id=b.stm_id,
            client_name=b.client_name or '',
            client_phone=b.phone or '',
            status='booked',
            closure_date=b.booking_date or b.created_at.date(),
            unit_no=unit,
            unit_type=b.villa_type or b.bunglow_type or '',
            booking_amount=b.plot_basic or None,
            total_amount=b.final_amount or None,
            remarks='[Restored from booking #%d]' % b.pk,
        )
        Booking.objects.filter(pk=b.pk).update(closure=c)


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0046_closure_company_survives_lead_delete'),
    ]

    operations = [
        migrations.RunPython(rebuild, migrations.RunPython.noop),
    ]
