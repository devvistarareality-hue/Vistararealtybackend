"""Bank Master: the company's own bank accounts that Loan payments are received into.

A bank's balance is its opening balance plus every live (not deleted) Loan receipt
recorded against it. Amounts are encrypted at rest, so the sum is taken in Python
rather than in SQL — the same as everywhere else in AR.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import is_platform_admin, scope_to_company
from companies.models import Company
from .engine import rupees
from .models import ARBank, ARReceipt
from .permissions import ar_can, has_ar_access
from .services import _d, parse_date

ZERO = Decimal('0')


def _deny(msg='You do not have access to Accounts Receivable.'):
    return Response({'detail': msg}, status=status.HTTP_403_FORBIDDEN)


def banks_qs(request):
    """The banks this person may see: their own company's, or — for a platform
    admin viewing a company — that company's."""
    qs = scope_to_company(ARBank.objects.all(), request.user)
    cid = request.query_params.get('company_id')
    if cid and is_platform_admin(request.user):
        qs = qs.filter(company_id=cid)
    return qs


def _company_for_create(request):
    company = getattr(request.user, 'company', None)
    cid = request.query_params.get('company_id') or request.data.get('company_id')
    if cid and is_platform_admin(request.user):
        company = Company.objects.filter(pk=cid).first()
    return company


def _money(raw, field, errs):
    if raw in (None, ''):
        return ZERO
    try:
        v = Decimal(str(raw).replace(',', '').strip())
    except (InvalidOperation, ValueError):
        errs[field] = 'Enter an amount in rupees.'
        return None
    if v < 0:
        errs[field] = 'Cannot be negative.'
        return None
    return v


def _received_by_bank(bank_ids):
    """Sum of live receipts per bank. Encrypted amounts, so summed here."""
    totals = {b: ZERO for b in bank_ids}
    for bid, amount in ARReceipt.objects.filter(bank_id__in=bank_ids, is_deleted=False).values_list('bank_id', 'amount'):
        totals[bid] = totals.get(bid, ZERO) + _d(amount)
    return totals


def bank_row(b, received):
    opening = _d(b.opening_balance)
    return {
        'id': b.id, 'name': b.name, 'account_no': b.account_no or '', 'is_active': b.is_active,
        'opening_balance': rupees(opening), 'received': rupees(received),
        'balance': rupees(opening + received),
    }


def _clean(data, partial=False):
    out, errs = {}, {}
    if 'name' in data or not partial:
        name = (data.get('name') or '').strip()[:120]
        if not name:
            errs['name'] = 'Bank name is required.'
        else:
            out['name'] = name
    if 'account_no' in data:
        out['account_no'] = (data.get('account_no') or '').strip()[:40]
    if 'opening_balance' in data or not partial:
        v = _money(data.get('opening_balance'), 'opening_balance', errs)
        if v is not None:
            out['opening_balance'] = v
    if 'is_active' in data:
        out['is_active'] = bool(data.get('is_active'))
    return out, errs


def _name_taken(company_id, name, exclude_id=None):
    # Names are encrypted, so the uniqueness check is done in Python.
    want = name.strip().lower()
    for bid, n in ARBank.objects.filter(company_id=company_id).values_list('id', 'name'):
        if bid != exclude_id and (n or '').strip().lower() == want:
            return True
    return False


def _log(request, summary, action, bank_id):
    try:
        from activity.recorder import note
        note(request, summary, action=action, target_type='ar_bank', target_id=bank_id, module='AR')
    except Exception:
        pass


class ARBankListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_ar_access(request.user):
            return _deny()
        banks = list(banks_qs(request))
        totals = _received_by_bank([b.id for b in banks])
        rows = sorted((bank_row(b, totals.get(b.id, ZERO)) for b in banks),
                      key=lambda r: (not r['is_active'], r['name'].lower()))
        return Response({'results': rows, 'can_manage': ar_can(request.user, 'ar.bank.manage')})

    def post(self, request):
        if not ar_can(request.user, 'ar.bank.manage'):
            return _deny('You cannot manage banks.')
        company = _company_for_create(request)
        if not company:
            return Response({'detail': 'Pick a company first.'}, status=status.HTTP_400_BAD_REQUEST)
        vals, errs = _clean(request.data)
        if not errs and _name_taken(company.id, vals['name']):
            errs['name'] = 'A bank with this name already exists.'
        if errs:
            return Response(errs, status=status.HTTP_400_BAD_REQUEST)
        b = ARBank.objects.create(company=company, created_by=request.user, **vals)
        _log(request, 'Added bank %s, opening balance ₹%s' % (b.name, '{:,}'.format(rupees(_d(b.opening_balance)))), 'created', b.id)
        return Response(bank_row(b, ZERO), status=status.HTTP_201_CREATED)


