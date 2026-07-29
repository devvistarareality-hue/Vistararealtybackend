from rest_framework import serializers
from .models import Scheme, Investor, Payout, ReferralReward, Lead, FollowUp, INTEREST_PAYOUT_CHOICES


class LeadListSerializer(serializers.ModelSerializer):
    assigned_to_name = serializers.CharField(source='assigned_to.name', read_only=True, default=None)
    scheme_interest_name = serializers.CharField(source='scheme_interest.name', read_only=True, default=None)
    created_by_name = serializers.CharField(source='created_by.name', read_only=True, default=None)

    class Meta:
        model = Lead
        fields = [
            'id', 'company_id', 'name', 'phone', 'alt_phone', 'email',
            'reference_name', 'reference_phone', 'source',
            'scheme_interest', 'scheme_interest_name', 'amount_interested',
            'assigned_to', 'assigned_to_name', 'status', 'remarks', 'next_follow_up_date',
            'created_by', 'created_by_name', 'created_at', 'updated_at',
        ]
        # next_follow_up_date is maintained by the FollowUp views and the status-change
        # logic in LeadDetailView.patch — never set directly through this serializer.
        read_only_fields = ['id', 'company_id', 'created_by', 'next_follow_up_date', 'created_at', 'updated_at']


class LeadCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Lead
        fields = [
            'id', 'name', 'phone', 'alt_phone', 'email', 'reference_name', 'reference_phone',
            'source', 'scheme_interest', 'amount_interested', 'assigned_to', 'status', 'remarks',
        ]
        extra_kwargs = {'assigned_to': {'required': False}, 'status': {'required': False}}


class FollowUpSerializer(serializers.ModelSerializer):
    lead_name = serializers.CharField(source='lead.name', read_only=True)
    assigned_to_name = serializers.CharField(source='assigned_to.name', read_only=True, default=None)

    class Meta:
        model = FollowUp
        fields = [
            'id', 'lead', 'lead_name', 'assigned_to', 'assigned_to_name',
            'scheduled_at', 'completed_at', 'status', 'remarks', 'outcome',
            'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']
        # assigned_to defaults to the requesting user when omitted (see
        # FollowUpListCreateView.post) — must not be required at the serializer level.
        extra_kwargs = {'assigned_to': {'required': False}}


class SchemeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Scheme
        fields = [
            'id', 'company_id', 'name', 'tenure_months', 'fixed_return_pct',
            'loyalty_benefit_pct', 'total_return_pct', 'min_ticket_size',
            'interest_payout_options', 'principal_payout', 'premature_redemption_allowed',
            'premature_redemption_lock_months', 'premature_redemption_rate_pct_per_month',
            'investor_approvers', 'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'company_id', 'created_at', 'updated_at']
        extra_kwargs = {'total_return_pct': {'required': False}}

    def validate_interest_payout_options(self, value):
        valid = {c[0] for c in INTEREST_PAYOUT_CHOICES}
        if not value or not isinstance(value, list) or not set(value).issubset(valid):
            raise serializers.ValidationError(f'Select at least one of: {", ".join(sorted(valid))}.')
        return value


class InvestorListSerializer(serializers.ModelSerializer):
    scheme_name = serializers.CharField(source='scheme.name', read_only=True)
    added_by_name = serializers.CharField(source='added_by.name', read_only=True, default=None)
    document_url = serializers.SerializerMethodField()
    loi_document_url = serializers.SerializerMethodField()

    def get_document_url(self, obj):
        return obj.document.url if obj.document else ''

    def get_loi_document_url(self, obj):
        return obj.loi_document.url if obj.loi_document else ''

    class Meta:
        model = Investor
        fields = [
            'id', 'company_id', 'scheme', 'scheme_name', 'reference_name', 'reference_phone',
            'name', 'phone', 'email', 'pan', 'amount_invested', 'investment_date',
            'maturity_date', 'interest_payout', 'total_return_pct', 'document_url', 'status',
            'approval_status', 'added_by', 'added_by_name', 'notes', 'lead', 'security',
            'loi_no', 'loi_document_url', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'company_id', 'maturity_date', 'status', 'approval_status', 'added_by',
                             'lead', 'loi_no', 'created_at', 'updated_at']


class InvestorCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Investor
        fields = ['id', 'scheme', 'reference_name', 'reference_phone', 'name', 'phone', 'email', 'pan',
                  'amount_invested', 'investment_date', 'interest_payout', 'total_return_pct', 'notes',
                  'lead', 'security']
        extra_kwargs = {
            'interest_payout': {'required': False},
            'total_return_pct': {'required': False},
            'lead': {'required': False},
            'security': {'required': False},
        }

    def validate(self, attrs):
        scheme = attrs.get('scheme')
        amount = attrs.get('amount_invested')
        if scheme and amount is not None and amount < scheme.min_ticket_size:
            raise serializers.ValidationError(
                {'amount_invested': f'Minimum ticket size for {scheme.name} is {scheme.min_ticket_size}.'}
            )
        interest_payout = attrs.get('interest_payout')
        if scheme and interest_payout and scheme.interest_payout_options and interest_payout not in scheme.interest_payout_options:
            raise serializers.ValidationError(
                {'interest_payout': f'{scheme.name} only allows: {", ".join(scheme.interest_payout_options)}.'}
            )
        return attrs


class PayoutSerializer(serializers.ModelSerializer):
    investor_name = serializers.CharField(source='investor.name', read_only=True)
    scheme_name = serializers.CharField(source='investor.scheme.name', read_only=True)

    class Meta:
        model = Payout
        fields = [
            'id', 'investor', 'investor_name', 'scheme_name', 'payout_type', 'due_date',
            'amount_due', 'status', 'paid_date', 'paid_by', 'notes', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'investor', 'payout_type', 'due_date', 'amount_due',
                             'paid_date', 'paid_by', 'created_at', 'updated_at']


class ReferralRewardSerializer(serializers.ModelSerializer):
    investor_name = serializers.CharField(source='investor.name', read_only=True)

    class Meta:
        model = ReferralReward
        fields = [
            'id', 'investor', 'investor_name', 'reference_name', 'reference_phone',
            'amount', 'status', 'paid_date', 'paid_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'investor', 'reference_name', 'reference_phone', 'amount',
                             'paid_date', 'paid_by', 'created_at', 'updated_at']
