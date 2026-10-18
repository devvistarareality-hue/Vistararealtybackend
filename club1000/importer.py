"""Import Club 1000 investors from Excel.

The template (InvestorImportTemplateView) has one row per investment with dropdowns
for scheme, interest payout and source, two worked examples and a Help sheet. An
upload (InvestorImportView) is read and checked row by row first — every problem is
reported against its Excel row number — and only then saved. Each saved row is
exactly what Add Investor makes: a pending investor that waits in Approvals, whose
payout schedule, lead and referral reward are created when it is approved.
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
    ('amount_invested', 'Amount Invested', True, 'In rupees, no commas needed — the amount invested TODAY (in the current term). At least the scheme\'s minimum ticket.'),
    ('investment_date', 'Investment Date', True, 'DD-MM-YYYY (or an Excel date) the investor FIRST invested. Every investor here is active today, so Nexora works out each renewal since then from the scheme\'s tenure — the current term, its maturity and the past terms (see Past Terms).'),
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
            # With the club since 2020 — Past Terms lists every year since by itself.
            {'name': 'Ramesh Patel', 'phone': '9876500000', 'scheme': sc.name if sc else 'RISE',
             'amount_invested': 5350000, 'investment_date': date(2020, 4, 1), 'interest_payout': 'maturity',
             'total_return_pct': 15, 'source': 'walk_in'},
        ]
        for ex in examples:
            ws.append([ex.get(k, '') for k, _, _, _ in COLUMNS])
        for r in (2, 3, 4):
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

        _past_terms_sheet(wb, ws, len(schemes))

        hlp = wb.create_sheet('Help')
        hlp.append(['Column', 'Required', 'How to fill it'])
        for c in hlp[1]:
            c.font = Font(bold=True)
        for _, h, required, d in COLUMNS:
            hlp.append([h, 'Yes' if required else '', d])
        hlp.append([])
        hlp.append(['Rows 2–3 are examples — replace or delete them.'])
        hlp.append(['Each imported investor waits in Club 1000 → Approvals, like one added by hand.'])
        hlp.append(['Columns marked * (red) must be filled. A row with any of them empty is NOT imported — '
                    'it is listed with the reason so you can fix it and upload again.'])
        hlp.append(['Investors: one row per investment that is ACTIVE TODAY — today\'s amount, and the date the '
                    'investor FIRST invested as Investment Date (e.g. 01-04-2020).'])
        hlp.append(['Past Terms fills itself: from the first date and the scheme\'s tenure (RISE = 12 months) it lists '
                    'every finished term up to today — 2020-21, 2021-22 … — and the current term starts after the last one.'])
        hlp.append(['A term at today\'s amount needs nothing. Where the amount was different (e.g. ₹1 crore in 2020-21), '
                    'type it in "Principal (if different)" on that term\'s row; Interest, Renewed With and Taken Out follow.'])
        hlp.append(['Fill the Investors sheet completely first, then type any different principals — rows on Past Terms '
                    'move when Investors rows are added or removed.'])
        hlp.append(['Past payouts (each term\'s interest and any principal taken out) are created as due — mark them paid '
                    'in Payouts with the dates they were actually paid.'])
        hlp.append(['Schemes: ' + ', '.join(f'{s.name} ({s.tenure_months} mo, min ₹{int(s.min_ticket_size):,}, payouts: {", ".join(s.interest_payout_options or ["maturity"])})' for s in schemes)])
        hlp.column_dimensions['A'].width = 20
        hlp.column_dimensions['C'].width = 90

        buf = io.BytesIO()
        wb.save(buf)
        resp = HttpResponse(buf.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        resp['Content-Disposition'] = 'attachment; filename="club1000-investor-import-template.xlsx"'
        return resp


PAST_HEADERS = ['Mobile', 'Investor Name', 'Term', 'Term Start', 'Maturity', 'Scheme', 'Interest Payout', 'Return %',
                'Principal (if different)', 'Principal', 'Interest', 'Renewed With', 'Taken Out']
PAST_INPUT = 'Principal (if different)'
PAST_ROWS = 600


def _past_terms_sheet(wb, inv_ws, n_schemes):
    """Past Terms fills itself: every investor on the Investors sheet is active today, so
    each one's finished terms run from their first investment date in steps of the
    scheme's tenure up to today — one row per term, all by formula. The only thing
    typed here is a Principal for a term whose amount was different from today's."""
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    col = {k: get_column_letter(i) for i, (k, _, _, _) in enumerate(COLUMNS, start=1)}
    n = len(COLUMNS)
    key_col, cnt_col, cum_col = (get_column_letter(n + 1), get_column_letter(n + 2), get_column_letter(n + 3))
    lists = f'Lists!$A$1:$A${max(1, n_schemes)}'
    ten_of = lambda cell: f'INDEX(Lists!$D$1:$D${max(1, n_schemes)},MATCH({cell},{lists},0))'
    last = 2000
    for r in range(2, last + 1):
        ph, nm, dt, sc = (f'{col["phone"]}{r}', f'{col["name"]}{r}', f'{col["investment_date"]}{r}', f'{col["scheme"]}{r}')
        inv_ws[f'{key_col}{r}'] = f'=IF({ph}="","",TRIM({ph})&"|"&LOWER(TRIM({nm})))'
        # finished terms between the first investment and today
        inv_ws[f'{cnt_col}{r}'] = (f'=IF(OR({ph}="",{dt}="",{sc}=""),0,IFERROR(MAX(0,INT(DATEDIF({dt},TODAY(),"m")/{ten_of(sc)})),0))')
        inv_ws[f'{cum_col}{r}'] = f'=SUM(${cnt_col}$2:{cnt_col}{r})'
    for c in (key_col, cnt_col, cum_col):
        inv_ws.column_dimensions[c].hidden = True

    ws = wb.create_sheet('Past Terms', 1)
    ws.append(PAST_HEADERS)
    for i, h in enumerate(PAST_HEADERS, start=1):
        c = ws.cell(row=1, column=i)
        c.font = Font(bold=True, color='FFFFFF')
        c.fill = PatternFill('solid', fgColor='D68910' if h == PAST_INPUT else '8A94A6')
        c.alignment = Alignment(vertical='center', wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = 16
    ws.column_dimensions['B'].width = 24
    ws.row_dimensions[1].height = 30
    inv = 'Investors!'
    cum = f'{inv}${cum_col}$2:${cum_col}${last}'
    input_fill = PatternFill('solid', fgColor='FFF4D6')
    for r in range(2, PAST_ROWS + 2):
        j = r - 1
        # hidden: which Investors row this term belongs to (N) and its term number (O)
        ws[f'N{r}'] = f'=IF({j}>MAX({cum}),"",IFERROR(MATCH({j}-0.5,{cum},1),0)+2)'
        ws[f'O{r}'] = f'=IF($N{r}="","",{j}-IF($N{r}=2,0,INDEX({inv}${cum_col}:${cum_col},$N{r}-1)))'
        g = lambda key: f'INDEX({inv}${col[key]}:${col[key]},$N{r})'
        ws[f'A{r}'] = f'=IF($N{r}="","",{g("phone")})'
        ws[f'B{r}'] = f'=IF($N{r}="","",{g("name")})'
        ws[f'C{r}'] = f'=IF($N{r}="","",$O{r})'
        ws[f'F{r}'] = f'=IF($N{r}="","",{g("scheme")})'
        ten = ten_of(f'$F{r}')
        ws[f'D{r}'] = f'=IF($N{r}="","",EDATE({g("investment_date")},($O{r}-1)*{ten}))'
        ws[f'E{r}'] = f'=IF($D{r}="","",EDATE($D{r},{ten}))'
        ws[f'G{r}'] = (f'=IF($N{r}="","",IF({g("interest_payout")}="",INDEX(Lists!$E$1:$E${max(1, n_schemes)},'
                       f'MATCH($F{r},{lists},0)),{g("interest_payout")}))')
        ws[f'H{r}'] = (f'=IF($N{r}="","",IF({g("total_return_pct")}="",INDEX(Lists!$F$1:$F${max(1, n_schemes)},'
                       f'MATCH($F{r},{lists},0)),{g("total_return_pct")}))')
        ws[f'I{r}'].fill = input_fill
        ws[f'J{r}'] = f'=IF($N{r}="","",IF($I{r}<>"",$I{r},{g("amount_invested")}))'
        ws[f'K{r}'] = f'=IF($J{r}="","",ROUND($J{r}*$H{r}/100,0))'
        ws[f'L{r}'] = f'=IF($N{r}="","",IF($N{r + 1}=$N{r},$J{r + 1},{g("amount_invested")}))'
        ws[f'M{r}'] = f'=IF($L{r}="","",IF($G{r}="maturity",$J{r}+$K{r}-$L{r},$J{r}-$L{r}))'
        for c in 'DE':
            ws[f'{c}{r}'].number_format = 'DD-MM-YYYY'
        for c in 'IJKLM':
            ws[f'{c}{r}'].number_format = '#,##0'
    ws.column_dimensions['N'].hidden = True
    ws.column_dimensions['O'].hidden = True
    ws.freeze_panes = 'C2'


def _read_past(raw, name):
    """Past Terms rows where a different principal was typed: [(row, mobile, name,
    term_start, principal)]. The rest of that sheet is formulas Nexora works out itself."""
    if name.lower().endswith(('.csv', '.txt')):
        return []
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    if 'Past Terms' not in wb.sheetnames:
        return []
    rows = list(wb['Past Terms'].iter_rows(values_only=True))
    if not rows:
        return []
    head = [str(h or '').replace('*', '').strip().lower() for h in rows[0]]
    at = lambda h: head.index(h.lower()) if h.lower() in head else None
    i_mob, i_name, i_start, i_amt = at('Mobile'), at('Investor Name'), at('Term Start'), at(PAST_INPUT)
    if None in (i_mob, i_name, i_start, i_amt):
        return []
    out = []
    for n, row in enumerate(rows[1:], start=2):
        row = list(row) + [None] * len(head)
        if row[i_amt] not in (None, ''):
            out.append((n, row[i_mob], row[i_name], row[i_start], row[i_amt]))
    return out


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


def _check(rows, request, past=()):
    """Check every row; returns [{row, ok, errors, warnings, data, preview}].

    Every investor on the sheet is active today, so the Investment Date is when they
    first invested and the terms since are worked out from the scheme's tenure: the
    current one is the term running today, and each finished one before it is a past
    term at today's amount — unless the Past Terms sheet gives that term a different
    principal (`past`: mobile + name + term start → principal)."""
    override, past_errs = {}, {}
    for n, mobile, nm, start, principal in past:
        digits = ''.join(ch for ch in _text(mobile) if ch.isdigit())[-10:]
        amt, when = _num(principal), _date(start)
        if amt in (None, False) or amt <= 0:
            past_errs[n] = 'Principal (if different) is not a number'
        elif len(digits) < 10 or not _text(nm) or not when:
            past_errs[n] = 'a Principal was typed on a row with no term'
        else:
            override[(digits, _text(nm).lower(), when)] = (n, amt)
    used = set()
    schemes = {s.name.strip().lower(): s for s in _schemes(request)}
    company = request.user.company
    existing = {}
    for inv in Investor.objects.filter(company=company).exclude(approval_status__in=['rejected', 'draft']).only(
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
        # The terms since the first investment, up to the one running today.
        renewals, first_on, past_terms, maturity = 0, None, None, None
        if scheme and inv_date and inv_date <= date.today():
            ten, today = scheme.tenure_months, date.today()
            k = 0
            while inv_date + relativedelta(months=ten * (k + 1)) <= today:
                k += 1
            if k:
                first_on, renewals = inv_date, k
                past_terms = []
                for t in range(k):
                    start = inv_date + relativedelta(months=ten * t)
                    hit = override.get((digits[-10:], name.lower(), start))
                    if hit:
                        used.add(hit[0])
                    past_terms.append({'start': start.isoformat(), 'maturity': (start + relativedelta(months=ten)).isoformat(),
                                       'principal': str(hit[1] if hit else amount if amount not in (None, False) else 0)})
                inv_date = inv_date + relativedelta(months=ten * k)     # the term running today
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
        for t in past_terms or []:
            t.update(return_pct=str(pct or 0), interest_payout=payout)
        out.append({
            'row': n, 'ok': not errs, 'errors': errs, 'warnings': warns,
            'preview': {'name': name, 'phone': digits[-10:] if digits else phone, 'scheme': scheme.name if scheme else _text(r.get('scheme')),
                        'amount': str(amount) if amount not in (None, False) else '', 'investment_date': str(inv_date or ''),
                        'maturity_date': str(maturity or ''), 'interest_payout': payout, 'return_pct': str(pct or ''),
                        'reference': ref_name, 'renewal_count': renewals, 'first_invested_on': str(first_on or ''),
                        'past_terms': [{'start': t['start'], 'principal': t['principal']} for t in past_terms or []]},
            'data': None if errs else {
                'scheme': scheme, 'name': name, 'phone': digits[-10:], 'email': _text(r.get('email')),
                'pan': _text(r.get('pan')).upper(), 'amount_invested': amount, 'investment_date': inv_date,
                'maturity_date': maturity, 'interest_payout': payout, 'total_return_pct': pct, 'source': source,
                'reference_name': ref_name, 'reference_phone': ref_phone, 'loi_no': _text(r.get('loi_no')),
                'security': _text(r.get('security')), 'notes': _text(r.get('notes')),
                'renewal_count': renewals, 'first_invested_on': first_on,
                'past_terms': past_terms,
            },
        })
    for n, (bad) in past_errs.items():
        out.append({'row': n, 'sheet': 'Past Terms', 'ok': False, 'warnings': [], 'data': None, 'errors': [bad],
                    'preview': {'name': '', 'phone': '', 'scheme': '', 'amount': '', 'investment_date': '',
                                'maturity_date': '', 'interest_payout': '', 'return_pct': '', 'reference': ''}})
    for (digits, nm, start), (n, _) in override.items():
        if n not in used:
            out.append({'row': n, 'sheet': 'Past Terms', 'ok': False, 'warnings': [], 'data': None,
                        'errors': [f'No term starting {start:%d-%m-%Y} for {nm} ({digits}) — the Investors row was changed after '
                                   'typing this principal; check the row and type it again'],
                        'preview': {'name': nm, 'phone': digits, 'scheme': '', 'amount': '', 'investment_date': '',
                                    'maturity_date': '', 'interest_payout': '', 'return_pct': '', 'reference': ''}})
    return out


class InvestorImportView(APIView):
    """POST an Excel/CSV of investors.
      file        multipart upload, or {name, data(base64)} in JSON (the app)
      commit      false (default) = check only and report; true = save the rows
                  that passed (the rest are left out and listed)
    Saved rows are pending investors in Approvals, exactly like Add Investor."""
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
        try:
            past = _read_past(raw, name)
        except Exception:
            past = []
        checked = _check(rows, request, past)
        commit = str(request.data.get('commit', '')).lower() in ('1', 'true', 'yes')
        created = []
        if commit:
            with transaction.atomic():
                for c in checked:
                    if not c['ok']:
                        continue
                    d = c['data']
                    inv = Investor.objects.create(
                        company=request.user.company, added_by=request.user, approval_status='pending', status='active',
                        **{k: v for k, v in d.items() if k != 'loi_no'}, loi_no=d['loi_no'],
                    )
                    created.append(inv)
                    c['investor_id'] = inv.id
            _notify_import(request, created)
            from activity.recorder import note
            note(request, f'Imported {len(created)} investor{"s" if len(created) != 1 else ""} from Excel for approval'
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


def _notify_import(request, investors):
    """One message per approver for the whole import, not one per row."""
    if not investors:
        return
    try:
        from notifications import notify, reporting_chain
        from accounts.models import User
        from django.db.models import Q
        from sales.views import MANAGER_ROLES
        company, me = request.user.company, request.user
        ids = set()
        for inv in investors:
            ids.update(inv.scheme.investor_approvers or [])
        recipients = list(User.objects.filter(id__in=ids, company=company, is_active=True)) if ids else []
        if not recipients:
            recipients = reporting_chain(me)
        if not recipients:
            recipients = list(User.objects.filter(company=company, is_active=True).filter(Q(role__in=MANAGER_ROLES) | Q(is_staff=True)))
        total = sum((i.amount_invested or 0) for i in investors)
        msg = f'{len(investors)} investor{"s" if len(investors) != 1 else ""} imported from Excel · ₹{int(total):,} — by {me.name}'
        for u in {u.id: u for u in recipients if u and u.id != me.id}.values():
            notify(u, 'investor_approval', 'Investors to approve', msg, {'investor_id': investors[0].id})
    except Exception:
        pass