class ARBankView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk):
        return banks_qs(request).filter(pk=pk).first()

    def patch(self, request, pk):
        if not ar_can(request.user, 'ar.bank.manage'):
            return _deny('You cannot manage banks.')
        b = self._get(request, pk)
        if not b:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        vals, errs = _clean(request.data, partial=True)
        if not errs and 'name' in vals and _name_taken(b.company_id, vals['name'], exclude_id=b.id):
            errs['name'] = 'A bank with this name already exists.'
        if errs:
            return Response(errs, status=status.HTTP_400_BAD_REQUEST)
        for k, v in vals.items():
            setattr(b, k, v)
        b.save()
        _log(request, 'Edited bank %s' % b.name, 'updated', b.id)
        return Response(bank_row(b, _received_by_bank([b.id]).get(b.id, ZERO)))

    def delete(self, request, pk):
        if not ar_can(request.user, 'ar.bank.manage'):
            return _deny('You cannot manage banks.')
        b = self._get(request, pk)
        if not b:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        # A bank that has money recorded against it is retired, so those receipts
        # keep their bank; one never used is removed outright.
        with transaction.atomic():
            if ARReceipt.objects.filter(bank=b).exists():
                b.is_active = False
                b.save(update_fields=['is_active', 'updated_at'])
                _log(request, 'Retired bank %s (it has receipts)' % b.name, 'updated', b.id)
                return Response({'retired': True})
            name = b.name
            b.delete()
        _log(request, 'Removed bank %s' % name, 'deleted', pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ARBankStatementView(APIView):
    """A bank's ledger: opening balance, then every live Loan receipt into it in date
    order with a running balance. With ?from= / ?to=, the payments before the range
    are rolled into a brought-forward line, so the closing figure always equals the
    balance Bank Master shows as of that date."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not has_ar_access(request.user):
            return _deny()
        b = banks_qs(request).filter(pk=pk).first()
        if not b:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        d_from = parse_date(request.query_params.get('from'))
        d_to = parse_date(request.query_params.get('to'))
        receipts = sorted(
            ARReceipt.objects.filter(bank=b, is_deleted=False)
            .select_related('account__booking__project', 'account__booking__plot', 'created_by'),
            key=lambda x: (x.paid_on, x.id))
        opening = _d(b.opening_balance)
        brought = opening
        rows, total_in = [], ZERO
        for x in receipts:
            amt = _d(x.amount)
            if d_from and x.paid_on < d_from:
                brought += amt
                continue
            if d_to and x.paid_on > d_to:
                continue
            total_in += amt
            bk = x.account.booking
            rows.append({
                'id': x.id, 'account_id': x.account_id, 'date': x.paid_on.isoformat(),
                'client': bk.client_name or '', 'project': bk.project.name if bk.project_id else '',
                'plots': bk.plot_numbers or (bk.plot.number if bk.plot_id else ''),
                'remarks': x.remarks or '', 'source': x.source,
                'recorded_by': x.created_by.name if x.created_by_id else '',
                'amount': rupees(amt), 'balance': rupees(brought + total_in),
            })
        return Response({
            'bank': bank_row(b, _received_by_bank([b.id]).get(b.id, ZERO)),
            'from': d_from.isoformat() if d_from else None, 'to': d_to.isoformat() if d_to else None,
            'opening_balance': rupees(opening),
            # Opening plus everything received before the range; equals opening when
            # there is no from-date.
            'brought_forward': rupees(brought),
            'total_in': rupees(total_in),
            'closing_balance': rupees(brought + total_in),
            'rows': rows,
        })

