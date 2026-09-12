"""Who may cancel a unit's soft hold.

A plot sits at status='hold' for two quite different reasons: somebody has it
selected (or drafted) on the unit map, which is what `held_by` records, or a
booking has been submitted against it and is waiting on a manager, at which point
submission clears `held_by`. Only the first is cancellable here — undoing the
second means rejecting the booking, which is the approvals screen's job and
carries its own trail.

Two people may cancel a selection: whoever made it, and whoever is trusted to
approve that project's bookings. The second is the point of this module — before
it, a unit someone selected and walked away from could only be freed by that
person or by waiting out the expiry, so a manager watching a live plot map had no
way to clear it.

Approver authority is not re-derived here. A CP-sourced deal answers to the
project's CP approver list and everything else to its regular one, and
_can_approve_booking already makes exactly that choice for approve, reject and
cancel — so this defers to it rather than keeping a second copy of the rule that
could drift.
"""

from .models import Booking, Plot


def _holding_booking(plot):
    """The draft or submitted booking behind this unit's hold, if there is one.

    A bare map selection has none. Checked so the approver list is chosen from the
    deal's own routing (its lead and Source) rather than assumed.
    """
    qs = (Booking.objects
          .filter(project_id=plot.project_id, status__in=('draft', 'pending'))
          .only('id', 'plot_id', 'plot_ids', 'lead_id', 'source'))
    direct = qs.filter(plot_id=plot.id).first()
    if direct is not None:
        return direct
    # Multi-unit bookings keep their units in plot_ids, which cannot be queried
    # against a JSON list portably, so scan the project's few open bookings.
    return next((b for b in qs if plot.id in (b.plot_ids or [])), None)


def can_cancel_plot_hold(user, plot):
    """True if `user` may release `plot`'s soft hold."""
    # Imported here, not at module scope: views imports this module, so a top-level
    # import back into views would close the loop.
    from .views import _can_approve_booking, _is_hard_admin

    if plot.status != Plot.HOLD or not plot.held_by_id:
        return False            # free, sold, or already submitted for approval
    if plot.held_by_id == user.id:
        return True             # your own selection is always yours to drop
    if _is_hard_admin(user):
        return True
    company = getattr(user, 'company', None)
    if company is None:
        return False
    b = _holding_booking(plot)
    return _can_approve_booking(
        user, plot.project_id, plot.project_id, getattr(b, 'lead_id', None),
        company, getattr(b, 'source', None))
