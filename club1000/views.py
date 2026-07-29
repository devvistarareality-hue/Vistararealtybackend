from datetime import date
from decimal import Decimal
from dateutil.relativedelta import relativedelta
from django.db.models import Q
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from accounts.models import User
from accounts.permissions import is_platform_admin, scope_to_company
from .permissions import is_club1000_manager, has_club1000_access, scope_leads_to_role, _scheme_approver_ids
from .models import Scheme, Investor, Payout, ReferralReward, Lead, FollowUp, REFERRAL_REWARD_PCT, REINVESTMENT_REFERRAL_REWARD_PCT
from .serializers import (
    SchemeSerializer, InvestorListSerializer,
    InvestorCreateSerializer, PayoutSerializer, ReferralRewardSerializer,
    LeadListSerializer, LeadCreateSerializer, FollowUpSerializer,
)
from .services import generate_payout_schedule, normalize_phone


def _company_filtered(qs, request, field='company'):
    """scope_to_company + honour ?company_id for platform admins (existing sales convention)."""
    qs = scope_to_company(qs, request.user, field)
    cid = request.query_params.get('company_id')
    if cid and is_platform_admin(request.user):
        qs = qs.filter(**{f'{field}_id': cid})
    return qs


def _no_access():
    return Response({'detail': 'You do not have access to Club 1000.'}, status=status.HTTP_403_FORBIDDEN)


def _no_permission():
    return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)


class StatsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()

        investors = _company_filtered(Investor.objects.select_related('scheme'), request)
        manager = is_club1000_manager(request.user)
        if not manager:
            investors = investors.filter(added_by=request.user)

        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        if date_from:
            investors = investors.filter(investment_date__gte=date_from)
        if date_to:
            investors = investors.filter(investment_date__lte=date_to)

        total_invested = Decimal('0')
        by_scheme = {}
        for inv in investors:
            total_invested += inv.amount_invested or Decimal('0')
            key = inv.scheme.name
            entry = by_scheme.setdefault(key, {'scheme': key, 'investors': 0, 'amount': Decimal('0')})
            entry['investors'] += 1
            entry['amount'] += inv.amount_invested or Decimal('0')

        payouts = Payout.objects.filter(investor__in=investors)
        pending = payouts.filter(status='pending')
        paid = payouts.filter(status='paid')
        pending_amount = sum((p.amount_due or Decimal('0') for p in pending), Decimal('0'))
        paid_amount = sum((p.amount_due or Decimal('0') for p in paid), Decimal('0'))

        data = {
            'total_invested': total_invested,
            'investor_count': investors.count(),
            'pending_payout_count': pending.count(),
            'pending_payout_amount': pending_amount,
            'paid_payout_count': paid.count(),
            'paid_payout_amount': paid_amount,
            'by_scheme': list(by_scheme.values()),
        }
        if manager:
            data['active_scheme_count'] = _company_filtered(Scheme.objects.filter(is_active=True), request).count()
        return Response(data)


class SchemeListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        schemes = _company_filtered(Scheme.objects.filter(is_active=True), request).order_by('tenure_months')
        return Response(SchemeSerializer(schemes, many=True).data)

    def post(self, request):
        if not is_club1000_manager(request.user):
            return _no_permission()
        ser = SchemeSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        scheme = ser.save(company=request.user.company, created_by=request.user)
        return Response(SchemeSerializer(scheme).data, status=status.HTTP_201_CREATED)


class SchemeDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk):
        return _company_filtered(Scheme.objects.all(), request).filter(pk=pk).first()

    def patch(self, request, pk):
        if not is_club1000_manager(request.user):
            return _no_permission()
        scheme = self._get(request, pk)
        if not scheme:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = SchemeSerializer(scheme, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        ser.save()
        return Response(ser.data)

    def delete(self, request, pk):
        if not is_club1000_manager(request.user):
            return _no_permission()
        scheme = self._get(request, pk)
        if not scheme:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if scheme.investors.exists():
            scheme.is_active = False
            scheme.save(update_fields=['is_active'])
            return Response({'detail': 'Scheme has investors — disabled instead of deleted.'})
        scheme.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReferenceSuggestionsView(APIView):
    """Distinct reference name/number pairs already used at this company, one
    per phone number (earliest spelling wins) — powers the Add Investor
    autocomplete so a name isn't re-typed with different casing each time."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        qs = _company_filtered(Investor.objects.exclude(reference_phone=''), request).order_by('created_at')
        seen = {}
        for name, phone in qs.values_list('reference_name', 'reference_phone'):
            key = normalize_phone(phone)
            if key and key not in seen:
                seen[key] = {'reference_name': name, 'reference_phone': phone}
        return Response(list(seen.values()))


class InvestorListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        qs = _company_filtered(Investor.objects.select_related('scheme', 'added_by'), request)
        if not is_club1000_manager(request.user):
            approver_scheme_ids = _scheme_approver_ids(request.user, request.user.company)
            if approver_scheme_ids:
                qs = qs.filter(Q(added_by=request.user) | Q(scheme_id__in=approver_scheme_ids))
            else:
                qs = qs.filter(added_by=request.user)
        if request.query_params.get('scheme_id'):
            qs = qs.filter(scheme_id=request.query_params['scheme_id'])
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        approval_status = request.query_params.get('approval_status')
        if approval_status == 'all':
            pass  # Approvals page's "All" tab — no filter, includes rejected.
        elif approval_status:
            qs = qs.filter(approval_status=approval_status)
        else:
            # Default view excludes rejected investors — they only show up under
            # an explicit ?approval_status=rejected (or =all) filter.
            qs = qs.exclude(approval_status='rejected')
        return Response(InvestorListSerializer(qs.order_by('-created_at'), many=True).data)

    def post(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        ser = InvestorCreateSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        scheme = ser.validated_data['scheme']
        if scheme.company_id and scheme.company_id != request.user.company_id and not is_platform_admin(request.user):
            return Response({'detail': 'Invalid scheme for your company.'}, status=status.HTTP_400_BAD_REQUEST)
        lead = ser.validated_data.get('lead')
        if lead and lead.company_id != request.user.company_id and not is_platform_admin(request.user):
            return Response({'detail': 'Invalid lead for your company.'}, status=status.HTTP_400_BAD_REQUEST)
        investment_date = ser.validated_data.get('investment_date') or date.today()
        maturity_date = investment_date + relativedelta(months=scheme.tenure_months)
        # Interest payout & return % are prefilled from the scheme but editable
        # per-investor — only fall back to the scheme's default if the caller
        # didn't send an explicit value.
        interest_payout = ser.validated_data.get('interest_payout') or (
            scheme.interest_payout_options[0] if scheme.interest_payout_options else 'maturity'
        )
        total_return_pct = ser.validated_data.get('total_return_pct')
        if total_return_pct is None:
            total_return_pct = scheme.total_return_pct

        # Canonicalize the reference by phone number — free-typed names drift
        # ("chinmay" vs "Chinmay"), but a phone number is stable. If this phone
        # was already used, reuse the earliest name/phone on file for it instead
        # of creating a fresh spelling variant.
        reference_name = ser.validated_data.get('reference_name', '')
        reference_phone = ser.validated_data.get('reference_phone', '')
        normalized = normalize_phone(reference_phone)
        if normalized:
            prior = _company_filtered(Investor.objects.exclude(reference_phone=''), request).order_by('created_at')
            for name, phone in prior.values_list('reference_name', 'reference_phone'):
                if normalize_phone(phone) == normalized:
                    reference_name, reference_phone = name, phone
                    break

        # Custom payout-schedule edits (if any) are held until approval, not
        # generated now — generate_payout_schedule runs in InvestorActionView.approve.
        payout_schedule = request.data.get('payout_schedule')
        investor = ser.save(
            company=request.user.company,
            added_by=request.user,
            investment_date=investment_date,
            maturity_date=maturity_date,
            interest_payout=interest_payout,
            total_return_pct=total_return_pct,
            reference_name=reference_name,
            reference_phone=reference_phone,
            approval_status='pending',
            pending_payout_schedule=payout_schedule if isinstance(payout_schedule, list) else None,
        )
        # NOTE: the source Lead is NOT marked converted, no Payout schedule is
        # generated, and no ReferralReward is created here — all three are
        # deferred to approval (InvestorActionView.approve), mirroring Sales'
        # Closure-only-on-approval precedent. A rejected investor leaves no trace.

        # Scan document, sent as base64 {name, type, data} — same convention as
        # sales' signed-LOI upload (BookingListCreateView).
        doc = request.data.get('document_file')
        if isinstance(doc, dict) and doc.get('data'):
            import base64
            from django.core.files.base import ContentFile
            ext = (doc.get('name') or '').rsplit('.', 1)[-1][:10] or 'bin'
            doc_path = f'investor_{investor.id}_doc.{ext}'
            try:
                investor.document.save(doc_path, ContentFile(base64.b64decode(doc['data'])), save=True)
            except Exception:
                # Don't silently orphan the document: log and relink the deterministic path.
                import logging
                logging.getLogger(__name__).exception('Investor document save failed for %s', investor.id)
                try:
                    investor.document.name = f'club1000/{doc_path}'
                    investor.save(update_fields=['document'])
                except Exception:
                    logging.getLogger(__name__).exception('Investor document relink failed for %s', investor.id)
        # Signed LOI, sent as base64 {name, type, data} — same convention as
        # sales' signed-LOI upload in BookingListCreateView.post; supersedes the
        # separate InvestorUploadLoiView round-trip as the primary path.
        loi = request.data.get('loi_file')
        if isinstance(loi, dict) and loi.get('data'):
            import base64
            from django.core.files.base import ContentFile
            if not investor.loi_no:
                investor.loi_no = _next_investor_loi_no(investor.company, investor.scheme)
                investor.save(update_fields=['loi_no'])
            try:
                investor.loi_document.save(_investor_loi_path(investor), ContentFile(base64.b64decode(loi['data'])), save=True)
            except Exception:
                import logging
                logging.getLogger(__name__).exception('Investor LOI save failed for %s', investor.id)
        _notify_investor_approvers(request.user.company, investor, request.user)
        return Response(InvestorListSerializer(investor).data, status=status.HTTP_201_CREATED)


class InvestorDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk):
        qs = _company_filtered(Investor.objects.select_related('scheme', 'added_by'), request)
        if not is_club1000_manager(request.user):
            qs = qs.filter(added_by=request.user)
        return qs.filter(pk=pk).first()

    def get(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        investor = self._get(request, pk)
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(InvestorListSerializer(investor).data)

    def patch(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        investor = self._get(request, pk)
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = InvestorListSerializer(investor, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        ser.save()
        return Response(ser.data)


class InvestorRedeemView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not is_club1000_manager(request.user):
            return _no_permission()
        investor = _company_filtered(Investor.objects.select_related('scheme'), request).filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        scheme = investor.scheme
        if investor.status != 'active':
            return Response({'detail': f'Investor is already {investor.get_status_display()}.'}, status=status.HTTP_400_BAD_REQUEST)
        if not scheme.premature_redemption_allowed:
            return Response({'detail': f'{scheme.name} does not allow premature redemption.'}, status=status.HTTP_400_BAD_REQUEST)

        today = date.today()
        months_elapsed = (today.year - investor.investment_date.year) * 12 + (today.month - investor.investment_date.month)
        lock = scheme.premature_redemption_lock_months or 0
        if months_elapsed < lock:
            return Response(
                {'detail': f'{scheme.name} is locked in for {lock} months ({months_elapsed} elapsed).'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        rate = scheme.premature_redemption_rate_pct_per_month
        amount_payable = investor.amount_invested * (Decimal('1') + Decimal(months_elapsed) * rate / Decimal('100'))

        investor.payouts.filter(payout_type='maturity', status='pending').delete()
        payout = Payout.objects.create(
            investor=investor,
            payout_type='premature_redemption',
            due_date=today,
            amount_due=amount_payable.quantize(Decimal('0.01')),
        )
        investor.status = 'premature_redeemed'
        investor.save(update_fields=['status'])
        return Response({
            'investor': InvestorListSerializer(investor).data,
            'payout': PayoutSerializer(payout).data,
        })


class PayoutListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        qs = _company_filtered(Payout.objects.select_related('investor', 'investor__scheme'), request, field='investor__company')
        if not is_club1000_manager(request.user):
            qs = qs.filter(investor__added_by=request.user)
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        return Response(PayoutSerializer(qs, many=True).data)


class PayoutMarkPaidView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not is_club1000_manager(request.user):
            return _no_permission()
        payout = _company_filtered(
            Payout.objects.select_related('investor'), request, field='investor__company'
        ).filter(pk=pk).first()
        if not payout:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        payout.status = 'paid'
        payout.paid_date = date.today()
        payout.paid_by = request.user
        payout.save(update_fields=['status', 'paid_date', 'paid_by', 'updated_at'])
        return Response(PayoutSerializer(payout).data)


class ReferralRewardListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        qs = _company_filtered(ReferralReward.objects.select_related('investor'), request, field='investor__company')
        if not is_club1000_manager(request.user):
            qs = qs.filter(investor__added_by=request.user)
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        return Response(ReferralRewardSerializer(qs, many=True).data)


class ReferralRewardMarkPaidView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not is_club1000_manager(request.user):
            return _no_permission()
        reward = _company_filtered(
            ReferralReward.objects.select_related('investor'), request, field='investor__company'
        ).filter(pk=pk).first()
        if not reward:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        reward.status = 'paid'
        reward.paid_date = date.today()
        reward.paid_by = request.user
        reward.save(update_fields=['status', 'paid_date', 'paid_by', 'updated_at'])
        return Response(ReferralRewardSerializer(reward).data)


class LeadListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        qs = scope_leads_to_role(
            _company_filtered(Lead.objects.select_related('assigned_to', 'scheme_interest', 'created_by'), request),
            request.user,
        )
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        if request.query_params.get('assigned_to'):
            qs = qs.filter(assigned_to_id=request.query_params['assigned_to'])
        if request.query_params.get('source'):
            qs = qs.filter(source=request.query_params['source'])
        if request.query_params.get('scheme_interest'):
            qs = qs.filter(scheme_interest_id=request.query_params['scheme_interest'])
        if request.query_params.get('date_from'):
            qs = qs.filter(created_at__date__gte=request.query_params['date_from'])
        if request.query_params.get('date_to'):
            qs = qs.filter(created_at__date__lte=request.query_params['date_to'])
        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(phone__icontains=search) | Q(email__icontains=search))
        return Response(LeadListSerializer(qs, many=True).data)

    def post(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        ser = LeadCreateSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        defaults = {'company': request.user.company, 'created_by': request.user}
        if not ser.validated_data.get('assigned_to'):
            defaults['assigned_to'] = request.user
        lead = ser.save(**defaults)
        return Response(LeadListSerializer(lead).data, status=status.HTTP_201_CREATED)


class LeadDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk):
        qs = scope_leads_to_role(
            _company_filtered(Lead.objects.select_related('assigned_to', 'scheme_interest', 'created_by'), request),
            request.user,
        )
        return qs.filter(pk=pk).first()

    def get(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        lead = self._get(request, pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(LeadListSerializer(lead).data)

    def patch(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        lead = self._get(request, pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = LeadListSerializer(lead, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        lead = ser.save()
        # A lead reaching a terminal status has nothing left to follow up on.
        if lead.status in ('not_interested', 'lost', 'converted') and lead.next_follow_up_date:
            lead.next_follow_up_date = None
            lead.save(update_fields=['next_follow_up_date'])
        return Response(LeadListSerializer(lead).data)

    def delete(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        lead = self._get(request, pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        lead.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FollowUpListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        qs = _company_filtered(
            FollowUp.objects.select_related('lead', 'assigned_to'), request, field='lead__company'
        )
        if not is_club1000_manager(request.user):
            from sales.views import _visible_user_ids
            qs = qs.filter(assigned_to__in=_visible_user_ids(request.user))
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        return Response(FollowUpSerializer(qs, many=True).data)

    def post(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        ser = FollowUpSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        lead = ser.validated_data['lead']
        if lead.company_id != request.user.company_id and not is_platform_admin(request.user):
            return Response({'detail': 'Invalid lead for your company.'}, status=status.HTTP_400_BAD_REQUEST)
        followup = ser.save(
            created_by=request.user,
            assigned_to=ser.validated_data.get('assigned_to') or request.user,
        )
        # Scheduling a follow-up sets the lead's next follow-up date.
        lead.next_follow_up_date = followup.scheduled_at
        lead.save(update_fields=['next_follow_up_date'])
        return Response(FollowUpSerializer(followup).data, status=status.HTTP_201_CREATED)


class FollowUpDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        qs = _company_filtered(
            FollowUp.objects.select_related('lead', 'assigned_to'), request, field='lead__company'
        )
        if not is_club1000_manager(request.user):
            from sales.views import _visible_user_ids
            qs = qs.filter(assigned_to__in=_visible_user_ids(request.user))
        followup = qs.filter(pk=pk).first()
        if not followup:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        old_scheduled_at = followup.scheduled_at
        ser = FollowUpSerializer(followup, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        followup = ser.save()
        # Keep the lead's next follow-up date in sync with this, its active follow-up.
        lead = followup.lead
        if lead.next_follow_up_date == old_scheduled_at:
            if followup.status in ('completed', 'missed'):
                lead.next_follow_up_date = None
                lead.save(update_fields=['next_follow_up_date'])
            elif followup.scheduled_at != old_scheduled_at:
                lead.next_follow_up_date = followup.scheduled_at
                lead.save(update_fields=['next_follow_up_date'])
        return Response(FollowUpSerializer(followup).data)


def _next_investor_loi_no(company, scheme):
    """Next per-scheme LOI number, e.g. 'RISE/028' — mirrors sales' _next_eoi_no."""
    existing = Investor.objects.filter(company=company, scheme=scheme).exclude(loi_no='').count()
    return f'{scheme.name}/{str(existing + 1).zfill(3)}'


def _investor_loi_path(investor):
    """Object storage path for a generated Investment Proposal Form — mirrors
    sales._loi_path's GAS-style layout."""
    import re
    san = lambda s: (re.sub(r'[\\/:*?"<>|]+', '', str(s or '')).strip() or 'NA')
    scheme_name = san(investor.scheme.name)
    client = san(investor.name)
    loi_no = san(investor.loi_no)
    return f'Club1000/{scheme_name}/{client} - {loi_no}/LOI.pdf'


class InvestorNextLoiNoView(APIView):
    """Preview the next per-scheme LOI number before the investor even exists,
    so the pre-submit downloaded PDF shows the real value — mirrors
    sales.views.BookingNextEOIView. The actual assignment at submit time
    (InvestorListCreateView.post) re-runs the same counting helper; nothing
    is reserved here."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        scheme_id = request.query_params.get('scheme_id')
        if not scheme_id:
            return Response({'detail': 'scheme_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        scheme = _company_filtered(Scheme.objects.all(), request).filter(pk=scheme_id).first()
        if not scheme:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'loi_no': _next_investor_loi_no(request.user.company, scheme)})


def _notify_investor_approvers(company, investor, submitter):
    """Notify whoever should review a freshly-submitted investor — mirrors
    sales.views._notify_booking_approvers exactly, one level down (Scheme
    instead of Project)."""
    try:
        from notifications import notify, reporting_chain
        # 1) Per-scheme configured approvers (most precise).
        ids = (investor.scheme.investor_approvers if investor.scheme_id else None) or []
        recipients = list(User.objects.filter(id__in=ids, company=company, is_active=True)) if ids else []
        # 2) Fallback: the submitter's own reporting-manager chain.
        if not recipients:
            recipients = reporting_chain(submitter)
        # 3) Last resort: every manager/admin in the company (so it's never silent).
        if not recipients:
            recipients = list(User.objects.filter(company=company, is_active=True).filter(Q(role='Manager') | Q(is_staff=True)))
        sub_id = getattr(submitter, 'id', None)
        recipients = [u for u in recipients if u and u.id != sub_id]
        if not recipients:
            return
        title = 'Investor approval needed'
        msg = '%s · %s · ₹%s — by %s' % (
            investor.name, investor.scheme.name if investor.scheme_id else '',
            int(investor.amount_invested or 0), getattr(submitter, 'name', ''),
        )
        seen = set()
        for u in recipients:
            if u.id not in seen:
                seen.add(u.id)
                notify(u, 'investor_approval', title, msg, {'investor_id': investor.id})
    except Exception:
        pass


def _first_approved_investment(investor):
    """The earliest OTHER approved Investor row for this same person (matched by
    their own phone number, normalized) in this company — i.e. their first
    approved investment, if this one isn't it. None if this IS their first (or
    only) approved investment."""
    own_phone = normalize_phone(investor.phone)
    if not own_phone:
        return None
    candidates = Investor.objects.filter(
        company=investor.company, approval_status='approved',
    ).exclude(pk=investor.pk).order_by('created_at').only('id', 'phone', 'reference_name', 'reference_phone')
    for cand in candidates:
        if normalize_phone(cand.phone) == own_phone:
            return cand
    return None


class InvestorActionView(APIView):
    """Approve / reject a pending investor (approver = a Club 1000 manager)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not is_club1000_manager(request.user):
            return _no_permission()
        investor = _company_filtered(Investor.objects.select_related('scheme', 'lead'), request).filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        action = request.data.get('action')
        if action not in ('approve', 'reject'):
            return Response({'detail': "action must be 'approve' or 'reject'."}, status=status.HTTP_400_BAD_REQUEST)
        if investor.approval_status != 'pending':
            return Response({'detail': f'Investor is already {investor.approval_status}.'}, status=status.HTTP_400_BAD_REQUEST)

        from notifications import notify

        if action == 'approve':
            investor.approval_status = 'approved'
            investor.status = 'active'
            investor.save(update_fields=['approval_status', 'status', 'updated_at'])
            # Now — and only now — run the three deferred side effects.
            if investor.lead_id:
                Lead.objects.filter(pk=investor.lead_id).update(status='converted', next_follow_up_date=None)
            generate_payout_schedule(investor, custom_entries=investor.pending_payout_schedule)
            # Tiered referral reward: 0.5% on this person's first approved
            # investment, 0.25% on every one after — always attributed to the
            # ORIGINAL referrer (from their first approved investment), even if
            # this submission's own reference fields are blank or different.
            first_investment = _first_approved_investment(investor)
            if first_investment:
                ref_name, ref_phone, pct = first_investment.reference_name, first_investment.reference_phone, REINVESTMENT_REFERRAL_REWARD_PCT
            else:
                ref_name, ref_phone, pct = investor.reference_name, investor.reference_phone, REFERRAL_REWARD_PCT
            if ref_phone:
                ReferralReward.objects.create(
                    investor=investor,
                    reference_name=ref_name,
                    reference_phone=ref_phone,
                    amount=(investor.amount_invested * pct / Decimal('100')).quantize(Decimal('0.01')),
                )
            investor.pending_payout_schedule = None
            investor.save(update_fields=['pending_payout_schedule'])
            try:
                notify(investor.added_by, 'investor_approved', 'Investor approved',
                       f'{investor.name} · {investor.scheme.name if investor.scheme_id else ""} was approved.',
                       {'investor_id': investor.id})
            except Exception:
                pass
        else:
            investor.approval_status = 'rejected'
            if investor.loi_document:
                investor.loi_document.delete(save=False)
            investor.save(update_fields=['approval_status', 'loi_document', 'updated_at'])
            try:
                notify(investor.added_by, 'investor_rejected', 'Investor rejected',
                       f'{investor.name} · {investor.scheme.name if investor.scheme_id else ""} was rejected.',
                       {'investor_id': investor.id})
            except Exception:
                pass
        return Response(InvestorListSerializer(investor).data)


