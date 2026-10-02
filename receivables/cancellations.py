"""Cancelling a deal the client stopped paying on — raised in AR, approved by the
project's booking/Accounts approver (or an admin), settled through Bank Master.

Settlement: we keep FORFEIT_PCT of (Total Deal − Stamp Duty − Registration),
capped at what the client actually paid; the rest of what they paid is refunded.
A client who paid less than that keeps nothing back and owes nothing more.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import is_platform_admin, scope_to_company
from .engine import rupees
from .models import ARBank, ARCancellation, ARRefund
from .permissions import ar_can, has_ar_access
from .services import _d, compute_account, parse_date

ZERO = Decimal('0')
FORFEIT_PCT = Decimal('10')


def _deny(msg='You do not have access to Accounts Receivable.'):
    return Response({'detail': msg}, status=status.HTTP_403_FORBIDDEN)


def _views():
    from . import views
    return views


def settlement(acct):
    """What a cancellation of this account settles to, today."""
    receipts = sorted(acct.receipts.filter(is_deleted=False), key=lambda x: (x.paid_on, x.id))
    _plan, r, _mm = compute_account(acct, timezone.localdate(), receipts)
    deal_net = _d(r.collectable)
    received = _d(r.received)
    forfeit = min((deal_net * FORFEIT_PCT / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP), received)
    return {'deal_net': deal_net, 'received': received, 'forfeit': forfeit,
            'refund_due': max(ZERO, received - forfeit), 'receipts': receipts, 'r': r}


def _unit(b):
    return b.plot_numbers or (b.plot.number if b.plot_id else '')


def _log(request, c, summary, action):
    try:
        from activity.recorder import note
        b = c.booking
        note(request, '%s — %s · %s Plot %s' % (summary, b.client_name or '—', b.project.name if b.project_id else '', _unit(b)),
             action=action, target_type='ar_account', target_id=c.account_id, module='AR')
    except Exception:
        pass


def can_decide(user, c):
    """The project's booking or Accounts approver, or an admin — the same people
    who may cancel the sale from Sales (sales.views.can_cancel_closure)."""
    if user.is_staff or is_platform_admin(user) or getattr(user, 'role', '') == 'Admin':
        return True
    closure = c.booking.closure
    if not closure:
        return False
    from sales.views import can_cancel_closure
    return can_cancel_closure(user, closure, c.company)


def _refunded(c):
    return sum((_d(x.amount) for x in c.refunds.all() if not x.is_deleted), ZERO)


def cancellation_row(c, user):
    refunds = sorted((x for x in c.refunds.all() if not x.is_deleted), key=lambda x: (x.paid_on, x.id))
    refunded = sum((_d(x.amount) for x in refunds), ZERO)
    due = _d(c.refund_due)
    b = c.booking
    if c.status == 'approved':
        stage = 'refunded' if refunded >= due else ('refund_pending' if due > 0 else 'closed')
    else:
        stage = c.status
    return {
        'id': c.id, 'account_id': c.account_id, 'status': c.status, 'stage': stage,
        'client': b.client_name or '', 'phone': b.phone or '', 'project': b.project.name if b.project_id else '',
        'plots': _unit(b), 'reason': c.reason or '',
        'deal_net': rupees(_d(c.deal_net)), 'forfeit_pct': float(c.forfeit_pct), 'forfeit': rupees(_d(c.forfeit)),
        'received': rupees(_d(c.received)), 'refund_due': rupees(due),
        'refunded': rupees(refunded), 'refund_balance': rupees(max(ZERO, due - refunded)),
        'requested_by': c.requested_by.name if c.requested_by_id else '', 'requested_at': c.requested_at.isoformat(),
        'decided_by': c.decided_by.name if c.decided_by_id else '',
        'decided_at': c.decided_at.isoformat() if c.decided_at else None, 'decision_note': c.decision_note or '',
        'can_decide': c.status == 'pending' and can_decide(user, c),
        'refunds': [{'id': x.id, 'paid_on': x.paid_on.isoformat(), 'amount': rupees(_d(x.amount)),
                     'bank': x.bank_id, 'bank_name': x.bank.name, 'reference': x.reference or '', 'remarks': x.remarks or '',
                     'recorded_by': x.created_by.name if x.created_by_id else ''} for x in refunds],
    }


def _cancellations_qs(request):
    qs = scope_to_company(ARCancellation.objects.all(), request.user)
    cid = request.query_params.get('company_id')
    if cid and is_platform_admin(request.user):
        qs = qs.filter(company_id=cid)
    return qs.select_related('booking__project', 'booking__plot', 'booking__closure', 'requested_by', 'decided_by', 'company') \
             .prefetch_related('refunds__bank', 'refunds__created_by')


class ARAccountCancellationView(APIView):
    """GET: what cancelling this account would settle to (and any cancellation
    already raised). POST {reason}: raise one for approval."""
    permission_classes = [IsAuthenticated]

    def _acct(self, request, pk):
        return _views()._accounts_qs(request).select_related('booking__project', 'booking__plot', 'company').filter(pk=pk).first()

    def get(self, request, pk):
        if not has_ar_access(request.user):
            return _deny()
        acct = self._acct(request, pk)
        if not acct:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        st = settlement(acct)
        active = acct.cancellations.filter(status__in=('pending', 'approved')).first()
        return Response({
            'deal_net': rupees(st['deal_net']), 'forfeit_pct': float(FORFEIT_PCT), 'forfeit': rupees(st['forfeit']),
            'received': rupees(st['received']), 'refund_due': rupees(st['refund_due']),
            'active': cancellation_row(active, request.user) if active else None,
            'can_request': ar_can(request.user, 'ar.cancel.request') and acct.status != 'frozen' and not active,
        })

    def post(self, request, pk):
        if not ar_can(request.user, 'ar.cancel.request'):
            return _deny('You cannot raise cancellations.')
        acct = self._acct(request, pk)
        if not acct:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if acct.status == 'frozen':
            return Response({'detail': 'This booking is already cancelled.'}, status=status.HTTP_400_BAD_REQUEST)
        if acct.cancellations.filter(status__in=('pending', 'approved')).exists():
            return Response({'detail': 'A cancellation is already raised for this plot.'}, status=status.HTTP_400_BAD_REQUEST)
        reason = (request.data.get('reason') or '').strip()[:1000]
        if not reason:
            return Response({'reason': 'Say why the plot is being cancelled.'}, status=status.HTTP_400_BAD_REQUEST)
        st = settlement(acct)
        c = ARCancellation.objects.create(
            company=acct.company, account=acct, booking=acct.booking, reason=reason, requested_by=request.user,
            deal_net=st['deal_net'], forfeit_pct=FORFEIT_PCT, forfeit=st['forfeit'],
            received=st['received'], refund_due=st['refund_due'])
        _log(request, c, 'Raised cancellation (refund ₹{:,})'.format(rupees(st['refund_due'])), 'created')
        c = _cancellations_qs(request).get(pk=c.pk)
        return Response(cancellation_row(c, request.user), status=status.HTTP_201_CREATED)


class ARCancellationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_ar_access(request.user):
            return _deny()
        rows = [cancellation_row(c, request.user) for c in _cancellations_qs(request).order_by('-requested_at')]
        counts = {}
        for r in rows:
            counts[r['stage']] = counts.get(r['stage'], 0) + 1
        return Response({'results': rows, 'counts': counts,
                         'can_refund': ar_can(request.user, 'ar.refund.record')})


class ARCancellationDecideView(APIView):
    """POST {action: approve|reject, note}. Approving cancels the sale (plot back to
    Sales at once), freezes the account and re-reads the settlement, since a
    payment may have come in while it waited."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not has_ar_access(request.user):
            return _deny()
        c = _cancellations_qs(request).filter(pk=pk).first()
        if not c:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if c.status != 'pending':
            return Response({'detail': 'This cancellation has already been decided.'}, status=status.HTTP_400_BAD_REQUEST)
        if not can_decide(request.user, c):
            return _deny('Only this project\'s booking or Accounts approver can decide a cancellation.')
        action = request.data.get('action')
        note = (request.data.get('note') or '').strip()[:1000]
        if action == 'reject':
            c.status, c.decided_by, c.decided_at, c.decision_note = 'rejected', request.user, timezone.now(), note
            c.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_note'])
            _log(request, c, 'Rejected cancellation', 'reject')
        elif action == 'approve':
            closure = c.booking.closure
            if not closure:
                return Response({'detail': 'This booking has no closure to cancel — cancel it from Sales.'},
                                status=status.HTTP_400_BAD_REQUEST)
            from sales.views import cancel_closure
            with transaction.atomic():
                acct = c.account
                st = settlement(acct)
                cancel_closure(request, closure, c.company)
                acct.status, acct.frozen_at = 'frozen', timezone.now()
                acct.save(update_fields=['status', 'frozen_at', 'updated_at'])
                c.deal_net, c.forfeit, c.received, c.refund_due = st['deal_net'], st['forfeit'], st['received'], st['refund_due']
                c.status, c.decided_by, c.decided_at, c.decision_note = 'approved', request.user, timezone.now(), note
                c.save()
            _log(request, c, 'Approved cancellation — plot released to Sales, refund ₹{:,}'.format(rupees(st['refund_due'])), 'approve')
        else:
            return Response({'detail': 'Action must be approve or reject.'}, status=status.HTTP_400_BAD_REQUEST)
        c = _cancellations_qs(request).get(pk=c.pk)
        return Response(cancellation_row(c, request.user))


