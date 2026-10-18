"""Import Club 1000 investors from Excel.

The template (InvestorImportTemplateView) has one row per investment with dropdowns
for scheme, interest payout and source, two worked examples and a Help sheet. An
upload (InvestorImportView) is read and checked row by row first — every problem is
reported against its Excel row number — and only then saved. Each saved row is a
DRAFT, exactly like Add Investor → Save Draft: the signed LOI is attached in
Approvals → Drafts → Edit & Submit, which sends it for approval (and tells the
approvers); its payout schedule, lead and referral reward are created on approval.
"""
import base64
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from dateutil.relativedelta import relativedelta
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from sales.fields import phone_blind_index
from .models import Investor, Scheme, LEAD_SOURCE_CHOICES
from .permissions import is_club1000_manager, has_club1000_access
from .services import normalize_phone

COLUMNS = [
    # key, header, required, help
    ('name', 'Investor Name', True, 'Full name as on the LOI.'),
    ('phone', 'Mobile', True, '10-digit mobile number.'),
    ('email', 'Email', False, ''),
    ('pan', 'PAN', False, ''),
    ('scheme', 'Scheme', True, 'Pick from the list — must match a scheme exactly.'),
    ('amount_invested', 'Amount Invested', True, 'In rupees (paise allowed), no commas needed. At least the scheme\'s minimum ticket.'),
    ('investment_date', 'Investment Date', True, 'DD-MM-YYYY (or an Excel date) of this investment. Maturity is worked out from the scheme\'s tenure. Renewals are NOT imported — renew it on the website (Investors → Renew) after it is approved.'),
    ('interest_payout', 'Interest Payout', False, 'monthly / quarterly / maturity — must be one the scheme offers. Blank = the scheme\'s first option.'),
    ('total_return_pct', 'Return %', False, 'Total return by maturity. Blank = the scheme\'s rate for that payout.'),
    ('source', 'Source', False, 'referral / walk_in / website / other. Blank = referral.'),
    ('reference_name', 'Reference Name', False, 'Who referred them — earns the referral reward on approval.'),
    ('reference_phone', 'Reference Mobile', False, ''),
    ('loi_no', 'LOI No', False, 'Keep the LOI number already issued, if any. Blank = assigned later.'),
    ('security', 'Security', False, 'Printed on the LOI.'),
    ('notes', 'Notes', False, ''),
]
HEADER_TO_KEY = {}
for _k, _h, _r, _d in COLUMNS:
    for alias in (_k, _h, _h.replace(' ', '_'), _h.replace(' %', '_pct')):
        HEADER_TO_KEY[alias.strip().lower().replace(' ', '_')] = _k
HEADER_TO_KEY.update({'mobile_number': 'phone', 'amount': 'amount_invested', 'date': 'investment_date',
                      'return': 'total_return_pct', 'reference': 'reference_name', 'payout': 'interest_payout'})
SOURCES = [k for k, _ in LEAD_SOURCE_CHOICES]
PAYOUTS = ['monthly', 'quarterly', 'maturity']


def _manager_only(request):
    if not has_club1000_access(request.user) or not is_club1000_manager(request.user):
        return Response({'detail': 'Only a Club 1000 manager can import investors.'}, status=status.HTTP_403_FORBIDDEN)
    return None


def _schemes(request):
    qs = Scheme.objects.filter(is_active=True) if hasattr(Scheme, 'is_active') else Scheme.objects.all()
    company = getattr(request.user, 'company', None)
    if company:
        qs = qs.filter(company=company)
    return list(qs.order_by('name'))


