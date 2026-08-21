"""Record the parent of existing revisions, where it can be established safely.

`revision_of` is new, so every booking made before it has none. A revision's parent
can be identified for the rows where exactly one candidate exists: an earlier booking
in the same project, for the same phone, with a lower revision_no, sharing either the
closure or the unit.

Deliberately conservative — where more than one candidate matches, the row is left
alone rather than guessed at. The resolver still falls back to closure/unit for those,
so nothing regresses; this only upgrades the links that are unambiguous.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    Booking = apps.get_model('sales', 'Booking')
    rows = list(Booking.objects.exclude(status='rejected').values(
        'id', 'project_id', 'phone', 'plot_numbers', 'plot__number', 'area',
        'revision_no', 'closure_id', 'revision_of_id'))

    def unit_of(r):
        return (r['plot_numbers'] or r['plot__number'] or r['area'] or '').strip()

    linked = 0
    for r in rows:
        if r['revision_of_id'] or (r['revision_no'] or 0) == 0:
            continue
        candidates = [
            o for o in rows
            if o['id'] != r['id']
            and o['project_id'] == r['project_id']
            and (o['phone'] or '').strip() == (r['phone'] or '').strip()
            and (o['revision_no'] or 0) < (r['revision_no'] or 0)
            and (
                (r['closure_id'] and o['closure_id'] == r['closure_id'])
                or (unit_of(r) and unit_of(o) == unit_of(r))
            )
        ]
        if not candidates:
            continue
        # The immediate parent is the highest revision below this one; ambiguity at
        # that level means we cannot tell, so skip.
        top = max(c['revision_no'] or 0 for c in candidates)
        best = [c for c in candidates if (c['revision_no'] or 0) == top]
        if len(best) != 1:
            continue
        Booking.objects.filter(pk=r['id']).update(revision_of_id=best[0]['id'])
        linked += 1
    print(f'  linked {linked} revision(s) to their parent')


class Migration(migrations.Migration):

    dependencies = [('sales', '0066_booking_revision_of')]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