class ARRefundCreateView(APIView):
    """POST {paid_on, amount, bank, reference, remarks}: money actually paid back."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not ar_can(request.user, 'ar.refund.record'):
            return _deny('You cannot record refunds.')
        c = _cancellations_qs(request).filter(pk=pk).first()
        if not c:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if c.status != 'approved':
            return Response({'detail': 'A refund can only be paid on an approved cancellation.'}, status=status.HTTP_400_BAD_REQUEST)
        errs = {}
        d = parse_date(request.data.get('paid_on'))
        if not d:
            errs['paid_on'] = 'A valid date is required.'
        elif d > timezone.localdate():
            errs['paid_on'] = 'Date cannot be in the future.'
        amount = _d(request.data.get('amount'))
        left = _d(c.refund_due) - _refunded(c)
        if amount <= 0:
            errs['amount'] = 'Amount must be greater than zero.'
        elif amount > left:
            errs['amount'] = 'Only ₹{:,} is left to refund.'.format(rupees(left))
        bank = ARBank.objects.filter(pk=request.data.get('bank') or None, company_id=c.company_id, is_active=True).first()
        if not bank:
            errs['bank'] = 'Pick the bank the refund is paid from.'
        if errs:
            return Response(errs, status=status.HTTP_400_BAD_REQUEST)
        x = ARRefund.objects.create(cancellation=c, bank=bank, paid_on=d, amount=amount, created_by=request.user,
                                    reference=(request.data.get('reference') or '').strip()[:80],
                                    remarks=(request.data.get('remarks') or '').strip()[:500])
        _log(request, c, 'Paid refund ₹{:,} from {}'.format(rupees(amount), bank.name), 'created')
        c = _cancellations_qs(request).get(pk=c.pk)
        return Response(cancellation_row(c, request.user), status=status.HTTP_201_CREATED)


class ARRefundView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, rid):
        if not ar_can(request.user, 'ar.refund.record'):
            return _deny('You cannot record refunds.')
        x = ARRefund.objects.filter(pk=rid, is_deleted=False, cancellation__in=_cancellations_qs(request)) \
            .select_related('cancellation__booking__project', 'cancellation__booking__plot', 'bank').first()
        if not x:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        x.is_deleted = True
        x.save(update_fields=['is_deleted'])
        _log(request, x.cancellation, 'Deleted refund ₹{:,} from {}'.format(rupees(_d(x.amount)), x.bank.name), 'deleted')
        return Response(status=status.HTTP_204_NO_CONTENT)


class ARCancellationLetterView(APIView):
    """The cancellation letter with the ledger statement, as a print-ready HTML page
    (the web prints it to PDF, the app turns it into one) — like the statement."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not has_ar_access(request.user):
            return _deny()
        c = _cancellations_qs(request).filter(pk=pk).first()
        if not c:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if c.status != 'approved':
            return Response({'detail': 'The letter is issued once the cancellation is approved.'}, status=status.HTTP_400_BAD_REQUEST)
        views = _views()
        acct = c.account
        b = c.booking
        receipts = sorted(acct.receipts.filter(is_deleted=False), key=lambda x: (x.paid_on, x.id))
        as_of = timezone.localtime(c.decided_at).date()
        _plan, r, _mm = compute_account(acct, as_of, receipts)
        fmt = lambda d: d.strftime('%d/%m/%Y') if d else '—'
        row = cancellation_row(c, request.user)
        followups = acct.followups.exclude(status='cancelled').count()
        html = render_to_string('receivables/cancellation_letter.html', {
            'company': c.company, 'c': row, 'b': b, 'letter_no': 'CAN/%s/%04d' % (as_of.strftime('%Y'), c.id),
            'date': fmt(as_of), 'booked': fmt(b.booking_date), 'followups': followups,
            'total_deal': rupees(_d(b.final_amount)), 'stamp': rupees(_d(b.stamp_duty)), 'reg': rupees(_d(b.reg_fees)),
            'plan': [{'no': x.line.no, 'label': x.line.label, 'due': fmt(x.line.due), 'amount': rupees(x.line.amount),
                      'paid': rupees(x.paid), 'pending': rupees(x.remaining)} for x in r.lines],
            'receipts': [{'date': fmt(x.paid_on), 'amount': rupees(_d(x.amount)), 'mode': views.MODES.get(x.mode, x.mode)}
                         for x in receipts],
            'refunds': [{'date': fmt(parse_date(x['paid_on'])), 'amount': x['amount'], 'reference': x['reference'], 'bank': x['bank_name']}
                        for x in row['refunds']],
            'generated': timezone.localtime().strftime('%d/%m/%Y %I:%M %p'),
        })
        return HttpResponse(html, content_type='text/html; charset=utf-8')
