from datetime import date
from decimal import Decimal
from dateutil.relativedelta import relativedelta
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from accounts.permissions import is_platform_admin, scope_to_company
from .permissions import is_club1000_manager, has_club1000_access
from .models import Scheme, Investor, Payout, ReferralReward, REFERRAL_REWARD_PCT
from .serializers import (
    SchemeSerializer, InvestorListSerializer,
    InvestorCreateSerializer, PayoutSerializer, ReferralRewardSerializer,
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
            qs = qs.filter(added_by=request.user)
        if request.query_params.get('scheme_id'):
            qs = qs.filter(scheme_id=request.query_params['scheme_id'])
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
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

        investor = ser.save(
            company=request.user.company,
            added_by=request.user,
            investment_date=investment_date,
            maturity_date=maturity_date,
            interest_payout=interest_payout,
            total_return_pct=total_return_pct,
            reference_name=reference_name,
            reference_phone=reference_phone,
        )
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
        payout_schedule = request.data.get('payout_schedule')
        generate_payout_schedule(investor, custom_entries=payout_schedule if isinstance(payout_schedule, list) else None)
        # Referral reward: 0.5% of this investor's amount_invested, owed to
        # whoever referred them — earned immediately on add, tracked pending/paid.
        if investor.reference_phone:
            ReferralReward.objects.create(
                investor=investor,
                reference_name=investor.reference_name,
                reference_phone=investor.reference_phone,
                amount=(investor.amount_invested * REFERRAL_REWARD_PCT / Decimal('100')).quantize(Decimal('0.01')),
            )
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