class InvestorImportTemplateView(APIView):
    """GET the import template (.xlsx): dropdowns, examples and a Help sheet."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        err = _manager_only(request)
        if err:
            return err
        import openpyxl
        from django.http import HttpResponse
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.datavalidation import DataValidation

        schemes = _schemes(request)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Investors'
        # Must-fill columns stand out: red header with a *, and their empty cells turn
        # red on a row that has been started. A row missing one is not imported.
        ws.append([f'{h} *' if required else h for _, h, required, _ in COLUMNS])
        req_fill, opt_fill = PatternFill('solid', fgColor='C0392B'), PatternFill('solid', fgColor='5C7CAC')
        for i, (_, _, required, _) in enumerate(COLUMNS, start=1):
            c = ws.cell(row=1, column=i)
            c.font = Font(bold=True, color='FFFFFF')
            c.fill = req_fill if required else opt_fill
            c.alignment = Alignment(vertical='center')
            ws.column_dimensions[get_column_letter(i)].width = 20
        ws.row_dimensions[1].height = 22
        sc = schemes[0] if schemes else None
        sc2 = schemes[1] if len(schemes) > 1 else sc
        def pay_of(s):
            return (s.interest_payout_options or ['maturity'])[0] if s else 'maturity'
        today = date.today()
        examples = [
            {'name': 'Rahul Sharma', 'phone': '9876543210', 'email': 'rahul@example.com', 'scheme': sc.name if sc else 'RISE',
             'amount_invested': 1000000, 'investment_date': (today - relativedelta(months=3)).replace(day=1), 'interest_payout': pay_of(sc),
             'source': 'referral', 'reference_name': 'Amit Patel', 'reference_phone': '9898989898'},
            {'name': 'Priya Mehta', 'phone': '9988776655', 'scheme': sc2.name if sc2 else 'RISE',
             'amount_invested': 500000, 'investment_date': (today - relativedelta(months=1)).replace(day=15), 'interest_payout': pay_of(sc2), 'source': 'walk_in'},
        ]
        for ex in examples:
            ws.append([ex.get(k, '') for k, _, _, _ in COLUMNS])
        for r in (2, 3):
            for i in range(1, len(COLUMNS) + 1):
                ws.cell(row=r, column=i).font = Font(italic=True, color='7A869A')
        for r in range(2, 2001):
            ws.cell(row=r, column=[k for k, _, _, _ in COLUMNS].index('investment_date') + 1).number_format = 'DD-MM-YYYY'
        ws.freeze_panes = 'A2'
        from openpyxl.formatting.rule import FormulaRule
        last = get_column_letter(len(COLUMNS))
        for i, (_, _, required, _) in enumerate(COLUMNS, start=1):
            if required:
                col = get_column_letter(i)
                ws.conditional_formatting.add(f'{col}2:{col}2000', FormulaRule(
                    formula=[f'AND(ISBLANK({col}2),COUNTA($A2:${last}2)>0)'], fill=PatternFill('solid', fgColor='F8D7DA')))

        # Dropdowns: schemes (from a hidden list), payout, source.
        lists = wb.create_sheet('Lists')
        for i, s in enumerate(schemes, start=1):
            lists.cell(row=i, column=1, value=s.name)
            lists.cell(row=i, column=4, value=s.tenure_months)
            lists.cell(row=i, column=5, value=pay_of(s))
            lists.cell(row=i, column=6, value=float(s.rate_for(pay_of(s)) or 0))
        for i, p in enumerate(PAYOUTS, start=1):
            lists.cell(row=i, column=2, value=p)
        for i, s in enumerate(SOURCES, start=1):
            lists.cell(row=i, column=3, value=s)
        lists.sheet_state = 'hidden'
        col_of = {k: get_column_letter(i) for i, (k, _, _, _) in enumerate(COLUMNS, start=1)}
        for key, src in (('scheme', f'Lists!$A$1:$A${max(1, len(schemes))}'),
                         ('interest_payout', f'Lists!$B$1:$B${len(PAYOUTS)}'),
                         ('source', f'Lists!$C$1:$C${len(SOURCES)}')):
            dv = DataValidation(type='list', formula1=src, allow_blank=True)
            ws.add_data_validation(dv)
            dv.add(f'{col_of[key]}2:{col_of[key]}2000')

        hlp = wb.create_sheet('Help')
        hlp.append(['Column', 'Required', 'How to fill it'])
        for c in hlp[1]:
            c.font = Font(bold=True)
        for _, h, required, d in COLUMNS:
            hlp.append([h, 'Yes' if required else '', d])
        hlp.append([])
        hlp.append(['Rows 2–3 are examples — replace or delete them.'])
        hlp.append(['Each imported investor is saved as a DRAFT: open Club 1000 → Approvals → Drafts → Edit & Submit, '
                    'attach the signed LOI and submit — only then does it go to the approvers.'])
        hlp.append(['Columns marked * (red) must be filled. A row with any of them empty is NOT imported — '
                    'it is listed with the reason so you can fix it and upload again.'])
        hlp.append(['One row per investment: its amount and its own Investment Date. Each one is a new investment '
                    '(its own LOI) — the same person can have several rows (Add more).'])
        hlp.append(['Renewals are not imported. An investment that has already passed its maturity is imported as '
                    'due for renewal — once approved, renew it on the website (Investors → Renew), where the ended term, '
                    'its interest and its LOI are handled.'])
        hlp.append(['Payouts already due are created as due — mark them paid in Payouts with the dates they were '
                    'actually paid.'])
        hlp.append(['Schemes: ' + ', '.join(f'{s.name} ({s.tenure_months} mo, min ₹{int(s.min_ticket_size):,}, payouts: {", ".join(s.interest_payout_options or ["maturity"])})' for s in schemes)])
        hlp.column_dimensions['A'].width = 20
        hlp.column_dimensions['C'].width = 90

        buf = io.BytesIO()
        wb.save(buf)
        resp = HttpResponse(buf.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        resp['Content-Disposition'] = 'attachment; filename="club1000-investor-import-template.xlsx"'
        return resp


def _read_rows(raw, name):
    """The upload as [(excel_row_no, {key: value})] — header names are matched loosely."""
    if name.lower().endswith(('.csv', '.txt')):
        import csv
        text = raw.decode('utf-8-sig', errors='ignore')
        reader = csv.reader(io.StringIO(text))
        table = list(reader)
    else:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb['Investors'] if 'Investors' in wb.sheetnames else wb[wb.sheetnames[0]]
        table = [list(r) for r in ws.iter_rows(values_only=True)]
    if not table:
        return []
    keys = [HEADER_TO_KEY.get(str(h or '').replace('*', '').strip().lower().replace(' ', '_')) for h in table[0]]
    out = []
    for n, row in enumerate(table[1:], start=2):
        rec = {k: row[i] for i, k in enumerate(keys) if k and i < len(row)}
        if any(v not in (None, '') for v in rec.values()):
            out.append((n, rec))
    return out


def _text(v):
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = _text(v)
    for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d', '%d.%m.%Y', '%d-%m-%y', '%d/%m/%y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _num(v):
    s = _text(v).replace(',', '').replace('₹', '').replace('%', '').strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return False


def _check(rows, request):
    """Check every row; returns [{row, ok, errors, warnings, data, preview}].

    Each row is one new investment from its own Investment Date. Renewals are not
    imported (they are done by hand on the website, where the ended term, its interest
    and its LOI are handled) — one already past maturity comes in due for renewal."""
    schemes = {s.name.strip().lower(): s for s in _schemes(request)}
    company = request.user.company
    existing = {}
    # Drafts count too: an import is saved as drafts, so uploading the same file twice is caught.
    for inv in Investor.objects.filter(company=company).exclude(approval_status='rejected').only(
            'phone_key', 'scheme_id', 'investment_date', 'amount_invested', 'name'):
        existing.setdefault((inv.phone_key, inv.scheme_id, inv.investment_date), []).append(inv)
    seen = set()
    out = []
    for n, r in rows:
        errs, warns = [], []
        name, phone = _text(r.get('name')), _text(r.get('phone'))
        digits = ''.join(ch for ch in phone if ch.isdigit())
        if not name:
            errs.append('Investor Name is empty')
        if len(digits) < 10:
            errs.append('Mobile needs 10 digits')
        scheme = schemes.get(_text(r.get('scheme')).lower())
        if not scheme:
            errs.append(f'Scheme "{_text(r.get("scheme"))}" not found' if _text(r.get('scheme')) else 'Scheme is empty')
        amount = _num(r.get('amount_invested'))
        if amount in (None, False) or (amount is not None and amount is not False and amount <= 0):
            errs.append('Amount Invested is missing or not a number')
        elif scheme and amount < scheme.min_ticket_size:
            errs.append(f'Amount below {scheme.name} minimum ₹{int(scheme.min_ticket_size):,}')
        inv_date = _date(r.get('investment_date'))
        if not inv_date:
            errs.append('Investment Date missing or not DD-MM-YYYY')
        elif inv_date > date.today():
            errs.append('Investment Date is in the future')
        payout = _text(r.get('interest_payout')).lower().replace(' ', '_').replace('at_maturity', 'maturity')
        options = (scheme.interest_payout_options if scheme else None) or ['maturity']
        if not payout:
            payout = options[0]
        elif payout not in PAYOUTS:
            errs.append(f'Interest Payout "{_text(r.get("interest_payout"))}" — use monthly / quarterly / maturity')
        elif scheme and payout not in options:
            errs.append(f'{scheme.name} only allows {", ".join(options)}')
        pct = _num(r.get('total_return_pct'))
        if pct is False:
            errs.append('Return % is not a number')
        if pct in (None, False) and scheme:
            pct = scheme.rate_for(payout) or Decimal('0')
        source = _text(r.get('source')).lower().replace('-', '_').replace(' ', '_') or 'referral'
        if source not in SOURCES:
            errs.append(f'Source "{_text(r.get("source"))}" — use {", ".join(SOURCES)}')
        ref_name, ref_phone = _text(r.get('reference_name')), _text(r.get('reference_phone'))
        if ref_phone and len(''.join(ch for ch in ref_phone if ch.isdigit())) < 10:
            errs.append('Reference Mobile needs 10 digits')
        if ref_name and not ref_phone:
            warns.append('Reference has no mobile — no referral reward will be created')
        key = None
        if not errs:
            key = (phone_blind_index(digits), scheme.id, inv_date)
            dup = [i for i in existing.get(key, []) if i.amount_invested == amount]
            if dup:
                errs.append(f'Already in Nexora ({dup[0].name}, same mobile, scheme, date and amount)')
            elif (key, amount) in seen:
                errs.append('Same investment appears twice in this file')
            seen.add((key, amount))
        maturity = inv_date + relativedelta(months=scheme.tenure_months) if (scheme and inv_date) else None
        if maturity and maturity <= date.today():
            warns.append(f'Matured on {maturity:%d-%m-%Y} — comes in due for renewal; renew it on the website after approval')
        out.append({
            'row': n, 'ok': not errs, 'errors': errs, 'warnings': warns,
            'preview': {'name': name, 'phone': digits[-10:] if digits else phone, 'scheme': scheme.name if scheme else _text(r.get('scheme')),
                        'amount': str(amount) if amount not in (None, False) else '', 'investment_date': str(inv_date or ''),
                        'maturity_date': str(maturity or ''), 'interest_payout': payout, 'return_pct': str(pct or ''),
                        'reference': ref_name},
            'data': None if errs else {
                'scheme': scheme, 'name': name, 'phone': digits[-10:], 'email': _text(r.get('email')),
                'pan': _text(r.get('pan')).upper(), 'amount_invested': amount, 'investment_date': inv_date,
                'maturity_date': maturity, 'interest_payout': payout, 'total_return_pct': pct, 'source': source,
                'reference_name': ref_name, 'reference_phone': ref_phone, 'loi_no': _text(r.get('loi_no')),
                'security': _text(r.get('security')), 'notes': _text(r.get('notes')),
            },
        })
    return out


class InvestorImportView(APIView):
    """POST an Excel/CSV of investors.
      file        multipart upload, or {name, data(base64)} in JSON (the app)
      commit      false (default) = check only and report; true = save the rows
                  that passed (the rest are left out and listed)
    Saved rows are drafts (Approvals → Drafts): each is sent for approval once its
    signed LOI is attached there, exactly like an Add Investor draft."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        err = _manager_only(request)
        if err:
            return err
        f = request.FILES.get('file')
        if f:
            raw, name = f.read(), f.name or 'upload.xlsx'
        else:
            j = request.data.get('file') if isinstance(request.data.get('file'), dict) else None
            if not j or not j.get('data'):
                return Response({'detail': 'Attach the filled-in template.'}, status=status.HTTP_400_BAD_REQUEST)
            try:
                raw, name = base64.b64decode(j['data']), j.get('name') or 'upload.xlsx'
            except Exception:
                return Response({'detail': 'Could not read the file.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            rows = _read_rows(raw, name)
        except Exception as e:
            return Response({'detail': f'Could not read the file: {e}'}, status=status.HTTP_400_BAD_REQUEST)
        if not rows:
            return Response({'detail': 'The file has no investor rows.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(rows) > 2000:
            return Response({'detail': 'Up to 2,000 rows at a time.'}, status=status.HTTP_400_BAD_REQUEST)
        checked = _check(rows, request)
        commit = str(request.data.get('commit', '')).lower() in ('1', 'true', 'yes')
        created = []
        if commit:
            with transaction.atomic():
                for c in checked:
                    if not c['ok']:
                        continue
                    d = c['data']
                    inv = Investor.objects.create(
                        company=request.user.company, added_by=request.user, approval_status='draft', status='active',
                        **{k: v for k, v in d.items() if k != 'loi_no'}, loi_no=d['loi_no'],
                    )
                    created.append(inv)
                    c['investor_id'] = inv.id
            from activity.recorder import note
            note(request, f'Imported {len(created)} investor{"s" if len(created) != 1 else ""} from Excel as drafts (signed LOI to attach, then submit)'
                          + (f' · {sum(1 for c in checked if not c["ok"])} rows left out' if any(not c['ok'] for c in checked) else ''),
                 action='import', target_type='investor', module='Club 1000')
        for c in checked:
            c.pop('data', None)
        return Response({
            'rows': checked,
            'total': len(checked),
            'ok': sum(1 for c in checked if c['ok']),
            'failed': sum(1 for c in checked if not c['ok']),
            'committed': commit,
            'created': len(created),
            'amount': str(sum((Decimal(c['preview']['amount'] or 0) for c in checked if c['ok']), Decimal('0'))),
        })