class InvestorLoiNoView(APIView):
    """Reserves (assigning + persisting if not already set) and returns this
    investor's LOI number — called before PDF generation so the number is
    stable and can be baked into the document itself, then just reused (not
    reassigned) when the generated PDF is uploaded via InvestorUploadLoiView."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        investor = _company_filtered(Investor.objects.select_related('scheme'), request).filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not investor.loi_no:
            investor.loi_no = _next_investor_loi_no(investor.company, investor.scheme)
            investor.save(update_fields=['loi_no'])
        return Response({'loi_no': investor.loi_no})


class InvestorUploadLoiView(APIView):
    """Stores the client-generated Investment Proposal Form PDF for an investor
    and assigns its LOI number if not already set — mirrors how Sales' signed
    LOI is saved in BookingListCreateView.post."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        investor = _company_filtered(Investor.objects.select_related('scheme'), request).filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        loi = request.data.get('loi_file')
        if not isinstance(loi, dict) or not loi.get('data'):
            return Response({'detail': 'loi_file is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not investor.loi_no:
            investor.loi_no = _next_investor_loi_no(investor.company, investor.scheme)
            investor.save(update_fields=['loi_no'])
        import base64
        from django.core.files.base import ContentFile
        try:
            investor.loi_document.save(_investor_loi_path(investor), ContentFile(base64.b64decode(loi['data'])), save=True)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Investor LOI save failed for %s', investor.id)
            return Response({'detail': 'Could not save LOI document.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(InvestorListSerializer(investor).data)


class InvestorLoiUrlView(APIView):
    """Short-lived signed URL for an investor's generated LOI PDF — mirrors
    sales.views.BookingLOIUrlView exactly."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        investor = _company_filtered(Investor.objects.all(), request).filter(pk=pk).first()
        if not investor or not investor.loi_document:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        is_scheme_approver = investor.scheme_id in _scheme_approver_ids(request.user, request.user.company)
        if not (is_club1000_manager(request.user) or investor.added_by_id == request.user.id or is_scheme_approver):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        from sales.supabase_storage import create_signed_url
        url = create_signed_url(investor.loi_document.name, expires_in=120)
        if not url:
            try:
                url = request.build_absolute_uri(investor.loi_document.url)
            except Exception:
                url = None
        if not url:
            return Response({'detail': 'LOI unavailable.'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'url': url})
