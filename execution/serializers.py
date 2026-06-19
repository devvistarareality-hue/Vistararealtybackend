from rest_framework import serializers
from .models import (
    PRHeader, PRLine,
    MaterialIssueHeader, MaterialIssueLine,
    MeasurementBook, MBLine, RABill,
    PR_STATUS_CHOICES,
)


class PRLineSerializer(serializers.ModelSerializer):
    item_name    = serializers.CharField(source='item_code.name', read_only=True)
    activity_code = serializers.CharField(source='activity.wbs_code', read_only=True)

    class Meta:
        model  = PRLine
        fields = ['id', 'activity', 'activity_code', 'project', 'item_code', 'item_name',
                  'qty_required', 'qty_on_po', 'uom', 'required_date', 'status', 'remarks']
        read_only_fields = ['status', 'qty_on_po']


class PRHeaderSerializer(serializers.ModelSerializer):
    lines            = PRLineSerializer(many=True)
    raised_by_name   = serializers.CharField(source='raised_by.name', read_only=True)
    approved_by_name = serializers.CharField(source='approved_by.name', read_only=True, default='')
    project_name     = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model  = PRHeader
        fields = ['id', 'pr_no', 'project', 'project_name', 'raised_by', 'raised_by_name',
                  'raised_date', 'status', 'approved_by', 'approved_by_name',
                  'approved_date', 'remarks', 'created_at', 'updated_at', 'lines']
        read_only_fields = ['pr_no', 'raised_date', 'status', 'created_at', 'updated_at']

    def create(self, validated_data):
        lines_data = validated_data.pop('lines')
        pr = PRHeader.objects.create(**validated_data)
        for line_data in lines_data:
            PRLine.objects.create(pr=pr, **line_data)
        return pr

    def update(self, instance, validated_data):
        lines_data = validated_data.pop('lines', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if lines_data is not None:
            instance.lines.all().delete()
            for line_data in lines_data:
                PRLine.objects.create(pr=instance, **line_data)
        return instance


class PRHeaderListSerializer(serializers.ModelSerializer):
    """Lightweight list view — no lines nested."""
    raised_by_name = serializers.CharField(source='raised_by.name', read_only=True)
    project_name   = serializers.CharField(source='project.name', read_only=True)
    line_count     = serializers.SerializerMethodField()

    def get_line_count(self, obj):
        return obj.lines.count()

    class Meta:
        model  = PRHeader
        fields = ['id', 'pr_no', 'project', 'project_name', 'raised_by_name',
                  'raised_date', 'status', 'line_count', 'created_at']


class PRTransitionSerializer(serializers.Serializer):
    line_id    = serializers.IntegerField()
    new_status = serializers.ChoiceField(choices=[s for s, _ in PR_STATUS_CHOICES])


# ─── Material Issue ───────────────────────────────────────────────────────────

class MaterialIssueLineSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source='item_code.name', read_only=True)

    class Meta:
        model  = MaterialIssueLine
        fields = ['id', 'item_code', 'item_name', 'grn_line', 'qty_issued', 'cost_rate', 'uom']


class MaterialIssueHeaderSerializer(serializers.ModelSerializer):
    lines          = MaterialIssueLineSerializer(many=True)
    issued_by_name = serializers.CharField(source='issued_by.name', read_only=True)
    activity_code  = serializers.CharField(source='activity.wbs_code', read_only=True)
    project_name   = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model  = MaterialIssueHeader
        fields = ['id', 'issue_no', 'project', 'project_name', 'activity', 'activity_code',
                  'issued_date', 'issued_by', 'issued_by_name', 'received_by', 'remarks',
                  'created_at', 'lines']
        read_only_fields = ['issue_no', 'created_at']

    def create(self, validated_data):
        lines_data = validated_data.pop('lines')
        issue = MaterialIssueHeader.objects.create(**validated_data)
        for line_data in lines_data:
            MaterialIssueLine.objects.create(issue=issue, **line_data)
        return issue


# ─── Measurement Book ─────────────────────────────────────────────────────────

class MBLineSerializer(serializers.ModelSerializer):
    activity_code = serializers.CharField(source='activity.wbs_code', read_only=True)
    activity_desc = serializers.CharField(source='activity.description', read_only=True)

    class Meta:
        model  = MBLine
        fields = ['id', 'activity', 'activity_code', 'activity_desc',
                  'description', 'qty_executed', 'unit_rate', 'amount']
        read_only_fields = ['amount']


class MeasurementBookSerializer(serializers.ModelSerializer):
    lines             = MBLineSerializer(many=True)
    prepared_by_name  = serializers.CharField(source='prepared_by.name', read_only=True)
    certified_by_name = serializers.CharField(source='certified_by.name', read_only=True, default='')
    project_name      = serializers.CharField(source='project.name', read_only=True)
    total_amount      = serializers.SerializerMethodField()

    def get_total_amount(self, obj):
        return sum(l.amount for l in obj.lines.all())

    class Meta:
        model  = MeasurementBook
        fields = ['id', 'mb_no', 'project', 'project_name', 'mb_date',
                  'prepared_by', 'prepared_by_name', 'certified_by', 'certified_by_name',
                  'status', 'remarks', 'created_at', 'total_amount', 'lines']
        read_only_fields = ['mb_no', 'created_at']

    def create(self, validated_data):
        lines_data = validated_data.pop('lines')
        mb = MeasurementBook.objects.create(**validated_data)
        for line_data in lines_data:
            MBLine.objects.create(mb=mb, **line_data)
        return mb


# ─── RA Bill ──────────────────────────────────────────────────────────────────

class RABillSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    mb_no        = serializers.CharField(source='mb.mb_no', read_only=True)

    class Meta:
        model  = RABill
        fields = ['id', 'ra_bill_no', 'project', 'project_name', 'mb', 'mb_no',
                  'period_from', 'period_to', 'total_amount', 'status', 'created_at']
        read_only_fields = ['ra_bill_no', 'created_at']
