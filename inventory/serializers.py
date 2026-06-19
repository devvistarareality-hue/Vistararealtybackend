from rest_framework import serializers
from .models import GRNHeader, GRNLine, StockLedger


class GRNLineSerializer(serializers.ModelSerializer):
    item_name     = serializers.CharField(source='item_code.name', read_only=True)
    activity_code = serializers.CharField(source='activity.wbs_code', read_only=True)
    po_line_item  = serializers.CharField(source='po_line.item_code.name', read_only=True)

    class Meta:
        model  = GRNLine
        fields = ['id', 'po_line', 'po_line_item', 'activity', 'activity_code', 'project',
                  'item_code', 'item_name', 'qty_received', 'qty_accepted', 'qty_rejected',
                  'uom', 'qc_status', 'qc_remarks', 'batch_no']


class GRNHeaderSerializer(serializers.ModelSerializer):
    lines            = GRNLineSerializer(many=True)
    vendor_name      = serializers.CharField(source='vendor.name', read_only=True)
    project_name     = serializers.CharField(source='project.name', read_only=True)
    received_by_name = serializers.CharField(source='received_by.name', read_only=True)
    po_no            = serializers.CharField(source='po.po_no', read_only=True)

    class Meta:
        model  = GRNHeader
        fields = ['id', 'grn_no', 'project', 'project_name', 'po', 'po_no',
                  'vendor', 'vendor_name', 'received_date', 'received_by', 'received_by_name',
                  'dc_no', 'vehicle_no', 'status', 'remarks', 'created_at', 'lines']
        read_only_fields = ['grn_no', 'created_at']

    def create(self, validated_data):
        from inventory.stock_utils import post_grn_to_ledger
        lines_data = validated_data.pop('lines')
        grn = GRNHeader.objects.create(**validated_data)
        for line_data in lines_data:
            GRNLine.objects.create(grn=grn, **line_data)
        post_grn_to_ledger(grn)
        return grn


class GRNHeaderListSerializer(serializers.ModelSerializer):
    vendor_name  = serializers.CharField(source='vendor.name', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    po_no        = serializers.CharField(source='po.po_no', read_only=True)

    class Meta:
        model  = GRNHeader
        fields = ['id', 'grn_no', 'project', 'project_name', 'po', 'po_no',
                  'vendor', 'vendor_name', 'received_date', 'status', 'created_at']


class GRNQCSerializer(serializers.Serializer):
    """Payload to update QC results on all lines of a GRN."""
    lines = serializers.ListField(
        child=serializers.DictField()
    )
    overall_status = serializers.ChoiceField(
        choices=['Pending QC', 'QC Passed', 'QC Failed', 'Stocked']
    )


class StockLedgerSerializer(serializers.ModelSerializer):
    item_code_str = serializers.CharField(source='item_code.item_code', read_only=True)
    item_name     = serializers.CharField(source='item_code.name', read_only=True)
    project_name  = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model  = StockLedger
        fields = ['id', 'project', 'project_name', 'item_code', 'item_code_str', 'item_name',
                  'txn_type', 'ref_doc_type', 'ref_doc_no', 'qty', 'cost_rate',
                  'txn_date', 'created_at']
        read_only_fields = ['created_at']


class StockBalanceSerializer(serializers.Serializer):
    """Read-only — computed from StockLedger aggregation."""
    project    = serializers.IntegerField()
    item_code  = serializers.IntegerField()
    item_name  = serializers.CharField()
    uom        = serializers.CharField()
    balance_qty = serializers.DecimalField(max_digits=14, decimal_places=3)
