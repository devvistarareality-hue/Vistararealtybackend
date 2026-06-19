from django.utils import timezone
from .models import StockLedger


def post_grn_to_ledger(grn):
    """Create StockLedger entries for all accepted lines in a GRN."""
    today = timezone.now().date()
    for line in grn.lines.all():
        if line.qty_accepted > 0:
            StockLedger.objects.create(
                project      = grn.project,
                item_code    = line.item_code,
                txn_type     = 'GRN_IN',
                ref_doc_type = 'GRN',
                ref_doc_no   = grn.grn_no,
                qty          = line.qty_accepted,
                cost_rate    = line.po_line.unit_rate,
                txn_date     = grn.received_date or today,
                created_by   = grn.received_by,
            )
        if line.qty_rejected > 0:
            StockLedger.objects.create(
                project      = grn.project,
                item_code    = line.item_code,
                txn_type     = 'REJECT_OUT',
                ref_doc_type = 'GRN',
                ref_doc_no   = grn.grn_no,
                qty          = -line.qty_rejected,
                cost_rate    = line.po_line.unit_rate,
                txn_date     = grn.received_date or today,
                created_by   = grn.received_by,
            )


def post_issue_to_ledger(issue):
    """Create StockLedger debit entries for all lines in a MaterialIssue."""
    today = timezone.now().date()
    for line in issue.lines.all():
        StockLedger.objects.create(
            project      = issue.project,
            item_code    = line.item_code,
            txn_type     = 'ISSUE_OUT',
            ref_doc_type = 'ISSUE',
            ref_doc_no   = issue.issue_no,
            qty          = -line.qty_issued,
            cost_rate    = line.cost_rate,
            txn_date     = issue.issued_date or today,
            created_by   = issue.issued_by,
        )


def get_stock_balance(project_id, item_code_id=None):
    """Return list of {item_code_id, balance_qty} dicts for a project."""
    from django.db.models import Sum
    from .models import StockLedger, Material
    qs = StockLedger.objects.filter(project_id=project_id)
    if item_code_id:
        qs = qs.filter(item_code_id=item_code_id)
    rows = qs.values('item_code').annotate(balance_qty=Sum('qty'))
    result = []
    for row in rows:
        try:
            mat = Material.objects.get(pk=row['item_code'])
            result.append({
                'project':     project_id,
                'item_code':   row['item_code'],
                'item_name':   mat.name,
                'uom':         mat.uom,
                'balance_qty': row['balance_qty'],
            })
        except Material.DoesNotExist:
            pass
    return result
