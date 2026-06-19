from rest_framework import serializers
from .models import VendorInvoice, VendorInvoiceLine, Payment


class VendorInvoiceLineSerializer(serializers.ModelSerializer):
    item_name     = serializers.CharField(source='item_code.name', read_only=True)
    activity_code = serializers.CharField(source='activity.wbs_code', read_only=True)
    po_line_rate  = serializers.DecimalField(source='po_line.unit_rate',
                                             max_digits=14, decimal_places=2, read_only=True)
    grn_qty       = serializers.DecimalField(source='grn_line.qty_accepted',
                                             max_digits=14, decimal_places=3, read_only=True,
                                             allow_null=True)

    class Meta:
        model  = VendorInvoiceLine
        fields = ['id', 'po_line', 'grn_line', 'activity', 'activity_code',
                  'item_code', 'item_name', 'billed_qty', 'billed_rate', 'amount',
                  'po_line_rate', 'grn_qty', 'rate_match', 'qty_match', 'match_note']
        read_only_fields = ['amount', 'rate_match', 'qty_match', 'match_note']


class VendorInvoiceSerializer(serializers.ModelSerializer):
    lines            = VendorInvoiceLineSerializer(many=True)
    vendor_name      = serializers.CharField(source='vendor.name', read_only=True)
    project_name     = serializers.CharField(source='project.name', read_only=True)
    po_no            = serializers.CharField(source='po.po_no', read_only=True)
    created_by_name  = serializers.CharField(source='created_by.name', read_only=True)
    amount_paid      = serializers.SerializerMethodField()

    def get_amount_paid(self, obj):
        return sum(p.amount for p in obj.payments.all())

    class Meta:
        model  = VendorInvoice
        fields = ['id', 'invoice_no', 'vendor', 'vendor_name', 'project', 'project_name',
                  'po', 'po_no', 'invoice_date', 'due_date',
                  'invoice_amount', 'tax_amount', 'total_amount',
                  'match_status', 'payment_status', 'remarks',
                  'created_by', 'created_by_name', 'created_at', 'amount_paid', 'lines']
        read_only_fields = ['match_status', 'payment_status', 'created_at']

    def create(self, validated_data):
        lines_data = validated_data.pop('lines')
        invoice = VendorInvoice.objects.create(**validated_data)
        for line_data in lines_data:
            line = VendorInvoiceLine.objects.create(invoice=invoice, **line_data)
            _run_3way_match(line)
        _update_invoice_match_status(invoice)
        return invoice


def _run_3way_match(line: VendorInvoiceLine):
    """Set rate_match and qty_match flags on the invoice line."""
    po_rate = line.po_line.unit_rate
    grn_qty = line.grn_line.qty_accepted if line.grn_line else None

    line.rate_match = (line.billed_rate <= po_rate)
    if grn_qty is not None:
        line.qty_match = (line.billed_qty <= grn_qty)
        line.match_note = (
            f'Rate: billed={line.billed_rate} PO={po_rate}; '
            f'Qty: billed={line.billed_qty} GRN={grn_qty}'
        )
    else:
        line.qty_match  = False
        line.match_note = f'No GRN line linked. Rate: billed={line.billed_rate} PO={po_rate}'
    line.save(update_fields=['rate_match', 'qty_match', 'match_note'])


def _update_invoice_match_status(invoice: VendorInvoice):
    lines = list(invoice.lines.all())
    if not lines:
        return
    all_grn_linked  = all(l.grn_line_id for l in lines)
    all_rate_match  = all(l.rate_match for l in lines)
    all_qty_match   = all(l.qty_match for l in lines)

    if all_grn_linked and all_rate_match and all_qty_match:
        invoice.match_status = '3-Way'
    elif all_rate_match:
        invoice.match_status = '2-Way'
    else:
        invoice.match_status = 'Disputed'
    invoice.save(update_fields=['match_status'])


class VendorInvoiceListSerializer(serializers.ModelSerializer):
    vendor_name  = serializers.CharField(source='vendor.name', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model  = VendorInvoice
        fields = ['id', 'invoice_no', 'vendor', 'vendor_name', 'project', 'project_name',
                  'invoice_date', 'total_amount', 'match_status', 'payment_status', 'created_at']


class PaymentSerializer(serializers.ModelSerializer):
    vendor_name      = serializers.CharField(source='vendor.name', read_only=True)
    invoice_no       = serializers.CharField(source='invoice.invoice_no', read_only=True)
    created_by_name  = serializers.CharField(source='created_by.name', read_only=True)

    class Meta:
        model  = Payment
        fields = ['id', 'payment_no', 'invoice', 'invoice_no', 'vendor', 'vendor_name',
                  'payment_date', 'amount', 'payment_mode', 'reference_no', 'remarks',
                  'created_by', 'created_by_name', 'created_at']
        read_only_fields = ['payment_no', 'created_at']
