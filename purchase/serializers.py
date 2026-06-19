from rest_framework import serializers
from .models import POHeader, POLine


class POLineSerializer(serializers.ModelSerializer):
    item_name     = serializers.CharField(source='item_code.name', read_only=True)
    activity_code = serializers.CharField(source='activity.wbs_code', read_only=True)
    pr_no         = serializers.CharField(source='pr_line.pr.pr_no', read_only=True)

    class Meta:
        model  = POLine
        fields = ['id', 'pr_line', 'pr_no', 'activity', 'activity_code', 'project',
                  'item_code', 'item_name', 'qty_ordered', 'qty_received',
                  'unit_rate', 'uom', 'amount', 'tax_pct']
        read_only_fields = ['qty_received', 'amount']


class POHeaderSerializer(serializers.ModelSerializer):
    lines            = POLineSerializer(many=True)
    vendor_name      = serializers.CharField(source='vendor.name', read_only=True)
    project_name     = serializers.CharField(source='project.name', read_only=True)
    created_by_name  = serializers.CharField(source='created_by.name', read_only=True)
    total_amount     = serializers.SerializerMethodField()

    def get_total_amount(self, obj):
        return sum(l.amount for l in obj.lines.all())

    class Meta:
        model  = POHeader
        fields = ['id', 'po_no', 'project', 'project_name', 'vendor', 'vendor_name',
                  'po_date', 'delivery_date', 'status', 'payment_terms',
                  'created_by', 'created_by_name', 'remarks',
                  'created_at', 'updated_at', 'total_amount', 'lines']
        read_only_fields = ['po_no', 'po_date', 'created_at', 'updated_at']

    def create(self, validated_data):
        lines_data = validated_data.pop('lines')
        po = POHeader.objects.create(**validated_data)
        for line_data in lines_data:
            POLine.objects.create(po=po, **line_data)
        # Transition each referenced PR line to 'PO Created'
        for line in po.lines.all():
            try:
                line.pr_line.transition('PO Created')
            except Exception:
                pass
        return po


class POHeaderListSerializer(serializers.ModelSerializer):
    vendor_name  = serializers.CharField(source='vendor.name', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    line_count   = serializers.SerializerMethodField()

    def get_line_count(self, obj):
        return obj.lines.count()

    class Meta:
        model  = POHeader
        fields = ['id', 'po_no', 'project', 'project_name', 'vendor', 'vendor_name',
                  'po_date', 'delivery_date', 'status', 'line_count', 'created_at']


class POStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=['Draft', 'Confirmed', 'Dispatched', 'Closed', 'Cancelled'])
