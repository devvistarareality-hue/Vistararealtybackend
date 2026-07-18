from rest_framework import serializers
from .models import Scheme, Investor, Payout, ReferralReward, INTEREST_PAYOUT_CHOICES


class SchemeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Scheme
        fields = [
            'id', 'company_id', 'name', 'tenure_months', 'fixed_return_pct',
            'loyalty_benefit_pct', 'total_return_pct', 'min_ticket_size',
            'interest_payout_options', 'principal_payout', 'premature_redemption_allowed',
            'premature_redemption_lock_months', 'premature_redemption_rate_pct_per_month',
            'is_active', 'created_at', 'updated_at',
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

    def get_document_url(self, obj):
        return obj.document.url if obj.document else ''

    class Meta:
        model = Investor
        fields = [
            'id', 'company_id', 'scheme', 'scheme_name', 'reference_name', 'reference_phone',
            'name', 'phone', 'email', 'pan', 'amount_invested', 'investment_date',
            'maturity_date', 'interest_payout', 'total_return_pct', 'document_url', 'status',
            'added_by', 'added_by_name', 'notes', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'company_id', 'maturity_date', 'status', 'added_by', 'created_at', 'updated_at']


class InvestorCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Investor
        fields = ['id', 'scheme', 'reference_name', 'reference_phone', 'name', 'phone', 'email', 'pan',
                  'amount_invested', 'investment_date', 'interest_payout', 'total_return_pct', 'notes']
        extra_kwargs = {
            'interest_payout': {'required': False},
            'total_return_pct': {'required': False},
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
