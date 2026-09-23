import logging
import os
import hmac
import re
import secrets
from datetime import datetime, time as dt_time, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
import requests as http_requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q, Count, OuterRef, Subquery, Case, When, Value, F, BooleanField
from django.utils import timezone
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated, AllowAny

logger = logging.getLogger(__name__)

from accounts.models import User
from accounts.capabilities import (SCOPE_COMPANY, SCOPE_OWN, SCOPE_PROJECTS, SCOPE_TEAM,
                                   permissions_version,
                                   data_scope, user_can)
from accounts.permissions import is_platform_admin, scope_to_company
from sales.fields import phone_blind_index


def _resolve_company(request):
    """Return the company for the request, honouring ?company_id for platform admins."""
    cid = request.query_params.get('company_id') or request.data.get('company_id')
    if cid and is_platform_admin(request.user):
        Company = __import__('companies.models', fromlist=['Company']).Company
        return Company.objects.filter(pk=cid).first() or request.user.company
    return request.user.company
from .models import (
    Lead, LeadSource, Project, Plot, FollowUp, SiteVisit, Closure, LeadStatusHistory,
    DistributionSettings, UserAvailability, UserDistributionWeight, DistributionLog,
    SalesTeamMember, MetaWebhookConfig, MetaFormMapping,
    UserProjectAssignment, Booking, BackupSettings, BackupRecord, LeadTransfer, ChannelPartner,
)
from .serializers import (
    LeadListSerializer, LeadDetailSerializer, LeadCreateSerializer, LeadUpdateSerializer,
    LeadSourceSerializer, ProjectSerializer, PlotSerializer,
    FollowUpSerializer, SiteVisitSerializer, ClosureSerializer,
    LeadStatusHistorySerializer, BookingSerializer,
    BackupSettingsSerializer, BackupRecordSerializer, LeadTransferSerializer, ChannelPartnerSerializer,
)

PAGE_SIZE = 25

# ── Optional pagination ──────────────────────────────────────────────────────
# These list endpoints used to serialise the whole table: My Conversions alone
# pulled ~2,000 site visits and closures on every open, over mobile data. They
# now page when the caller asks (?page=1&page_size=50) and still return a plain
# list when it doesn't, so older app builds keep working unchanged.
LIST_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def maybe_paginate(request, qs, serializer_cls, **ser_kwargs):
    page = request.query_params.get('page')
    if not page:
        return Response(serializer_cls(qs, many=True, **ser_kwargs).data)
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        size = min(MAX_PAGE_SIZE, max(1, int(request.query_params.get('page_size') or LIST_PAGE_SIZE)))
    except (TypeError, ValueError):
        size = LIST_PAGE_SIZE
    total = qs.count()
    start = (page - 1) * size
    rows = qs[start:start + size]
    return Response({
        'results': serializer_cls(rows, many=True, **ser_kwargs).data,
        'count': total,
        'page': page,
        'page_size': size,
        'has_next': start + size < total,
    })



# The staff hierarchy, most senior first. Everything at or above Manager carries
# Manager's authority — a Director who could do less than the Manager reporting to
# them would be a permissions bug, so these are compared as a set rather than
# spelled out at each call site.
ROLE_HIERARCHY = ['Director', 'General Manager', 'Manager', 'Employee', 'Intern']
MANAGER_ROLES = ('Admin', 'Director', 'General Manager', 'Manager')


def is_manager_role(user):
    """True for Manager and anything senior to it (not Admin/staff — see below)."""
    return getattr(user, 'role', '') in ('Director', 'General Manager', 'Manager')


def is_admin_or_manager(user):
    return user.role in MANAGER_ROLES or user.is_staff


def _is_sales_admin(user):
    """True/hard Admin, platform staff, or a Sales Admin-Modules user — the same
    gate as Data Reset (see SalesDataResetView._is_admin), used for admin-only
    company master data like Channel Partners."""
    return bool(
        getattr(user, 'is_staff', False) or getattr(user, 'role', '') == 'Admin' or is_platform_admin(user)
        or 'Sales' in (getattr(user, 'admin_modules', None) or [])
    )


CP_MODULE = 'Channel Partner'


def has_cp_access(user):
    """Channel Partner is its own module now, granted in User Management the way
    AR and Club 1000 are. Admins always have it."""
    if not (user and getattr(user, 'is_authenticated', True)):
        return False
    return bool(
        is_admin_or_manager(user)
        or CP_MODULE in (getattr(user, 'modules', None) or [])
        or CP_MODULE in (getattr(user, 'manager_modules', None) or [])
        or CP_MODULE in (getattr(user, 'admin_modules', None) or [])
    )


def has_sales_access(user):
    """Admin/Manager, or a plain employee who's been granted the Sales module —
    used for actions (like bulk lead import) that used to be manager-only but
    shouldn't be gated tighter than "can this person use Sales at all".

    Channel Partner counts too: it is a separate module that works the same lead
    tables, scoped to partner-sourced records."""
    mods = getattr(user, 'modules', None) or []
    return is_admin_or_manager(user) or 'Sales' in mods or CP_MODULE in mods


def _designation(user):
    return (getattr(user, 'designation', '') or '').lower()


# Who does what is configured per company on the Designation master now (see
# accounts/capabilities.py). A designation nobody has configured still falls back to
# the old rules — matching text in the title — so behaviour is unchanged until a
# company edits its own designations.

def is_telecaller(user):
    """Works the calling queue."""
    return user_can(user, 'sales.pipeline.telecalling')


def is_stm(user):
    """Works site visits, closures and bookings."""
    return user_can(user, 'sales.pipeline.stm')


def is_cp(user):
    """CP Executive — an employee-level Channel Partner who sources & works their
    own leads (no Meta distribution). Scoped like an STM (by the lead's stm field)."""
    return user_can(user, 'sales.pipeline.cp')


def is_cp_manager(user):
    """A Manager whose designation starts with 'cp' (e.g. 'CP Cluster Head') —
    gets into the Channel Partner module ONLY, not the rest of Sales. Their lead
    visibility within it still comes from the existing Manager project-assignment
    mechanism (manager_project_ids/scope_leads_to_project) — no CP-specific
    scoping needed, it already applies to any Manager regardless of designation."""
    return getattr(user, 'role', '') == 'Manager' and user_can(user, 'sales.pipeline.cp_manager')


def is_cp_designated(user):
    """Anyone whose designation puts them in the Channel Partner module — a CP
    Executive (is_cp, own-records-only) or a CP-designation Manager
    (is_cp_manager, project-scoped). Used everywhere a cp_only query param
    would otherwise need to be sent by the caller: forces the CP-only pool
    server-side for both tiers regardless of which page/params reach the
    endpoint, so a CP Executive's own leads (which are CP-attributed) aren't
    excluded by the plain Sales view's "hide CP leads" default."""
    return is_cp(user) or is_cp_manager(user)


def can_access_cp_module(user):
    """Who can reach the Channel Partner module. It is a module of its own now,
    so the answer is the same as for AR or Club 1000: whoever has been granted
    it in User Management. A CP designation still gets in on its own, so nobody
    loses access on the day this ships."""
    return bool(has_cp_access(user) or _is_hard_admin(user) or is_cp_manager(user) or is_cp(user))


def cp_lead_q(prefix=''):
    """A lead belongs to the Channel Partner module if EITHER it was added
    through the CP module itself (channel_partner FK set) OR it was added
    through the regular Sales flow with Source explicitly set to "Channel
    Partner" (a plain LeadSource, no specific partner attached). `prefix`
    lets callers scope a related model (e.g. 'lead__' for FollowUp/SiteVisit/
    Closure/Booking, which don't have these fields directly)."""
    return (
        Q(**{f'{prefix}channel_partner__isnull': False}) |
        Q(**{f'{prefix}source__name__iexact': 'Channel Partner'})
    )


# ── Hierarchy-based visibility ───────────────────────────────────────────────
# Data visibility is driven by the org tree (User.reporting_manager), NOT by
# designation strings. A user sees records owned (as STM or telecaller) by
# themselves or by anyone reporting to them, transitively. This scales to any
# designation/role without code changes — you only maintain reporting_manager.

def _sees_all_company(user, request=None, include_manager_role=True):
    """Users who see ALL company data: platform admins, staff, the Admin role,
    Managers, and top-of-tree department heads (report to no one but manage others).

    A Manager sees every project's leads, follow-ups, site visits and closures — the
    reporting chain does not limit them. Bookings are the deliberate exception: that
    surface is scoped by who is *named an approver* on a project, so the booking
    views call this with include_manager_role=False and a Manager falls through to
    the approver/reporting-chain rules below. MyTeamView also opts out, keeping its
    own `?scope=all` org-chart toggle as the way to widen that view.

    A Sales Admin-Modules user (a Manager granted 'Sales' in Admin Modules) gets
    full company visibility when `request` carries `?admin_view=1` — sent only by
    the web/app's mirrored "Admin" section pages (see isSalesModuleAdmin in
    sales/layout.js). Deliberately a distinct param name from the pre-existing
    `scope` (used by MyTeamView for its own unrelated org-chart toggle) to avoid
    colliding with it. Real admins (Chinmay, Prince, platform staff) are unaffected
    — they already return True unconditionally below."""
    if is_platform_admin(user) or user.is_staff or getattr(user, 'role', '') == 'Admin':
        return True
    if include_manager_role and is_manager_role(user):
        return True
    if (request is not None and request.query_params.get('admin_view') == '1'
            and 'Sales' in (getattr(user, 'admin_modules', None) or [])):
        return True
    # Top of the tree: reports to nobody, but has active reports under them.
    if user.reporting_manager_id is None and User.objects.filter(
        company=user.company, reporting_manager_id=user.id, is_active=True
    ).exists():
        return True
    return False


def _visible_user_ids(user):
    """Requester's own id + every user reporting to them, transitively, in the same
    company. Cycle-safe (tracked via the `ids` set) and depth-capped."""
    ids = {user.id}
    frontier = [user.id]
    for _ in range(50):  # safety cap on tree depth
        children = list(
            User.objects.filter(
                company=user.company, reporting_manager_id__in=frontier, is_active=True
            ).exclude(id__in=ids).values_list('id', flat=True)
        )
        if not children:
            break
        ids.update(children)
        frontier = children
    return ids


def _is_hard_admin(user):
    """A real company/platform administrator, as opposed to someone who merely reaches
    company-wide visibility through the org tree (a top-of-tree department head) or the
    Sales admin-modules flag. Only these are exempt from per-project approver scoping."""
    return is_platform_admin(user) or user.is_staff or getattr(user, 'role', '') == 'Admin'


def _approver_project_ids(user, company):
    """Ids of projects where `user` is a configured booking approver — they should
    see every booking for that project regardless of the STM's reporting chain."""
    return [
        p.id for p in Project.objects.filter(company=company).only('id', 'booking_approvers')
        if user.id in (p.booking_approvers or [])
    ]


def _can_approve_project(user, project, company):
    """Whether `user` is a configured approver for `project`'s bookings.

    Only the managers named on the project may approve it — being a manager is not
    itself authority to approve. A project that names nobody is approvable only by a
    real admin, rather than by everyone: the previous rule returned True whenever the
    list was empty, which let any manager approve bookings for the projects that had
    not been configured yet.

    Shared by approve/reject and cancel so the two cannot drift apart — cancel undoes
    an approval, frees the plots and deletes the signed LOI, so it needs at least the
    same authority as granting the approval did.
    """
    if _is_hard_admin(user):
        return True
    project_id = getattr(project, 'id', project)
    return project_id in _approver_project_ids(user, company)


def _cp_approver_project_ids(user, company):
    """Ids of projects where `user` is a configured CP booking approver — mirrors
    _approver_project_ids but reads cp_booking_approvers, the separate list that
    gates bookings whose lead came through a Channel Partner."""
    return [
        p.id for p in Project.objects.filter(company=company).only('id', 'cp_booking_approvers')
        if user.id in (p.cp_booking_approvers or [])
    ]


def _can_approve_cp_project(user, project, company):
    """Whether `user` is a configured CP approver for `project`'s Channel-Partner-
    sourced bookings — mirrors _can_approve_project exactly, against the separate
    cp_booking_approvers list."""
    if _is_hard_admin(user):
        return True
    project_id = getattr(project, 'id', project)
    return project_id in _cp_approver_project_ids(user, company)


def _is_cp_sourced(lead_id):
    """Whether the given lead (if any) is Channel-Partner-referred — decides which
    approver list (regular vs CP) gates a booking/closure tied to it. Uses the
    same definition as cp_lead_q (channel_partner FK OR Source = "Channel
    Partner") so a lead that shows up as CP in Leads/Dashboard also gets CP
    approval routing once it's booked — they used to disagree."""
    if not lead_id:
        return False
    return Lead.objects.filter(cp_lead_q(), id=lead_id).exists()


def _is_cp_sourced_booking(lead_id, booking_source=None):
    """Whether a booking counts as Channel-Partner-sourced for approval routing.

    The booking's own Source field decides whenever it says anything at all:
    "Channel Partner" routes to the CP approvers, and any other answer routes to
    the project's regular ones. Only a booking that names no source falls back to
    its lead's attribution (the ChannelPartner directory, or Lead.source set to
    "Channel Partner" — same test as cp_lead_q), which is what keeps a CP lead
    booked without a source from disappearing out of the module.

    The fallback used to apply even when the booking contradicted it, so a deal
    revised away from Channel Partner stayed in the CP approval queue for good:
    EOI-23's R1 and R2 both read Source = Reference and were still waiting on a CP
    approver because the lead behind them was CP-attributed. What the deal says
    now is what routes it.

    cp_booking_q is the query form of this — change the two together.
    """
    src = (booking_source or '').strip().lower()
    if src == 'channel partner':
        return True
    if src:
        return False
    return _is_cp_sourced(lead_id)


def cp_booking_q(source_field='source', lead_prefix='lead__'):
    """_is_cp_sourced_booking in query form, for filtering and annotating lists.

    Kept beside it deliberately: when the two drifted, the same booking counted one
    way in a list and the other way in a permission check.
    """
    names_cp = Q(**{f'{source_field}__iexact': 'channel partner'})
    unset = Q(**{f'{source_field}__isnull': True}) | Q(**{source_field: ''})
    return names_cp | (unset & cp_lead_q(prefix=lead_prefix))


def cp_closure_q():
    """A closure belongs to the Channel Partner module when its booking does.

    Deliberately defined as "any of its bookings is a CP booking" rather than by
    restating the rule: every closure figure then splits the two books exactly
    where the approvals do. Reading only the lead, as the dashboard used to, left
    47 of Vistara's closures in neither book — counted in Sales, approved by the
    CP approvers.

    A closure recorded before its booking exists has nothing to read, so that one
    falls back to its lead — the older rule, and the only case where it applies.

    It reaches through the reverse `bookings` relation, so a `filter()` on it
    needs `.distinct()`. `exclude()` compiles to a subquery and does not.
    """
    return (Q(bookings__id__in=Booking.objects.filter(cp_booking_q()).values('id'))
            | (Q(bookings__isnull=True) & cp_lead_q(prefix='lead__')))


def _can_approve_booking(user, project_id, project, lead_id, company, booking_source=None):
    """Which approver list gates a booking or closure: a Channel-Partner-sourced
    one is approved by the project's CP approvers, everything else by its regular
    ones. No project at all → no approver-scoping check applies (matches the
    original `if b.project_id and not _can_approve_project(...)` shape at every
    call site this replaces)."""
    if not project_id:
        return True
    if _is_cp_sourced_booking(lead_id, booking_source):
        return _can_approve_cp_project(user, project, company)
    return _can_approve_project(user, project, company)


def _accounts_approver_project_ids(user, company):
    """Ids of projects where `user` is a configured ACCOUNTS approver — mirrors
    _approver_project_ids, but for the separate accounts-stage sign-off a sold
    booking now needs before it counts as approved in the Accounts module."""
    return [
        p.id for p in Project.objects.filter(company=company).only('id', 'accounts_booking_approvers')
        if user.id in (p.accounts_booking_approvers or [])
    ]


def _can_approve_accounts_project(user, project, company):
    """Whether `user` is a configured Accounts approver for `project`'s bookings.
    Mirrors _can_approve_project exactly, against accounts_booking_approvers —
    a separate list, so being a Sales/CP approver does not itself grant
    Accounts-stage authority. Real admins are exempt, same as Sales."""
    if _is_hard_admin(user):
        return True
    project_id = getattr(project, 'id', project)
    return project_id in _accounts_approver_project_ids(user, company)


def _accounts_cp_approver_project_ids(user, company):
    """CP-sourced-booking counterpart of _accounts_approver_project_ids, reading
    accounts_cp_booking_approvers."""
    return [
        p.id for p in Project.objects.filter(company=company).only('id', 'accounts_cp_booking_approvers')
        if user.id in (p.accounts_cp_booking_approvers or [])
    ]


def _can_approve_accounts_cp_project(user, project, company):
    """Mirrors _can_approve_accounts_project for Channel-Partner-sourced bookings,
    against the separate accounts_cp_booking_approvers list."""
    if _is_hard_admin(user):
        return True
    project_id = getattr(project, 'id', project)
    return project_id in _accounts_cp_approver_project_ids(user, company)


def _can_approve_accounts_booking(user, project_id, project, lead_id, company, booking_source=None):
    """Which ACCOUNTS approver list gates a booking's accounts-stage sign-off —
    mirrors _can_approve_booking's CP/regular dispatch exactly, against the
    separate accounts_* approver lists instead of the Sales/CP ones."""
    if not project_id:
        return True
    if _is_cp_sourced_booking(lead_id, booking_source):
        return _can_approve_accounts_cp_project(user, project, company)
    return _can_approve_accounts_project(user, project, company)


def can_assign_leads(user):
    """Who may hand a lead to someone else. By default telecallers, STMs and CP
    Executives cannot and everyone else can — a company can change that on the
    designation."""
    return user_can(user, 'sales.lead.assign')


# A project's floor plans, site-map zones and unit-type plans are large JSON blobs —
# 160 KB on Pratishtha 2 — and select_related('project') repeats the whole project row
# on every joined row. Listing that project's 537 units was pulling 84 MB out of the
# database to draw one unit map, which is where the ten-second wait came from. Nothing
# that lists units, bookings or leads draws a site map, so the join leaves them behind;
# the screens that do draw one load the project itself, which still carries everything.
PROJECT_BLOBS = ('project__floor_plans', 'project__site_map_zones', 'project__plot_type_plans')


def _dist_type_for(user):
    """'telecaller' | 'stm' | None for a user based on their designation."""
    if is_telecaller(user):
        return 'telecaller'
    if is_stm(user):
        return 'stm'
    return None


# Self-marked availability stays active for this many hours, then auto-resets.
AVAILABILITY_TTL_HOURS = 12



def _role_signout(company, designation):
    """Configured sign-out time for a TC/STM role, or None (no settings / other role)."""
    s = DistributionSettings.objects.filter(company=company).first()
    if not s:
        return None
    d = (designation or '').lower()
    if 'telecaller' in d or 'tele caller' in d:
        return s.tc_signout_time
    if 'stm' in d or 'sales team' in d or 'sales executive' in d:
        return s.stm_signout_time
    return None


def _availability_expires_at(user):
    """ISO timestamp when the user's availability auto-expires today = the role's
    sign-out time. None if no sign-out is configured (caller falls back to the TTL)."""
    signout = _role_signout(getattr(user, 'company', None), getattr(user, 'designation', ''))
    if signout is None:
        return None
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    tz = ZoneInfo('Asia/Kolkata')
    return _dt.combine(timezone.now().astimezone(tz).date(), signout, tzinfo=tz).isoformat()


def _availability_active(avail, user=None):
    """True if marked available *today* and it's still before the role's configured
    sign-out time — availability auto-expires at sign-out. Falls back to a 12h TTL
    when the company has no distribution sign-out configured."""
    if not avail or not avail.is_available or not avail.checked_in_at:
        return False
    from zoneinfo import ZoneInfo
    now_ist = timezone.now().astimezone(ZoneInfo('Asia/Kolkata'))
    if avail.date != now_ist.date():          # a stale prior-day record is expired
        return False
    u = user or avail.user
    signout = _role_signout(u.company, u.designation)
    if signout is None:                        # no sign-out configured → legacy 12h TTL
        return (timezone.now() - avail.checked_in_at) < timedelta(hours=AVAILABILITY_TTL_HOURS)
    return now_ist.time() < signout            # auto-expires at sign-out


# Only Manager is confined to assigned projects. Director and General Manager sit
# above the project line and always see the whole company, so they are never scoped
# and are never offered a project assignment.
PROJECT_SCOPED_ROLES = ('Manager',)


def manager_project_ids(user):
    """Projects a Manager is confined to, or None if they are not project-scoped.

    A Manager who has projects assigned sees leads, site visits and closures for
    those projects only, instead of the whole company.

    Assignment is the opt-in: a Manager with no project assigned keeps company-wide
    visibility, so introducing this does not blank out anyone's screens — you scope a
    manager by assigning them projects. Admins, platform staff, Directors and General
    Managers are never scoped.

    Bookings deliberately do not use this: a manager may book a plot on any project.
    """
    if is_platform_admin(user) or getattr(user, 'is_staff', False) or getattr(user, 'role', '') == 'Admin':
        return None
    if getattr(user, 'role', '') not in PROJECT_SCOPED_ROLES:
        return None
    pids = list(
        UserProjectAssignment.objects.filter(user=user).values_list('project_id', flat=True)
    )
    return pids or None


def scope_leads_to_project(qs, user, lead_prefix=''):
    """Narrow a lead-side queryset to the manager's assigned projects, if any."""
    pids = manager_project_ids(user)
    if pids is None:
        return qs
    return qs.filter(**{f'{lead_prefix}project__in': pids})


def scope_owned_to_role(qs, user, owner_fields, project_field=None, request=None):
    """The same restriction scope_leads_to_role applies to leads, for the records
    that hang off them — site visits, closures, follow-ups.

    They are counted from their own tables, not derived from the leads queryset,
    so without this a designation set to "own records only" saw its lead count
    shrink while Site Visits and Closures still showed the whole desk's — and the
    conversion rate on the dashboard was one scope divided by another.

    `owner_fields` are the columns that say whose record it is; `project_field`
    is how the row reaches its project, for the project-scoped case.
    """
    def by_owner(ids):
        f = Q()
        for field in owner_fields:
            f |= Q(**{f'{field}__in': ids})
        return qs.filter(f)

    def by_project():
        pids = manager_project_ids(user)
        if pids is None or project_field is None:
            return qs
        return qs.filter(**{f'{project_field}__in': pids})

    scope = data_scope(user)
    if scope == SCOPE_COMPANY:
        return qs
    if scope in (SCOPE_OWN, SCOPE_TEAM):
        return by_owner({user.id} if scope == SCOPE_OWN else _visible_user_ids(user))
    if scope == SCOPE_PROJECTS:
        return by_project()
    # Nothing configured: the rule these counts have always used.
    if _sees_all_company(user, request):
        return by_project()
    return by_owner(_visible_user_ids(user))


def scope_leads_to_role(qs, user, lead_prefix='', request=None):
    """Restrict a Lead-related queryset by org hierarchy: a user sees leads OWNED (as
    STM or telecaller) by themselves or by anyone reporting to them, transitively.
    Admins / staff / top-of-tree heads see all company data. `lead_prefix` lets callers
    scope related models (e.g. 'lead__' for SiteVisit / Closure). Pass `request` through
    so a Sales Admin-Modules user gets full data when their Admin section explicitly
    asks for it via `?scope=company` (see _sees_all_company).

    A Sales department manager (Sales in manager_modules — the same flag Club 1000
    uses for its manager-level access) additionally sees the whole unassigned pool
    (no stm, no telecaller) alongside their own team's owned leads — they need that
    visibility to review and distribute leads, not just to see what's already theirs."""
    # Frontline callers (telecaller / STM / CP) are ALWAYS restricted to leads owned
    # by them (or their own reports) — never the unassigned pool, never full-company
    # data — regardless of any manager_modules flag or reporting-line quirk. Without
    # this a telecaller who also carries the 'Sales' manager flag (mis-config) would
    # see every unrouted lead in the company as "My Leads".
    # A company can set the scope on the designation itself; '' keeps the old
    # role-and-reporting-tree behaviour below (see accounts/capabilities.py).
    scope = data_scope(user)
    if scope == SCOPE_COMPANY:
        return scope_leads_to_project(qs, user, lead_prefix)
    if scope in (SCOPE_OWN, SCOPE_TEAM):
        ids = {user.id} if scope == SCOPE_OWN else _visible_user_ids(user)
        return qs.filter(
            Q(**{f'{lead_prefix}stm__in': ids}) | Q(**{f'{lead_prefix}telecaller__in': ids})
        )
    if scope == SCOPE_PROJECTS:
        return scope_leads_to_project(qs, user, lead_prefix)
    if is_telecaller(user) or is_stm(user) or is_cp(user):
        ids = _visible_user_ids(user)
        return qs.filter(
            Q(**{f'{lead_prefix}stm__in': ids}) | Q(**{f'{lead_prefix}telecaller__in': ids})
        )
    if _sees_all_company(user, request):
        # A manager assigned to specific projects sees only those projects' leads.
        return scope_leads_to_project(qs, user, lead_prefix)
    ids = _visible_user_ids(user)
    own_filter = Q(**{f'{lead_prefix}stm__in': ids}) | Q(**{f'{lead_prefix}telecaller__in': ids})
    if 'Sales' in (getattr(user, 'manager_modules', None) or []):
        own_filter |= Q(**{f'{lead_prefix}stm__isnull': True}) & Q(**{f'{lead_prefix}telecaller__isnull': True})
    return qs.filter(own_filter)


def _lead_in_scope(request, lead_id):
    """True if the given lead belongs to the requester's company (or requester is platform admin)."""
    if not lead_id:
        return False
    return scope_to_company(Lead.objects.filter(pk=lead_id), request.user).exists()


def _project_in_scope(request, project_id):
    """True if the given project belongs to the requester's company (or requester is platform admin)."""
    if not project_id:
        return False
    return scope_to_company(Project.objects.filter(pk=project_id), request.user).exists()


class StatsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.core.cache import cache

        # Dashboard stats are ~5 COUNT/aggregate queries; cache briefly per
        # (user, company) so repeated dashboard loads don't re-hit Postgres.
        # 20s TTL keeps numbers near-live. Shared (consistent) once Redis is on.
        company_id = request.query_params.get('company_id')
        date_from  = request.query_params.get('date_from')
        date_to    = request.query_params.get('date_to')

        # Include date range, admin_view AND cp_only in cache key — otherwise a Sales
        # Admin-Modules user's team-scoped dashboard and their Admin-section (full
        # company) dashboard, or the regular Sales dashboard and the Channel Partner
        # one, would collide on the same key and serve each other's stale data.
        admin_view = request.query_params.get('admin_view') == '1'
        # A CP-designation Manager only ever gets the CP dashboard's numbers,
        # regardless of which query params reach here (see is_cp_manager) —
        # the frontend's cp_only param is the normal path but not the only one
        # that can reach this view.
        cp_only = request.query_params.get('cp_only') == 'true' or is_cp_designated(request.user)
        # The permissions version is in the key so a designation change takes
        # effect at once, instead of at the end of the cache's 20 seconds.
        _pv = permissions_version(getattr(request.user, 'company_id', None))
        cache_key = f'sales_stats:{request.user.id}:{company_id or "own"}:{date_from or ""}:{date_to or ""}:{"admin" if admin_view else "own"}:{"cp" if cp_only else "all"}:{_pv}'
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        today = timezone.localdate()
        leads_qs = scope_to_company(Lead.objects.all(), request.user)
        # Used below to exempt a CP lead handed off to a regular Sales STM/
        # telecaller from the CP/non-CP split — see the comment on leads_qs.
        own_ids = _visible_user_ids(request.user)

        # Channel Partner leads have their own module and dashboard — the main
        # Sales dashboard's counts never include them, and the CP dashboard's
        # counts are ONLY them (see LeadListView for the matching All Leads
        # behaviour, and cp_lead_q for what counts as a CP lead). Same ownership
        # exception as LeadListView: a CP lead handed off to a regular Sales
        # STM/telecaller counts in THEIR dashboard once it's theirs to work —
        # otherwise a transferred lead vanished from both the CP dashboard (no
        # longer CP-owned) and the Sales one (blanket-excluded), stranding it
        # nowhere and leaving "To Call" undercounted for the person who has it.
        if cp_only:
            leads_qs = leads_qs.filter(cp_lead_q())
        else:
            leads_qs = leads_qs.exclude(cp_lead_q() & ~Q(stm__in=own_ids) & ~Q(telecaller__in=own_ids))

        # Telecallers / STMs only see stats for leads assigned to them.
        leads_qs = scope_leads_to_role(leads_qs, request.user, request=request)

        # Platform admin: filter by a specific company (used by admin company picker)
        if company_id and is_platform_admin(request.user):
            leads_qs   = leads_qs.filter(company_id=company_id)
            sv_filter  = {'lead__company_id': company_id}
            cl_filter  = {'company_id': company_id}
            prj_filter = {'company_id': company_id}
        else:
            sv_filter = cl_filter = prj_filter = {}

        # Role/company-scoped leads WITHOUT the created_at window — used for the
        # SQL funnel count (leads that *became* warm within the window).
        leads_scope = leads_qs

        # Apply optional date range filter
        if date_from:
            leads_qs = leads_qs.filter(created_at__date__gte=date_from)
        if date_to:
            leads_qs = leads_qs.filter(created_at__date__lte=date_to)

        # Single aggregate query instead of 6 separate COUNTs
        agg = leads_qs.aggregate(
            total_leads=Count('id'),
            new_leads=Count('id', filter=Q(status='new')),
            # Genuinely unassigned: nobody owns it yet. `status='new'` alone is NOT
            # this — a lead handed to a telecaller (or self-sourced by an STM) keeps
            # status='new' until it moves warm to an STM, so counting status alone
            # reported assigned leads as unassigned. Mirrors the distributor's own
            # pool definition in _distribute(); keep the two in step.
            unassigned_leads=Count('id', filter=Q(
                status='new', telecaller__isnull=True, stm__isnull=True)),
            leads_today=Count('id', filter=Q(created_at__date=today)),
        )

        # "Called/MQL" and "Total Called" must mean "worked within this range", not
        # "created within this range and currently has a status" — same fix already
        # applied to the "Called" tab's own date filter (see _leads_worked_in_range),
        # which this used to disagree with: a lead created outside the window but
        # actually called inside it was missed here and counted there, and vice
        # versa. Without a date range there's no "worked in range" to compute, so it
        # falls back to the plain "currently called" snapshot, same as before.
        if date_from or date_to:
            status_field = 'stm_status' if (is_stm(request.user) or is_cp(request.user)) else 'telecaller_status'
            called_count = len(_leads_worked_in_range(leads_scope, status_field, '', date_from, date_to))
        else:
            called_count = leads_qs.exclude(telecaller_status='').count()

        # Status-bucket counts (hot/warm/callback/not_reachable/cold, and the STM
        # equivalents) are counted by the date the lead's status actually CHANGED
        # to that value (LeadStatusHistory), not by created_at. A lead received on
        # an earlier date but marked warm today must land in "today"'s warm count —
        # counting against the created_at-filtered leads_qs hid it entirely. Same
        # fix already applied to sql_count below and to StatsTrendView's per-day
        # warm/hot/cold rows; this brings the stat-card tiles in line with those.
        def _status_transition_count(field, value):
            qs = LeadStatusHistory.objects.filter(
                lead__in=leads_scope, field_changed=field, new_value=value)
            if date_from:
                qs = qs.filter(created_at__date__gte=date_from)
            if date_to:
                qs = qs.filter(created_at__date__lte=date_to)
            return qs.values('lead').distinct().count()

        hot_count           = _status_transition_count('telecaller_status', 'hot')
        warm_count          = _status_transition_count('telecaller_status', 'warm')
        callback_count      = _status_transition_count('telecaller_status', 'callback')
        not_reachable_count = _status_transition_count('telecaller_status', 'not_reachable')
        cold_count          = _status_transition_count('telecaller_status', 'cold')

        # STM-pipeline hot/warm/cold: a lead that got a site-visit outcome of Hot
        # still sits at stm_status='sv_done' (the outcome doesn't overwrite it —
        # see SiteVisitDetailView), so it counts toward Hot here too, dated by
        # when that visit was completed rather than a stm_status transition.
        _latest_sv_for_lead = SiteVisit.objects.filter(
            lead=OuterRef('pk'), status='completed',
        ).order_by('-visited_at')

        def _sv_outcome_lead_ids(value):
            qs = leads_scope.filter(stm_status='sv_done').annotate(
                _sv_outcome=Subquery(_latest_sv_for_lead.values('outcome')[:1]),
                _sv_visited=Subquery(_latest_sv_for_lead.values('visited_at')[:1]),
            ).filter(_sv_outcome=value)
            if date_from:
                qs = qs.filter(_sv_visited__date__gte=date_from)
            if date_to:
                qs = qs.filter(_sv_visited__date__lte=date_to)
            return set(qs.values_list('id', flat=True))

        def _effective_stm_count(value):
            direct_hist = LeadStatusHistory.objects.filter(
                lead__in=leads_scope, field_changed='stm_status', new_value=value)
            if date_from:
                direct_hist = direct_hist.filter(created_at__date__gte=date_from)
            if date_to:
                direct_hist = direct_hist.filter(created_at__date__lte=date_to)
            direct_ids = set(direct_hist.values_list('lead_id', flat=True))
            return len(direct_ids | _sv_outcome_lead_ids(value))

        # STM-pipeline counts (by stm_status) for the STM/CP dashboard.
        stm_hot_count           = _effective_stm_count('hot')
        stm_warm_count          = _effective_stm_count('warm')
        stm_cold_count          = _effective_stm_count('cold')
        stm_sv_scheduled_count  = _status_transition_count('stm_status', 'sv_scheduled')
        # The Site Visits tile reports visits that actually HAPPENED — a scheduled,
        # no-show or cancelled visit is not one. Counting every row made the tile read
        # 48 where only 26 had been done. Dated by when the visit happened, not when
        # the row was created, so a date range means "visited in this period" the same
        # way Closures means "closed in this period".
        sv_qs = scope_to_company(SiteVisit.objects.all(), request.user, 'lead__company')
        sv_qs = sv_qs.filter(status='completed', visited_at__isnull=False)
        cl_qs = scope_to_company(Closure.objects.all(), request.user, 'company').exclude(status='cancelled')
        # Same CP/non-CP split as leads_qs above — these are independent queries
        # against SiteVisit/Closure, not derived from leads_qs, so they need the
        # same cp_lead_q filter applied directly or the CP dashboard's Site
        # Visits/Closures tiles would silently show the whole company's numbers.
        if cp_only:
            sv_qs = sv_qs.filter(cp_lead_q(prefix='lead__'))
            cl_qs = cl_qs.filter(cp_closure_q()).distinct()
        else:
            # Same ownership exception as leads_qs above.
            sv_qs = sv_qs.exclude(cp_lead_q(prefix='lead__') & ~Q(stm__in=own_ids) & ~Q(referred_by_telecaller__in=own_ids))
            cl_qs = cl_qs.exclude(cp_closure_q() & ~Q(stm__in=own_ids) & ~Q(referred_by_telecaller__in=own_ids))
        # Whose visits and closures these are follows the designation's scope, the
        # same one the leads above use — otherwise the tiles disagree with each
        # other and the conversion rate compares two different populations.
        owners = ('stm', 'referred_by_telecaller')
        sv_qs = scope_owned_to_role(sv_qs, request.user, owners, 'lead__project', request)
        cl_qs = scope_owned_to_role(cl_qs, request.user, owners, 'project', request)
        # A revised booking brings its own closure and the replaced one stays on
        # the books, so without this the tile counts a revised deal twice and
        # disagrees with Approvals, which drops the superseded bookings.
        cl_qs = cl_qs.exclude(id__in=_superseded_closure_ids(_stats_company_id(request, company_id)))
        if date_from:
            sv_qs = sv_qs.filter(visited_at__date__gte=date_from)
            cl_qs = cl_qs.filter(closure_date__gte=date_from)
        if date_to:
            sv_qs = sv_qs.filter(visited_at__date__lte=date_to)
            cl_qs = cl_qs.filter(closure_date__lte=date_to)
        # Follow-up calls: a completed follow-up IS a call that was made, counted on
        # the day it was completed so the dashboard's date filter applies to it the
        # same way it does to everything else. Scoped by assignee exactly as the
        # Follow-Ups screen is, so the tile and that list agree.
        fu_qs = scope_to_company(FollowUp.objects.all(), request.user, 'lead__company')
        fu_qs = scope_owned_to_role(fu_qs, request.user, ('assigned_to',), 'lead__project', request)
        if company_id and is_platform_admin(request.user):
            fu_qs = fu_qs.filter(lead__company_id=company_id)
        fu_done = fu_qs.filter(status='completed', completed_at__isnull=False)
        if date_from:
            fu_done = fu_done.filter(completed_at__date__gte=date_from)
        if date_to:
            fu_done = fu_done.filter(completed_at__date__lte=date_to)
        followup_call_count = fu_done.count()

        # Still-open follow-ups, for the Pending / Overdue tiles. A pending row has
        # no completed_at, so these are dated on scheduled_at instead: the range then
        # reads as "due in this window" and the tiles agree with the Follow-Ups
        # screen's own Pending/Overdue chips, which count the same way. Overdue is
        # that same set narrowed to rows already past their slot.
        fu_open = fu_qs.filter(status='pending')
        if date_from:
            fu_open = fu_open.filter(scheduled_at__date__gte=date_from)
        if date_to:
            fu_open = fu_open.filter(scheduled_at__date__lte=date_to)
        followup_pending_count = fu_open.count()
        followup_overdue_count = fu_open.filter(scheduled_at__lt=timezone.now()).count()

        # "To Call" backlog: assigned leads this user has not actioned yet. Same rule
        # as the All Leads "To Call" tab (work=pending), so the tile and that tab can
        # never disagree. Both status columns are blank-not-null, so '' is the whole
        # of "not yet worked".
        if is_telecaller(request.user):
            to_call_count = leads_qs.filter(telecaller_status='').count()
        elif is_stm(request.user) or is_cp(request.user):
            to_call_count = leads_qs.filter(stm_status='').count()
        else:
            to_call_count = leads_qs.filter(status='new').count()

        cl_scoped = cl_qs.filter(**cl_filter)
        sv_scoped = sv_qs.filter(**sv_filter)
        # Active projects are the company's projects, not a count of the person's
        # own records: the same number on every dashboard, so "9 here, 11 there,
        # 4 after changing a scope" cannot happen. The only narrowing is a
        # manager assigned to particular projects, who sees theirs.
        active_projects_qs = scope_to_company(Project.objects.filter(is_active=True), request.user).filter(**prj_filter)
        _assigned = manager_project_ids(request.user)
        if _assigned is not None:
            active_projects_qs = active_projects_qs.filter(id__in=_assigned)
        sv_done, closures, active_projects = (
            sv_scoped.count(),
            cl_scoped.count(),
            active_projects_qs.count(),
        )
        # Post-visit outcome breakdown of the same completed-visits window above.
        sv_hot_count  = sv_scoped.filter(outcome='hot').count()
        sv_warm_count = sv_scoped.filter(outcome='warm').count()
        sv_cold_count = sv_scoped.filter(outcome='cold').count()
        sv_not_interested_count = sv_scoped.filter(outcome='not_interested').count()

        # SQL funnel: distinct leads that are effectively warm (stm_status → warm,
        # or still sv_done with a Warm visit outcome) in the window — same
        # definition as stm_warm_count above.
        sql_count = stm_warm_count

        # Avg closure timeline: mean days from lead arrival (created_at) to closure_date.
        _diffs = [
            (cdate - created.date()).days
            for created, cdate in cl_scoped.values_list('lead__created_at', 'closure_date')
            if created and cdate
        ]
        avg_closure_days = round(sum(_diffs) / len(_diffs), 1) if _diffs else None
        # No .only() here: LeadListSerializer reads ~11 more fields (meta_*, statuses,
        # is_duplicate, …); deferring them caused a per-field query per lead (N+1).
        recent = (leads_qs.select_related('project', 'source', 'telecaller', 'stm')
                  .defer(*PROJECT_BLOBS).order_by('-created_at')[:8])
        payload = {
            'total_leads':        agg['total_leads'],
            'new_leads':          agg['new_leads'],
            'unassigned_leads':   agg['unassigned_leads'],
            'leads_today':        agg['leads_today'],
            'called_count':       called_count,
            'to_call_count':      to_call_count,
            'followup_call_count': followup_call_count,
            'followup_pending_count': followup_pending_count,
            'followup_overdue_count': followup_overdue_count,
            # Every call made in the window: new leads worked plus follow-up calls.
            'total_called_count': called_count + followup_call_count,
            'hot_count':          hot_count,
            'warm_count':         warm_count,
            'callback_count':     callback_count,
            'not_reachable_count':not_reachable_count,
            'cold_count':         cold_count,
            'stm_hot_count':          stm_hot_count,
            'stm_warm_count':         stm_warm_count,
            'stm_cold_count':         stm_cold_count,
            'stm_sv_scheduled_count': stm_sv_scheduled_count,
            'sv_done':            sv_done,
            'sv_hot_count':  sv_hot_count,
            'sv_warm_count': sv_warm_count,
            'sv_cold_count': sv_cold_count,
            'sv_not_interested_count': sv_not_interested_count,
            'closures':           closures,
            'sql_count':          sql_count,
            'avg_closure_days':   avg_closure_days,
            'active_projects':    active_projects,
            'recent_leads':       LeadListSerializer(recent, many=True).data,
        }
        cache.set(cache_key, payload, timeout=20)
        return Response(payload)


class StatsTrendView(APIView):
    """Daily MQL and SV counts for the last 30 days (or within date_from/date_to)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models.functions import TruncDate
        from datetime import date

        company_id = request.query_params.get('company_id')
        date_from  = request.query_params.get('date_from')
        date_to    = request.query_params.get('date_to')

        today = timezone.localdate()
        if not date_from:
            date_from = str(today - timedelta(days=29))
        if not date_to:
            date_to = str(today)

        leads_qs = scope_to_company(Lead.objects.all(), request.user)
        leads_qs = scope_leads_to_role(leads_qs, request.user, request=request)
        if company_id and is_platform_admin(request.user):
            leads_qs = leads_qs.filter(company_id=company_id)

        # MQL: leads actually WORKED (their status/stm_status first left blank) on
        # each day, same as the Called/MQL tile's own date filter (StatsView —
        # _leads_worked_in_range) — not leads that merely arrived that day.
        #
        # This used to group by created_at, i.e. "of the leads that ARRIVED on each
        # day, how many currently have a status" — a lead received one day but
        # called days later (or vice versa) landed on the wrong day, or wasn't
        # worked at all yet, and disagreed with the tile above it (17 on the chart
        # against 29 on the tile, on the exact same filter). Grouping instead by
        # when the status-history row shows it first left blank is what actually
        # answers "worked on this day" — same event _leads_worked_in_range counts.
        mql_status_field = 'stm_status' if (is_stm(request.user) or is_cp(request.user)) else 'telecaller_status'
        mql_rows = (
            LeadStatusHistory.objects
            .filter(
                lead__in=leads_qs,
                field_changed=mql_status_field,
                old_value='',
                created_at__date__gte=date_from,
                created_at__date__lte=date_to,
            )
            .annotate(day=TruncDate('created_at'))
            .values('day')
            .annotate(count=Count('lead_id', distinct=True))
            .order_by('day')
        )

        sv_qs = scope_to_company(SiteVisit.objects.all(), request.user, 'lead__company')
        if not _sees_all_company(request.user, request):
            ids = _visible_user_ids(request.user)
            sv_qs = sv_qs.filter(Q(stm__in=ids) | Q(referred_by_telecaller__in=ids))
        if company_id and is_platform_admin(request.user):
            sv_qs = sv_qs.filter(lead__company_id=company_id)

        # SV: site visits created per day
        sv_rows = (
            sv_qs
            .filter(created_at__date__gte=date_from, created_at__date__lte=date_to)
            .annotate(day=TruncDate('created_at'))
            .values('day')
            .annotate(count=Count('id'))
            .order_by('day')
        )

        # Warm/SQL: count by the date the lead actually BECAME warm — the status-history
        # entry where telecaller_status changed to 'warm' — not when the lead arrived or
        # was last edited (updated_at). Scoped to the same visible leads.
        warm_rows = (
            LeadStatusHistory.objects
            .filter(
                lead__in=leads_qs,
                field_changed='telecaller_status',
                new_value='warm',
                created_at__date__gte=date_from,
                created_at__date__lte=date_to,
            )
            .annotate(day=TruncDate('created_at'))
            .values('day')
            .annotate(count=Count('id'))
            .order_by('day')
        )

        # Closures per day (by closure_date) — for the STM/CP reports charts.
        cl_qs = scope_to_company(Closure.objects.all(), request.user, 'company').exclude(status='cancelled')
        if not _sees_all_company(request.user, request):
            ids = _visible_user_ids(request.user)
            cl_qs = cl_qs.filter(Q(stm__in=ids) | Q(referred_by_telecaller__in=ids))
        if company_id and is_platform_admin(request.user):
            cl_qs = cl_qs.filter(company_id=company_id)
        # The same two books the tile above splits into, or the chart contradicts it.
        if cp_only:
            cl_qs = cl_qs.filter(cp_closure_q()).distinct()
        else:
            _own = _visible_user_ids(request.user)
            cl_qs = cl_qs.exclude(cp_closure_q() & ~Q(stm__in=_own) & ~Q(referred_by_telecaller__in=_own))
        # Same as the Closures tile: one deal, one closure, however often it was revised.
        cl_qs = cl_qs.exclude(id__in=_superseded_closure_ids(_stats_company_id(request, company_id)))
        # booking/total amounts are EncryptedDecimalField (cannot Sum() in the DB),
        # so aggregate count + amount per day in Python for the closures chart tooltip.
        closure_map = {}
        for c in (cl_qs
                  .filter(closure_date__gte=date_from, closure_date__lte=date_to)
                  .only('closure_date', 'total_amount', 'booking_amount')):
            key = str(c.closure_date)
            amt = c.total_amount or c.booking_amount or 0
            entry = closure_map.setdefault(key, {'count': 0, 'amount': 0.0})
            entry['count'] += 1
            entry['amount'] += float(amt)
        closures_ser = [
            {'date': k, 'count': v['count'], 'amount': v['amount']}
            for k, v in sorted(closure_map.items())
        ]

        # STM pipeline trends — count by the day the lead's stm_status changed to
        # hot/warm/cold (status-history), for the STM/CP dashboard & reports charts.
        def _stm_status_trend(val):
            return (
                LeadStatusHistory.objects
                .filter(lead__in=leads_qs, field_changed='stm_status', new_value=val,
                        created_at__date__gte=date_from, created_at__date__lte=date_to)
                .annotate(day=TruncDate('created_at'))
                .values('day').annotate(count=Count('id')).order_by('day')
            )
        stm_hot_rows  = _stm_status_trend('hot')
        stm_warm_rows = _stm_status_trend('warm')
        stm_cold_rows = _stm_status_trend('cold')

        def _ser(rows):
            return [{'date': str(r['day']), 'count': r['count']} for r in rows]

        return Response({
            'mql':      _ser(mql_rows),
            'sv':       _ser(sv_rows),
            'warm':     _ser(warm_rows),
            'closures': closures_ser,
            'stm_hot':  _ser(stm_hot_rows),
            'stm_warm': _ser(stm_warm_rows),
            'stm_cold': _ser(stm_cold_rows),
            'date_from': date_from,
            'date_to':   date_to,
        })


def _leads_by_status_transition(leads_scope, field, value, date_from, date_to):
    """Lead ids whose `field` changed to `value` within [date_from, date_to] —
    mirrors StatsView's transition-count logic so a dashboard tile's date range
    and a click-through list filter mean the same thing."""
    hist = LeadStatusHistory.objects.filter(
        lead__in=leads_scope, field_changed=field, new_value=value)
    if date_from:
        hist = hist.filter(created_at__date__gte=date_from)
    if date_to:
        hist = hist.filter(created_at__date__lte=date_to)
    return set(hist.values_list('lead_id', flat=True))


def _leads_worked_in_range(leads_scope, field, uncalled_value, date_from, date_to):
    """Lead ids whose `field` transitioned OUT of `uncalled_value` for the
    FIRST time within [date_from, date_to] — i.e. when they were first
    actually called/worked. Powers the "Called" tab's date filter so a lead
    worked today isn't attributed to whenever it happened to be created —
    e.g. a lead that arrived Monday while the rep was on leave, then got
    called once they were back Tuesday, is Tuesday's work, not Monday's.

    Deliberately keyed on old_value=uncalled_value (the transition AWAY from
    "never touched"), not "any change to a real value" — a lead is only ever
    Called once. Editing its status again later (a second call, a status
    correction) does not recount it as Called on that later day too;
    re-engaging with an already-called lead is what Follow-Ups are for."""
    hist = LeadStatusHistory.objects.filter(
        lead__in=leads_scope, field_changed=field, old_value=uncalled_value)
    if date_from:
        hist = hist.filter(created_at__date__gte=date_from)
    if date_to:
        hist = hist.filter(created_at__date__lte=date_to)
    return set(hist.values_list('lead_id', flat=True))


def _leads_by_sv_outcome(leads_scope, value, date_from, date_to):
    """Lead ids still at stm_status='sv_done' whose latest completed visit outcome
    matches `value`, dated by when that visit happened — mirrors StatsView's
    sv-outcome union for the effective hot/warm/cold count."""
    latest_sv = SiteVisit.objects.filter(
        lead=OuterRef('pk'), status='completed',
    ).order_by('-visited_at')
    qs = leads_scope.filter(stm_status='sv_done').annotate(
        _sv_outcome=Subquery(latest_sv.values('outcome')[:1]),
        _sv_visited=Subquery(latest_sv.values('visited_at')[:1]),
    ).filter(_sv_outcome=value)
    if date_from:
        qs = qs.filter(_sv_visited__date__gte=date_from)
    if date_to:
        qs = qs.filter(_sv_visited__date__lte=date_to)
    return set(qs.values_list('id', flat=True))


def _merge_into_existing_lead(existing, validated, user, can_assign):
    """Same phone + same project as an already-live lead: update that lead in
    place instead of inserting a duplicate row, regardless of who's adding it
    or who already owns it. Whoever adds it now takes over their own stage
    (telecaller or STM/CP) — an STM/CP taking over always replaces the
    previous STM/CP, by design: two people chasing the same contact should
    collapse onto one record, not fork into two.

    If the lead has a telecaller but NO STM/CP yet and an STM/CP is the one
    taking it over, that's the first handoff — the telecaller's independent
    work just got confirmed by someone else finding the same contact, which
    is exactly the signal 'warm' already means, so it's set automatically
    (reusing the same warm-transfer path a telecaller picking 'warm'
    themselves triggers, further down in post()). If an STM/CP is ALREADY
    assigned, that handoff already happened — a later STM/CP taking over from
    the previous one is a pure ownership swap on the STM side; the telecaller
    side is left untouched.
    """
    old_tc_status = existing.telecaller_status
    if is_cp(user) or is_stm(user):
        first_handoff = existing.telecaller_id and not existing.stm_id
        existing.stm = user
        existing.stm_assigned_at = timezone.now()
        if validated.get('stm_status'):
            existing.stm_status = validated['stm_status']
        if validated.get('stm_remarks'):
            existing.stm_remarks = validated['stm_remarks']
        if first_handoff and existing.telecaller_status != 'warm':
            existing.telecaller_status = 'warm'
    elif is_telecaller(user):
        existing.telecaller = user
        existing.telecaller_assigned_at = timezone.now()
        if validated.get('telecaller_status'):
            existing.telecaller_status = validated['telecaller_status']
        if validated.get('telecaller_remarks'):
            existing.telecaller_remarks = validated['telecaller_remarks']
    elif can_assign:
        # Admin/manager re-adding the same contact — apply whatever assignment
        # and status fields they explicitly submitted.
        for field in ('telecaller', 'stm', 'telecaller_status', 'telecaller_remarks',
                      'stm_status', 'stm_remarks'):
            if validated.get(field):
                setattr(existing, field, validated[field])
        if validated.get('telecaller') or validated.get('stm'):
            existing.telecaller_assigned_at = timezone.now() if validated.get('telecaller') else existing.telecaller_assigned_at
            existing.stm_assigned_at = timezone.now() if validated.get('stm') else existing.stm_assigned_at

    existing.duplicate_count += 1
    existing.save()
    LeadStatusHistory.objects.create(
        lead=existing, changed_by=user, field_changed='lead_merged',
        old_value='', new_value=f'Re-added by {user.name or user.username}',
    )

    # Same warm-transfer sync the create path runs: telecaller_status becoming
    # 'warm' here (set above) moves the lead into the STM pipeline exactly like
    # a telecaller picking 'warm' themselves would — except the STM is already
    # the one who just took ownership above, so no auto-distribution is needed.
    if old_tc_status != 'warm' and existing.telecaller_status == 'warm' and existing.status != 'warm_transferred':
        old_status = existing.status
        existing.status = 'warm_transferred'
        existing.save(update_fields=['status'])
        LeadStatusHistory.objects.create(
            lead=existing, changed_by=user,
            field_changed='status', old_value=old_status, new_value=existing.status,
        )
        LeadStatusHistory.objects.create(
            lead=existing, changed_by=user,
            field_changed='warm_transfer', old_value='', new_value='Transferred to STM',
        )
    if existing.stm_status and existing.status != existing.stm_status:
        old_status = existing.status
        existing.status = existing.stm_status
        existing.save(update_fields=['status'])
        LeadStatusHistory.objects.create(
            lead=existing, changed_by=user,
            field_changed='status', old_value=old_status, new_value=existing.status,
        )
    existing.refresh_from_db()
    return existing


class LeadListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Defer heavy text blobs not needed for list view
        qs = scope_to_company(
            Lead.objects.select_related('project', 'source', 'telecaller', 'stm').defer(*PROJECT_BLOBS),
            request.user,
        ).defer(
            'telecaller_remarks', 'stm_remarks', 'requirement',
            'preferred_location', 'budget_min', 'budget_max',
        )

        # Channel Partner leads are their own module — the main Sales "All Leads"
        # never shows them, only the Channel Partner section itself does (via
        # cp_only/channel_partner_id below). Keeps the two pools disjoint in both
        # directions: a CP-sourced lead never lands in a regular Sales view, and a
        # regular lead never leaks into the CP one. See cp_lead_q for what counts
        # as a CP lead (channel_partner FK set OR Source explicitly "Channel Partner").
        # A CP-designation Manager or CP Executive is boxed into the CP pool
        # unconditionally (see is_cp_designated) — not just when the CP
        # module's own pages happen to send cp_only, or navigating straight to
        # this endpoint's plain /sales/leads page would exclude their own
        # (CP-attributed) leads entirely instead of showing them.
        #
        # Exception: a CP lead handed off to a regular Sales STM/telecaller (the
        # CP Cluster Head's "Assign STM") stops being CP-exclusive the moment it's
        # theirs to work — the blanket exclude used to hide it from that very
        # person (and their manager) even though scope_leads_to_role below would
        # otherwise show it, since ownership is what actually governs visibility.
        # Only exempts leads owned by the viewer or their own reporting chain, not
        # every CP lead in the company — the pool stays disjoint for everyone else.
        if not (request.query_params.get('cp_only') == 'true' or request.query_params.get('channel_partner_id')
                or is_cp_designated(request.user)):
            own_ids = _visible_user_ids(request.user)
            qs = qs.exclude(cp_lead_q() & ~Q(stm__in=own_ids) & ~Q(telecaller__in=own_ids))

        # The visit's Hot/Warm/Cold outcome (most recent completed visit) — shown
        # alongside "sv done" so the list reads e.g. "SV Done · Hot" instead of
        # just the generic stage, without overwriting stm_status itself.
        latest_completed_sv_outcome = SiteVisit.objects.filter(
            lead=OuterRef('pk'), status='completed',
        ).order_by('-visited_at').values('outcome')[:1]
        qs = qs.annotate(sv_outcome=Subquery(latest_completed_sv_outcome))

        # Telecallers / STMs only see leads assigned to them.
        qs = scope_leads_to_role(qs, request.user, request=request)

        # Filters
        search = request.query_params.get('search', '').strip()
        if search:
            # name/phone/email are encrypted, so the database can't match on them.
            # A full 10-digit number goes through the blind index (indexed, exact).
            # Anything else -- a partial number, a name fragment -- is matched in
            # Python over the already company/role-scoped rows, then fed back as an
            # id filter so every downstream filter, sort and page still works.
            # Measured at ~8us per decrypted value; a 11k-lead company costs ~250ms
            # and only on an explicit search.
            digits = ''.join(c for c in search if c.isdigit())
            if len(digits) >= 10 and not any(c.isalpha() for c in search):
                qs = qs.filter(phone_key=phone_blind_index(digits))
            else:
                needle = search.lower()
                hits = [
                    pk for pk, nm, ph, em in qs.values_list('id', 'name', 'phone', 'email')
                    if needle in (nm or '').lower()
                    or needle in (ph or '').lower()
                    or needle in (em or '').lower()
                ]
                qs = qs.filter(id__in=hits)

        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        date_from_param = request.query_params.get('date_from')
        date_to_param = request.query_params.get('date_to')
        # A dashboard tile like "Warm/SQL" counts leads that BECAME that status
        # within the selected date range (see StatsView), not leads currently
        # sitting at that status — so its click-through must filter the same way,
        # or a lead received earlier but marked warm today would be missing from
        # one and present in the other. Only kicks in when a date range is
        # actually given; a bare status filter (no date) still means "currently
        # this status", which is its own useful, unrelated view.
        skip_created_at_filter = False
        telecaller_status_filter = request.query_params.get('telecaller_status')
        TRANSITION_TC_STATUSES = {'hot', 'warm', 'callback', 'not_reachable', 'cold'}
        if telecaller_status_filter:
            if (date_from_param or date_to_param) and telecaller_status_filter in TRANSITION_TC_STATUSES:
                ids = _leads_by_status_transition(
                    qs, 'telecaller_status', telecaller_status_filter, date_from_param, date_to_param)
                qs = qs.filter(id__in=ids)
                skip_created_at_filter = True
            else:
                qs = qs.filter(telecaller_status=telecaller_status_filter)
        stm_status_filter = request.query_params.get('stm_status')
        if stm_status_filter:
            if stm_status_filter in ('hot', 'warm', 'cold'):
                if date_from_param or date_to_param:
                    direct_ids = _leads_by_status_transition(
                        qs, 'stm_status', stm_status_filter, date_from_param, date_to_param)
                    sv_ids = _leads_by_sv_outcome(qs, stm_status_filter, date_from_param, date_to_param)
                    qs = qs.filter(id__in=(direct_ids | sv_ids))
                    skip_created_at_filter = True
                else:
                    # A lead still sitting at "sv done" whose visit outcome was Hot
                    # counts as Hot too — the outcome doesn't overwrite stm_status,
                    # but it should still surface here alongside leads reclassified
                    # to hot/warm/cold directly.
                    qs = qs.filter(
                        Q(stm_status=stm_status_filter)
                        | Q(stm_status='sv_done', sv_outcome=stm_status_filter)
                    )
            elif stm_status_filter == 'sv_scheduled' and (date_from_param or date_to_param):
                ids = _leads_by_status_transition(
                    qs, 'stm_status', stm_status_filter, date_from_param, date_to_param)
                qs = qs.filter(id__in=ids)
                skip_created_at_filter = True
            else:
                qs = qs.filter(stm_status=stm_status_filter)
        # The "Called" tab's date filter must mean "worked within this range", not
        # "created within this range" — a lead that arrived while the rep was on
        # leave (say Monday) and only got called once they were back (Tuesday) is
        # Tuesday's work, not Monday's, even though it was created Monday. Only
        # kicks in when nothing above already narrowed by a specific status value
        # (that path already date-filters correctly on its own terms).
        work = request.query_params.get('work')
        if work == 'called' and (date_from_param or date_to_param) and not skip_created_at_filter:
            if is_telecaller(request.user):
                ids = _leads_worked_in_range(qs, 'telecaller_status', '', date_from_param, date_to_param)
            elif is_stm(request.user) or is_cp(request.user):
                ids = _leads_worked_in_range(qs, 'stm_status', '', date_from_param, date_to_param)
            else:
                ids = _leads_worked_in_range(qs, 'status', 'new', date_from_param, date_to_param)
            qs = qs.filter(id__in=ids)
            skip_created_at_filter = True
        project_id = request.query_params.get('project_id')
        if project_id == 'none':
            qs = qs.filter(project__isnull=True)   # unmapped leads (no project)
        elif project_id:
            qs = qs.filter(project_id=project_id)
        if request.query_params.get('source_id'):
            qs = qs.filter(source_id=request.query_params['source_id'])
        if request.query_params.get('channel_partner_id'):
            qs = qs.filter(channel_partner_id=request.query_params['channel_partner_id'])
        elif request.query_params.get('cp_only') == 'true' or is_cp_designated(request.user):
            # The Channel Partner section's "CP Leads" tab — every lead referred
            # by any channel partner, OR added via the regular Sales flow with
            # Source set to "Channel Partner" (see cp_lead_q).
            qs = qs.filter(cp_lead_q())
        if request.query_params.get('telecaller_id'):
            qs = qs.filter(telecaller_id=request.query_params['telecaller_id'])
        if request.query_params.get('stm_id'):
            qs = qs.filter(stm_id=request.query_params['stm_id'])
        # Drill-through for the dashboard's "Unassigned" tile. `telecaller_id`/
        # `stm_id` above only accept a concrete id, so there is no way to ask for
        # "nobody owns this" without a dedicated flag.
        if request.query_params.get('unassigned') == 'true':
            qs = qs.filter(status='new', telecaller__isnull=True, stm__isnull=True)
        if request.query_params.get('is_duplicate') == 'true':
            qs = qs.filter(is_duplicate=True)
        if not skip_created_at_filter:
            if date_from_param:
                qs = qs.filter(created_at__date__gte=date_from_param)
            if date_to_param:
                qs = qs.filter(created_at__date__lte=date_to_param)
        if request.query_params.get('campaign'):
            qs = qs.filter(meta_campaign_name__icontains=request.query_params['campaign'])

        # Platform admin: filter by a specific company (used by admin company picker)
        if request.query_params.get('company_id') and is_platform_admin(request.user):
            qs = qs.filter(company_id=request.query_params['company_id'])

        # Work split for telecaller / STM portals: separate the leads they still have
        # to call ('pending') from the ones they've already actioned ('called'),
        # keyed off their own status field. Admins/managers fall back to overall status.
        # (`work` itself was already read above, alongside the date-range handling.)
        if work == 'pending':
            if is_telecaller(request.user):
                qs = qs.filter(telecaller_status='')
            elif is_stm(request.user) or is_cp(request.user):
                qs = qs.filter(stm_status='')
            else:
                qs = qs.filter(status='new')
        elif work == 'called':
            if is_telecaller(request.user):
                qs = qs.exclude(telecaller_status='')
            elif is_stm(request.user) or is_cp(request.user):
                qs = qs.exclude(stm_status='')
            else:
                qs = qs.exclude(status='new')

        # Optional ordering override (default is newest-first from the model Meta).
        # 'pending' lists use oldest-first (FIFO) so fresh leads queue at the bottom
        # and never push down the lead currently being worked.
        ordering = request.query_params.get('ordering')
        if ordering in ('created_at', '-created_at', 'updated_at', '-updated_at', 'stm_assigned_at', '-stm_assigned_at'):
            qs = qs.order_by(ordering)

        total = qs.count()
        page = int(request.query_params.get('page', 1))
        offset = (page - 1) * PAGE_SIZE
        leads = qs[offset: offset + PAGE_SIZE]

        return Response({
            'count': total,
            'results': LeadListSerializer(leads, many=True).data,
        })

    def post(self, request):
        # Any authenticated Sales user (incl. telecallers) may add a lead.
        # Consistent with PATCH (lead update), which has no admin/manager gate.
        # Only admins/managers may assign a telecaller/STM on create; strip those
        # fields for callers (they self-source) so they can't assign to others.
        data = {k: v for k, v in request.data.items()}
        can_assign = can_assign_leads(request.user)
        if not can_assign:
            data.pop('telecaller', None)
            data.pop('stm', None)
        ser = LeadCreateSerializer(data=data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        company = request.user.company

        # If a project is supplied it must belong to the requester's company.
        project = ser.validated_data.get('project')
        if project and not _project_in_scope(request, project.id):
            return Response({'detail': 'Invalid project for your company.'}, status=status.HTTP_400_BAD_REQUEST)

        phone = ser.validated_data['phone']
        clean = ''.join(c for c in phone if c.isdigit())[-10:]
        phone_idx = phone_blind_index(clean) if clean else ''

        # Same phone + same project as an already-live lead → update that lead
        # in place instead of inserting a duplicate row (see
        # _merge_into_existing_lead). A different project is a genuinely
        # separate inquiry and falls through to the normal create path below.
        # A lead already closed/lost is left alone too — re-adding one of
        # those is a new inquiry, not the same live conversation.
        if phone_idx and project:
            same_lead = (
                Lead.objects.filter(company=company, project=project, phone_key=phone_idx)
                .exclude(status__in=('closed', 'lost'))
                .order_by('-created_at').first()
            )
            if same_lead:
                merged = _merge_into_existing_lead(same_lead, ser.validated_data, request.user, can_assign)
                return Response(LeadDetailSerializer(merged).data, status=status.HTTP_200_OK)

        # Duplicate check — match last 10 digits regardless of +91 prefix. Scoped to
        # the creator's own bucket (telecaller→their leads, STM/CP→their leads) so a
        # CP's lead is only a duplicate of another CP lead, not of someone else's.
        # Admins/managers keep the company-wide check.
        dup_qs = (
            scope_leads_to_role(scope_to_company(Lead.objects.all(), request.user), request.user)
            .filter(phone_key=phone_idx)
            if clean else Lead.objects.none()
        )
        existing = dup_qs.first()

        # Self-sourced (manually added) leads are assigned to their creator so they
        # land in that person's pipeline. Bucket follows whatever status the creator
        # actually set on the form: blank → "To Call" (they haven't spoken to the
        # lead yet), any status → "Called" (they have). This used to force a status
        # ('warm'/'callback') onto a blank field so a self-sourced lead always
        # landed in "Called" — reversed on request: a status left blank while adding
        # a lead must mean it still needs a call, for CP, STM and Telecaller alike.
        extra = {}
        if not can_assign:
            # Callers self-source: own the lead. Status is whatever they set — see above.
            if is_cp(request.user) or is_stm(request.user):
                extra['stm'] = request.user
            elif is_telecaller(request.user):
                extra['telecaller'] = request.user
        else:
            # Admin/manager assigned via the form → stamp assignment time. Status is
            # left empty so the lead lands in the assignee's "To Call" bucket — a
            # status/remarks typed alongside an assignment to someone ELSE is the
            # assigner's own note, not that person's call outcome (they haven't
            # called yet), so it's dropped here rather than silently landing the
            # lead in their "Called" bucket before they've done anything. E.g. a CP
            # Cluster Head creating a lead, assigning it straight to an STM, and
            # jotting "seems warm" as they do it used to make it look like the STM
            # had already called. Same reset LeadDetailView.patch does when an
            # EXISTING lead is later reassigned to someone else.
            picked_telecaller = ser.validated_data.get('telecaller')
            picked_stm = ser.validated_data.get('stm')
            if picked_telecaller:
                extra['telecaller_assigned_at'] = timezone.now()
                if picked_telecaller.id != request.user.id:
                    extra['telecaller_status'] = ''
                    extra['telecaller_remarks'] = ''
            if picked_stm:
                extra['stm_assigned_at'] = timezone.now()
                if picked_stm.id != request.user.id:
                    extra['stm_status'] = ''
                    extra['stm_remarks'] = ''
            # Nobody picked on the form → the creator owns it, same as the
            # self-sourced branch above. This used to fall through to distribution,
            # which silently drops ("skipped" in _distribute) any lead whose project
            # has no member of the matching designation — leaving it unassigned for
            # good. The STM slot is the right one: can_assign is only true for users
            # who are neither telecaller nor STM nor CP (see can_assign_leads), so
            # the creator here is an admin/manager/cluster head. Status stays empty
            # so it lands in their "To Call" bucket, as an assigned lead already does.
            if not picked_telecaller and not picked_stm:
                extra['stm'] = request.user
                extra['stm_assigned_at'] = timezone.now()

        lead = ser.save(
            company=company,
            is_duplicate=bool(existing),
            duplicate_of=existing if existing else None,
            **extra,
        )
        if existing:
            existing.duplicate_count += 1
            existing.save(update_fields=['duplicate_count'])

        # Optional backdate — "when did this lead actually come in" (e.g. a walk-in
        # logged a day later). created_at is auto_now_add, so it can't be set via the
        # serializer; overwrite it directly afterward, same as the bulk importer does.
        lead_date = _imp_dt(data.get('lead_date'))
        if lead_date:
            lead.created_at = lead_date
            lead.save(update_fields=['created_at'])

        _record_lead_created(lead, by=request.user)
        # A telecaller/STM/admin can set an initial TC/STM Status in the SAME request
        # that creates the lead (e.g. a telecaller working a live WhatsApp chat calls
        # it warm right away instead of adding it blank and PATCHing afterward). The
        # PATCH handler logs every status transition to LeadStatusHistory — this path
        # only ever logged 'created' (+ 'warm_transfer' below), never the underlying
        # telecaller_status/stm_status/status transitions themselves, so a lead that
        # went warm at creation was invisible to every date-ranged status filter and
        # dashboard tile (all of them read LeadStatusHistory, not the live field).
        old_status = lead.status
        creation_history = []
        if lead.telecaller_status:
            creation_history.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='telecaller_status', old_value='', new_value=lead.telecaller_status,
            ))
        if lead.stm_status:
            creation_history.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='stm_status', old_value='', new_value=lead.stm_status,
            ))
        if creation_history:
            LeadStatusHistory.objects.bulk_create(creation_history)
        # Notify the assignee when an admin/manager hand-picks them on create.
        if can_assign:
            from notifications import notify
            # Never notify the creator about their own lead — with the fallback
            # above they are usually the assignee, and "New Lead Assigned" for a
            # lead you just typed in is pure noise.
            if lead.telecaller_id and lead.telecaller_id != request.user.id:
                notify(lead.telecaller, 'new_lead', 'New Lead Assigned',
                       f'{lead.name} has been assigned to you.', {'lead_id': lead.id})
            if lead.stm_id and lead.stm_id != request.user.id:
                notify(lead.stm, 'new_lead', 'New Lead Assigned',
                       f'{lead.name} has been assigned to you.', {'lead_id': lead.id})
        # Telecaller marked the new lead "warm" → warm-transfer into the STM pipeline
        # (mirrors the PATCH behaviour): overall status = warm_transferred, then
        # auto-assign an STM. Applies whether warm came from a caller or an admin form.
        if lead.telecaller_status == 'warm' and lead.status != 'warm_transferred':
            lead.status = 'warm_transferred'
            lead.save(update_fields=['status'])
            LeadStatusHistory.objects.create(
                lead=lead, changed_by=request.user,
                field_changed='status', old_value=old_status, new_value=lead.status,
            )
            LeadStatusHistory.objects.create(
                lead=lead, changed_by=request.user,
                field_changed='warm_transfer', old_value='', new_value='Transferred to STM',
            )
        # A lead that starts directly in the STM pipeline (self-sourced by an STM/CP,
        # or given an initial STM Status on the create form — e.g. a Channel Partner
        # lead) has Overall mirror STM Status immediately, same as every later PATCH
        # already does (see LeadDetailView.patch).
        if lead.stm_status and lead.status != lead.stm_status:
            old_status = lead.status
            lead.status = lead.stm_status
            lead.save(update_fields=['status'])
            LeadStatusHistory.objects.create(
                lead=lead, changed_by=request.user,
                field_changed='status', old_value=old_status, new_value=lead.status,
            )
        if lead.status == 'warm_transferred' and lead.stm_id is None:
            _run_distribution(lead.company, 'stm')
        # Auto-distribute to a telecaller only when the lead is still unassigned
        # (admin didn't pick one and it isn't self-sourced / warm-transferred).
        elif not lead.telecaller_id and not lead.stm_id:
            _run_distribution(company, 'telecaller')
        lead.refresh_from_db()

        return Response(LeadDetailSerializer(lead).data, status=status.HTTP_201_CREATED)


class LeadDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_lead(self, request, pk):
        try:
            qs = scope_to_company(
                Lead.objects.select_related('project', 'source', 'telecaller', 'stm').defer(*PROJECT_BLOBS),
                request.user,
            )
            # Telecallers / STMs can only open leads assigned to them.
            qs = scope_leads_to_role(qs, request.user, request=request)
            return qs.get(pk=pk)
        except Lead.DoesNotExist:
            return None

    def get(self, request, pk):
        lead = self._get_lead(request, pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        data = LeadDetailSerializer(lead).data
        # Most recent 30 events, returned oldest→newest so the timeline reads in order.
        # Tie-break by id keeps same-second events in their logical creation order
        # (e.g. status change → warm transfer → STM assigned).
        recent = list(lead.history.order_by('-created_at', '-id')[:30])
        recent.reverse()
        data['history'] = LeadStatusHistorySerializer(recent, many=True).data
        data['follow_ups'] = FollowUpSerializer(lead.follow_ups.all(), many=True).data
        data['site_visits'] = SiteVisitSerializer(lead.site_visits.all(), many=True).data
        return Response(data)

    def patch(self, request, pk):
        lead = self._get_lead(request, pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        old_status       = lead.status
        old_tc_status    = lead.telecaller_status
        old_stm_status   = lead.stm_status
        old_tc_id        = lead.telecaller_id
        old_stm_id       = lead.stm_id
        old_tc_name      = lead.telecaller.name if lead.telecaller else ''
        old_stm_name     = lead.stm.name        if lead.stm        else ''
        old_tc_remarks   = lead.telecaller_remarks
        old_stm_remarks  = lead.stm_remarks

        # Field-level write restrictions (mirrors the portal UI):
        #  - Telecallers may only write telecaller (TC) fields.
        #  - STMs may only write STM fields.
        #  - Neither may (re)assign leads. Admins/managers/Sales CRM may edit everything.
        data = {k: v for k, v in request.data.items()}
        if not can_assign_leads(request.user):
            for f in ('telecaller', 'stm'):
                data.pop(f, None)
        if is_telecaller(request.user):
            for f in ('stm', 'stm_status', 'stm_remarks'):
                data.pop(f, None)
        elif is_stm(request.user):
            for f in ('telecaller', 'telecaller_status', 'telecaller_remarks'):
                data.pop(f, None)

        ser = LeadUpdateSerializer(lead, data=data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        lead = ser.save()

        # Handing a lead to a different STM starts their work fresh — the new owner
        # hasn't called them yet, so their own stm_status/remarks can't legitimately
        # carry over from whoever had it before (or from whatever the person doing
        # the reassigning typed in the same form, which is THEIR note, not the new
        # owner's call outcome). Without this a reassigned lead kept its old status
        # and landed straight in the new STM's "Called" bucket (see LeadListView's
        # work=pending/called split, keyed on stm_status=='') even though they
        # hadn't touched it — exactly what CREATING a lead and assigning it already
        # avoids (LeadListView.post leaves stm_status empty on purpose so it lands
        # in the assignee's "To Call" bucket); this brings reassignment via PATCH
        # in line with that. Only fires on an actual change of owner, not every
        # save, and not when stm is being cleared back to nobody.
        if old_stm_id != lead.stm_id and lead.stm_id:
            lead.stm_status = ''
            lead.stm_remarks = ''
            lead.status = 'assigned'
            lead.save(update_fields=['stm_status', 'stm_remarks', 'status'])

        # A lead is "warm" when EITHER the telecaller sets TC Status = warm OR the
        # overall status is set to warm_transferred. Keep both in sync so the TC Status
        # column always shows 'warm' and Overall always shows 'warm_transferred',
        # then hand the lead to the STM pipeline. (TC's warm ≠ STM status — stm_status
        # stays blank.)
        warm_now = (
            (old_tc_status != 'warm' and lead.telecaller_status == 'warm') or
            (old_status != 'warm_transferred' and lead.status == 'warm_transferred')
        )
        if warm_now:
            sync = []
            if lead.status != 'warm_transferred':
                lead.status = 'warm_transferred'; sync.append('status')
            if lead.telecaller_status != 'warm':
                lead.telecaller_status = 'warm'; sync.append('telecaller_status')
            if sync:
                lead.save(update_fields=sync)

        # Once the lead is with sales, the Overall Status mirrors the STM's status
        # exactly (assigned → on TC assignment; warm_transferred → on TC warm; then
        # whatever the STM sets — cold, sv_scheduled, sv_done, closed, …).
        if lead.stm_status and old_stm_status != lead.stm_status:
            if lead.status != lead.stm_status:
                lead.status = lead.stm_status
                lead.save(update_fields=['status'])

        history_entries = []
        if old_status != lead.status:
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='status', old_value=old_status, new_value=lead.status,
            ))
        if old_tc_status != lead.telecaller_status:
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='telecaller_status', old_value=old_tc_status, new_value=lead.telecaller_status,
            ))
        if old_stm_status != lead.stm_status:
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='stm_status', old_value=old_stm_status, new_value=lead.stm_status,
            ))
        # Remarks are free text, not a status transition — logged so the STM (or anyone
        # else) can see exactly what the telecaller wrote and when, once the lead is
        # transferred to them. Same for STM's own remarks, for symmetry.
        # new_value is capped at 100 chars in the DB, but remarks can run much longer —
        # the full text goes in `remarks` (a TextField), new_value just holds a preview.
        if old_tc_remarks != lead.telecaller_remarks and lead.telecaller_remarks:
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user, field_changed='telecaller_remarks',
                old_value='', new_value=lead.telecaller_remarks[:100], remarks=lead.telecaller_remarks,
            ))
        if old_stm_remarks != lead.stm_remarks and lead.stm_remarks:
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user, field_changed='stm_remarks',
                old_value='', new_value=lead.stm_remarks[:100], remarks=lead.stm_remarks,
            ))
        if old_tc_id != lead.telecaller_id:
            new_tc_name = lead.telecaller.name if lead.telecaller else ''
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='telecaller', old_value=old_tc_name, new_value=new_tc_name,
            ))
            if lead.telecaller:
                from notifications import notify
                notify(lead.telecaller, 'new_lead', 'New Lead Assigned',
                       f'{lead.name} has been assigned to you.', {'lead_id': lead.id})
        if old_stm_id != lead.stm_id:
            new_stm_name = lead.stm.name if lead.stm else ''
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='stm', old_value=old_stm_name, new_value=new_stm_name,
            ))
            if lead.stm:
                from notifications import notify
                notify(lead.stm, 'new_lead', 'New Lead Assigned',
                       f'{lead.name} has been assigned to you.', {'lead_id': lead.id})
        if warm_now:
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='warm_transfer', old_value='', new_value='Transferred to STM',
            ))
        if history_entries:
            LeadStatusHistory.objects.bulk_create(history_entries)

        # Auto-assign whenever the lead is in the warm bucket and has no STM yet —
        # whether it got there via TC Status = warm OR by setting Overall Status
        # to 'warm_transferred' directly. Window-gated; no-op if no STM available.
        if lead.status == 'warm_transferred' and lead.stm_id is None:
            _run_distribution(lead.company, 'stm')
            lead.refresh_from_db()

        return Response(LeadDetailSerializer(lead).data)

    def delete(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        lead = self._get_lead(request, pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        lead.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class BulkDeleteLeadsView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        ids = request.data.get('ids', [])
        if not ids:
            return Response({'detail': 'No IDs provided.'}, status=status.HTTP_400_BAD_REQUEST)
        deleted, _ = scope_to_company(Lead.objects.filter(id__in=ids), request.user).delete()
        return Response({'deleted': deleted})


def _sync_plots(project):
    existing_count = project.plots.count()
    target = project.total_plots or 0
    # Only auto-create numbered plots if NO plots exist yet.
    # This prevents re-triggering on PATCH (e.g. after bulk typed-plot creation).
    if target > 0 and existing_count == 0:
        Plot.objects.bulk_create([
            Plot(project=project, number=str(i))
            for i in range(1, target + 1)
        ])


class ProjectListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        projects = scope_to_company(
            Project.objects.annotate(lead_count=Count('leads')).prefetch_related('plots'),
            request.user,
        )
        # A project-scoped Manager (see manager_project_ids) already only sees leads,
        # site visits and closures for their assigned project(s) — but this endpoint
        # is what every "pick a project" screen calls (Booking's Select Project,
        # the Leads/Site Visits/Closure filter dropdowns, …), and it was never
        # scoped the same way, so a Manager assigned to just one project still saw
        # every project in the company to pick from here, even though picking an
        # unauthorized one returned no data anyway.
        pids = manager_project_ids(request.user)
        if pids is not None:
            projects = projects.filter(id__in=pids)
        if request.query_params.get('active_only') == 'true':
            projects = projects.filter(is_active=True)
        if request.query_params.get('company_id') and is_platform_admin(request.user):
            projects = projects.filter(company_id=request.query_params['company_id'])
        data = ProjectSerializer(projects, many=True).data
        # The floor plans and site-map zones are the bulk of this response — 90% of it
        # on a company with drawn site maps — and nothing that lists projects draws a
        # site map. The screens that do (Manage Plots, the closure viewer, the project
        # edit form) each load one project from ProjectDetailView, which still returns
        # everything. ?full=1 asks for them here anyway, for any caller that needs the
        # whole record straight from the list.
        if request.query_params.get('full') != '1':
            for row in data:
                row.pop('floor_plans', None)
                row.pop('site_map_zones', None)
        return Response(data)

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        ser = ProjectSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        project = ser.save(company=request.user.company)
        _sync_plots(project)
        project = Project.objects.annotate(lead_count=Count('leads')).prefetch_related('plots').get(pk=project.pk)
        return Response(ProjectSerializer(project).data, status=status.HTTP_201_CREATED)


class ProjectDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            project = scope_to_company(
                Project.objects.annotate(lead_count=Count('leads')).prefetch_related('plots'),
                request.user,
            ).get(pk=pk)
        except Project.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(ProjectSerializer(project).data)

    def patch(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        # Who approves a project's bookings is an administrative setting, not a field any
        # Manager may edit — otherwise an approver restricted to one project could simply
        # add themselves to another and approve it. Mirrors the same gate the Approver
        # Setup panel uses on the client (role Admin / staff / Sales admin-module).
        if (('booking_approvers' in request.data or 'cp_booking_approvers' in request.data) and not (
            _is_hard_admin(request.user) or 'Sales' in (getattr(request.user, 'admin_modules', None) or [])
        )):
            return Response(
                {'detail': 'Only an administrator can change booking approvers.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        # Same idea for the Accounts-stage approver lists, but restricted tighter
        # than Sales' own approver setup: a real admin only, not an Accounts
        # Admin-Modules user — deciding who signs off at the money stage doesn't
        # extend to whoever was merely granted admin rights over that module.
        if (('accounts_booking_approvers' in request.data or 'accounts_cp_booking_approvers' in request.data) and not (
            _is_hard_admin(request.user)
        )):
            return Response(
                {'detail': 'Only an administrator can change Accounts approvers.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            project = scope_to_company(Project.objects.all(), request.user).get(pk=pk)
        except Project.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = ProjectSerializer(project, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        project = ser.save()
        # _sync_plots intentionally NOT called on PATCH — plots are managed via /plots/bulk/
        project = Project.objects.annotate(lead_count=Count('leads')).prefetch_related('plots').get(pk=project.pk)
        return Response(ProjectSerializer(project).data)

    def delete(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            project = scope_to_company(Project.objects.all(), request.user).get(pk=pk)
        except Project.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        project.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


PLOT_HOLD_TIMEOUT = timedelta(minutes=10)


def _release_plots(plot_ids, booking=None):
    """Release held plots back to whatever they were before the hold —
    'resale' if pre_hold_status says so, else 'available'. Every release path
    (self-release, expiry, discard draft, booking reject, closure cancel)
    goes through this, so a previously-sold unit put up for resale that gets
    tentatively held — someone testing whether it can be rebooked, or a
    booking attempt that didn't go through — returns to resale on release
    instead of silently losing that flag and defaulting to available.

    A unit another live booking still holds is NOT freed. Releasing blindly is how
    Pratishtha 102 came to read as available while Umesh's approved sale stood on it:
    an older cancelled booking on the same unit let its release put the unit back on
    the map, and a rep then drafted on it. `booking` is the one being released — its
    own revision chain is ignored, since a revision legitimately shares the unit with
    the version it replaces.

    Exception: a plot whose pre_hold_status is 'resale' was already, deliberately,
    taken off its old sold booking's books before this hold began — that 'sold'
    booking is history being kept on purpose (see _reclaim_units_with_a_live_sale),
    not a live claim, so it must not keep a later soft hold stuck forever. Kalrav 2
    plots 64/65 got wedged exactly this way: re-held after being marked resale, the
    stale hold outlived PLOT_HOLD_TIMEOUT by days because the old 'sold' booking kept
    reading as still live. A 'pending' booking on a resale plot is a real live claim
    and still blocks release.
    """
    if not plot_ids:
        return 0
    wanted = set(plot_ids)
    resale_ids = set(Plot.objects.filter(id__in=wanted, pre_hold_status='resale')
                      .values_list('id', flat=True))
    live = Booking.objects.filter(status__in=('pending', 'sold'))
    if booking is not None:
        # strict: only recorded revisions of this booking, never rows that merely look
        # alike. Freeing a unit is not the place for a guess.
        live = live.exclude(id__in=_revision_chain_ids(booking.id, booking.company, strict=True))
    still_held = set()
    for b in live.only('id', 'plot_id', 'plot_ids', 'status'):
        held = set(b.plot_ids or [])
        if b.plot_id:
            held.add(b.plot_id)
        matched = held & wanted
        if b.status == 'sold':
            matched -= resale_ids
        still_held |= matched
    free = wanted - still_held
    if not free:
        return 0
    return Plot.objects.filter(id__in=free).update(
        status=Case(When(pre_hold_status='resale', then=Value('resale')), default=Value('available')),
        held_by=None, held_at=None, pre_hold_status='',
    )


def _reclaim_units_with_a_live_sale(plots_qs):
    """Self-healing: a unit a live booking holds must never read as available.

    Three separate paths had freed a sold unit — a cancelled sibling booking's release,
    a discarded draft, a status edit in the plot editor — and each was fixed where it
    stood. This is the net under all of them, and under whatever is written next: the
    unit map reconciles itself on read, so a unit that slips back onto the map is
    corrected the moment anyone looks at the project rather than being sold twice.

    Only 'available' is reclaimed. 'resale' is a deliberate decision to offer a sold
    unit again and keeps its old booking on purpose, so it is left exactly alone.
    """
    loose = list(plots_qs.filter(status='available').values_list('id', flat=True))
    if not loose:
        return 0
    loose_set = set(loose)
    # Only this tenant's bookings can hold these units, so the scan stops at the
    # company line instead of reading every live booking on the platform — which is
    # what it did on every single unit-map load, for every company there is.
    company_ids = set(
        Project.objects.filter(plots__id__in=loose).values_list('company_id', flat=True))
    claimed = {}
    for b in (Booking.objects.filter(status__in=('pending', 'sold'), company_id__in=company_ids)
              .only('id', 'plot_id', 'plot_ids', 'status')):
        held = set(b.plot_ids or [])
        if b.plot_id:
            held.add(b.plot_id)
        for pid in held & loose_set:
            # A completed sale outranks one still waiting on an approver.
            if b.status == 'sold' or pid not in claimed:
                claimed[pid] = b.status
    fixed = 0
    for want in ('sold', 'pending'):
        ids = [pid for pid, st in claimed.items() if st == want]
        if ids:
            # A booking still pending leaves the unit spoken for, not gone.
            fixed += Plot.objects.filter(id__in=ids).update(
                status=('sold' if want == 'sold' else 'hold'),
                held_by=None, held_at=None, pre_hold_status='')
    if fixed:
        logging.getLogger(__name__).warning(
            'reclaimed %d unit(s) that read available while a live booking held them: %s',
            fixed, sorted(claimed))
    return fixed


def _release_expired_holds(plots_qs):
    """Self-healing: flip stale soft-holds (a rep selected the unit on the picker but
    never submitted) back to available before reading. Only touches held_by-tracked
    holds — never a hard hold backed by a real pending Booking (held_by is cleared at
    submission time), and never an admin's manual hold via PlotDetailView.patch (which
    never sets held_by). A hold pinned by a saved draft is also exempt — the rep is
    still mid-way through the form, not just browsing; it only frees on submit,
    discard, or an explicit release."""
    cutoff = timezone.now() - PLOT_HOLD_TIMEOUT
    candidates = list(plots_qs.filter(status='hold', held_by__isnull=False, held_at__lt=cutoff)
                               .values_list('id', 'project_id'))
    if not candidates:
        return
    candidate_ids = {pid for pid, _ in candidates}
    project_ids   = {proj for _, proj in candidates}
    pinned = set()
    for b in Booking.objects.filter(status='draft', project_id__in=project_ids).only('plot_id', 'plot_ids'):
        if b.plot_id:
            pinned.add(b.plot_id)
        pinned.update(b.plot_ids or [])
    to_expire = candidate_ids - pinned
    _release_plots(to_expire)


class PlotListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        project_id = request.query_params.get('project')
        if not project_id or not str(project_id).isdigit():
            return Response({'detail': 'A valid numeric project query param is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not _project_in_scope(request, project_id):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        _release_expired_holds(Plot.objects.filter(project_id=project_id))
        # …and the other direction: a unit a live booking holds must not be sitting on
        # the map as available, whatever put it there.
        _reclaim_units_with_a_live_sale(Plot.objects.filter(project_id=project_id))
        plots = (Plot.objects.filter(project_id=project_id)
                 .select_related('project').defer(*PROJECT_BLOBS))
        # context: can_cancel_hold is per-viewer, so the serializer needs the request.
        return Response(PlotSerializer(plots, many=True, context={'request': request}).data)


def _resync_generated_price_book(plot, before):
    """Keep a generated price book in step with the areas it was generated from.

    A unit's price book is computed from its `size`, so editing the size afterwards
    left the two disagreeing — and nothing said so. Four Pratishtha 2 shops drifted
    that way: D-SHOP18 showed 425 sq.ft on the unit map and priced at 415 in the
    booking form, a ₹1.2 lakh difference on a ₹51 lakh shop, with the form quoting
    the stale figure.

    Regenerated only when the stored book is provably the generator's own output for
    the areas it had before — recompute it from the old values and require an exact
    match. A hand-written or differently-generated book fails that test and is left
    untouched rather than silently replaced by a guess.
    """
    from .pricing import pratishtha2
    keys = ('size', 'terrace_area', 'facing', 'floor')
    if all(getattr(plot, k) == before.get(k) for k in keys):
        return
    stored = plot.price_book or {}
    if not stored:
        return

    def generated(size, terrace, facing, floor):
        area = pratishtha2.area_of(size)
        return pratishtha2.price_book_for(
            plot.number, flat_area=area, terrace_area=pratishtha2.area_of(terrace) or 0,
            sq_feet=area, facing=facing, floor=floor)

    try:
        was = generated(before.get('size'), before.get('terrace_area'),
                        before.get('facing'), before.get('floor'))
        if not was or was != stored:
            return                      # not this generator's book — leave it alone
        now = generated(plot.size, plot.terrace_area, plot.facing, plot.floor)
    except Exception:
        return
    if now and now != stored:
        plot.price_book = now
        plot.save(update_fields=['price_book'])


class PlotDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            plot = scope_to_company(Plot.objects.all(), request.user, 'project__company').get(pk=pk)
        except Plot.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = PlotSerializer(plot, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        # Freeing a unit here does not cancel the booking that holds it, so a unit
        # edited back to available could be sold again while the first sale stayed
        # live and approved. Six units ended up with two live sales that way — both
        # sides APPROVED, both closures intact, no cancellation recorded anywhere.
        # Cancelling is a real operation: it voids the signed LOI, deletes the
        # closure, reopens the lead and notifies the chain. It has to go through that
        # path, not through a status dropdown.
        #
        # 'resale' is exempt from this guard: it only ever applies to an already-sold
        # plot and is explicitly designed to keep that sold booking untouched while
        # relisting the unit (see moveToResaleFromPanel's confirm text). That booking
        # will always show up as a "holder", so checking resale here made the Move to
        # Resale button reject every plot it was ever used on.
        new_status = str(request.data.get('status') or '').strip()
        if new_status and new_status != plot.status and new_status == 'available':
            holder = next(
                (b for b in Booking.objects.filter(company=plot.project.company,
                                                   status__in=('pending', 'sold'))
                 .only('id', 'plot_id', 'plot_ids', 'client_name', 'status')
                 if plot.id in set(b.plot_ids or []) | ({b.plot_id} if b.plot_id else set())),
                None)
            if holder:
                return Response(
                    {'detail': f'{plot.number} is held by a {holder.status} booking for '
                               f'{holder.client_name} (#{holder.id}). Cancel that booking '
                               f'first — freeing the unit here would leave the sale standing.'},
                    status=status.HTTP_409_CONFLICT,
                )

        before = {k: getattr(plot, k) for k in ('size', 'terrace_area', 'facing', 'floor')}
        saved = ser.save()
        # The price is computed from the areas, so a change to them has to reach the
        # price book or the booking form goes on quoting the old one.
        _resync_generated_price_book(saved, before)
        return Response(PlotSerializer(saved, context={'request': request}).data)


class PlotHoldView(APIView):
    """A rep selecting units on the plot map — soft-reserve them immediately so no
    other rep can also select the same unit while this one is getting an LOI signed.
    Self-releases after PLOT_HOLD_TIMEOUT if never submitted (see _release_expired_holds)."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ids = [int(x) for x in (request.data.get('plot_ids') or []) if str(x).isdigit()]
        held, failed = [], []
        with transaction.atomic():
            for pid in ids:
                try:
                    plot = scope_to_company(Plot.objects.select_for_update(), request.user, 'project__company').get(pk=pid)
                except Plot.DoesNotExist:
                    failed.append({'id': pid, 'reason': 'not_found'})
                    continue
                _release_expired_holds(Plot.objects.filter(pk=pid))
                plot.refresh_from_db()
                # A 'resale' plot is a previously-sold unit an admin has put back
                # on the market — bookable exactly like 'available', just kept
                # visually distinct (purple, not green) so the team can tell a
                # fresh unit from a resale one.
                if plot.status not in ('available', 'resale'):
                    reason = 'held_by_other' if (plot.held_by_id and plot.held_by_id != request.user.id) else plot.status
                    failed.append({'id': pid, 'number': plot.number, 'reason': reason})
                    continue
                # The plot's own status is not the last word. A unit whose status was
                # wrongly freed still belongs to whoever bought it, and the rep picking
                # it on the map is the first person who would find out — by drafting a
                # booking on a unit that is already sold. Checked against the bookings
                # themselves, so selection is safe even before the map reconciles.
                claim = next(
                    (b for b in Booking.objects.filter(company=plot.project.company,
                                                       status__in=('pending', 'sold'))
                     .only('id', 'plot_id', 'plot_ids', 'status')
                     if plot.id == b.plot_id or plot.id in (b.plot_ids or [])),
                    None)
                if claim and plot.status != 'resale':
                    failed.append({'id': pid, 'number': plot.number, 'reason': claim.status})
                    continue
                plot.pre_hold_status = plot.status
                plot.status, plot.held_by, plot.held_at = 'hold', request.user, timezone.now()
                plot.save(update_fields=['status', 'held_by', 'held_at', 'pre_hold_status'])
                held.append(pid)
        return Response({'held': held, 'failed': failed})


class PlotReleaseView(APIView):
    """Release units this rep soft-held but didn't end up booking (deselected, hit
    Clear, or picked something else). No-ops on plots not held by this user — in
    particular a plot that's since become a real booking's hard hold (held_by cleared
    at submission) is silently skipped rather than accidentally freed."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ids = [int(x) for x in (request.data.get('plot_ids') or []) if str(x).isdigit()]
        mine = list(Plot.objects.filter(pk__in=ids, held_by=request.user, status='hold').values_list('id', flat=True))
        n = _release_plots(mine)
        return Response({'released': n})


class PlotCancelHoldView(APIView):
    """Cancel the soft hold on units someone has selected or drafted but not yet
    submitted.

    PlotReleaseView already frees a rep's own selections, but only their own and only
    as part of deselecting on the picker. This is the deliberate act, and it lets a
    project's booking approver clear a unit somebody else left sitting — previously
    that needed the holder to come back or the expiry to run out.

    Refuses a unit whose booking is already submitted: that is an approval to reject,
    not a hold to drop, and it leaves a record this endpoint does not.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .permissions_plot import can_cancel_plot_hold
        company = _resolve_company(request)
        ids = [int(x) for x in (request.data.get('plot_ids') or []) if str(x).isdigit()]
        if not ids and str(request.data.get('plot_id') or '').isdigit():
            ids = [int(request.data['plot_id'])]
        if not ids:
            return Response({'detail': 'No units given.'}, status=status.HTTP_400_BAD_REQUEST)

        plots = list(Plot.objects.filter(id__in=ids, project__company=company)
                     .select_related('project').defer(*PROJECT_BLOBS))
        if not plots:
            return Response({'detail': 'Unit not found.'}, status=status.HTTP_404_NOT_FOUND)

        submitted = [p.number for p in plots if p.status == Plot.HOLD and not p.held_by_id]
        if submitted:
            return Response(
                {'detail': 'Booking already submitted for %s — reject it from Approvals '
                           'instead of cancelling it here.' % ', '.join(submitted)},
                status=status.HTTP_400_BAD_REQUEST)

        allowed = [p for p in plots if can_cancel_plot_hold(request.user, p)]
        if not allowed:
            return Response({'detail': 'You cannot cancel this selection.'},
                            status=status.HTTP_403_FORBIDDEN)

        # A drafted unit's hold is pinned by the draft, so freeing the plot without
        # clearing the draft would leave the draft pointing at a unit someone else can
        # now book. Discard the drafts too, which is what the drafter's own discard
        # does — the unit is being taken back either way.
        ids_allowed = [p.id for p in allowed]
        drafts = [b for b in Booking.objects.filter(
            project__company=company, status='draft').only('id', 'plot_id', 'plot_ids')
            if b.plot_id in ids_allowed or set(b.plot_ids or []) & set(ids_allowed)]
        for b in drafts:
            b.delete()
        released = _release_plots(ids_allowed)
        return Response({
            'released': released,
            'drafts_discarded': len(drafts),
            'skipped': [p.number for p in plots if p.id not in ids_allowed],
        })


class LeadSourceListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        sources = scope_to_company(LeadSource.objects.filter(is_active=True), request.user)
        if request.query_params.get('company_id') and is_platform_admin(request.user):
            sources = sources.filter(company_id=request.query_params['company_id'])
        return Response(LeadSourceSerializer(sources, many=True).data)

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        ser = LeadSourceSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(LeadSourceSerializer(ser.save(company=request.user.company)).data, status=status.HTTP_201_CREATED)


class LeadCompanySearchView(APIView):
    """Company-wide lead lookup by name/phone — deliberately NOT scoped to the
    caller's own reporting tree the way LeadListView.get's default search is.
    Its whole point is to answer "does this client already have a lead, and
    who owns it" before someone creates a duplicate — e.g. an STM who can't
    find a telecaller's warm-transferred-but-not-yet-distributed lead in
    their own scoped list (scope_leads_to_role never shows a frontline role
    the unassigned pool), so they'd otherwise add a fresh lead that silently
    orphans that telecaller's site-visit incentive.

    Read-only, minimal fields only (name/phone/status/project/owners) — no
    remarks, budget, email, etc. Deliberately INCLUDES Channel Partner leads
    despite that pool being kept separate everywhere else (see cp_lead_q) —
    a duplicate can just as easily already exist as a CP lead, and this
    view's entire purpose is catching that before someone adds a fresh one;
    excluding CP leads defeated it. Each result is flagged is_cp so the
    caller can show it's a CP lead rather than a regular Sales one."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_sales_access(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        search = request.query_params.get('search', '').strip()
        if not search:
            return Response([])
        qs = scope_to_company(Lead.objects.all(), request.user)
        digits = ''.join(c for c in search if c.isdigit())
        if len(digits) >= 10 and not any(c.isalpha() for c in search):
            qs = qs.filter(phone_key=phone_blind_index(digits[-10:]))
        else:
            needle = search.lower()
            hits = [
                pk for pk, nm, ph in qs.values_list('id', 'name', 'phone')
                if needle in (nm or '').lower() or needle in (ph or '').lower()
            ]
            qs = qs.filter(id__in=hits)
        qs = (qs.select_related('telecaller', 'stm', 'project', 'source', 'channel_partner')
              .defer(*PROJECT_BLOBS).order_by('-created_at')[:25])
        return Response([
            {
                'id': l.id,
                'name': l.name,
                'phone': l.phone,
                'status': l.status,
                'project_id': l.project_id,
                'project_name': l.project.name if l.project_id else '',
                'telecaller_name': l.telecaller.name if l.telecaller_id else '',
                'stm_name': l.stm.name if l.stm_id else '',
                'created_at': l.created_at,
                'is_cp': bool(l.channel_partner_id or (l.source_id and l.source.name.lower() == 'channel partner')),
                'channel_partner_name': l.channel_partner.name if l.channel_partner_id else '',
            }
            for l in qs
        ])


class BackfillDuplicatesView(APIView):
    """One-time endpoint to mark existing duplicate leads based on last 10 phone digits."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=403)
        from collections import defaultdict
        # Stream rows with .iterator() so the whole Lead table is never materialised in
        # memory at once (prevents OOM on large tenants). Only id/phone are accumulated.
        leads = (
            scope_to_company(Lead.objects.all(), request.user)
            .only('id', 'phone', 'created_at')
            .order_by('created_at')
            .iterator(chunk_size=2000)
        )
        phone_map = defaultdict(list)
        for l in leads:
            clean = ''.join(c for c in (l.phone or '') if c.isdigit())[-10:]
            if clean:
                phone_map[clean].append(l.id)
        marked = 0
        for clean, ids in phone_map.items():
            if len(ids) > 1:
                original_id = ids[0]
                dup_ids = ids[1:]
                Lead.objects.filter(id__in=dup_ids).update(is_duplicate=True, duplicate_of_id=original_id)
                Lead.objects.filter(id=original_id).update(duplicate_count=len(dup_ids))
                marked += len(dup_ids)
        return Response({'marked_duplicates': marked})


class LeadSourceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            source = scope_to_company(LeadSource.objects.all(), request.user).get(pk=pk)
        except LeadSource.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        source.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ChannelPartnerListCreateView(APIView):
    """Admin-only directory of external referral partners (CP Details) — distinct
    from a 'CP Executive' employee, who manages the relationship with these."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Read access is open to anyone with Sales access (not just admins/CP
        # managers) — an STM booking a unit through the regular Sales module
        # needs this list too, to pick a Channel Partner Name from the directory
        # rather than typing it freehand. Mirrors LeadSourceListView.get, which
        # is similarly unrestricted; only creating/editing/deleting entries
        # (below) stays admin/CP-manager only.
        qs = scope_to_company(ChannelPartner.objects.all(), request.user).annotate(lead_count=Count('leads'))
        if request.query_params.get('company_id') and is_platform_admin(request.user):
            qs = qs.filter(company_id=request.query_params['company_id'])
        if request.query_params.get('category'):
            qs = qs.filter(category=request.query_params['category'])
        search = request.query_params.get('search', '').strip()
        if search:
            # name/contact_no/firm_name — matched in Python like Lead's search,
            # since name/contact_no are encrypted and can't be filtered in SQL.
            needle = search.lower()
            hits = [
                pk for pk, nm, ph, firm in qs.values_list('id', 'name', 'contact_no', 'firm_name')
                if needle in (nm or '').lower() or needle in (ph or '').lower() or needle in (firm or '').lower()
            ]
            qs = qs.filter(id__in=hits)
        return Response(ChannelPartnerSerializer(qs, many=True).data)

    def post(self, request):
        if not can_access_cp_module(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        ser = ChannelPartnerSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        cp = ser.save(company=_resolve_company(request), created_by=request.user)
        # Optional backdate — "when did this partnership actually start" — same
        # override-after-create trick as a Lead's lead_date (created_at is
        # auto_now_add, so it can't be set via the serializer).
        date_added = _imp_dt(request.data.get('date_added'))
        if date_added:
            cp.created_at = date_added
            cp.save(update_fields=['created_at'])
        return Response(ChannelPartnerSerializer(cp).data, status=status.HTTP_201_CREATED)


class ChannelPartnerDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if not can_access_cp_module(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            cp = scope_to_company(ChannelPartner.objects.all(), request.user).get(pk=pk)
        except ChannelPartner.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = ChannelPartnerSerializer(cp, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        ser.save()
        return Response(ser.data)

    def delete(self, request, pk):
        if not can_access_cp_module(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            cp = scope_to_company(ChannelPartner.objects.all(), request.user).get(pk=pk)
        except ChannelPartner.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        cp.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FollowUpListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = scope_to_company(
            FollowUp.objects.select_related('lead', 'assigned_to'),
            request.user, 'lead__company',
        )
        if not _sees_all_company(request.user, request):
            qs = qs.filter(assigned_to__in=_visible_user_ids(request.user))
        else:
            # A follow-up has no project of its own — scope through its lead.
            qs = scope_leads_to_project(qs, request.user, 'lead__')
        if request.query_params.get('company_id') and is_platform_admin(request.user):
            qs = qs.filter(lead__company_id=request.query_params['company_id'])
        if request.query_params.get('lead_id'):
            qs = qs.filter(lead_id=request.query_params['lead_id'])
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        if request.query_params.get('cp_only') == 'true' or is_cp_designated(request.user):
            qs = qs.filter(cp_lead_q(prefix='lead__'))
        return maybe_paginate(request, qs.order_by('-scheduled_at', '-id'), FollowUpSerializer)

    def post(self, request):
        ser = FollowUpSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        if not _lead_in_scope(request, request.data.get('lead')):
            return Response({'detail': 'Invalid lead for your company.'}, status=status.HTTP_400_BAD_REQUEST)
        followup = ser.save(created_by=request.user)
        if followup.assigned_to and followup.assigned_to_id != request.user.id:
            from notifications import notify
            when = followup.scheduled_at.strftime('%d %b %I:%M %p') if followup.scheduled_at else ''
            notify(followup.assigned_to, 'followup', 'New Follow-Up',
                   (f'{followup.lead.name} · {when}').strip(' ·'),
                   {'lead_id': followup.lead_id, 'followup_id': followup.id})
        return Response(FollowUpSerializer(followup).data, status=status.HTTP_201_CREATED)


class FollowUpDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        try:
            followup = scope_to_company(FollowUp.objects.all(), request.user, 'lead__company').get(pk=pk)
        except FollowUp.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = FollowUpSerializer(followup, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(FollowUpSerializer(ser.save()).data)


class SiteVisitListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = scope_to_company(
            SiteVisit.objects.select_related('lead', 'project', 'stm',
                                             'referred_by_telecaller', 'lead__telecaller')
                             .defer(*PROJECT_BLOBS),
            request.user, 'lead__company',
        )
        if not _sees_all_company(request.user, request):
            _ids = _visible_user_ids(request.user)
            qs = qs.filter(Q(stm__in=_ids) | Q(referred_by_telecaller__in=_ids))
        else:
            # A manager assigned to specific projects sees only those projects' visits.
            qs = scope_leads_to_project(qs, request.user)
        # Platform admin viewing a specific company (?company_id) — honour the filter.
        # A site visit has no company of its own; it belongs to its lead's company, the
        # same path scope_to_company uses above. Filtering company_id directly raised
        # FieldError, so this endpoint 500'd for any platform admin with a company
        # selected — which is what left My Conversions showing 0 site visits.
        cid = request.query_params.get('company_id')
        if cid and is_platform_admin(request.user):
            qs = qs.filter(lead__company_id=cid)
        if request.query_params.get('lead_id'):
            qs = qs.filter(lead_id=request.query_params['lead_id'])
        if request.query_params.get('cp_only') == 'true' or is_cp_designated(request.user):
            qs = qs.filter(cp_lead_q(prefix='lead__'))
        else:
            # The Sales module's book. A partner-sourced visit belongs to Channel
            # Partner, with the same handed-off exception the dashboard makes: a CP
            # lead passed to a Sales person is theirs to show once it is theirs to
            # work. Written as the dashboard writes it so the two cannot drift —
            # without it this list said 1,623 completed visits where the tile said
            # 1,563, the 60 partner visits being the whole of the difference.
            _own = _visible_user_ids(request.user)
            qs = qs.exclude(cp_lead_q(prefix='lead__') & ~Q(stm__in=_own) & ~Q(referred_by_telecaller__in=_own))
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        # Headline counts without shipping the rows: the app's stat tiles used to
        # be derived from the full list, which is the reason it downloaded it.
        if request.query_params.get('counts_only') == 'true':
            from django.db.models import Count
            rows = qs.values('status').annotate(n=Count('id'))
            return Response({r['status']: r['n'] for r in rows})
        return maybe_paginate(request, qs.order_by('-scheduled_at', '-id'), SiteVisitSerializer)

    def post(self, request):
        ser = SiteVisitSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        if not _lead_in_scope(request, request.data.get('lead')):
            return Response({'detail': 'Invalid lead for your company.'}, status=status.HTTP_400_BAD_REQUEST)
        sv = ser.save()
        sched = sv.scheduled_at.strftime('%d %b %I:%M %p') if sv.scheduled_at else ''
        # A visit can be created already-completed (the sv_done fallback when no
        # scheduled visit exists yet) — label it as such, with the outcome, rather
        # than always saying "Scheduled" regardless of its actual status.
        if sv.status == 'completed':
            label = f'Completed · {sv.get_outcome_display()}' if sv.outcome else 'Completed'
        else:
            label = f'Scheduled · {sched}' if sched else 'Scheduled'
        LeadStatusHistory.objects.create(
            lead=sv.lead, changed_by=request.user, field_changed='site_visit',
            old_value='', new_value=label[:100],
            remarks='Site visit scheduled' if sv.status != 'completed' else 'Site visit completed',
        )
        from notifications import notify
        for who in (sv.stm, sv.referred_by_telecaller):
            if who and who.id != request.user.id:
                notify(who, 'sv', 'Site Visit Scheduled',
                       (f'{sv.lead.name} · {sched}').strip(' ·'),
                       {'lead_id': sv.lead_id, 'sv_id': sv.id})
        return Response(SiteVisitSerializer(sv).data, status=status.HTTP_201_CREATED)


class SiteVisitDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        try:
            sv = scope_to_company(SiteVisit.objects.all(), request.user, 'lead__company').get(pk=pk)
        except SiteVisit.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        old_status = sv.status
        # Marking a visit Done must record what came of it — an outcome and remarks,
        # not just a status flip — so the pipeline can tell an interested walk-in
        # from a dead one. Checked against the merged (existing + incoming) values so
        # a client that already set these on an earlier PATCH isn't forced to resend.
        if request.data.get('status') == 'completed' and old_status != 'completed':
            new_outcome = request.data.get('outcome', sv.outcome)
            new_remarks = request.data.get('remarks', sv.remarks)
            if not new_outcome or not str(new_remarks or '').strip():
                return Response(
                    {'detail': 'Outcome and remarks are required to mark a site visit as done.'},
                    status=status.HTTP_400_BAD_REQUEST)
        ser = SiteVisitSerializer(sv, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        sv = ser.save()
        if sv.status != old_status:
            # Include the outcome in the logged transition — "Completed · Hot" —
            # so the lead's history timeline shows what came of the visit, not
            # just that it happened.
            new_value = sv.get_status_display()
            if sv.status == 'completed' and sv.outcome:
                new_value = f'Completed · {sv.get_outcome_display()}'
            LeadStatusHistory.objects.create(
                lead=sv.lead, changed_by=request.user, field_changed='site_visit',
                old_value=old_status, new_value=new_value,
                remarks='Site visit updated',
            )
            if sv.status == 'completed':
                # Telecaller who referred the lead + the STM both hear that the SV is done.
                from notifications import notify
                for who in (sv.referred_by_telecaller, sv.stm):
                    if who and who.id != request.user.id:
                        notify(who, 'sv_done', 'Site Visit Done',
                               f"{sv.lead.name}'s site visit is complete.",
                               {'lead_id': sv.lead_id, 'sv_id': sv.id})
        return Response(SiteVisitSerializer(sv).data)


class ClosureListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = scope_to_company(
            Closure.objects.select_related('lead', 'project', 'stm',
                                           'referred_by_telecaller', 'lead__telecaller')
                           .defer(*PROJECT_BLOBS),
            request.user, 'company',
        )
        if not _sees_all_company(request.user, request):
            _ids = _visible_user_ids(request.user)
            qs = qs.filter(Q(stm__in=_ids) | Q(referred_by_telecaller__in=_ids))
        else:
            # A manager assigned to specific projects sees only those projects' closures.
            qs = scope_leads_to_project(qs, request.user)
        # Platform admin viewing a specific company (?company_id) — honour the filter.
        cid = request.query_params.get('company_id')
        if cid and is_platform_admin(request.user):
            qs = qs.filter(company_id=cid)
        if request.query_params.get('cp_only') == 'true' or is_cp_designated(request.user):
            # The Channel Partner book — see cp_closure_q for what counts as one.
            qs = qs.filter(cp_closure_q()).distinct()
        else:
            # The Sales module's book. A partner-sourced closure belongs to Channel
            # Partner, with the same handed-off exception the dashboard makes: a CP
            # lead passed to a Sales person is theirs to show once it is theirs to
            # work. Written as the dashboard writes it so the two cannot drift.
            _own = _visible_user_ids(request.user)
            qs = qs.exclude(cp_closure_q() & ~Q(stm__in=_own) & ~Q(referred_by_telecaller__in=_own))
        # A cancelled closure is a deal that came off the books, not a conversion —
        # the dashboard has always left it out, and this list counted it.
        qs = qs.exclude(status='cancelled')
        # One deal, one closure. Revising a booking issues the revision its own
        # closure and leaves the replaced one behind, so a revised deal was listed
        # twice here while Approvals showed it once.
        qs = qs.exclude(id__in=_superseded_closure_ids(_stats_company_id(request, cid)))
        if request.query_params.get('counts_only') == 'true':
            return Response({'total': qs.count()})
        return maybe_paginate(request, qs.order_by('-closure_date', '-id'), ClosureSerializer)

    def post(self, request):
        ser = ClosureSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        if not _lead_in_scope(request, request.data.get('lead')):
            return Response({'detail': 'Invalid lead for your company.'}, status=status.HTTP_400_BAD_REQUEST)
        closure = ser.save()
        parts = [closure.get_status_display()]
        unit = f'{closure.unit_type} {closure.unit_no}'.strip()
        if unit:
            parts.append(unit)
        if closure.total_amount:
            parts.append(f'₹{closure.total_amount:g}')
        LeadStatusHistory.objects.create(
            lead=closure.lead, changed_by=request.user, field_changed='closure',
            old_value='', new_value=' · '.join(parts)[:100], remarks='Closure recorded',
        )
        if closure.stm:
            from notifications import notify_many, reporting_chain
            notify_many(reporting_chain(closure.stm), 'closure', 'New Closure',
                        (f'{closure.stm.name} closed {closure.lead.name} · {unit}').strip(' ·'),
                        {'lead_id': closure.lead_id, 'closure_id': closure.id})
        return Response(ClosureSerializer(closure).data, status=status.HTTP_201_CREATED)


class TelecallerListView(APIView):
    """Users for lead assignment. Filters by User.designation icontains crm_role param.
    Falls back to all Sales-module users if no designation match found."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        crm_role = request.query_params.get('crm_role')
        cid      = request.query_params.get('company_id')
        if is_platform_admin(request.user):
            if cid:
                from companies.models import Company as Co
                co = Co.objects.filter(pk=cid).first()
                base_qs = User.objects.filter(company=co, is_active=True) if co else User.objects.none()
            else:
                base_qs = User.objects.filter(is_active=True)
        else:
            base_qs = User.objects.filter(company=request.user.company, is_active=True)
        sales_qs = base_qs.filter(modules__contains=['Sales']).order_by('name')

        if crm_role in ('telecaller', 'stm'):
            users = base_qs.filter(designation__icontains=crm_role).order_by('name')
            if not users.exists():
                users = sales_qs
        elif crm_role == 'cp':
            # CP executives (channel partners) — for CP managers assigning leads.
            users = base_qs.filter(
                Q(designation__icontains='cp executive') | Q(designation__icontains='channel partner')
            ).order_by('name')
            if not users.exists():
                users = sales_qs
        elif crm_role == 'cp_module':
            # Everyone with access to the Channel Partner module — admins/staff/
            # Sales Admin-Modules users, CP Executives and CP-designation Managers
            # (is_cp_designated) — plus anyone who currently owns a CP lead as its
            # stm even without a CP designation (a lead transferred to a regular
            # STM, say). Not a designation substring, so filtered in Python like
            # the other cross-cutting permission checks in this file.
            company_ids = base_qs.values_list('company_id', flat=True).distinct()
            cp_lead_stm_ids = set(
                Lead.objects.filter(cp_lead_q(), company_id__in=company_ids)
                .exclude(stm__isnull=True).values_list('stm_id', flat=True)
            )
            users = sorted(
                (u for u in base_qs if _is_sales_admin(u) or is_cp_designated(u) or u.id in cp_lead_stm_ids),
                key=lambda u: u.name or '',
            )
        elif crm_role == 'accounts_module':
            # Everyone with access to the Accounts & Finance module — admins/
            # staff, an Accounts Admin-Modules user, and anyone granted the
            # 'Accounts & Finance' module (employee-level `modules` or
            # manager-level `manager_modules`) — the pool the Accounts
            # Approvers picker offers, mirroring cp_module above.
            users = sorted(
                (u for u in base_qs if _is_hard_admin(u)
                 or 'Accounts & Finance' in (getattr(u, 'admin_modules', None) or [])
                 or 'Accounts & Finance' in (getattr(u, 'modules', None) or [])
                 or 'Accounts & Finance' in (getattr(u, 'manager_modules', None) or [])),
                key=lambda u: u.name or '',
            )
        elif crm_role == 'sales_cp':
            # Same-company employees with Sales module access — for the CP
            # module's "Assign STM" dropdown, letting a CP Cluster Head hand a
            # CP lead straight to a Sales-side person, no approval step.
            # Company-scoped like every other list here (base_qs already
            # honours the caller's own company, or ?company_id for a platform
            # admin) — no separate CP-access requirement on the assignee.
            users = sales_qs
            project_id = request.query_params.get('project_id')
            if project_id:
                # Strictly the STMs assigned to THIS project (Team Users →
                # Assign, the same UserProjectAssignment used by
                # manager_project_ids) — not the opt-in "unassigned = every
                # project" fallback used elsewhere, since most employees have
                # no assignment row at all and that made this list barely
                # narrow down. Only an explicit assignment gets someone listed.
                assigned_ids = set(
                    UserProjectAssignment.objects.filter(project_id=project_id).values_list('user_id', flat=True)
                )
                users = [u for u in users if u.id in assigned_ids]
        else:
            users = sales_qs

        data = [
            {'id': u.id, 'name': u.name, 'user_code': u.user_code, 'role': u.role, 'designation': u.designation}
            for u in users
        ]
        return Response(data)


class CompanyUsersSlimView(APIView):
    """Lightweight user list for Sales CRM — only fields the UI needs, no heavy JSONField serialization."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company_id = request.query_params.get('company_id')
        company = (
            __import__('companies.models', fromlist=['Company']).Company.objects.filter(pk=company_id).first()
            if company_id and is_platform_admin(request.user)
            else request.user.company
        )
        users = (
            User.objects
            .filter(company=company, is_active=True)
            .exclude(role='Admin')
            .only('id', 'name', 'user_code', 'designation', 'role', 'phone', 'email')
            .order_by('name')
        )
        data = [{
            'id':          u.id,
            'name':        u.name,
            'user_code':   u.user_code,
            'designation': u.designation,
            'role':        u.role,
            'phone':       u.phone,
            'email':       u.email,
        } for u in users]
        return Response(data)


# ── Sales Team Members ──────────────────────────────────────────────────────
# models already imported at top of file


class SalesTeamView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        cid = request.query_params.get('company_id')
        if is_platform_admin(request.user):
            if cid:
                from companies.models import Company as Co
                company = Co.objects.filter(pk=cid).first()
                users = User.objects.filter(company=company, is_active=True, department__icontains='sales') if company else User.objects.none()
            else:
                users = User.objects.filter(is_active=True, department__icontains='sales')
        else:
            users = User.objects.filter(company=request.user.company, is_active=True, department__icontains='sales')
        users = users.order_by('name')

        data = [{
            'id':          u.id,
            'name':        u.name,
            'email':       u.email,
            'phone':       u.phone,
            'user_code':   u.user_code,
            'designation': u.designation,
            'role':        u.role,
        } for u in users]
        return Response(data)

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        user_id  = request.data.get('user_id')
        crm_role = request.data.get('crm_role', 'telecaller')
        try:
            user = User.objects.get(pk=user_id, company=request.user.company)
        except User.DoesNotExist:
            return Response({'detail': 'User not found in your company.'}, status=status.HTTP_404_NOT_FOUND)
        member, created = SalesTeamMember.objects.get_or_create(user=user, defaults={'crm_role': crm_role})
        if not created:
            member.crm_role  = crm_role
            member.is_active = True
            member.save()
        return Response({'id': member.id, 'user_id': user.id, 'name': user.name, 'crm_role': member.crm_role, 'designation': user.designation, 'user_code': user.user_code}, status=status.HTTP_201_CREATED)


class SalesTeamMemberDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            m = SalesTeamMember.objects.get(pk=pk, user__company=request.user.company)
        except SalesTeamMember.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if 'crm_role' in request.data:
            m.crm_role = request.data['crm_role']
        if 'is_active' in request.data:
            m.is_active = request.data['is_active']
        m.save()
        return Response({'id': m.id, 'crm_role': m.crm_role, 'is_active': m.is_active})

    def delete(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            m = SalesTeamMember.objects.get(pk=pk, user__company=request.user.company)
        except SalesTeamMember.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        m.is_active = False
        m.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Distribution Settings ─────────────────────────────────────────────────────
class DistributionSettingsView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_or_create(self, company):
        obj, _ = DistributionSettings.objects.get_or_create(company=company)
        return obj

    def get(self, request):
        company = _resolve_company(request)
        s = self._get_or_create(company)
        managers = list(
            User.objects.filter(company=company, is_active=True, role__in=MANAGER_ROLES)
            .exclude(role='Admin')
            .order_by('name').values('id', 'name', 'designation')
        )
        return Response({
            'tc_signin_time':   str(s.tc_signin_time)[:5],
            'tc_signout_time':  str(s.tc_signout_time)[:5],
            'stm_signin_time':  str(s.stm_signin_time)[:5],
            'stm_signout_time': str(s.stm_signout_time)[:5],
            'managers': managers,   # for the per-project booking-approver picker
            # Real pending pools for the two "N unassigned leads" lines.
            'pending': _distribution_pool_counts(company),
        })

    def put(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        s = self._get_or_create(_resolve_company(request))
        for field in ('tc_signin_time', 'tc_signout_time', 'stm_signin_time', 'stm_signout_time'):
            if field in request.data:
                setattr(s, field, request.data[field])
        s.save()
        return Response({'detail': 'Saved.'})


# ── Availability ──────────────────────────────────────────────────────────────
class AvailabilityView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from datetime import date as date_cls
        today = request.query_params.get('date', str(date_cls.today()))
        company = _resolve_company(request)
        desig_map = {'TELECALLER': 'telecaller', 'STM': 'stm'}
        users = (
            User.objects
            .filter(company=company, is_active=True)
            .exclude(role='Admin')
            .filter(designation__in=['TELECALLER', 'STM'])
            .only('id', 'name', 'designation')
            .order_by('name')
        )
        avail_map = {}
        checkin_map = {}
        for a in UserAvailability.objects.filter(user__company=request.user.company, date=today).select_related('user', 'user__company'):
            active = _availability_active(a)
            avail_map[a.user_id] = active
            if active and a.checked_in_at:
                checkin_map[a.user_id] = a.checked_in_at.isoformat()
        # Assigned projects per user (for the availability label).
        proj_map: dict[int, list] = {}
        for uid, pname in (
            UserProjectAssignment.objects
            .filter(user__in=users)
            .values_list('user_id', 'project__name')
        ):
            proj_map.setdefault(uid, []).append(pname)
        data = []
        for u in users:
            data.append({
                'user_id':      u.id,
                'name':         u.name,
                'role':         desig_map.get(u.designation.upper(), u.designation.lower()),
                'is_available': avail_map.get(u.id, False),
                'checked_in_at': checkin_map.get(u.id),
                'projects':     proj_map.get(u.id, []),
            })
        return Response(data)

    def post(self, request):
        """Admin toggles any user's availability for today (by user_id)."""
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        from datetime import date as date_cls
        user_id      = request.data.get('user_id')
        is_available = request.data.get('is_available', True)
        today        = str(date_cls.today())
        company      = _resolve_company(request)
        try:
            user = User.objects.get(pk=user_id, company=company)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=404)
        obj = _mark_available(user, company, is_available, today)
        # Marking available flushes the unassigned bucket to this role (window-gated).
        dist_type = _dist_type_for(user)
        if obj.is_available and dist_type:
            _run_distribution(user.company, dist_type)
        return Response({'user_id': user.id, 'is_available': obj.is_available})


class AvailabilityHistoryExportView(APIView):
    """The sign-in history as an .xlsx — the same records AvailabilityHistoryView
    renders, over the same date range, one row per person per day.

    Flat on purpose: the screen groups by day because that reads well, but a sheet is
    something you sort and pivot, so the day is a column rather than a heading. Counts
    per day are in the summary line; the rows carry who and when.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from datetime import date as date_cls
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        company = _resolve_company(request)
        today = date_cls.today()
        date_from = request.query_params.get('date_from') or str(today - timedelta(days=29))
        date_to   = request.query_params.get('date_to')   or str(today)

        desig_map = {'TELECALLER': 'Telecaller', 'STM': 'STM'}
        rows = (
            UserAvailability.objects
            .filter(user__company=company, date__gte=date_from, date__lte=date_to,
                    user__designation__in=['TELECALLER', 'STM'])
            .select_related('user')
            .order_by('-date', 'user__designation', 'user__name')
        )

        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
        NAVY, PAPER = 'FF0F1838', 'FFEEF1F7'

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Sign-in History'

        data = []
        for a in rows:
            checked_in = (timezone.localtime(a.checked_in_at).strftime('%I:%M %p')
                          if a.checked_in_at else '')
            data.append([
                a.date.strftime('%d/%m/%Y'),
                desig_map.get((a.user.designation or '').upper(), a.user.designation or ''),
                a.user.name or '',
                a.user.user_code or '',
                'Yes' if a.is_available else 'No',
                checked_in,
                # The handicap a late sign-in forfeits — the reason a day's share of
                # the backlog can be smaller than the room's, which is exactly what
                # someone reading this sheet is usually trying to explain.
                a.distribution_credit or 0,
            ])

        signed_in = sum(1 for r in data if r[4] == 'Yes')
        ws.append([f"{company.name if company else ''} — Sign-in History"])
        ws.append([f"{date_from} to {date_to}  ·  {len(data)} record{'' if len(data) == 1 else 's'}  ·  "
                   f"{signed_in} signed in  ·  generated "
                   f"{timezone.localtime(timezone.now()).strftime('%d/%m/%Y %I:%M %p')}"])
        ws['A1'].font = Font(bold=True, size=14, color=NAVY)
        ws['A2'].font = Font(size=10, color='FF8492A6')

        headings = ['Date', 'Role', 'Name', 'User Code', 'Signed In', 'Sign-in Time', 'Missed Leads']
        ws.append(headings)
        header_row = ws.max_row
        for cell in ws[header_row]:
            cell.font = Font(bold=True, color='FFFFFFFF', size=10)
            cell.fill = PatternFill('solid', fgColor=NAVY)
            cell.alignment = Alignment(horizontal='center', vertical='center')
        # Addressed by name, not via ws.cell(): asking openpyxl for a cell creates it,
        # which would leave an empty row under the header.
        ws.freeze_panes = f'A{header_row + 1}'

        for line in data:
            ws.append(line)

        total_row = ws.max_row + 1
        ws.cell(row=total_row, column=1, value='TOTAL')
        ws.cell(row=total_row, column=5, value=f'{signed_in} of {len(data)} signed in')
        if data:
            col = get_column_letter(len(headings))
            ws.cell(row=total_row, column=len(headings),
                    value=f'=SUM({col}{header_row + 1}:{col}{ws.max_row - 1})')
        for cell in ws[total_row]:
            cell.font = Font(bold=True, color=NAVY, size=10)
            cell.fill = PatternFill('solid', fgColor=PAPER)

        for idx, heading in enumerate(headings, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = max(14, len(heading) + 6)

        buf = BytesIO()
        wb.save(buf)
        resp = HttpResponse(
            buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        resp['Content-Disposition'] = (
            f'attachment; filename="Sign-in-History-{date_from}-to-{date_to}.xlsx"')
        return resp


class AvailabilityHistoryView(APIView):
    """Sign-in history day by day — who marked available and at what time.

    Reports what was recorded on each date rather than reusing _availability_active(),
    which expires any prior-day record by design: correct for today's board, but a
    history row must still show that someone signed in on the 3rd. Project labels are
    likewise left off, since assignments are current state and would misrepresent what
    a person was on back then.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from datetime import date as date_cls
        company = _resolve_company(request)
        today = date_cls.today()
        date_from = request.query_params.get('date_from') or str(today - timedelta(days=29))
        date_to   = request.query_params.get('date_to')   or str(today)

        desig_map = {'TELECALLER': 'telecaller', 'STM': 'stm'}
        rows = (
            UserAvailability.objects
            .filter(user__company=company, date__gte=date_from, date__lte=date_to,
                    user__designation__in=['TELECALLER', 'STM'])
            .select_related('user')
            .order_by('-date', 'user__name')
        )
        days = {}
        for a in rows:
            d = days.setdefault(str(a.date), {'date': str(a.date), 'telecallers': [], 'stms': []})
            entry = {
                'user_id':       a.user_id,
                'name':          a.user.name,
                'is_available':  a.is_available,
                'checked_in_at': a.checked_in_at.isoformat() if a.checked_in_at else None,
            }
            role = desig_map.get((a.user.designation or '').upper())
            d['stms' if role == 'stm' else 'telecallers'].append(entry)
        out = []
        for d in days.values():
            d['telecaller_count'] = sum(1 for x in d['telecallers'] if x['is_available'])
            d['stm_count']        = sum(1 for x in d['stms'] if x['is_available'])
            out.append(d)
        return Response(out)


class MyAvailabilityView(APIView):
    """Self-service availability for telecallers / STMs.
    Marking available stays active for AVAILABILITY_TTL_HOURS, then auto-resets."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from datetime import date as date_cls
        today = str(date_cls.today())
        avail = UserAvailability.objects.filter(user=request.user, date=today).first()
        active = _availability_active(avail, request.user)
        expires_at = None
        if active:
            # Auto-expires at the role sign-out time; fall back to the TTL if unset.
            expires_at = _availability_expires_at(request.user)
            if expires_at is None and avail and avail.checked_in_at:
                expires_at = (avail.checked_in_at + timedelta(hours=AVAILABILITY_TTL_HOURS)).isoformat()
        return Response({
            'is_available':  active,
            'checked_in_at': avail.checked_in_at.isoformat() if (avail and avail.checked_in_at) else None,
            'expires_at':    expires_at,
            'ttl_hours':     AVAILABILITY_TTL_HOURS,
            # Signing in after the role's time forfeits a share of the backlog — see
            # _distribution_credit_for. Reported so the person can see why their
            # count is behind the room's instead of assuming distribution is broken.
            **_late_signin_payload(request.user, avail),
        })

    def post(self, request):
        from datetime import date as date_cls
        if not (is_telecaller(request.user) or is_stm(request.user)):
            return Response({'detail': 'Only telecallers and STMs can mark their own availability.'},
                            status=status.HTTP_403_FORBIDDEN)
        is_available = request.data.get('is_available', True)
        today = str(date_cls.today())
        obj = _mark_available(request.user, _resolve_company(request), is_available, today)
        active = _availability_active(obj, request.user)
        # Marking available flushes the unassigned bucket to this user's role (window-gated).
        if active:
            _run_distribution(request.user.company, _dist_type_for(request.user))
        expires_at = None
        if active:
            expires_at = _availability_expires_at(request.user)
            if expires_at is None and obj.checked_in_at:
                expires_at = (obj.checked_in_at + timedelta(hours=AVAILABILITY_TTL_HOURS)).isoformat()
        return Response({'is_available': active, 'expires_at': expires_at,
                         'ttl_hours': AVAILABILITY_TTL_HOURS,
                         **_late_signin_payload(request.user, obj)})


# ── Distribution Weights ──────────────────────────────────────────────────────
class DistributionWeightView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = _resolve_company(request)
        users = (
            User.objects
            .filter(company=company, is_active=True, designation__in=['TELECALLER', 'STM'])
            .only('id', 'name', 'designation')
        )
        weight_map = {
            w.user_id: w.weight
            for w in UserDistributionWeight.objects.filter(user__company=company)
        }
        return Response([
            {'user_id': u.id, 'name': u.name, 'role': u.designation.upper(), 'weight': weight_map.get(u.id, 1)}
            for u in users
        ])

    def patch(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        company = _resolve_company(request)
        updates = request.data.get('updates', [])  # [{user_id, weight}]
        saved = []
        for item in updates:
            uid = item.get('user_id')
            w   = max(1, int(item.get('weight', 1)))
            try:
                user = User.objects.get(pk=uid, company=company)
                UserDistributionWeight.objects.update_or_create(user=user, defaults={'weight': w})
                saved.append('%s = %d' % (user.name, w))
            except User.DoesNotExist:
                pass
        from activity.recorder import note
        note(request, 'Set distribution weights: %s' % (', '.join(saved) or 'none'),
             action='updated', target_type='distribution')
        return Response({'detail': 'Weights saved.'})


# ── Distribution ─────────────────────────────────────────────────────────────
def _window_state(company, dist_type):
    """Return 'open' | 'before_signin' | 'after_signout' for the company's
    sign-in/sign-out window (IST). No settings → treated as 'open'."""
    from zoneinfo import ZoneInfo
    settings = DistributionSettings.objects.filter(company=company).first()
    if not settings:
        return 'open'
    field_prefix = 'tc' if dist_type == 'telecaller' else 'stm'
    now_ist = timezone.now().astimezone(ZoneInfo('Asia/Kolkata')).strftime('%H:%M')
    signin  = str(getattr(settings, f'{field_prefix}_signin_time'))[:5]
    signout = str(getattr(settings, f'{field_prefix}_signout_time'))[:5]
    if now_ist < signin:
        return 'before_signin'
    if now_ist >= signout:
        return 'after_signout'
    return 'open'


def _late_signin_payload(user, avail):
    """What to tell someone about signing in late, for the availability widgets.

    `signed_in_late` is about this sign-in specifically, not the clock now — asked an
    hour later it must still describe what happened, so it reads the recorded
    handicap rather than re-testing the time.
    """
    if not (avail and avail.is_available):
        return {'signed_in_late': False, 'missed_leads': 0, 'signin_time': None}
    dist_type = _dist_type_for(user)
    signin = None
    if dist_type:
        settings = DistributionSettings.objects.filter(company=user.company).first()
        if settings:
            field = 'tc_signin_time' if dist_type == 'telecaller' else 'stm_signin_time'
            signin = str(getattr(settings, field))[:5]
    credit = avail.distribution_credit or 0
    return {'signed_in_late': credit > 0, 'missed_leads': credit, 'signin_time': signin}


def _mark_available(user, company, is_available, date_str):
    """Record a sign-in/out, awarding the late handicap on the way in.

    Both the admin toggle and a rep's own switch land here, so the two cannot
    disagree about who counts as late.
    """
    credit = 0
    if is_available:
        dist_type = _dist_type_for(user)
        if dist_type:
            # Recomputed on each sign-in rather than kept from an earlier one today:
            # somebody who signs out and back in should be levelled against the room
            # as it stands then, not as it stood this morning.
            credit = _distribution_credit_for(user, company, dist_type)
    obj, _created = UserAvailability.objects.update_or_create(
        user=user, date=date_str,
        defaults={
            'is_available': is_available,
            'checked_in_at': timezone.now() if is_available else None,
            'distribution_credit': credit,
        },
    )
    return obj


def _signed_in_late(company, dist_type, when=None):
    """Whether `when` is past this role's designated sign-in time.

    On time means at the sign-in time or before it. A company with no distribution
    settings has no deadline, so nobody is late.
    """
    from zoneinfo import ZoneInfo
    settings = DistributionSettings.objects.filter(company=company).first()
    if not settings:
        return False
    field = 'tc_signin_time' if dist_type == 'telecaller' else 'stm_signin_time'
    signin = str(getattr(settings, field))[:5]
    now_ist = (when or timezone.now()).astimezone(ZoneInfo('Asia/Kolkata')).strftime('%H:%M')
    return now_ist > signin


def _distribution_credit_for(user, company, dist_type, when=None):
    """What a late arrival is treated as already holding, so they join the rotation
    level with everyone else instead of being handed a catch-up burst.

    Ranking is by today's lead count, so somebody arriving at 2pm on zero used to
    outrank colleagues holding 50 and take the next 50 leads by themselves. Crediting
    them with the lowest effective count among the people they will actually share
    leads with puts them level: the next lead goes round the room one each, which is
    what a late joiner should get.

    The lowest rather than the highest, deliberately — late is not meant to be a
    penalty, only the loss of the backlog they were not there for.

    Scoped to peers who share a project, because distribution is project-strict: the
    counts of somebody working an entirely different project say nothing about the
    queue this person is joining.
    """
    from datetime import date as date_cls
    if not _signed_in_late(company, dist_type, when):
        return 0
    desig = 'TELECALLER' if dist_type == 'telecaller' else 'STM'
    peers = [
        a for a in UserAvailability.objects.filter(
            user__company=company, user__designation__iexact=desig,
            date=str(date_cls.today()), is_available=True,
        ).exclude(user=user)
        if _availability_active(a)
    ]
    if not peers:
        return 0            # first one in today — there is no backlog to have missed
    mine = set(UserProjectAssignment.objects.filter(user=user).values_list('project_id', flat=True))
    if mine:
        shared = {}
        for uid, pid in UserProjectAssignment.objects.filter(
                user_id__in=[a.user_id for a in peers]).values_list('user_id', 'project_id'):
            shared.setdefault(uid, set()).add(pid)
        peers = [a for a in peers if shared.get(a.user_id, set()) & mine]
        if not peers:
            return 0        # nobody else is on this person's projects
    field = 'telecaller' if dist_type == 'telecaller' else 'stm'
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (Lead.objects
            .filter(**{f'{field}_id__in': [a.user_id for a in peers],
                       f'{field}_assigned_at__gte': today_start})
            .values(f'{field}_id').annotate(n=Count('id')))
    counts = {r[f'{field}_id']: r['n'] for r in rows}
    # A peer's own credit counts too, or a third latecomer would be levelled against
    # the second one's raw count and undo the second one's handicap.
    return min((counts.get(a.user_id, 0) + (a.distribution_credit or 0)) for a in peers)


def _telecaller_project_ids(company):
    """Projects that actually have a telecaller on them.

    A project with nobody from telecalling assigned has no telecaller stage, so its
    leads go straight to an STM. Derived from the assignments rather than a setting
    someone has to remember to flip: assign a telecaller and the project rejoins the
    telecaller flow by itself; remove the last one and it leaves.
    """
    return set(
        UserProjectAssignment.objects.filter(
            user__company=company,
            user__is_active=True,
            user__designation__iexact='TELECALLER',
        ).values_list('project_id', flat=True)
    )


def _pending_direct_to_stm(company):
    """New leads whose project has no telecaller assigned to it."""
    return Lead.objects.filter(
        company=company, status='new',
        telecaller__isnull=True, stm__isnull=True,
        project__isnull=False,
    ).exclude(project_id__in=_telecaller_project_ids(company))


def _distribution_pool_counts(company):
    """How many leads the next distribution run would actually pick up.

    Must mirror the two querysets in _distribute() exactly — the Distribution
    page previously showed stats' `new_leads` (every lead at status='new',
    including ones already owned by a telecaller/STM) for the TC pool and
    `sv_done` (completed site visits!) for the STM pool, so neither number
    described the pool it was labelling.
    """
    company_leads = Lead.objects.filter(company=company)
    tc_project_ids = _telecaller_project_ids(company)
    tc_pool = company_leads.filter(
        telecaller__isnull=True, stm__isnull=True, status='new',
        project_id__in=tc_project_ids,
    )
    stm_pool = company_leads.filter(
        Q(status='warm_transferred', stm__isnull=True)
        | (Q(status='new', stm__isnull=True, telecaller__isnull=True,
             project__isnull=False)
           & ~Q(project_id__in=tc_project_ids))
    )
    return {
        'telecaller': tc_pool.count(),
        'stm': stm_pool.count(),
        'blocked': _unroutable_by_project(company, tc_pool, stm_pool),
    }


def _unroutable_by_project(company, tc_pool, stm_pool):
    """Leads that distribution can never place, grouped by project.

    _distribute matches a lead to members assigned to its project AND holding the
    matching designation; anything else it counts as "skipped" and moves on. On an
    auto-run (lead created) that count is discarded, so a project with no STM on it
    accumulates unassigned leads indefinitely with nothing surfacing it. This is the
    structural case — who is *assigned*, not who happened to mark available today.
    """
    assigned = {}
    for pid, desig in UserProjectAssignment.objects.filter(
        user__company=company, user__is_active=True,
    ).values_list('project_id', 'user__designation'):
        assigned.setdefault(pid, set()).add((desig or '').upper())

    out = []
    for pool, desig in ((tc_pool, 'TELECALLER'), (stm_pool, 'STM')):
        rows = (pool.values('project_id', 'project__name')
                    .annotate(n=Count('id')).order_by('-n'))
        for r in rows:
            pid = r['project_id']
            if pid is not None and desig in assigned.get(pid, ()):
                continue          # someone can receive these
            out.append({
                'project': r['project__name'] or '(no project)',
                'count': r['n'],
                'needs': desig,
                'reason': ('lead has no project' if pid is None
                           else f'no active {desig} is assigned to this project'),
            })
    return sorted(out, key=lambda x: -x['count'])


def _run_distribution(company, dist_type, triggered_by=None, gate='full'):
    """Distribute, then hand telecaller-less projects straight to an STM.

    A project with no telecaller assigned has no telecalling stage, so its leads must
    reach an STM. That has to happen even when the telecaller pass returned early --
    window closed, nobody marked available -- because those leads never needed a
    telecaller in the first place.
    """
    result = _distribute(company, dist_type, triggered_by, gate)
    if dist_type != 'telecaller' or not _pending_direct_to_stm(company).exists():
        return result

    direct = _distribute(company, 'stm', triggered_by, gate)
    merged = dict(result)
    merged['distributed'] = result.get('distributed', 0) + direct.get('distributed', 0)
    merged['assignments'] = {**result.get('assignments', {}), **direct.get('assignments', {})}
    if direct.get('distributed'):
        merged.pop('message', None)
    elif direct.get('message'):
        merged['message'] = ' '.join(filter(None, [result.get('message'), direct['message']]))
    return merged


def _distribute(company, dist_type, triggered_by=None, gate='full'):
    """Weighted, project-aware, window-gated assignment of the current unassigned
    bucket to available telecallers/STMs. Reusable by both the manual Distribute
    button and the automatic triggers (lead created / marked available / warm).

    gate='full'    → only runs when the window is 'open' (auto-assignment).
    gate='signout' → runs unless 'after_signout' (manual admin override).

    triggered_by=None marks the assignment as automatic ("System") in history.
    Returns the same dict shape the API has always returned.
    """
    from datetime import date as date_cls

    desig = 'TELECALLER' if dist_type == 'telecaller' else 'STM'

    state = _window_state(company, dist_type)
    if state == 'after_signout':
        return {'distributed': 0, 'message': f'Distribution window closed for {desig}. Leads remain unassigned.'}
    if gate == 'full' and state != 'open':
        return {'distributed': 0, 'message': f'Outside {desig} distribution window. Leads remain unassigned.'}

    today = str(date_cls.today())

    # Users marked available today. Availability auto-expires at the role's sign-out
    # time, which the window gate above already enforces (distribution never runs
    # after sign-out), so a same-day check-in stays valid through the whole window.
    avail_ids = set(
        UserAvailability.objects.filter(
            user__company=company,
            user__designation__iexact=desig,
            date=today,
            is_available=True,
        ).values_list('user_id', flat=True)
    )
    if not avail_ids:
        return {'distributed': 0, 'message': f'No {desig}s have marked available today.'}

    members = list(User.objects.filter(pk__in=avail_ids, is_active=True).only('id', 'name'))
    if not members:
        return {'distributed': 0, 'message': f'No active {desig} users available.'}

    weight_map = {
        w.user_id: w.weight
        for w in UserDistributionWeight.objects.filter(user__in=members)
    }

    with transaction.atomic():
        # Lock unassigned leads row-by-row so concurrent distribution calls
        # (auto + manual firing simultaneously) can't grab the same leads.
        company_leads = Lead.objects.filter(company=company)
        if dist_type == 'telecaller':
            # stm__isnull=True too — a lead an STM (or CP) already self-sourced has
            # stm set but stays status='new'/telecaller=NULL (nothing else moves it
            # off 'new' at create time), so without this it silently qualified as
            # "unassigned" and got swept into telecaller distribution the next time
            # ANY unrelated lead-create triggered this company-wide run — handing an
            # STM's own lead to a telecaller entirely by accident.
            # Only projects that have a telecaller assigned. The rest are handled by
            # the STM pass below; leaving them here would park them as "skipped".
            qs = (company_leads
                  .filter(telecaller__isnull=True, stm__isnull=True, status='new',
                          project_id__in=_telecaller_project_ids(company))
                  .select_for_update(skip_locked=True).order_by('created_at'))
        else:
            # Warm-transferred leads, plus new leads whose project has no telecaller
            # assigned to it at all.
            qs = (company_leads
                  .filter(Q(status='warm_transferred', stm__isnull=True)
                          | (Q(status='new', stm__isnull=True, telecaller__isnull=True,
                               project__isnull=False)
                             & ~Q(project_id__in=_telecaller_project_ids(company))))
                  .select_for_update(skip_locked=True).order_by('created_at'))

        leads = list(qs)
        if not leads:
            return {'distributed': 0, 'message': 'No unassigned leads found.'}

        # Today's existing assignment counts (for fair weighted continuation across runs).
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if dist_type == 'telecaller':
            count_qs = Lead.objects.filter(
                telecaller__in=members, telecaller_assigned_at__gte=today_start
            ).values('telecaller_id').annotate(n=Count('id'))
            counts = {row['telecaller_id']: row['n'] for row in count_qs}
        else:
            count_qs = Lead.objects.filter(
                stm__in=members, stm_assigned_at__gte=today_start
            ).values('stm_id').annotate(n=Count('id'))
            counts = {row['stm_id']: row['n'] for row in count_qs}
        for m in members:
            counts.setdefault(m.id, 0)
        # Somebody who signed in after their time carries a handicap so the rotation
        # does not back-fill them to the level of those who were here on time — see
        # _distribution_credit_for. Zero for everyone who signed in on time, which
        # leaves the ranking below exactly as it was.
        credits = {
            a.user_id: (a.distribution_credit or 0)
            for a in UserAvailability.objects.filter(
                user_id__in=[m.id for m in members], date=today, is_available=True)
        }

        # Project assignments (STRICT): a member only receives leads of the project(s)
        # assigned to them. A member with NO project assigned receives NOTHING — and a
        # lead with no project can't be routed to anyone.
        proj_map = {}
        for uid, pid in UserProjectAssignment.objects.filter(
            user__in=members
        ).values_list('user_id', 'project_id'):
            proj_map.setdefault(uid, set()).add(pid)

        member_ids   = [m.id for m in members]
        id_to_member = {m.id: m for m in members}
        user_leads   = {m.id: [] for m in members}
        now = timezone.now()
        skipped = 0

        # Pre-bucket eligible members by project so each lead is matched in O(1)
        # instead of scanning every member (O(L×M) → O(L+M)). Members are added in
        # member_ids order, so the weighted-min tie-break stays identical to before.
        proj_to_uids = {}
        for uid in member_ids:
            for pid in proj_map.get(uid, ()):
                proj_to_uids.setdefault(pid, []).append(uid)

        for lead in leads:
            eligible = proj_to_uids.get(lead.project_id) if lead.project_id is not None else None
            if not eligible:
                skipped += 1
                continue
            best = min(eligible, key=lambda uid: (counts[uid] + credits.get(uid, 0)) / (weight_map.get(uid, 1)))
            user_leads[best].append(lead.pk)
            counts[best] += 1

        assignments = []
        history_rows = []
        note = 'Auto-assigned' if triggered_by is None else 'Manually assigned'
        for uid, pks in user_leads.items():
            if not pks:
                continue
            if dist_type == 'telecaller':
                Lead.objects.filter(pk__in=pks).update(
                    telecaller_id=uid, status='assigned', telecaller_assigned_at=now,
                )
            else:
                Lead.objects.filter(pk__in=pks).update(stm_id=uid, stm_assigned_at=now)
                # A lead that skipped the telecaller stage arrives still 'new';
                # mark it assigned. Not 'warm_transferred' — nobody transferred it,
                # and that status feeds the warm/SQL funnel.
                Lead.objects.filter(pk__in=pks, status='new').update(status='assigned')
            for pk in pks:
                history_rows.append(LeadStatusHistory(
                    lead_id=pk, changed_by=triggered_by,
                    field_changed=dist_type, old_value='', new_value=id_to_member[uid].name,
                    remarks=note,
                ))
            assignments.append({'name': id_to_member[uid].name, 'count': len(pks)})
            from notifications import notify
            notify(id_to_member[uid], 'new_lead', 'New Leads Assigned',
                   f'{len(pks)} new lead{"s" if len(pks) > 1 else ""} assigned to you.')

        if history_rows:
            LeadStatusHistory.objects.bulk_create(history_rows)

        distributed = sum(a['count'] for a in assignments)
        if distributed:
            DistributionLog.objects.create(
                company=company,
                dist_type=dist_type,
                triggered_by=triggered_by,
                leads_distributed=distributed,
                details={'assignments': assignments, 'auto': triggered_by is None},
            )

    resp = {'distributed': distributed, 'assignments': {a['name']: a['count'] for a in assignments}}
    if skipped:
        resp['message'] = f'{skipped} lead(s) left unassigned — no available {desig} is assigned to their project.'
    return resp


def _record_lead_created(lead, by=None):
    """Add the opening 'Lead created' entry to a lead's history (with its source)."""
    src = lead.source.name if lead.source_id else 'manual'
    campaign = lead.meta_campaign_name or ''
    new_value = (f'{src} · {campaign}' if campaign else src)[:100]
    LeadStatusHistory.objects.create(
        lead=lead, changed_by=by, field_changed='created',
        old_value='', new_value=new_value, remarks='Lead created',
    )


class DistributeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        dist_type = request.data.get('dist_type', request.data.get('type', 'telecaller'))
        company   = _resolve_company(request)
        # Manual admin trigger: weight-based, allowed before sign-in, blocked after sign-out.
        resp = _run_distribution(company, dist_type, triggered_by=request.user, gate='signout')
        from activity.recorder import note
        n = int(resp.get('distributed') or 0)
        note(request, ('Distributed %d lead%s to %ss' % (n, '' if n == 1 else 's', dist_type)) if n
             else 'Ran %s distribution — %s' % (dist_type, resp.get('message') or 'nothing to distribute'),
             action='distributed', target_type='distribution')
        return Response(resp)


class DistributionLogView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        logs = scope_to_company(
            DistributionLog.objects.select_related('triggered_by'), request.user
        )
        if request.query_params.get('company_id') and is_platform_admin(request.user):
            logs = logs.filter(company_id=request.query_params['company_id'])
        logs = logs[:30]
        data = [{
            'id':                  log.id,
            'dist_type':           log.dist_type,
            'leads_distributed':   log.leads_distributed,
            'triggered_by_name':   log.triggered_by.name if log.triggered_by else 'System',
            'details':             log.details,
            'created_at':          log.created_at,
        } for log in logs]
        return Response(data)

    def delete(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        qs = scope_to_company(DistributionLog.objects.all(), request.user)
        if request.query_params.get('company_id') and is_platform_admin(request.user):
            qs = qs.filter(company_id=request.query_params['company_id'])
        qs.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Bulk Import ───────────────────────────────────────────────────────────────
# ── Lead-import helpers (flexible cell parsing for the lifecycle template) ──────
def _imp_dt(val):
    """Parse a cell into an aware datetime. Accepts ISO, yyyy-mm-dd, dd-mm-yyyy, dd/mm/yyyy."""
    from datetime import datetime as _dt, time as _time
    from django.utils.dateparse import parse_datetime, parse_date
    import re as _re
    s = str(val or '').strip()
    if not s:
        return None
    dt = parse_datetime(s)
    if dt:
        dt = timezone.make_aware(dt) if timezone.is_naive(dt) else dt
        # Midnight (incl. Excel date cells) → noon so the calendar date is timezone-stable.
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            dt = dt.replace(hour=12)
        return dt
    d = parse_date(s)
    if d:
        # Anchor date-only values at noon so the calendar date is stable across timezones.
        return timezone.make_aware(_dt.combine(d, _time(12, 0)))
    m = _re.match(r'^(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})$', s)
    if m:
        dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yy < 100:
            yy += 2000
        try:
            return timezone.make_aware(_dt(yy, mm, dd, 12, 0))
        except ValueError:
            return None
    return None


def _imp_date(val):
    dt = _imp_dt(val)
    return dt.date() if dt else None


def _imp_int(val):
    s = str(val or '').replace(',', '').strip()
    if not s:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _imp_dec(val):
    s = str(val or '').replace(',', '').replace('₹', '').strip()
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _imp_purpose(val, valid):
    """Comma/pipe/semicolon-separated purpose values ('Investment, End Use') → the
    matching canonical option keys, dropping anything that doesn't match."""
    if not val:
        return []
    import re as _re
    parts = [p.strip().lower().replace(' ', '_') for p in _re.split(r'[,;|/]+', str(val)) if p.strip()]
    return [p for p in parts if p in valid]


# Canonical row keys the importer understands (the full-pipeline columns).
IMPORT_COLUMNS = [
    'name', 'phone', 'alt_phone', 'email', 'project', 'source', 'campaign', 'adset', 'ad_name',
    'requirement', 'budget_min', 'budget_max', 'preferred_location', 'city', 'address', 'purpose', 'budget_bucket',
    'lead_date', 'overall_status',
    'telecaller_code', 'telecaller_status', 'telecaller_remarks',
    'stm_code', 'stm_status', 'stm_remarks',
    'sv_scheduled_date', 'sv_visited_date', 'sv_status', 'sv_referred_by_code', 'sv_remarks',
    'closure_date', 'closure_status', 'unit_no', 'unit_type', 'booking_amount', 'total_amount', 'closure_remarks',
]
# Header → canonical-key aliases (the per-row loop reads 'creative', not 'ad_name').
_IMP_ALIASES = {
    'name': {'name', 'full_name', 'fullname', 'customer_name', 'lead_name', 'first_name'},
    'phone': {'phone', 'phone_number', 'phonenumber', 'mobile', 'mobile_number', 'contact', 'cell'},
    'alt_phone': {'alt_phone', 'alternate_phone', 'phone_2', 'secondary_phone', 'other_phone'},
    'email': {'email', 'e_mail', 'email_address'},
    'campaign': {'campaign', 'campaign_name', 'meta_campaign', 'utm_campaign', 'ad_campaign'},
    'adset': {'adset', 'adset_name', 'ad_set', 'ad_group_name', 'adgroup'},
    'creative': {'creative', 'ad_name', 'creative_name', 'ad_creative', 'advertisement_name'},
    'lead_date': {'lead_date', 'date', 'created', 'created_at', 'submission_date', 'timestamp'},
    # Backward-compat: these columns used to hold a raw numeric id (pre-user_code
    # rename) — a template downloaded before the rename, or a habitually-typed old
    # header, should still auto-map instead of silently dropping the column.
    'telecaller_code': {'telecaller_code', 'telecaller_id'},
    'stm_code': {'stm_code', 'stm_id'},
    'sv_referred_by_code': {'sv_referred_by_code', 'sv_referred_by_id'},
}
_IMP_CANON = set(IMPORT_COLUMNS) | {'creative'}


def _imp_canon_key(header):
    import re as _re
    k = _re.sub(r'[\s\-]+', '_', str(header or '').strip().lower())
    for field, aliases in _IMP_ALIASES.items():
        if k in aliases:
            return field
    return k if k in _IMP_CANON else None


def _imp_parse_file(f):
    """Parse an uploaded .xlsx/.csv into a list of row dicts keyed by canonical column names."""
    import io
    fname = (getattr(f, 'name', '') or '').lower()
    headers, raw_rows = [], []
    if fname.endswith('.csv') or fname.endswith('.txt'):
        import csv
        data = f.read()
        text = data.decode('utf-8-sig', errors='ignore') if isinstance(data, bytes) else data
        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []
        raw_rows = [dict(r) for r in reader]
    else:
        import openpyxl
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        ws = wb['Leads'] if 'Leads' in wb.sheetnames else wb[wb.sheetnames[0]]
        it = ws.iter_rows(values_only=True)
        headers = [('' if h is None else str(h).strip()) for h in (next(it, []) or [])]
        for r in it:
            raw_rows.append({headers[i]: r[i] for i in range(min(len(headers), len(r)))})
    colmap = {h: _imp_canon_key(h) for h in headers}
    rows = []
    for rr in raw_rows:
        out = {}
        for h, v in rr.items():
            c = colmap.get(h)
            if c and v is not None and str(v).strip() != '':
                out[c] = v
        if out.get('name') or out.get('phone'):
            rows.append(out)
    return rows


class BulkImportLeadsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not has_sales_access(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)

        from .models import LEAD_STATUS, TC_STATUS, STM_STATUS, SV_STATUS, CLOSURE_STATUS, BUDGET_BUCKETS

        rows       = request.data.get('leads', [])
        project_id = request.data.get('project_id')   # default project for every row
        source_id  = request.data.get('source_id')    # default source for every row
        company    = request.user.company
        # An STM only works the STM stage — telecaller assignment isn't theirs to set,
        # so any telecaller_code/status/remarks in the file is ignored for their
        # uploads (mirrors the template omitting those columns for an STM login).
        uploader_is_stm = is_stm(request.user)

        # App/web may upload the spreadsheet itself (multipart) instead of pre-parsed
        # JSON rows — parse it server-side into the same canonical row dicts.
        if not rows and request.FILES.get('file'):
            try:
                rows = _imp_parse_file(request.FILES['file'])
            except Exception as e:
                return Response({'detail': 'Could not read the file: %s' % e}, status=status.HTTP_400_BAD_REQUEST)

        if not rows:
            return Response({'detail': 'No leads provided.'}, status=status.HTTP_400_BAD_REQUEST)

        # A supplied default project/source must belong to the requester's company.
        if project_id and not _project_in_scope(request, project_id):
            return Response({'detail': 'Invalid project for your company.'}, status=status.HTTP_400_BAD_REQUEST)
        if source_id and not scope_to_company(LeadSource.objects.filter(pk=source_id), request.user).exists():
            return Response({'detail': 'Invalid source for your company.'}, status=status.HTTP_400_BAD_REQUEST)

        # Allowed status values + company-scoped lookup tables (resolved once).
        LEAD_ST = {k for k, _ in LEAD_STATUS}
        TC_ST   = {k for k, _ in TC_STATUS}
        STM_ST  = {k for k, _ in STM_STATUS}
        SV_ST   = {k for k, _ in SV_STATUS}
        CL_ST   = {k for k, _ in CLOSURE_STATUS}
        BB_ST   = {k for k, _ in BUDGET_BUCKETS}
        PURPOSE_VALID = {'investment', 'end_use', 'other'}
        proj_by_name = {p.name.strip().lower(): p.id for p in scope_to_company(Project.objects.all(), request.user)}
        src_by_name  = {s.name.strip().lower(): s.id for s in scope_to_company(LeadSource.objects.all(), request.user)}
        uq = User.objects.filter(is_active=True)
        if company:
            uq = uq.filter(company=company)
        uq = list(uq)
        code_to_id = {u.user_code.strip().lower(): u.id for u in uq if u.user_code}
        valid_user_ids = {u.id for u in uq}

        def _uid(v):
            s = str(v or '').strip()
            if not s:
                return None
            hit = code_to_id.get(s.lower())
            if hit:
                return hit
            # Backward-compat: a file from before the user_code rename may still
            # carry a raw numeric id in the cell (only the header changed).
            i = _imp_int(s)
            return i if i in valid_user_ids else None

        imported = 0
        duplicates = 0
        errors = 0
        bare_new = 0
        failed = []
        warnings = []  # non-fatal: row still imports, but a code/value didn't resolve

        # Build existing dup set (last-10-digits) scoped to this company — O(n) once.
        company_leads = scope_to_company(Lead.objects.all(), request.user)
        # Read the blind index rather than decrypting every stored phone.
        existing_keys = set(company_leads.values_list('phone_key', flat=True))
        existing_keys.discard('')

        to_create = []   # Lead objects
        meta      = []   # parallel per-row dict carrying lead_date + SV/closure raw data
        for i, row in enumerate(rows):
            name  = str(row.get('name', '')).strip()
            phone = str(row.get('phone', '')).strip()
            if not name or not phone:
                errors += 1
                failed.append({'row': i + 1, 'name': name, 'phone': phone, 'reason': 'Missing name or phone'})
                continue

            clean = ''.join(c for c in phone if c.isdigit())[-10:]
            # existing_keys holds blind-index hashes, so hash before comparing.
            clean_key = phone_blind_index(clean) if clean else ''
            is_dup = bool(clean_key) and clean_key in existing_keys

            rproj = proj_by_name.get(str(row.get('project', '')).strip().lower()) or project_id or None
            rsrc  = src_by_name.get(str(row.get('source', '')).strip().lower()) or source_id or None
            tc_id  = None if uploader_is_stm else _uid(row.get('telecaller_code'))
            stm_id = _uid(row.get('stm_code'))
            sv_ref_id = _uid(row.get('sv_referred_by_code'))
            code_checks = [
                ('STM Code', row.get('stm_code'), stm_id),
                ('SV Referred By Code', row.get('sv_referred_by_code'), sv_ref_id),
            ]
            if not uploader_is_stm:
                code_checks.insert(0, ('Telecaller Code', row.get('telecaller_code'), tc_id))
            for label, raw_val, resolved in code_checks:
                if str(raw_val or '').strip() and not resolved:
                    warnings.append({'row': i + 1, 'name': name, 'field': label, 'value': str(raw_val).strip(),
                                      'reason': "didn't match any user's code — left unassigned"})
            # An STM uploading their own leads (e.g. a walk-in sign-in sheet, no STM
            # Code column filled in) self-sources them, same as the single "Add Lead"
            # flow already does — checked after the warning above so a genuinely wrong
            # code still surfaces its warning rather than silently becoming "assign to
            # me". Otherwise a row with neither STM nor telecaller falls through to
            # telecaller auto-distribution, handing the STM's own lead to someone else.
            if uploader_is_stm and not stm_id:
                stm_id = request.user.id

            tc_status  = '' if uploader_is_stm else str(row.get('telecaller_status', '')).strip().lower()
            tc_status  = tc_status if tc_status in TC_ST else ''
            stm_status = str(row.get('stm_status', '')).strip().lower()
            stm_status = stm_status if stm_status in STM_ST else ''

            budget_bucket = str(row.get('budget_bucket', '')).strip().lower().replace(' ', '_')
            budget_bucket = budget_bucket if budget_bucket in BB_ST else ''
            purpose = _imp_purpose(row.get('purpose'), PURPOSE_VALID)

            lead_dt = _imp_dt(row.get('lead_date'))

            # SV / closure presence
            sv_sched = _imp_dt(row.get('sv_scheduled_date'))
            sv_vis   = _imp_dt(row.get('sv_visited_date'))
            sv_stat  = str(row.get('sv_status', '')).strip().lower()
            sv_stat  = sv_stat if sv_stat in SV_ST else ''
            has_sv   = bool(sv_sched or sv_vis or sv_stat or str(row.get('sv_remarks', '')).strip())
            cl_date  = _imp_date(row.get('closure_date'))

            # Overall lead status: explicit wins; otherwise derive from the furthest stage reached.
            overall = str(row.get('overall_status', '')).strip().lower()
            if overall not in LEAD_ST:
                if cl_date:
                    overall = 'closed'
                elif has_sv:
                    overall = 'sv_done' if sv_stat == 'completed' else 'sv_scheduled'
                elif stm_id:
                    overall = 'warm_transferred'
                elif tc_id:
                    overall = 'assigned'
                else:
                    overall = 'new'

            to_create.append(Lead(
                company=company,
                name=name,
                phone=phone,
                # bulk_create skips save(), so set the lookup key here or these rows
                # would be invisible to duplicate detection and phone search.
                phone_key=clean_key,
                alt_phone=str(row.get('alt_phone', '')).strip(),
                email=str(row.get('email', '')).strip(),
                project_id=rproj,
                source_id=rsrc,
                meta_campaign_name=str(row.get('campaign', '')).strip(),
                meta_adset_name=str(row.get('adset', '')).strip(),
                meta_ad_name=str(row.get('creative', '')).strip(),
                requirement=str(row.get('requirement', '')).strip(),
                preferred_location=str(row.get('preferred_location', '')).strip(),
                budget_min=_imp_int(row.get('budget_min')),
                budget_max=_imp_int(row.get('budget_max')),
                city=str(row.get('city', '')).strip(),
                address=str(row.get('address', '')).strip(),
                purpose=purpose,
                budget_bucket=budget_bucket,
                status=overall,
                telecaller_id=tc_id,
                telecaller_status=tc_status,
                telecaller_remarks='' if uploader_is_stm else str(row.get('telecaller_remarks', '')).strip(),
                telecaller_assigned_at=(lead_dt or timezone.now()) if tc_id else None,
                stm_id=stm_id,
                stm_status=stm_status,
                stm_remarks=str(row.get('stm_remarks', '')).strip(),
                stm_assigned_at=(lead_dt or timezone.now()) if stm_id else None,
                is_duplicate=is_dup,
            ))
            meta.append({
                'lead_dt': lead_dt,
                'has_sv': has_sv, 'sv_sched': sv_sched, 'sv_vis': sv_vis, 'sv_stat': sv_stat or 'scheduled',
                'sv_ref': sv_ref_id, 'sv_remarks': str(row.get('sv_remarks', '')).strip(),
                'cl_date': cl_date, 'cl_status': (str(row.get('closure_status', '')).strip().lower() if str(row.get('closure_status', '')).strip().lower() in CL_ST else 'booked'),
                'unit_no': str(row.get('unit_no', '')).strip(), 'unit_type': str(row.get('unit_type', '')).strip(),
                'booking_amount': _imp_dec(row.get('booking_amount')), 'total_amount': _imp_dec(row.get('total_amount')),
                'cl_remarks': str(row.get('closure_remarks', '')).strip(),
            })

            if is_dup:
                duplicates += 1
            else:
                imported += 1
                if clean_key:
                    existing_keys.add(clean_key)  # catch in-batch duplicates too
            # Only a genuinely untouched lead (no telecaller AND no STM) should be swept
            # into telecaller auto-distribution — mirrors the single-lead-create check
            # above (`not lead.telecaller_id and not lead.stm_id`). A row that names an
            # STM but not a telecaller has already skipped/passed that stage; sweeping
            # it in anyway is what was handing STM-assigned leads to a telecaller too.
            if not tc_id and not stm_id and overall == 'new':
                bare_new += 1

        with transaction.atomic():
            created = Lead.objects.bulk_create(to_create)

            # Honour historical lead_date by overriding the auto_now_add created_at.
            dated = []
            for lead, m in zip(created, meta):
                if m['lead_dt']:
                    lead.created_at = m['lead_dt']
                    dated.append(lead)
            if dated:
                Lead.objects.bulk_update(dated, ['created_at'])

            # Materialise Site Visits + Closures linked to each freshly created lead.
            svs, closures = [], []
            for lead, m in zip(created, meta):
                if m['has_sv']:
                    svs.append(SiteVisit(
                        lead=lead, project_id=lead.project_id,
                        scheduled_at=m['sv_sched'], visited_at=m['sv_vis'], status=m['sv_stat'],
                        stm_id=lead.stm_id, referred_by_telecaller_id=(m['sv_ref'] or lead.telecaller_id),
                        remarks=('[Imported] ' + m['sv_remarks']).strip(),
                    ))
                if m['cl_date']:
                    # Historical closure (no Booking/LOI) — tagged so it's distinguishable
                    # from closures produced by the booking form.
                    closures.append(Closure(
                        company_id=lead.company_id, lead=lead,
                        client_name=lead.name or '', client_phone=lead.phone or '',
                        project_id=lead.project_id, stm_id=lead.stm_id,
                        referred_by_telecaller_id=lead.telecaller_id, status=m['cl_status'],
                        closure_date=m['cl_date'], unit_no=m['unit_no'], unit_type=m['unit_type'],
                        booking_amount=m['booking_amount'], total_amount=m['total_amount'],
                        remarks=('[Imported] ' + m['cl_remarks']).strip(),
                    ))
            if svs:
                SiteVisit.objects.bulk_create(svs)
            if closures:
                Closure.objects.bulk_create(closures)

        # Auto-assign only the genuinely bare/new bucket (rows that carried an STM/TC
        # or a later stage are already placed and must not be redistributed).
        if bare_new:
            _run_distribution(company, 'telecaller')
        return Response({
            'imported': imported, 'duplicates': duplicates, 'errors': errors, 'failed': failed,
            'warnings': warnings,
            'site_visits': len([m for m in meta if m['has_sv']]),
            'closures': len([m for m in meta if m['cl_date']]),
        })


class LeadImportTemplateView(APIView):
    """Generates the Full-Pipeline import template (.xlsx) server-side with dropdowns,
    a styled table, coloured required/closure headers and a Reference sheet — so the
    mobile app (which can't build a rich xlsx on-device) downloads the same template
    the web generates."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_sales_access(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)

        import openpyxl
        from io import BytesIO
        from openpyxl.worksheet.table import Table, TableStyleInfo
        from openpyxl.worksheet.datavalidation import DataValidation
        from openpyxl.styles import PatternFill, Font
        from openpyxl.utils import get_column_letter

        company = request.user.company
        projects = list(scope_to_company(Project.objects.all(), request.user).values_list('name', flat=True))
        sources  = list(scope_to_company(LeadSource.objects.all(), request.user).values_list('name', flat=True))
        uq = User.objects.filter(is_active=True).exclude(role='Admin')
        if company:
            uq = uq.filter(company=company)
        users = list(uq.values('id', 'name', 'user_code', 'designation', 'role', 'phone').order_by('name'))

        # An STM only works the STM stage — telecaller assignment isn't theirs to set,
        # so the template doesn't even offer those columns for an STM login (uploads
        # ignore them regardless — see BulkImportLeadsView — this just avoids handing
        # out a template with fields that'll silently be dropped).
        uploader_is_stm = is_stm(request.user)

        cols = [
            'name', 'phone', 'alt_phone', 'email', 'project', 'source', 'campaign', 'adset', 'ad_name',
            'requirement', 'budget_min', 'budget_max', 'preferred_location', 'city', 'address', 'purpose', 'budget_bucket',
            'lead_date', 'overall_status',
            'telecaller_code', 'telecaller_status', 'telecaller_remarks', 'stm_code', 'stm_status', 'stm_remarks',
            'sv_scheduled_date', 'sv_visited_date', 'sv_status', 'sv_referred_by_code', 'sv_remarks',
            'closure_date', 'closure_status', 'unit_no', 'unit_type', 'booking_amount', 'total_amount', 'closure_remarks',
        ]
        if uploader_is_stm:
            cols = [c for c in cols if c not in ('telecaller_code', 'telecaller_status', 'telecaller_remarks')]
        # Display-only header text — the parser normalises spaces/case back to the
        # canonical snake_case key (see _imp_canon_key), so this is purely cosmetic.
        HEADER_LABELS = {
            'name': 'Name', 'phone': 'Phone', 'alt_phone': 'Alt Phone', 'email': 'Email',
            'project': 'Project', 'source': 'Source', 'campaign': 'Campaign Name', 'adset': 'Ad Set',
            'ad_name': 'Ad Name', 'requirement': 'Requirement', 'budget_min': 'Budget Min',
            'budget_max': 'Budget Max', 'preferred_location': 'Preferred Location', 'city': 'City',
            'address': 'Address', 'purpose': 'Purpose', 'budget_bucket': 'Budget Bucket',
            'lead_date': 'Lead Date', 'overall_status': 'Overall Status',
            'telecaller_code': 'Telecaller Code', 'telecaller_status': 'Telecaller Status',
            'telecaller_remarks': 'Telecaller Remarks', 'stm_code': 'STM Code', 'stm_status': 'STM Status',
            'stm_remarks': 'STM Remarks', 'sv_scheduled_date': 'SV Scheduled Date',
            'sv_visited_date': 'SV Visited Date', 'sv_status': 'SV Status',
            'sv_referred_by_code': 'SV Referred By Code', 'sv_remarks': 'SV Remarks',
            'closure_date': 'Closure Date', 'closure_status': 'Closure Status', 'unit_no': 'Unit No',
            'unit_type': 'Unit Type', 'booking_amount': 'Booking Amount', 'total_amount': 'Total Amount',
            'closure_remarks': 'Closure Remarks',
        }
        STATUS = {
            'overall_status': 'new,assigned,contacted,not_reachable,warm_transferred,hot,warm,cold,not_interested,sv_scheduled,sv_done,closed,lost',
            'telecaller_status': 'warm,cold,not_interested,not_reachable,callback',
            'stm_status': 'hot,warm,cold,not_interested,sv_scheduled,sv_done,closed',
            'sv_status': 'scheduled,completed,cancelled,no_show',
            'closure_status': 'booked,cancelled,refunded',
            'budget_bucket': 'lt_10l,10_50l,50l_1cr,1_2cr,2_3cr,3_5cr,gt_5cr',
        }
        if uploader_is_stm:
            STATUS.pop('telecaller_status', None)
        # purpose is multi-select (comma-separated) so it can't use the same
        # single-value dropdown as STATUS — documented in the Reference sheet instead.
        PURPOSE_VALUES = 'investment, end_use, other'
        def _role(u):
            return (u['designation'] or u['role'] or '').lower()
        tc_code  = next((u['user_code'] for u in users if 'tele' in _role(u) and u['user_code']), (users[0]['user_code'] if users else ''))
        stm_code = next((u['user_code'] for u in users if any(k in _role(u) for k in ('stm', 'sales', 'manager')) and u['user_code']),
                        (users[1]['user_code'] if len(users) > 1 else (users[0]['user_code'] if users else '')))

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Leads'
        ws.append([HEADER_LABELS.get(c, c) for c in cols])
        ex1 = {'name': 'Rahul Sharma', 'phone': '9876543210', 'email': 'rahul@example.com', 'source': (sources[0] if sources else 'meta'), 'campaign': 'Meta - Luxury Homes', 'ad_name': 'Video 2BHK', 'city': 'Ahmedabad', 'purpose': 'end_use', 'budget_bucket': '50l_1cr', 'lead_date': '01-05-2025', 'overall_status': 'new', 'telecaller_code': tc_code, 'telecaller_status': 'callback', 'telecaller_remarks': 'Call back evening'}
        ex2 = {'name': 'Priya Mehta', 'phone': '9988776655', 'email': 'priya@example.com', 'project': (projects[0] if projects else 'Kalrav'), 'source': (sources[0] if sources else 'walk-in'), 'city': 'Vadodara', 'address': '12 Alkapuri Society', 'purpose': 'investment, end_use', 'budget_bucket': '1_2cr', 'lead_date': '02-04-2025', 'overall_status': 'closed', 'telecaller_code': tc_code, 'telecaller_status': 'warm', 'stm_code': stm_code, 'stm_status': 'closed', 'sv_scheduled_date': '05-04-2025', 'sv_visited_date': '06-04-2025', 'sv_status': 'completed', 'sv_remarks': 'Liked plot A-12', 'closure_date': '08-04-2025', 'closure_status': 'booked', 'unit_no': 'A-12', 'unit_type': '2BHK', 'booking_amount': 200000, 'total_amount': 5000000, 'closure_remarks': 'Token received'}
        for ex in (ex1, ex2):
            ws.append([ex.get(c, '') for c in cols])

        last_col = get_column_letter(len(cols))
        table = Table(displayName='LeadsImport', ref='A1:%s3' % last_col)
        table.tableStyleInfo = TableStyleInfo(name='TableStyleMedium2', showRowStripes=True)
        ws.add_table(table)
        ws.freeze_panes = 'A2'
        for i, c in enumerate(cols, start=1):
            ws.column_dimensions[get_column_letter(i)].width = min(26, max(12, len(HEADER_LABELS.get(c, c)) + 3))

        def col_of(name):
            return get_column_letter(cols.index(name) + 1)
        red, purple, white = PatternFill('solid', fgColor='C62828'), PatternFill('solid', fgColor='7C3AED'), Font(bold=True, color='FFFFFF')
        for f in ('name', 'phone'):
            ws['%s1' % col_of(f)].fill = red
            ws['%s1' % col_of(f)].font = white
        for f in ('closure_date', 'closure_status', 'unit_no', 'unit_type', 'booking_amount', 'total_amount', 'closure_remarks'):
            ws['%s1' % col_of(f)].fill = purple
            ws['%s1' % col_of(f)].font = white

        lists = wb.create_sheet('Lists')
        lists.sheet_state = 'hidden'
        for i, n in enumerate(projects, start=1):
            lists['A%d' % i] = n
        for i, n in enumerate(sources, start=1):
            lists['B%d' % i] = n

        MAXROW = 1000
        def add_dv(name, formula):
            dv = DataValidation(type='list', formula1=formula, allow_blank=True, showErrorMessage=True, errorStyle='warning')
            ws.add_data_validation(dv)
            dv.add('%s2:%s%d' % (col_of(name), col_of(name), MAXROW))
        for field, vals in STATUS.items():
            add_dv(field, '"%s"' % vals)
        if projects:
            add_dv('project', 'Lists!$A$1:$A$%d' % len(projects))
        if sources:
            add_dv('source', 'Lists!$B$1:$B$%d' % len(sources))

        ref = wb.create_sheet('Reference — codes & values')
        ref.append([
            '— TEAM — put this code in the STM Code / SV Referred By Code columns —' if uploader_is_stm else
            '— TEAM — put this code in the Telecaller Code / STM Code / SV Referred By Code columns —',
        ])
        ref.append(['User Code', 'Name', 'Role / Designation', 'Phone'])
        ref['A2'].font = Font(bold=True)
        for u in users:
            ref.append([u['user_code'] or '—', u['name'], (u['designation'] or u['role'] or ''), u['phone'] or ''])
        ref.append([])
        ref.append(['— ALLOWED VALUES (the Leads sheet has dropdowns for these) —'])
        for k, v in STATUS.items():
            ref.append([HEADER_LABELS.get(k, k), v.replace(',', ', ')])
        ref.append(['Purpose (multi-select — separate multiple with a comma)', PURPOSE_VALUES])
        ref.append([])
        ref.append(['— NOTES —'])
        ref.append(['Header colours: RED = required (name, phone). PURPLE = closure columns.'])
        ref.append(['Dates: dd-mm-yyyy. project/source are matched by name. Leave a cell blank to skip.'])
        ref.append(['Fill any sv_* column to create a Site Visit; fill closure_date to create a Closure.'])
        ref.append(['purpose accepts multiple values in one cell, e.g. "investment, end_use".'])
        ref.column_dimensions['A'].width = 24
        ref.column_dimensions['B'].width = 62

        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        resp = HttpResponse(buf.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        resp['Content-Disposition'] = 'attachment; filename="vistara_pipeline_import_template.xlsx"'
        return resp


# ── Reports ───────────────────────────────────────────────────────────────────
class ReportsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Count, Sum, Q

        user      = request.user
        leads_qs  = scope_to_company(Lead.objects.all(), user)
        sv_qs     = scope_to_company(SiteVisit.objects.all(), user, 'lead__company')
        closure_qs = scope_to_company(Closure.objects.all(), user, 'company').exclude(status='cancelled')
        company_id = request.query_params.get('company_id')
        if company_id and is_platform_admin(user):
            leads_qs   = leads_qs.filter(company_id=company_id)
            sv_qs      = sv_qs.filter(lead__company_id=company_id)
            closure_qs = closure_qs.filter(company_id=company_id)

        # Optional date window — bounds the aggregate scans. No default, so the
        # existing all-time behaviour is unchanged unless the client sends dates.
        date_from = request.query_params.get('date_from')
        date_to   = request.query_params.get('date_to')
        if date_from:
            leads_qs   = leads_qs.filter(created_at__date__gte=date_from)
            sv_qs      = sv_qs.filter(created_at__date__gte=date_from)
            closure_qs = closure_qs.filter(closure_date__gte=date_from)
        if date_to:
            leads_qs   = leads_qs.filter(created_at__date__lte=date_to)
            sv_qs      = sv_qs.filter(created_at__date__lte=date_to)
            closure_qs = closure_qs.filter(closure_date__lte=date_to)

        # Hierarchy scope: managers (anyone with reports below them) get a team report
        # over their subtree; leaf users get a personal report. Admins/top heads see all.
        if _sees_all_company(user, request):
            team_view = True
        else:
            _ids = _visible_user_ids(user)
            leads_qs   = leads_qs.filter(Q(stm__in=_ids) | Q(telecaller__in=_ids))
            sv_qs      = sv_qs.filter(Q(stm__in=_ids) | Q(referred_by_telecaller__in=_ids))
            closure_qs = closure_qs.filter(Q(stm__in=_ids) | Q(referred_by_telecaller__in=_ids))
            team_view  = len(_ids) > 1  # has at least one subordinate → manager view

        def get_campaigns():
            return list(
                leads_qs.exclude(meta_campaign_name='')
                .values('meta_campaign_name')
                .annotate(
                    total=Count('id'),
                    warm=Count('id', filter=Q(status__in=['warm_transferred', 'sv_scheduled', 'sv_done', 'closed'])),
                    sv=Count('id', filter=Q(status__in=['sv_done', 'closed'])),
                    closed=Count('id', filter=Q(status='closed')),
                )
                .order_by('-total')[:20]
            )

        def get_telecallers():
            return list(
                leads_qs.exclude(telecaller__isnull=True)
                .values('telecaller__id', 'telecaller__name')
                .annotate(
                    total=Count('id'),
                    warm=Count('id', filter=Q(telecaller_status='warm')),
                    transferred=Count('id', filter=Q(status='warm_transferred')),
                    sv=Count('id', filter=Q(status__in=['sv_done', 'closed'])),
                )
                .order_by('-total')
            )

        def get_stms():
            return list(
                leads_qs.exclude(stm__isnull=True)
                .values('stm__id', 'stm__name')
                .annotate(
                    total=Count('id'),
                    hot=Count('id', filter=Q(stm_status='hot')),
                    sv_scheduled=Count('id', filter=Q(stm_status='sv_scheduled')),
                    sv_done=Count('id', filter=Q(stm_status__in=['sv_done'])),
                    closed=Count('id', filter=Q(status='closed')),
                )
                .order_by('-total')
            )

        def get_summary():
            # Amounts are encrypted at rest → can't SQL-Sum; sum in Python. Revenue is
            # the FULL closure value (total_amount = final amount), falling back to
            # booking_amount (plot basic) for older closures with no total.
            cnt = closure_qs.count()
            total = sum((c.total_amount or c.booking_amount or 0) for c in closure_qs.only('id', 'booking_amount', 'total_amount'))
            return {
                'total_sv':       sv_qs.count(),
                'completed_sv':   sv_qs.filter(status='completed').count(),
                'total_closures': cnt,
                'total_revenue':  float(total or 0),
                'meta_leads':     leads_qs.exclude(meta_campaign_name='').count(),
            }

        def get_closures():
            return (closure_qs.select_related('lead', 'project', 'stm', 'referred_by_telecaller')
                    .defer(*PROJECT_BLOBS).order_by('-closure_date')[:20])

        # Run sequentially. These are indexed aggregates (fast); the previous
        # ThreadPoolExecutor opened 5 DB connections per request and didn't close
        # them in the worker threads — a connection leak that, with the pooled
        # endpoint + multiple gunicorn workers, risked exhausting Neon.
        return Response({
            # Team-performance tables are management-only; personal reports omit them.
            'team_view':   team_view,
            'campaigns':   get_campaigns()   if team_view else [],
            'telecallers': get_telecallers() if team_view else [],
            'stms':        get_stms()        if team_view else [],
            'closures':    ClosureSerializer(get_closures(), many=True).data,
            'summary':     get_summary(),
        })


class MyTeamView(APIView):
    """Everyone reporting under the requester (their org subtree), with lead/closure
    counts — powers the manager 'My Team' view. Returns [] for users with no reports."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        # Honour the admin "Viewing Company" filter (?company_id) for platform admins.
        company = _resolve_company(request)
        module = (request.query_params.get('module') or '').strip()  # department/module org chart
        scope  = request.query_params.get('scope')                   # 'all' → full company org
        admin_view = request.query_params.get('admin_view') == '1'
        cp_chart = request.query_params.get('cp') == '1'             # Channel Partner org chart
        ids = _visible_user_ids(user) - {user.id}   # subtree, excluding self
        is_admin = _sees_all_company(user, request, include_manager_role=False)

        def _full_company():
            # Everyone in a reporting relationship + all Managers (leadership shows
            # even before anyone reports to them); standalone users stay out.
            return list(
                User.objects.filter(company=company, is_active=True)
                .filter(
                    Q(reporting_manager__isnull=False)
                    | Q(subordinates__isnull=False)
                    | Q(role__in=MANAGER_ROLES)
                )
                .distinct().select_related('reporting_manager').order_by('name')
            )

        if is_admin and cp_chart:
            # The Channel Partner org chart. Scoped by designation rather than by
            # `module`, because there is no "Channel Partner" module to assign anyone
            # to — CP staff sit in Sales and are marked by a CP designation, which is
            # the same test that decides who gets into the module at all.
            all_users = (User.objects.filter(company=company, is_active=True)
                         .select_related('reporting_manager').order_by('name'))
            members = [u for u in all_users if is_cp_designated(u)]
            ids = {u.id for u in members}
        elif is_admin and module:
            # Department/module org chart — users assigned to this module.
            all_users = (User.objects.filter(company=company, is_active=True)
                         .select_related('reporting_manager').order_by('name'))
            members = [u for u in all_users
                       if module in (u.modules or []) or module in (u.manager_modules or [])]
            ids = {u.id for u in members}
        elif is_admin and (scope == 'all' or admin_view or not ids):
            # Full company org (User Management / admin default).
            members = _full_company()
            ids = {u.id for u in members}
        elif not ids:
            return Response([])
        else:
            members = list(
                User.objects.filter(id__in=ids, company=company)
                .select_related('reporting_manager').order_by('name')
            )
        # Admins never appear in the org chart — it reflects the operational hierarchy.
        members = [m for m in members
                   if getattr(m, 'role', '') != 'Admin' and not getattr(m, 'is_staff', False)]
        ids = {m.id for m in members}
        # Owned-lead counts (as STM or telecaller) and closure counts, in a few aggregates.
        lead_counts, closure_counts = {}, {}
        for fld in ('stm_id', 'telecaller_id'):
            for row in Lead.objects.filter(company=company, **{f'{fld}__in': ids}).values(fld).annotate(c=Count('id')):
                lead_counts[row[fld]] = lead_counts.get(row[fld], 0) + row['c']
        for fld in ('stm_id', 'referred_by_telecaller_id'):
            for row in (Closure.objects.filter(company=company, **{f'{fld}__in': ids})
                        .exclude(status='cancelled').values(fld).annotate(c=Count('id'))):
                closure_counts[row[fld]] = closure_counts.get(row[fld], 0) + row['c']
        data = [{
            'id':                u.id,
            'name':              u.name,
            'user_code':         u.user_code,
            'designation':       u.designation,
            'role':              u.role,
            'phone':             u.phone,
            'email':             u.email,
            'reporting_manager':    u.reporting_manager.name if u.reporting_manager_id else None,
            'reporting_manager_id': u.reporting_manager_id,
            'is_direct_report':     u.reporting_manager_id == user.id,
            'leads':             lead_counts.get(u.id, 0),
            'closures':          closure_counts.get(u.id, 0),
        } for u in members]
        return Response(data)


# ──────────────────────────────────────────────
#  Booking  (native plot booking — replaces the GAS web app for Vistara)
# ──────────────────────────────────────────────

def _loi_enabled(company):
    """LOI / EOI documents are a per-company entitlement, not a platform feature."""
    return bool(getattr(company, 'loi_enabled', False))


LOI_NAME_ALLOWED = r'[^A-Za-z0-9 ._-]+'


def _loi_safe(s):
    """A file/folder name that can never break a signed link: letters, digits,
    spaces, dot, underscore and hyphen only. Everything else a buyer's name can
    hold — "&", "%", ",", brackets, slashes, non-Latin script — is dropped.

    This used to be a list of characters to remove, grown one broken booking at a
    time ("&" for "PARAG & SAHIL BHAI", then "," for joint buyers "A (50) ,B (50)"),
    each failing every open with Supabase's InvalidSignature. An allow-list can't
    be surprised by the next character."""
    import re
    import unicodedata
    text = unicodedata.normalize('NFKD', str(s or '')).encode('ascii', 'ignore').decode()
    text = re.sub(LOI_NAME_ALLOWED, ' ', text)
    return re.sub(r'\s+', ' ', text).strip(' .') or 'NA'


def _loi_path(b):
    """GAS-style object path: <Project>/Plot <no> - <Client>/R<rev>_LOI_Plot<no>_<Client>.pdf"""
    proj = _loi_safe(b.project.name if b.project_id else 'Project')
    # EOI bookings have no plot — fall back to the EOI code held in plot_numbers.
    plot = _loi_safe(b.plot.number if b.plot_id else (b.plot_numbers or b.area))
    client = _loi_safe(b.client_name)
    rev = b.revision_no or 0
    return f'{proj}/Plot {plot} - {client}/R{rev}_LOI_Plot{plot}_{client}.pdf'


def _next_eoi_no(company, project_id, prefer='', block=None):
    """Next per-project EOI code. Honours a client-supplied code if it's still free,
    otherwise assigns the next available so numbers never collide.

    Default format (block=None, every pre-existing call site): EOI-1, EOI-2, …
    Block-wise industrial projects pass `block` instead — a block's own running
    number, prefixed with the block letter ('E' -> E1, E2, …) or bare if there's no
    block ('' -> 1, 2, 3…). Scoped to block_industrial projects only, so this never
    changes behaviour for any existing project."""
    prefer = (prefer or '').strip()
    if block is not None:
        import re
        prefix = block or ''
        existing = set(
            Booking.objects.filter(company=company, project_id=project_id)
            .exclude(plot_numbers='').values_list('plot_numbers', flat=True)
        )
        if prefer and prefer not in existing:
            return prefer
        pat = re.compile(rf'^{re.escape(prefix)}(\d+)$')
        used = [int(m.group(1)) for code in existing if (m := pat.match(code))]
        n = (max(used) + 1) if used else 1
        while f'{prefix}{n}' in existing:
            n += 1
        return f'{prefix}{n}'

    existing = set(
        Booking.objects.filter(company=company, project_id=project_id,
                               plot_numbers__istartswith='EOI')
        .values_list('plot_numbers', flat=True)
    )
    if prefer and prefer not in existing:
        return prefer
    n = len(existing) + 1
    while f'EOI-{n}' in existing:
        n += 1
    return f'EOI-{n}'


# A schedule typed a few hundred rupees short (or over) used to slip through: the
# form checked percentages to ±0.01% (about ₹1,500 on a ₹1.5 Cr schedule), and the
# server did not add the installments up at all. Rounding each installment to the
# rupee can leave a rupee or two; anything beyond this is a typing mistake.
SCHEDULE_TOLERANCE = Decimal('10')


def _schedule_gap(data):
    """How far a booking's payment schedule is from its deal, in rupees (schedule −
    deal), or None when there is no schedule to check. The deal is every sale-deed
    and Extra Work (NSD) installment, the "Extra" row (Legal & Other Charges with
    stamp duty and registration) and any extra-work installments. EOIs carry only a
    token by design and are not checked; neither is a booking whose schedule has no
    amounts (a Pratishtha Regular plan)."""
    if data.get('eoi') or str(data.get('plot_numbers') or '').upper().startswith('EOI'):
        return None

    def amt(x):
        try:
            return Decimal(str((x or {}).get('amt') or 0))
        except (InvalidOperation, ValueError, TypeError, AttributeError):
            return Decimal('0')

    inst = data.get('installments') or []
    ew = data.get('extra_work_inst') or []
    if not isinstance(inst, list):
        return None
    is_extra_row = lambda i: str((i or {}).get('no', '')).strip().lower() == 'extra'
    # Only a schedule whose unit / Extra Work installments carry amounts is checked —
    # the Legal & Other row alone is not a schedule.
    if not any(amt(i) > 0 for i in inst if not is_extra_row(i)):
        return None
    try:
        deal = Decimal(str(data.get('final_amount') or 0))
    except (InvalidOperation, ValueError):
        return None
    total = sum((amt(i) for i in inst), Decimal('0')) + sum((amt(i) for i in (ew if isinstance(ew, list) else [])), Decimal('0'))
    if not any(is_extra_row(i) for i in inst):
        try:
            total += Decimal(str(data.get('total_extra') or 0))
        except (InvalidOperation, ValueError):
            pass
    return total - deal


MIN_INST_YEAR, MAX_INST_YEAR = 2015, 2100


def _bad_installment_dates(data):
    """Installment dates whose year can't be real — "0026-01-26" for 2026, typed on
    the booking form. AR then reads the unit as two thousand years overdue and it
    tops the collections list with a meaningless figure."""
    bad = []
    for key in ('installments', 'extra_work_inst'):
        for i in (data.get(key) or []):
            d = str((i or {}).get('date') or '')
            if len(d) >= 4 and d[:4].isdigit() and not (MIN_INST_YEAR <= int(d[:4]) <= MAX_INST_YEAR):
                bad.append((str((i or {}).get('no') or '?'), d))
    return bad


def _schedule_error(data):
    bad = _bad_installment_dates(data)
    if bad:
        return ('Check the installment date%s: %s. The year must be between %d and %d.'
                % ('' if len(bad) == 1 else 's',
                   ', '.join('#%s is %s' % (no, d) for no, d in bad[:5]), MIN_INST_YEAR, MAX_INST_YEAR))
    gap = _schedule_gap(data)
    if gap is None or abs(gap) <= SCHEDULE_TOLERANCE:
        return None
    word = 'short of' if gap < 0 else 'more than'
    return (f'The payment schedule is Rs. {abs(gap):,.0f} {word} the total deal. '
            f'Adjust the installments so they add up to the deal amount exactly.')


class BookingListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = _resolve_company(request)
        qs = (Booking.objects.filter(company=company)
              .select_related('project', 'plot', 'stm', 'approved_by', 'rejected_by', 'cancelled_by',
                              'resale_of', 'accounts_approved_by', 'accounts_rejected_by')
              .defer(*PROJECT_BLOBS))
        # Drafts are half-finished commercial terms, so they are not browsable by
        # every manager the way a submitted booking is. Visible to their author, to a
        # real admin, and to whoever approves that project's bookings — the same people
        # who can already cancel a drafted unit from the plot map, so the two agree.
        #
        # Which approver list governs follows the booking's own routing, as approve and
        # reject do: a Channel-Partner-sourced deal answers to cp_booking_approvers,
        # everything else to booking_approvers. A regular approver therefore does not
        # see CP drafts on their project, or the separation would be undone here.
        #
        # This runs before every other visibility rule below, since those exist to
        # broaden access and would otherwise let drafts back in by another route.
        if not _is_hard_admin(request.user):
            cp_sourced = (Q(source__iexact='Channel Partner')
                          | Q(lead__in=Lead.objects.filter(cp_lead_q())))
            visible_draft = (
                Q(stm=request.user)
                | (Q(project_id__in=_approver_project_ids(request.user, company)) & ~cp_sourced)
                | (Q(project_id__in=_cp_approver_project_ids(request.user, company)) & cp_sourced)
            )
            qs = qs.exclude(Q(status='draft') & ~visible_draft)
        # Naming someone a project's booking approver is a narrowing statement: they
        # review those projects and no others. It therefore takes precedence over the
        # broad org-tree visibility a Manager may otherwise have (a top-of-tree head
        # like Sachin sees all company data everywhere else, but approves only the
        # projects he is named on). `?mine` is the user's own bookings list, so it is
        # left on the normal scoping or an approver loses sight of their own bookings
        # in projects they don't approve. Real admins are exempt entirely.
        # Own id plus everyone reporting to the requester, transitively — the pool a
        # manager sees when no approver list narrows things.
        own_and_team = _visible_user_ids(request.user)
        cp_scoped = (request.query_params.get('cp_only') == 'true'
                     or is_cp_designated(request.user))
        approver_project_ids = [] if _is_hard_admin(request.user) else _approver_project_ids(request.user, company)
        # A Channel-Partner-sourced booking is gated by its own approver list, so
        # someone named a CP approver (but not a regular one) still needs to see
        # those bookings without gaining visibility into the project's other ones.
        cp_approver_project_ids = [] if _is_hard_admin(request.user) else _cp_approver_project_ids(request.user, company)
        # See _is_cp_sourced_booking: the booking's own Source decides when it says
        # anything, and only a booking naming no source falls back to its lead.
        is_cp_booking_q = cp_booking_q()

        # Two screens, two questions, and conflating them is what made this drift.
        #
        #   `mine` — "My Bookings": what I and my people have sold. Own work plus the
        #   reporting tree, and in the CP module the CP pool as well, since that is
        #   the business the module exists to track.
        #
        #   otherwise — "Approvals": what I am named to decide, and nothing else.
        #   Deliberately NOT widened to my own work: a booking I made but cannot
        #   approve does not belong on the screen where the verdict is given. It shows
        #   under My Bookings instead.
        if request.query_params.get('mine'):
            mine_q = Q(stm_id__in=own_and_team)
            # The CP pool belongs to My Bookings only in the CP module, so this reads
            # the explicit flag rather than the viewer's designation: a CP manager
            # looking at Sales My Bookings should see their own and their team's work,
            # not every partner-sourced deal in the company.
            if request.query_params.get('cp_only') == 'true':
                mine_q |= is_cp_booking_q
            qs = qs.filter(mine_q)
        else:
            if approver_project_ids or cp_approver_project_ids:
                qs = qs.filter(
                    (Q(project_id__in=approver_project_ids) & ~is_cp_booking_q)
                    | (Q(project_id__in=cp_approver_project_ids) & is_cp_booking_q)
                )
            elif not _sees_all_company(request.user, request, include_manager_role=False):
                qs = qs.filter(stm_id__in=own_and_team)
            if cp_scoped:
                # The CP module's approvals are the CP pool, full stop. Reuse
                # is_cp_booking_q rather than cp_lead_q alone: a Sales-module booking
                # tagged Source = "Channel Partner" routes to the CP approvers, so it
                # has to be visible to them or it is authorized but unreachable.
                qs = qs.filter(is_cp_booking_q)
        # Which book this list is: the Sales module asks for source=sales so its
        # bookings and approvals hold no partner-sourced deals — those belong to
        # Channel Partner, which has its own module, its own approvers and its own
        # list. A booking counts as partner-sourced by its own Source, falling back
        # to its lead — the same rule the CP module's scoping uses, so the two
        # always agree and no booking lands in both.
        source = (request.query_params.get('source') or '').lower()
        if source == 'cp':
            qs = qs.filter(is_cp_booking_q)
        elif source == 'sales':
            qs = qs.exclude(is_cp_booking_q)
        # Chains are resolved against the whole company, not this viewer's slice —
        # otherwise whether a replaced booking still shows depends on who is looking.
        qs = _drop_superseded_revisions(qs, scope=Booking.objects.filter(company=company))
        if request.query_params.get('closure'):
            qs = qs.filter(closure_id=request.query_params['closure'])
        if request.query_params.get('plot'):
            qs = qs.filter(plot_id=request.query_params['plot'])
        # A cancelled booking and a rejected one both sit at status='rejected' — the
        # difference is in approval_status, and they are different events: one was
        # refused before it counted, the other was a live sale that came off the
        # books and keeps its signed LOI. The tabs ask for them separately, so the
        # filter separates them rather than lumping both under Rejected.
        st = request.query_params.get('status')
        if st == 'cancelled':
            qs = qs.filter(status='rejected', approval_status__icontains='CANCEL')
        elif st == 'rejected':
            qs = qs.filter(status='rejected').exclude(approval_status__icontains='CANCEL')
        elif st:
            qs = qs.filter(status=st)
        # The visibility rules above are ORed conditions that reach through `lead`
        # into the CP directory and the source table. Those are forward foreign keys
        # today, so a booking matching two arms still comes back once — but that is a
        # property of the current joins, not of the query, and one reverse or
        # many-to-many relation added to cp_lead_q would start listing bookings twice
        # with nothing to signal it. Cheap to assert here, and the alternative is a
        # duplicate showing up in someone's sales figures.
        # Resolved once for the whole page rather than per row: the CP test reaches
        # into the lead's partner and source, so the serializer computing it alone
        # would be a query per booking. Powers the 'Source: CP' filter in the CP
        # module, and it is the same predicate that routes approvals.
        qs = qs.annotate(cp_sourced_ann=Case(
            When(is_cp_booking_q, then=Value(True)),
            default=Value(False), output_field=BooleanField()))
        return Response(
            BookingSerializer(qs.distinct(), many=True, context={'request': request}).data)

    def post(self, request):
        company = _resolve_company(request)
        data = request.data

        # Reject an oversized signed LOI before any booking/lead/plot side effects run —
        # base64 inflates the raw file by ~1/3, so compare against the encoded length.
        lf_check = data.get('loi_file')
        if isinstance(lf_check, dict) and lf_check.get('data'):
            max_b64_len = int(settings.MAX_UPLOAD_FILE_MB * 1024 * 1024 * 4 / 3)
            if len(lf_check['data']) > max_b64_len:
                return Response(
                    {'detail': f'File too large (max {settings.MAX_UPLOAD_FILE_MB} MB).'},
                    status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                )

        schedule_err = _schedule_error(data)
        if schedule_err:
            return Response({'detail': schedule_err, 'installments': [schedule_err]}, status=status.HTTP_400_BAD_REQUEST)

        # A booking against a project that HAS plots mapped must name one. Without
        # this the API accepted a booking with no plot, so nothing was reserved and
        # the unit map kept showing the unit as available — and because the display
        # falls back to the area, an 80,000 sq.ft parcel rendered as "Unit 80000".
        #
        # EOIs are exempt by design: they are raised before a unit is chosen. A
        # project with no plots mapped is left alone too — land sold by area (the
        # industrial projects) has no unit list to choose from, and refusing would
        # stop those sales outright.
        if not data.get('eoi'):
            proj_id = data.get('project')
            has_plot = bool(data.get('plot')) or bool(
                [x for x in (data.get('plot_ids') or []) if str(x).isdigit()])
            if proj_id and not has_plot and Plot.objects.filter(project_id=proj_id).exists():
                return Response(
                    {'detail': 'Select a unit for this booking — this project has units mapped.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Pratishtha prices every unit from that unit's price book. With no book
            # there is nothing to price from, and the client falls back to a rate-based
            # form whose formulas have no pratishtha branch — that saved bookings at a
            # zero total. The client blocks this now, but an older build (or a direct
            # call) must not be able to write a mispriced booking. EOIs stay exempt:
            # they are raised before a unit is chosen, so no book applies.
            if proj_id and has_plot and Project.objects.filter(
                    pk=proj_id, formula_set='pratishtha').exists():
                pids = [int(x) for x in (data.get('plot_ids') or []) if str(x).isdigit()]
                if not pids and str(data.get('plot') or '').isdigit():
                    pids = [int(data['plot'])]
                bookless = [
                    p.number for p in Plot.objects.filter(pk__in=pids)
                    if not (p.price_book or {})
                ]
                if bookless:
                    return Response(
                        {'detail': 'Price book not loaded for %s. A Pratishtha booking '
                                   'cannot be priced until it is.' % ', '.join(bookless)},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        # Guard against duplicate submissions: a plot shouldn't have more than one
        # active (pending/approved) booking at a time. Traced real production
        # duplicates (same client/plot/amount, 10-30s apart) to a user resubmitting
        # after an unclear success state — this is the authoritative backend check
        # regardless of the exact client-side cause. Revisions (revision_of)
        # legitimately reuse the same plot, so they're excluded, as are EOIs
        # (no real plot reserved yet).
        # Declared out here, not inside the branch below: a revision or an EOI skips
        # that branch entirely and this is read further down regardless.
        resale_of = None
        if not data.get('revision_of') and not data.get('eoi'):
            requested_plot_ids = set()
            raw_plot_ids = data.get('plot_ids')
            if isinstance(raw_plot_ids, list) and raw_plot_ids:
                requested_plot_ids = {int(x) for x in raw_plot_ids if str(x).isdigit()}
            elif data.get('plot') and str(data['plot']).isdigit():
                requested_plot_ids = {int(data['plot'])}
            # A unit an admin has put back on the market carries its earlier sale on
            # purpose — that sale is what is being resold, so it must not read as a
            # clash. Without this the guard closed the resale flow outright: booking a
            # resale unit answered "this plot already has a sold booking for …".
            resale_pids = set(Plot.objects.filter(
                id__in=requested_plot_ids, status='resale').values_list('id', flat=True))
            if requested_plot_ids:
                # 'sold' is the stored status of an approved booking — there is no
                # 'approved' row anywhere in the table. Looking for one meant the guard
                # only ever caught a second submission while the first was still
                # pending, and waved through every unit that had already been sold.
                # Six units in production ended up with two live approved bookings to
                # two different buyers, five of them on one project.
                active = Booking.objects.filter(company=company, status__in=['pending', 'sold'])
                for b in active.only('id', 'plot_id', 'plot_ids', 'client_name', 'status'):
                    b_plot_ids = set(b.plot_ids or [])
                    if b.plot_id:
                        b_plot_ids.add(b.plot_id)
                    overlap = b_plot_ids & requested_plot_ids
                    if overlap - resale_pids:
                        return Response(
                            {'detail': f'This plot already has a {b.status} booking for {b.client_name} (#{b.id}).'},
                            status=status.HTTP_409_CONFLICT,
                        )
                    if overlap and b.status == 'sold':
                        # The sale being resold. Newest wins if a unit has been round
                        # more than once.
                        if resale_of is None or b.id > resale_of.id:
                            resale_of = b
                # A plot soft-held by a DIFFERENT rep (selected on the plot-map picker
                # via PlotHoldView, not yet submitted) or already sold blocks submission
                # too — closes the gap where someone bypasses the picker's lock (stale
                # page, direct API call) and submits anyway. select_for_update so this
                # resolves consistently against a PlotHoldView call racing at the same
                # instant.
                with transaction.atomic():
                    for p in Plot.objects.select_for_update().filter(pk__in=requested_plot_ids):
                        ok = p.status in ('available', 'resale') or (p.status == 'hold' and p.held_by_id == request.user.id)
                        if not ok:
                            return Response(
                                {'detail': f'Plot {p.number} is no longer available — it may have just been selected or booked by another salesperson.'},
                                status=status.HTTP_409_CONFLICT,
                            )

        # Resolve or create the lead (Book Unit flow types a new client; Record Closure
        # passes an existing lead).
        lead_id = data.get('lead') or None
        if not lead_id and (data.get('client_name') or '').strip():
            src = None
            sname = (data.get('source') or '').strip()
            if sname:
                src = LeadSource.objects.filter(company=company, name__iexact=sname).first()
            # cp_name means different things depending on source (CP name, a plain
            # Reference person, or "Other") — only resolve it against the Channel
            # Partner directory when the source actually is Channel Partner, or a
            # reference person's typed name would wrongly link to a same-named CP.
            # name is encrypted (no blind index), so this has to compare in Python —
            # fine at this scale, same as other encrypted-field lookups in this file.
            channel_partner = None
            cp_name = (data.get('cp_name') or '').strip()
            if cp_name and sname.lower() == 'channel partner':
                channel_partner = next(
                    (cp for cp in ChannelPartner.objects.filter(company=company)
                     if cp.name.strip().lower() == cp_name.lower()),
                    None,
                )
            lead = Lead.objects.create(
                company=company, name=data.get('client_name', '').strip(),
                phone=(data.get('phone') or '').strip(), status='new',
                project_id=data.get('project') or None, source=src,
                channel_partner=channel_partner,
                # STM self-sourced this client straight into a booking — no
                # telecaller ever touched it. stm must be set (same value the
                # Booking itself gets below), or _distribute's telecaller pool
                # (which requires stm__isnull=True) silently scoops this lead up
                # and hands someone else's direct sale to a random telecaller.
                stm=request.user,
            )
            lead_id = lead.id

        # Revision of an existing (sold) booking — carries the prior lead, bumps the
        # revision number, and leaves the plot/closure untouched until approved.
        prior = None
        rev_of = data.get('revision_of')
        if rev_of:
            prior = Booking.objects.filter(id=rev_of, company=company).first()
            if prior:
                lead_id = prior.lead_id

        # Submitting a saved draft promotes that same row instead of creating a new
        # Booking — otherwise the draft would be left behind as an orphaned duplicate.
        draft = None
        if data.get('draft_id'):
            draft = Booking.objects.filter(id=data['draft_id'], company=company,
                                            stm=request.user, status='draft').first()

        ser = BookingSerializer(draft, data=data, partial=True) if draft else BookingSerializer(data=data)
        ser.is_valid(raise_exception=True)
        if prior:
            extra = dict(revision_no=prior.revision_no + 1, closure=prior.closure,
                         revision_of=prior,
                         approval_status='REVISION R%d PENDING' % (prior.revision_no + 1))
            # A revision inherits from its parent only what it does not supply itself.
            # `plot` used to be inherited unconditionally, which overwrote a unit the
            # revision had just chosen — and when the parent was an EOI holding no
            # plot, it overwrote that choice with nothing. Carrying the unit and area
            # across matters just as much: without them a revision of an EOI on a
            # project with no plots mapped came out blank and displayed its area as
            # though it were a plot number.
            chose_plot = bool(data.get('plot')) or bool(
                [x for x in (data.get('plot_ids') or []) if str(x).isdigit()])
            if not chose_plot:
                extra['plot'] = prior.plot
                if not str(data.get('plot_numbers') or '').strip() and prior.plot_numbers:
                    extra['plot_numbers'] = prior.plot_numbers
            if not str(data.get('area') or '').strip() and prior.area:
                extra['area'] = prior.area
        else:
            extra = dict(revision_no=0, approval_status='PENDING')
        booking = ser.save(company=company, stm=request.user, lead_id=lead_id, status='pending', **extra)

        # Multi-plot: resolve ALL selected plots. `plot` stays the primary (first);
        # plot_ids holds every selected id and plot_numbers is the comma display.
        pids = data.get('plot_ids')
        if isinstance(pids, list) and pids:
            pids = [int(x) for x in pids if str(x).isdigit()]
        elif prior and prior.plot_ids:
            pids = list(prior.plot_ids)
        elif booking.plot_id:
            pids = [booking.plot_id]
        else:
            pids = []
        if resale_of is not None and not booking.is_resale:
            booking.is_resale = True
            booking.resale_of = resale_of
            booking.save(update_fields=['is_resale', 'resale_of'])
        if pids:
            num_map = dict(Plot.objects.filter(id__in=pids).values_list('id', 'number'))
            booking.plot_ids = pids
            booking.plot_numbers = ', '.join(num_map[p] for p in pids if p in num_map)
            if not booking.plot_id:
                booking.plot_id = pids[0]
            booking.save(update_fields=['plot_ids', 'plot_numbers', 'plot'])

        # EOI (Expression of Interest) — a booking on a project that has no plots yet
        # (raised before govt approvals). No plot is reserved; the sequential per-project
        # EOI code (EOI-1, EOI-2, …) is stored in plot_numbers so the LOI renders as an EOI.
        if data.get('eoi'):
            if prior:
                # Revising an EOI keeps the same EOI code (EOI-20 stays EOI-20).
                booking.plot_numbers = prior.plot_numbers
            else:
                # Block-prefixed numbering only for block-wise industrial projects.
                eoi_block = data.get('eoi_block') if getattr(booking.project, 'block_industrial', False) else None
                booking.plot_numbers = _next_eoi_no(company, booking.project_id,
                                                     prefer=(data.get('eoi_no') or ''), block=eoi_block)
            booking.save(update_fields=['plot_numbers'])

        # A booking on a project with no units mapped, submitted without a unit and
        # without the EOI flag, would otherwise carry no identity at all — the list
        # then falls back to its area, which is how "80,000 sq.ft" once rendered as
        # "Unit 80000". There is no other identity available on such a project, so it
        # is numbered as the EOI it effectively is. Projects that DO have units are
        # already refused above unless one is named.
        if (not data.get('eoi') and not booking.plot_numbers and not booking.plot_id
                and booking.project_id
                and not Plot.objects.filter(project_id=booking.project_id).exists()):
            booking.plot_numbers = _next_eoi_no(company, booking.project_id)
            booking.save(update_fields=['plot_numbers'])

        # Signed LOI (sent as base64 {name,type,data}). Stored GAS-style:
        # <Project>/Plot <no> - <Client>/R<rev>_LOI_Plot<no>_<Client>.pdf
        lf = data.get('loi_file')
        if isinstance(lf, dict) and lf.get('data') and not _loi_enabled(company):
            return Response(
                {'detail': 'LOI / EOI documents are not enabled for this company.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if isinstance(lf, dict) and lf.get('data'):
            import base64
            from django.core.files.base import ContentFile
            try:
                booking.loi_document.save(_loi_path(booking),
                                          ContentFile(base64.b64decode(lf['data'])), save=True)
            except Exception:
                # The file may already be in storage (e.g. the storage POST succeeded but the
                # model save failed, or the request timed out). Persist the deterministic path
                # so the signed LOI isn't orphaned/invisible, and surface the error in logs.
                import logging
                logging.getLogger(__name__).exception('LOI document save failed for booking %s', booking.id)
                try:
                    booking.loi_document.name = _loi_path(booking)
                    booking.save(update_fields=['loi_document'])
                except Exception:
                    logging.getLogger(__name__).exception('LOI document path relink failed for booking %s', booking.id)

        if not prior:
            # New booking: reserve ALL selected plots. The Closure is mirrored into
            # My Conversions on APPROVAL (see BookingActionView) — so a booking that
            # is still pending approval does NOT appear as a booked closure.
            # held_by/held_at are cleared here — this is now a hard hold backed by a
            # real pending Booking, not the picker's soft hold, so it never auto-expires.
            # pre_hold_status is normally already set by PlotHoldView's earlier soft
            # hold (left untouched here); the fallback captures it directly for a
            # submission that skipped that step and arrived straight from
            # available/resale.
            if pids:
                Plot.objects.filter(id__in=pids).update(
                    pre_hold_status=Case(When(pre_hold_status='', then=F('status')), default=F('pre_hold_status')),
                    status='hold', held_by=None, held_at=None,
                )

        # Notify the admin-selected approvers (managers) via push.
        _notify_booking_approvers(company, booking, request.user)
        # The rep gets a receipt, and Accounts & Finance follow the money from the
        # moment it is submitted — a revised LOI changes the figure they track.
        _notify_booking_event(company, booking, 'submitted', request.user, 'awaiting Sales approval', request=request)

        return Response(BookingSerializer(booking).data, status=status.HTTP_201_CREATED)


def _can_view_booking(user, booking, company):
    """Who may open one booking.

    The person whose it is, a real admin, and whoever approves it — the same
    authority the approvals screen uses, via _can_approve_booking, so a CP-sourced
    booking follows the CP list.

    Beyond that, anything the viewer's own list already shows them: their reporting
    tree, and in the CP module the partner-sourced pool. Without this a director
    could read a booking in My Bookings and be refused when opening it — which is
    what happened to the revision history, since approving no projects is normal for
    someone who sees everything through the tree instead.

    A draft stops at the narrow rule. Half-finished commercial terms are deliberately
    not browsable by every manager (see the draft rule in BookingListCreateView), and
    widening the general case must not quietly undo that.
    """
    if (booking.stm_id == user.id
            or _is_hard_admin(user)
            or _can_approve_booking(user, booking.project_id, booking.project_id,
                                    booking.lead_id, company, booking.source)):
        return True
    if booking.status == 'draft':
        return False
    if booking.stm_id in _visible_user_ids(user):
        return True
    if is_cp_designated(user) and _is_cp_sourced_booking(booking.lead_id, booking.source):
        return True
    # Company-wide viewers, and the Accounts & Finance reviewers who already read
    # every approved booking in their own list — the same test that screen uses.
    return bool(_can_view_all_bookings(user))


class BookingRevisionsView(APIView):
    """Every version of one deal, newest first — the current one, then back through
    R1, R0 — each with its own figures and its own signed LOI.

    Newest first because the current terms are what is usually being checked, and the
    history is read backwards from them: what does this deal say now, and what did it
    say before.

    Only the latest version is listed anywhere, which is right: a deal should appear
    once and at its current terms. But the earlier ones are what was signed at the
    time, and until now there was no way to reach them from the product at all.

    Gated exactly as opening the booking is. The earlier versions are the same deal,
    so seeing the current one is the authority to see how it got there.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        company = _resolve_company(request)
        b = Booking.objects.filter(pk=pk, company=company).first()
        # 404 rather than 403: a booking you may not see should not be confirmed to
        # exist by the error you get back.
        if b is None or not _can_view_booking(request.user, b, company):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        qs = (Booking.objects.filter(id__in=_revision_chain_ids(b.id, company), company=company)
              .select_related('project', 'plot', 'stm').order_by('-revision_no', '-id'))
        return Response(BookingSerializer(qs, many=True, context={'request': request}).data)


class BookingDetailView(APIView):
    """One booking by id, for opening it directly rather than hunting the list.

    The booking form used to load a draft by fetching the whole drafts list and
    searching it, which made resuming hostage to whatever scoping that list applies
    — approver narrowing and the CP pool filter both silently returned nothing, and
    the form sat on "Loading unit pricing…" with every field blank. Asking for the
    one record by id removes that class of failure entirely.

    Visible to the person whose booking it is, to a real admin, and to whoever
    approves it — the same authority the approvals screen uses, via
    _can_approve_booking, so a CP-sourced booking follows the CP list.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        company = _resolve_company(request)
        b = (Booking.objects.filter(pk=pk, company=company)
             .select_related('project', 'plot', 'stm').first())
        if b is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not _can_view_booking(request.user, b, company):
            # 404 rather than 403: a booking you may not see should not be
            # confirmed to exist by the error you get back.
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(BookingSerializer(b).data)


class BookingDraftView(APIView):
    """Save an in-progress booking as a draft — same payload shape as
    BookingListCreateView.post, but with none of its completeness requirements (no
    signed LOI, no 100%-installment check — those are the frontend's job to enforce
    only when calling Submit, not Save). Lets a rep persist partially-filled work so
    closing the browser mid-flow doesn't lose it. Pass `id` to update an existing
    draft in place rather than creating a new row on every Save click."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company = _resolve_company(request)
        data = request.data

        draft = None
        if data.get('id'):
            draft = Booking.objects.filter(id=data['id'], company=company,
                                            stm=request.user, status='draft').first()
            if not draft:
                return Response({'detail': 'Draft not found.'}, status=status.HTTP_404_NOT_FOUND)

        # Resolve or create the lead — reuse the draft's existing lead on repeat
        # Saves instead of minting a new one every time the rep clicks Save.
        lead_id = data.get('lead') or (draft.lead_id if draft else None)
        if not lead_id and (data.get('client_name') or '').strip():
            src = None
            sname = (data.get('source') or '').strip()
            if sname:
                src = LeadSource.objects.filter(company=company, name__iexact=sname).first()
            lead = Lead.objects.create(
                company=company, name=data.get('client_name', '').strip(),
                phone=(data.get('phone') or '').strip(), status='new',
                project_id=data.get('project') or None, source=src,
                # STM self-sourced this client straight into a booking — no
                # telecaller ever touched it. stm must be set (same value the
                # Booking itself gets below), or _distribute's telecaller pool
                # (which requires stm__isnull=True) silently scoops this lead up
                # and hands someone else's direct sale to a random telecaller.
                stm=request.user,
            )
            lead_id = lead.id

        ser = BookingSerializer(draft, data=data, partial=True) if draft else BookingSerializer(data=data)
        ser.is_valid(raise_exception=True)
        booking = ser.save(company=company, stm=request.user, lead_id=lead_id, status='draft')

        # Resolve selected plots the same way BookingListCreateView.post does.
        pids = data.get('plot_ids')
        if isinstance(pids, list) and pids:
            pids = [int(x) for x in pids if str(x).isdigit()]
        elif booking.plot_id:
            pids = [booking.plot_id]
        else:
            pids = []

        plot_conflicts = []
        if pids:
            num_map = dict(Plot.objects.filter(id__in=pids).values_list('id', 'number'))
            booking.plot_ids = pids
            booking.plot_numbers = ', '.join(num_map[p] for p in pids if p in num_map)
            if not booking.plot_id:
                booking.plot_id = pids[0]
            booking.save(update_fields=['plot_ids', 'plot_numbers', 'plot'])

            # Claim any plot that's free; never fail the whole save over one that
            # isn't — losing typed data is worse than a stale plot reference. Flag
            # the conflict instead so the frontend can warn without discarding anything.
            with transaction.atomic():
                for plot in Plot.objects.select_for_update().filter(pk__in=pids):
                    if plot.status in ('available', 'resale'):
                        plot.pre_hold_status = plot.status
                        plot.status, plot.held_by, plot.held_at = 'hold', request.user, timezone.now()
                        plot.save(update_fields=['status', 'held_by', 'held_at', 'pre_hold_status'])
                    elif not (plot.status == 'hold' and plot.held_by_id == request.user.id):
                        plot_conflicts.append({'id': plot.id, 'number': plot.number})

        # Signed LOI, if one happens to already be attached — same handling as the
        # real submit path, kept for forward compatibility even though drafts don't
        # require it.
        lf = data.get('loi_file')
        if isinstance(lf, dict) and lf.get('data') and _loi_enabled(booking.company):
            max_b64_len = int(settings.MAX_UPLOAD_FILE_MB * 1024 * 1024 * 4 / 3)
            if len(lf['data']) <= max_b64_len:
                import base64
                from django.core.files.base import ContentFile
                try:
                    booking.loi_document.save(_loi_path(booking),
                                              ContentFile(base64.b64decode(lf['data'])), save=True)
                except Exception:
                    logging.getLogger(__name__).exception('LOI document save failed for draft %s', booking.id)

        resp = BookingSerializer(booking).data
        resp['plot_conflicts'] = plot_conflicts
        return Response(resp, status=status.HTTP_200_OK)


class BookingDiscardDraftView(APIView):
    """Discard a saved draft — releases any plots it still holds and deletes the row.
    Irreversible. The drafter can discard their own; a real Admin can discard anyone's;
    a Manager can discard one belonging to an STM in their own reporting chain, not
    just any Manager company-wide (e.g. from the plot map, where a drafted unit's name
    is visible to the whole team even though the draft's own details aren't)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        company = _resolve_company(request)
        try:
            b = Booking.objects.get(pk=pk, company=company, status='draft')
        except Booking.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        # _visible_user_ids already includes the requester themselves, so this alone
        # covers "the drafter", "an admin" is the only other unconditional case, and a
        # Manager only clears it when the drafting STM is actually in their own
        # reporting subtree — not any Manager company-wide.
        if not _is_hard_admin(request.user) and b.stm_id not in _visible_user_ids(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        pids = b.plot_ids or ([b.plot_id] if b.plot_id else [])
        # A draft is not a live booking, so it never protects a unit itself — but the
        # unit may belong to somebody else's live sale, and discarding the draft must
        # not hand it back to the map.
        _release_plots(pids, booking=b)
        b.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class BookingNextEOIView(APIView):
    """Preview the next per-project EOI code so the form + LOI can show it before submit."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = _resolve_company(request)
        pid = request.query_params.get('project')
        if not pid:
            return Response({'detail': 'project is required'}, status=status.HTTP_400_BAD_REQUEST)
        # Block-prefixed numbering only applies to block-wise industrial projects — a
        # stray `block` param on any other project is ignored so nothing else changes.
        project = Project.objects.filter(id=pid).only('block_industrial').first()
        block = request.query_params.get('block') if (project and project.block_industrial) else None
        return Response({'eoi_no': _next_eoi_no(company, pid, block=block)})


def _drop_superseded_revisions(qs, scope=None):
    """Hide bookings that a later revision has replaced, so a deal appears once.

    Revising a booking creates a NEW row carrying revision_no + 1, and approving that
    revision leaves the original approved as well — so both were listed and the project
    totals counted the deal twice. Only the latest revision should stand.

    `revision_of` is the authoritative link and is followed first. It has only been
    recorded since the field was added, so two older facts still stand in for it, and
    neither works alone:

      * they share a closure — which survives an EOI being converted to an LOI, where
        the unit is renumbered from "EOI-2" to a real plot number; but
      * a revision is issued its own closure, so a plain revise leaves the closures
        different while the unit stays the same.

    So rows are connected if they share EITHER a closure OR a (project, phone, unit),
    and the connections are followed transitively — a chain that was revised twice and
    then converted still resolves to one deal.

    A group is only collapsed when it actually contains a revision, so two ordinary
    bookings that happen to share a key are left alone; rejected rows are excluded,
    belonging in the Rejected tab rather than folded into a live chain. Ties on
    revision_no fall to the newest row, which happens where a booking was revised twice
    from the same parent.

    `scope` is where chains are detected, and should be every booking in the company
    even when `qs` is one person's slice of them. Detecting within the slice alone
    made staleness depend on what the viewer happens to see: a CP cluster head still
    had booking #478 listed as a live approved deal because its replacement, #482,
    was booked by someone outside his module — so his figure read 108 where the same
    slice read 107 in Sales, and the extra row carried superseded commercial terms.
    """
    drop = _superseded_booking_ids(scope if scope is not None else qs)
    return qs.exclude(id__in=drop) if drop else qs


def _superseded_booking_ids(scope):
    """The bookings a later revision has replaced. See _drop_superseded_revisions
    for how a deal's rows are connected; this is which of them lose."""
    drop = set()
    for g in _revision_groups(scope).values():
        if len(g) < 2 or not any((x['revision_no'] or 0) > 0 for x in g):
            continue
        keep = max(g, key=lambda x: ((x['revision_no'] or 0), x['id']))
        drop.update(x['id'] for x in g if x['id'] != keep['id'])
    return drop


def _stats_company_id(request, company_id):
    """Whose books these figures are: the company a platform admin has picked in
    the company switcher, and otherwise the viewer's own."""
    if company_id and is_platform_admin(request.user):
        return company_id
    return getattr(request.user, 'company_id', None)


def _superseded_closure_ids(company_id):
    """Closures left stranded by a revision.

    Revising a booking issues the revision its own closure and leaves the old
    booking's closure on the books, so every closure figure counted a revised
    deal twice — the Sales dashboard read 479 closures where Approvals listed
    418 live bookings for the same company, and My Conversions read 512.

    Only a closure whose every booking has been superseded is dropped: one still
    carried by a live booking is the deal, whatever else points at it.
    """
    if not company_id:
        return set()
    scope = Booking.objects.filter(company_id=company_id)
    dropped = _superseded_booking_ids(scope)
    if not dropped:
        return set()
    stale = {c for c in scope.filter(id__in=dropped).values_list('closure_id', flat=True) if c}
    live = {c for c in scope.exclude(id__in=dropped).values_list('closure_id', flat=True) if c}
    return stale - live


def _revision_groups(scope):
    """The rows of `scope`, gathered into one group per deal. See
    _drop_superseded_revisions for how rows are connected; this is that grouping on
    its own, so the revision history can reuse it rather than restate it."""
    rows = [r for r in scope.values('id', 'project_id', 'phone', 'plot_numbers', 'plot__number',
                                    'area', 'revision_no', 'status', 'closure_id', 'revision_of_id')
            if r['status'] != 'rejected']

    parent = {r['id']: r['id'] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    present = {r['id'] for r in rows}
    seen = {}
    for r in rows:
        # The recorded parent, where there is one — exact, no inference needed.
        if r['revision_of_id'] and r['revision_of_id'] in present:
            union(r['revision_of_id'], r['id'])
        keys = []
        if r['closure_id']:
            keys.append(('closure', r['closure_id']))
        unit = (r['plot_numbers'] or r['plot__number'] or r['area'] or '').strip()
        if unit:
            keys.append(('unit', r['project_id'], (r['phone'] or '').strip(), unit))
        for k in keys:
            if k in seen:
                union(seen[k], r['id'])
            else:
                seen[k] = r['id']

    groups = {}
    for r in rows:
        groups.setdefault(find(r['id']), []).append(r)
    return groups


def _revision_chain_ids(booking_id, company, strict=False):
    """Every booking that is a version of this same deal, this one included.

    Starts from the grouping the listing uses, then walks `revision_of` in both
    directions to pull in rejected versions too: a rejected R1 is excluded from "what
    is live", which is right for a list, but it is exactly what someone opening the
    history wants to see.

    `strict` drops the heuristic grouping and follows only recorded revision_of links.
    The grouping joins rows that merely share a project, phone and unit, which is the
    right guess for showing a history but far too loose for deciding whether a unit is
    free: two genuinely separate bookings on one unit to the same buyer looked like one
    deal, and discarding the draft released the unit out from under a live sale.
    """
    ids = {booking_id}
    if not strict:
        for g in _revision_groups(Booking.objects.filter(company=company)).values():
            group_ids = {r['id'] for r in g}
            if booking_id in group_ids:
                ids |= group_ids
                break
    links = list(Booking.objects.filter(company=company, revision_of__isnull=False)
                 .values_list('id', 'revision_of_id'))
    adjacent = {}
    for child, par in links:
        adjacent.setdefault(child, set()).add(par)
        adjacent.setdefault(par, set()).add(child)
    queue = list(ids)
    while queue:
        for nxt in adjacent.get(queue.pop(), ()):
            if nxt not in ids:
                ids.add(nxt)
                queue.append(nxt)
    return ids


def _can_view_all_bookings(user):
    """Whole-company booking visibility: company-wide viewers (admin/staff/dept head)
    plus the Accounts & Finance department (read-only review of LOIs/EOIs). The
    Manager role is excluded — bookings stay scoped by approver assignment."""
    if _sees_all_company(user, include_manager_role=False):
        return True
    mods = [str(m).lower() for m in (getattr(user, 'modules', None) or [])]
    return any('account' in m or 'finance' in m for m in mods)


class BookingAllView(APIView):
    """Read-only: ALL company bookings (LOI + EOI) for authorised viewers — used by the
    Accounts & Finance module to review booking / LOI / EOI details. No create or edit."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _can_view_all_bookings(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        company = _resolve_company(request)
        qs = _drop_superseded_revisions(
            Booking.objects.filter(company=company)
            .select_related('project', 'plot', 'stm', 'approved_by', 'rejected_by', 'cancelled_by',
                            'resale_of', 'accounts_approved_by', 'accounts_rejected_by')
            .defer(*PROJECT_BLOBS)
        ).order_by('-created_at')
        # is_cp_sourced reaches into the lead; resolved once for the page here rather
        # than per row, the same way the sales list does it.
        qs = qs.annotate(cp_sourced_ann=Case(
            When(cp_booking_q(), then=Value(True)),
            default=Value(False), output_field=BooleanField()))
        # context: can_accounts_approve is per-viewer, so the serializer needs the request.
        return Response(BookingSerializer(qs[:1000], many=True, context={'request': request}).data)


def _can_export_bookings(user):
    """Who may download the approved-bookings workbook.

    Granted per person in User Management (can_export_bookings), plus real admins,
    who have it unconditionally. Deliberately NOT implied by having the Sales module:
    the export crosses the Sales/CP line and carries every commercial figure of every
    approved deal in the company, which is a different thing from being able to work
    your own bookings.
    """
    return bool(_is_hard_admin(user) or getattr(user, 'can_export_bookings', False))


# The workbook's columns, in order: (heading, attribute, kind).
# kind 'money' is summed into the grand total row and formatted as rupees;
# 'num' is a plain number; everything else is text.
BOOKING_EXPORT_COLUMNS = [
    ('Project',                  'project_name',        'text'),
    ('Unit',                     'unit',                'text'),
    ('Module',                   'module',              'text'),
    ('Client Name',              'client_name',         'text'),
    ('Gender',                   'gender',              'text'),
    ('Phone',                    'phone',               'text'),
    ('Address',                  'address',             'text'),
    ('Source',                   'source',              'text'),
    ('CP / Reference Name',      'cp_name',             'text'),
    ('Booked By',                'booked_by',           'text'),
    ('Booking Date',             'booking_date',        'text'),

    ('Area',                     'area',                'text'),
    ('Area Unit',                'area_unit',           'text'),
    ('Construction Area',        'const_area',          'text'),
    ('Villa / Unit Type',        'villa_type',          'text'),

    ('Land Rate',                'land_rate',           'num'),
    ('Development Rate',         'dev_rate',            'num'),
    ('Construction Rate',        'const_rate',          'num'),
    ('Sale Deed Rate',           'sale_deed_rate',      'num'),
    ('Dev Agreement Rate',       'dev_agreement_rate',  'num'),
    ('Sale Deed %',              'sale_deed_pct',       'num'),
    ('Maintenance Rate',         'maint_rate',          'num'),
    ('Maintenance Months',       'maint_months',        'num'),

    ('Plot Basic',               'plot_basic',          'money'),
    ('Plot Development',         'plot_dev',            'money'),
    ('Construction Amount',      'const_amt',           'money'),
    ('Sale Deed',                'sale_deed',           'money'),
    ('Sale Deed Amount',         'sale_deed_amount',    'money'),
    ('Land Sale Deed',           'land_sale_deed',      'money'),
    ('Construction Agreement',   'const_agreement',     'money'),
    ('Development Agreement',    'dev_agreement',       'money'),
    ('Premium Location',         'premium_location',    'money'),
    ('Stamp Duty',               'stamp_duty',          'money'),
    ('Registration Fees',        'reg_fees',            'money'),
    ('GST',                      'gst',                 'money'),
    ('Maintenance',              'maintenance',         'money'),
    ('Maintenance Deposit',      'maint_deposit',       'money'),
    ('Maintenance Advance',      'maint_advance',       'money'),
    ('Legal Charges',            'legal_charges',       'money'),
    ('Total Extra',              'total_extra',         'money'),
    ('Extra Work Amount',        'extra_work_amount',   'money'),
    ('Extra Work Description',   'extra_work_desc',     'text'),
    ('Discount',                 'discount',            'money'),
    ('Final Amount',             'final_amount',        'money'),

    ('Registration Fee Applied', 'apply_reg_fee',       'text'),
    ('Page Fee Applied',         'apply_page_fee',      'text'),
    ('Stamp Duty Applied',       'apply_stamp_duty',    'text'),
    ('GST Applied',              'apply_gst',           'text'),

    ('Revision No',              'revision_no',         'num'),
    ('Approval Status',          'approval_status',     'text'),
    ('Approved By',              'approved_by_name',    'text'),
    ('Approved On',              'approved_on',         'text'),
    ('Accounts Status',          'accounts_status',     'text'),
    ('Accounts Approved By',     'accounts_by',         'text'),
    ('Accounts Approved On',     'accounts_on',         'text'),
]


def _unit_sort_key(unit):
    """Order unit numbers the way a person reads them, not the way strings sort.

    Plain alphabetical puts 1004 before 101 and Shop10 before Shop4. This splits the
    label into text and number runs and compares the numbers as numbers, so 101, 102,
    … 1004, 1102 come out in order and Shop4 sorts before Shop10. Units that start
    with a number (flats, plots) lead; the prefixed ones (Shop-, EOI-) follow, each
    prefix grouped together.
    """
    text = str(unit or '').strip()
    if not text:
        return (2, [])                      # a booking with no unit label goes last
    parts = [p for p in re.split(r'(\d+)', text.upper()) if p]
    return (0 if text[:1].isdigit() else 1,
            [(1, int(p)) if p.isdigit() else (0, p) for p in parts])


class BookingExportView(APIView):
    """Every approved booking in the company as an .xlsx, for the Sales module.

    Sales and Channel Partner in one sheet: the export applies no CP filter at all,
    which is the point of it — the Sales module's download is the combined picture,
    and the CP module has no download of its own. A Module column says which side each
    booking came from.

    Approved means what the Approved tab means (status='sold'), whether or not Accounts
    has signed off yet; the Accounts Status column says where each one stands. Only the
    current version of a revised booking is listed — a superseded revision would
    double-count its deal in the grand total.

    ?project=<id> narrows it to one project. The last row is a grand total across every
    money column.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _can_export_bookings(request.user):
            return Response({'detail': 'You do not have access to download booking data.'},
                            status=status.HTTP_403_FORBIDDEN)
        company = _resolve_company(request)

        qs = Booking.objects.filter(company=company, status='sold')
        project = None
        project_id = request.query_params.get('project')
        if project_id and str(project_id).isdigit():
            project = Project.objects.filter(id=project_id, company=company).first()
            if project is None:
                return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
            qs = qs.filter(project_id=project.id)
        qs = (_drop_superseded_revisions(qs, scope=Booking.objects.filter(company=company))
              .select_related('project', 'plot', 'stm', 'approved_by',
                              'accounts_approved_by', 'lead')
              .defer(*PROJECT_BLOBS)
              .order_by('project__name', 'booking_date', 'id'))

        # Unit order within each project — what someone reading the sheet expects, and
        # not something the database can do: the unit label is text ("401", "Shop4",
        # "EOI-23") and sorting it as text puts 1004 before 101.
        rows = sorted((self._row(b) for b in qs),
                      key=lambda r: (r['project_name'], _unit_sort_key(r['unit'])))
        wb = self._workbook(rows, company, project)

        stamp = timezone.now().strftime('%Y-%m-%d')
        label = (project.name if project else 'All Projects')
        safe  = re.sub(r'[^A-Za-z0-9]+', '-', label).strip('-') or 'All-Projects'
        buf = BytesIO()
        wb.save(buf)
        resp = HttpResponse(
            buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        resp['Content-Disposition'] = f'attachment; filename="Bookings-{safe}-{stamp}.xlsx"'
        return resp

    def _row(self, b):
        """One booking flattened to the column attributes above."""
        def dt(value):
            return timezone.localtime(value).strftime('%d/%m/%Y %I:%M %p') if value else ''

        unit = b.plot_numbers or (b.plot.number if b.plot_id else '') or b.area or ''
        return {
            'project_name': b.project.name if b.project_id else '',
            'unit': unit,
            'module': 'Channel Partner' if _is_cp_sourced_booking(b.lead_id, b.source) else 'Sales',
            'client_name': b.client_name or '',
            'gender': b.gender or '',
            'phone': b.phone or '',
            'address': b.address or '',
            'source': b.source or '',
            'cp_name': b.cp_name or '',
            # manual_stm_name covers a booking entered on behalf of someone with no login.
            'booked_by': (b.stm.name if b.stm_id else '') or b.manual_stm_name or '',
            'booking_date': b.booking_date.strftime('%d/%m/%Y') if b.booking_date else '',
            'area': b.area or '',
            'area_unit': b.area_unit or '',
            'const_area': b.const_area or '',
            'villa_type': b.villa_type or b.bunglow_type or '',
            'land_rate': b.land_rate, 'dev_rate': b.dev_rate, 'const_rate': b.const_rate,
            'sale_deed_rate': b.sale_deed_rate, 'dev_agreement_rate': b.dev_agreement_rate,
            'sale_deed_pct': b.sale_deed_pct, 'maint_rate': b.maint_rate, 'maint_months': b.maint_months,
            'plot_basic': b.plot_basic, 'plot_dev': b.plot_dev, 'const_amt': b.const_amt,
            'sale_deed': b.sale_deed, 'sale_deed_amount': b.sale_deed_amount,
            'land_sale_deed': b.land_sale_deed, 'const_agreement': b.const_agreement,
            'dev_agreement': b.dev_agreement, 'premium_location': b.premium_location,
            'stamp_duty': b.stamp_duty, 'reg_fees': b.reg_fees, 'gst': b.gst,
            'maintenance': b.maintenance, 'maint_deposit': b.maint_deposit,
            'maint_advance': b.maint_advance, 'legal_charges': b.legal_charges,
            'total_extra': b.total_extra, 'extra_work_amount': b.extra_work_amount,
            'extra_work_desc': b.extra_work_desc or '', 'discount': b.discount,
            'final_amount': b.final_amount,
            'apply_reg_fee': b.apply_reg_fee or '', 'apply_page_fee': b.apply_page_fee or '',
            'apply_stamp_duty': b.apply_stamp_duty or '', 'apply_gst': b.apply_gst or '',
            'revision_no': b.revision_no or 0,
            'approval_status': b.approval_status or '',
            'approved_by_name': b.approved_by.name if b.approved_by_id else '',
            'approved_on': dt(b.approved_at),
            'accounts_status': (b.accounts_status or '').title(),
            'accounts_by': b.accounts_approved_by.name if b.accounts_approved_by_id else '',
            'accounts_on': dt(b.accounts_approved_at),
        }

    def _workbook(self, rows, company, project):
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        NAVY  = 'FF0F1838'
        PAPER = 'FFEEF1F7'
        MONEY = '#,##0.00'

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Approved Bookings'

        title = f"{company.name if company else ''} — Approved Bookings"
        subtitle = (f"{project.name if project else 'All Projects'}  ·  "
                    f"{len(rows)} booking{'' if len(rows) == 1 else 's'}  ·  "
                    f"generated {timezone.localtime(timezone.now()).strftime('%d/%m/%Y %I:%M %p')}")
        ws.append([title])
        ws.append([subtitle])
        ws.append([])
        ws['A1'].font = Font(bold=True, size=14, color=NAVY)
        ws['A2'].font = Font(size=10, color='FF8492A6')

        # Read the header's row number back from the sheet rather than predicting it:
        # openpyxl's append([]) advances its write cursor without moving max_row, so a
        # predicted number was one short and the grand total's SUM range started on the
        # header itself.
        ws.append([c[0] for c in BOOKING_EXPORT_COLUMNS])
        header_row = ws.max_row
        for cell in ws[header_row]:
            cell.font = Font(bold=True, color='FFFFFFFF', size=10)
            cell.fill = PatternFill('solid', fgColor=NAVY)
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        # Addressed by name, not via ws.cell(): asking openpyxl for a cell creates it,
        # which left an empty row between the header and the first booking.
        ws.freeze_panes = f'A{header_row + 1}'

        def as_number(value):
            """Money and rates are encrypted columns that come back as Decimal, but a
            blank reads as None or ''. Excel needs a real number to sum, so anything
            unparseable becomes 0 rather than text that would break the total."""
            if value in (None, ''):
                return 0
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0

        for row in rows:
            line = []
            for _, attr, kind in BOOKING_EXPORT_COLUMNS:
                value = row.get(attr)
                line.append(as_number(value) if kind in ('money', 'num') else (value or ''))
            ws.append(line)
            for idx, (_, _, kind) in enumerate(BOOKING_EXPORT_COLUMNS, start=1):
                if kind == 'money':
                    ws.cell(row=ws.max_row, column=idx).number_format = MONEY

        # Grand total — every money column summed over the rows above it, using a real
        # SUM formula so it stays right if somebody filters or edits the sheet.
        first, last = header_row + 1, ws.max_row
        total_row = ws.max_row + 1
        ws.cell(row=total_row, column=1, value='GRAND TOTAL')
        for idx, (_, _, kind) in enumerate(BOOKING_EXPORT_COLUMNS, start=1):
            cell = ws.cell(row=total_row, column=idx)
            if kind == 'money' and rows:
                col = get_column_letter(idx)
                cell.value = f'=SUM({col}{first}:{col}{last})'
                cell.number_format = MONEY
            cell.font = Font(bold=True, color=NAVY, size=10)
            cell.fill = PatternFill('solid', fgColor=PAPER)

        for idx, (heading, _, kind) in enumerate(BOOKING_EXPORT_COLUMNS, start=1):
            width = 34 if heading in ('Address', 'Extra Work Description') else max(13, min(len(heading) + 4, 26))
            ws.column_dimensions[get_column_letter(idx)].width = width
        return wb


def _notify_closure_cancellation(stm, project_obj, company, unit, client, amount, canceller, extra_data=None):
    """Notify STM + approver/manager chain when a closure is cancelled.
    Accepts pre-extracted primitive values so it is safe to call after the closure is deleted."""
    try:
        from notifications import notify, reporting_chain
        canceller_name = getattr(canceller, 'name', '')
        canceller_id   = getattr(canceller, 'id', None)
        stm_id         = getattr(stm, 'id', None)
        project_name   = getattr(project_obj, 'name', '') or ''
        data           = extra_data or {}

        # 1. Notify the STM (skip if they are the one cancelling).
        if stm and stm_id != canceller_id:
            notify(
                stm, 'booking_cancelled',
                'Booking Cancelled',
                f'{client} · {project_name} Unit {unit} has been cancelled.',
                data,
            )

        # 2. Notify project approvers → STM's reporting chain → all managers/admins (first non-empty).
        approver_ids = (getattr(project_obj, 'booking_approvers', None) or [])
        recipients = list(User.objects.filter(id__in=approver_ids, company=company, is_active=True)) if approver_ids else []
        if not recipients and stm:
            recipients = reporting_chain(stm)
        if not recipients:
            recipients = list(
                User.objects.filter(company=company, is_active=True)
                .filter(Q(role__in=MANAGER_ROLES) | Q(is_staff=True))
            )
        seen = set()
        for u in recipients:
            if u and u.id not in seen and u.id != canceller_id and u.id != stm_id:
                seen.add(u.id)
                notify(
                    u, 'booking_cancelled',
                    'Booking Cancelled',
                    f'{client} · {project_name} Unit {unit} · ₹{amount} — cancelled by {canceller_name}',
                    data,
                )
    except Exception:
        import logging
        logging.getLogger(__name__).exception('_notify_closure_cancellation failed')


def _notify_booking_approvers(company, booking, submitter):
    try:
        from notifications import notify, reporting_chain
        # A Channel-Partner-sourced booking routes to the project's CP approver
        # list instead of its regular one — same split the approve/reject
        # endpoint and the list scoping enforce (see _can_approve_booking).
        is_cp = _is_cp_sourced_booking(booking.lead_id, booking.source)
        can_approve_fn = _can_approve_cp_project if is_cp else _can_approve_project
        # 1) Per-project configured approvers (most precise).
        approver_field = 'cp_booking_approvers' if is_cp else 'booking_approvers'
        ids = (getattr(booking.project, approver_field, None) if booking.project_id else None) or []
        recipients = list(User.objects.filter(id__in=ids, company=company, is_active=True)) if ids else []
        # 2) Fallback: the submitting STM's reporting-manager chain.
        if not recipients and booking.stm_id:
            recipients = reporting_chain(booking.stm)
        # 3) Last resort: every manager/admin in the company (so it's never silent).
        #    role='Admin' is included explicitly — a company admin need not be is_staff,
        #    and without this the last resort skipped exactly the people who can always
        #    approve.
        if not recipients:
            recipients = list(User.objects.filter(company=company, is_active=True)
                              .filter(Q(role__in=MANAGER_ROLES) | Q(is_staff=True)))
        # Never notify the person who submitted it; de-dup.
        sub_id = getattr(submitter, 'id', None)
        recipients = [u for u in recipients if u and u.id != sub_id]
        # This is a request to *act*, so it must reach only people who actually can:
        # the same authority the approve/reject endpoint enforces. Without this, the
        # tier-2/3 fallbacks would ask a manager to approve a project they are not an
        # approver for -- they would tap the notification and get a 403 -- and would
        # ask non-managers, who cannot approve at all.
        if booking.project_id:
            recipients = [
                u for u in recipients
                if is_admin_or_manager(u) and can_approve_fn(u, booking.project, company)
            ]
            # Never let the filter silence the request entirely: if no one qualifies,
            # fall back to the company admins, who can approve any project.
            if not recipients:
                recipients = [u for u in User.objects.filter(company=company, is_active=True)
                              if _is_hard_admin(u) and u.id != sub_id]
        if not recipients:
            return
        unit = booking.plot_numbers or (booking.plot.number if booking.plot_id else booking.area)
        rev = (' (R%d)' % booking.revision_no) if booking.revision_no else ''
        # The rep(s) who sold the unit get their own "submitted" notice from
        # _notify_booking_event, so they are not asked to approve it here.
        owner_ids = {u.id for u in _booking_owners(booking)} - {u.id for u in recipients}
        recipients = [u for u in recipients if u.id not in owner_ids]
        title = 'Booking approval needed%s' % rev
        msg = '%s · %s Unit %s · ₹%s — by %s' % (
            booking.client_name or '—', booking.project.name if booking.project_id else '',
            unit, int(booking.final_amount or 0), getattr(submitter, 'name', ''),
        )
        seen = set()
        for u in recipients:
            if u.id not in seen:
                seen.add(u.id)
                notify(u, 'booking_approval', title, msg, {'booking_id': booking.id})
    except Exception:
        pass


def _notify_accounts_managers(company, ntype, title, body, data=None):
    """Notify every active manager of the Accounts & Finance module (managers have
    'Accounts & Finance' in their manager_modules). Best-effort — never raises."""
    try:
        from notifications import notify
        recipients = [
            u for u in User.objects.filter(company=company, is_active=True)
            if 'Accounts & Finance' in (getattr(u, 'manager_modules', None) or [])
        ]
        seen = set()
        for u in recipients:
            if u.id not in seen:
                seen.add(u.id)
                notify(u, ntype, title, body, data or {})
    except Exception:
        import logging
        logging.getLogger(__name__).exception('_notify_accounts_managers failed')


# Who hears about each booking event, by audience. Three audiences, each told once:
#   owner    — the rep who made the booking (and the original rep on a revision)
#   sales    — the project's Sales approvers (plus its CP approvers on a CP deal)
#   accounts — the project's Accounts approvers (CP list on a CP deal); if the
#              project names none, every Accounts & Finance manager
# Each entry is (notification type, title) — the type decides where a tap lands on
# web and in the app. None means that audience is not told about that event.
BOOKING_EVENTS = {
    'submitted': {
        'owner':    ('booking_submitted', 'Booking submitted — pending approval'),
        # Sales approvers get the "approval needed" request from _notify_booking_approvers.
        'sales':    None,
        'accounts': ('accounts_booking_update', 'New booking — pending Sales approval'),
    },
    'sales_approved': {
        'owner':    ('booking_approved', 'Booking approved — pending Accounts'),
        'sales':    ('booking_update', 'Booking approved'),
        'accounts': ('accounts_booking_approval', 'Accounts approval needed'),
    },
    'sales_rejected': {
        'owner':    ('booking_rejected', 'Booking rejected'),
        'sales':    ('booking_update', 'Booking rejected'),
        'accounts': ('accounts_booking_update', 'Booking rejected by Sales'),
    },
    'accounts_approved': {
        'owner':    ('accounts_booking_approved', 'Approved by Accounts — booking confirmed'),
        'sales':    ('booking_update', 'Approved by Accounts'),
        'accounts': ('accounts_booking_update', 'Approved by Accounts'),
    },
    'accounts_rejected': {
        'owner':    ('accounts_booking_rejected', 'Rejected by Accounts'),
        'sales':    ('booking_update', 'Rejected by Accounts'),
        'accounts': ('accounts_booking_update', 'Rejected by Accounts'),
    },
    'cancelled': {
        'owner':    ('booking_cancelled', 'Booking cancelled'),
        'sales':    ('booking_cancelled', 'Booking cancelled'),
        'accounts': ('accounts_booking_cancelled', 'Booking cancelled'),
    },
}


def _booking_owners(booking):
    owners = [booking.stm] if booking.stm_id else []
    if booking.revision_of_id and booking.revision_of and booking.revision_of.stm_id:
        owners.append(booking.revision_of.stm)
    return owners


def _booking_sales_side(company, booking):
    """Sales/CP approvers of the booking's project. A CP deal is decided by the CP
    approvers but the project's Sales approvers hear about it too — both teams sell
    the same units. Falls back to the rep's reporting chain."""
    from notifications import reporting_chain
    is_cp = _is_cp_sourced_booking(booking.lead_id, booking.source)
    fields = ('cp_booking_approvers', 'booking_approvers') if is_cp else ('booking_approvers',)
    ids = []
    if booking.project_id:
        for f in fields:
            ids += list(getattr(booking.project, f, None) or [])
    users = list(User.objects.filter(id__in=ids, company=company, is_active=True)) if ids else []
    if not users and booking.stm_id:
        users = reporting_chain(booking.stm)
    return users


def _booking_accounts_side(company, booking):
    """Accounts approvers of the booking's project; every Accounts & Finance manager
    when the project names nobody, so Accounts never misses a deal."""
    ids = []
    if booking.project_id:
        is_cp = _is_cp_sourced_booking(booking.lead_id, booking.source)
        field = 'accounts_cp_booking_approvers' if is_cp else 'accounts_booking_approvers'
        ids = list(getattr(booking.project, field, None) or [])
    if ids:
        return list(User.objects.filter(id__in=ids, company=company, is_active=True))
    return [u for u in User.objects.filter(company=company, is_active=True)
            if 'Accounts & Finance' in (getattr(u, 'manager_modules', None) or [])]


BOOKING_LOG = {  # event -> (action, verb, module) for the activity log
    'submitted':         ('submitted', 'Submitted', 'Sales'),
    'sales_approved':    ('approved', 'Approved', 'Sales'),
    'sales_rejected':    ('rejected', 'Rejected', 'Sales'),
    'accounts_approved': ('approved', 'Approved', 'Accounts & Finance'),
    'accounts_rejected': ('rejected', 'Rejected', 'Accounts & Finance'),
    'cancelled':         ('cancelled', 'Cancelled', 'Sales'),
}


def _log_booking_event(request, booking, event, detail=''):
    """One clear line in the activity log for a booking event."""
    try:
        from activity.recorder import note
        action, verb, module = BOOKING_LOG[event]
        if event.startswith('sales_') or event == 'submitted':
            if _is_cp_sourced_booking(booking.lead_id, booking.source):
                module = 'Channel Partner'
        unit = booking.plot_numbers or (booking.plot.number if booking.plot_id else booking.area)
        rev = (' R%d' % booking.revision_no) if booking.revision_no else ''
        what = 'booking%s — %s · %s Unit %s · ₹%s' % (
            rev, booking.client_name or '—', booking.project.name if booking.project_id else '',
            unit, int(booking.final_amount or 0))
        stage = ' (Accounts)' if event.startswith('accounts_') else ''
        reason = ''
        if 'rejected' in event and ': ' in (detail or ''):
            reason = ' — ' + detail.split(': ', 1)[1]
        note(request, '%s%s %s%s' % (verb, stage, what, reason), action=action,
             target_type='booking', target_id=booking.id, module=module)
    except Exception:
        logger.exception('Could not note booking event %s', event)


def _notify_booking_event(company, booking, event, actor=None, detail='', request=None):
    """Tell the owner, the Sales/CP side and the Accounts side about a booking event
    (see BOOKING_EVENTS). Everyone is told once — the first audience they belong to
    wins — and the person who acted is not told about their own action, except the
    rep's own "submitted" receipt. In-app + push via notifications.notify.
    Best-effort — never raises."""
    try:
        if request is not None:
            _log_booking_event(request, booking, event, detail)
        from notifications import notify
        spec = BOOKING_EVENTS[event]
        unit = booking.plot_numbers or (booking.plot.number if booking.plot_id else booking.area)
        rev = (' (R%d)' % booking.revision_no) if booking.revision_no else ''
        actor_id = getattr(actor, 'id', None)
        by = getattr(actor, 'name', '') or ''
        body = '%s · %s Unit %s · ₹%s' % (
            booking.client_name or 'Booking', booking.project.name if booking.project_id else '',
            unit, int(booking.final_amount or 0))
        tail = detail or (('by %s' % by) if by else '')
        if tail:
            body += ' — ' + tail
        data = {'booking_id': booking.id}
        if booking.closure_id:
            data['closure_id'] = booking.closure_id
        seen = set()
        groups = (
            ('owner', lambda: _booking_owners(booking)),
            ('sales', lambda: _booking_sales_side(company, booking)),
            ('accounts', lambda: _booking_accounts_side(company, booking)),
        )
        for audience, users in groups:
            if not spec.get(audience):
                continue
            ntype, title = spec[audience]
            for u in users():
                if not u or u.id in seen:
                    continue
                if u.id == actor_id and not (audience == 'owner' and event == 'submitted'):
                    continue
                seen.add(u.id)
                notify(u, ntype, title + rev, body, data)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('_notify_booking_event(%s) failed', event)


def _ensure_lead_and_site_visit_for_booking(b):
    """On a booking's first approval, guarantee it exists in the pipeline as a lead
    with a completed site visit dated on the booking date.

    Two flows land here. A closure recorded from a lead already has the lead but may
    have no visit — a walk-in that booked without one ever being logged. A unit booked
    directly has a lead created at submission time, but likewise no visit. Either way
    the sale is real and the visit demonstrably happened, so the pipeline should say so
    rather than showing a closure that came from nowhere.

    Deliberately conservative:
      - Never duplicates. A completed visit already on this lead for this project is
        left exactly as it is, which is the normal Record-Closure-from-a-visit path.
      - Only fabricates a lead when there is a name or phone to build one from.
      - Attributes only to the booking's STM — never to a telecaller. Whoever is on
        lead.telecaller_id may have had no part in this particular sale (e.g. an
        earlier lead they once worked reused for an unrelated direct booking), so
        crediting them here would hand out site-visit incentive for a visit they
        didn't do. A real, telecaller-referred visit already gets its own SiteVisit
        row logged at the time — this fabricated one exists purely to backfill the
        pipeline for a closure that has none, not to attribute credit beyond the STM.
    """
    if not b.booking_date:
        return None, None                     # nothing to date the visit by

    lead_id = b.lead_id
    if not lead_id:
        name  = (b.client_name or '').strip()
        phone = (b.phone or '').strip()
        if not (name or phone):
            return None, None                 # no identity to build a lead from
        # Match on the number first. The same client often already exists as a lead —
        # they were called, or they booked a second unit — and creating another record
        # would split one person's history across two leads. Phone is encrypted, so the
        # lookup goes through the blind index, which normalises to the last ten digits.
        existing = None
        key = phone_blind_index(phone) if phone else ''
        if key:
            existing = (Lead.objects.filter(company_id=b.company_id, phone_key=key)
                        .order_by('id').first())
        if existing:
            lead_id = existing.id
            # Attach the sale to them without overwriting a working history: only fill
            # the STM in, and only when nobody is on it.
            fields = {}
            if not existing.stm_id and b.stm_id:
                fields['stm_id'] = b.stm_id
            if fields:
                Lead.objects.filter(pk=lead_id).update(**fields)
        else:
            lead = Lead.objects.create(
                company_id=b.company_id, name=name, phone=phone, status='closed',
                project_id=b.project_id, stm=b.stm, stm_status='closed',
            )
            lead_id = lead.id
        Booking.objects.filter(pk=b.pk).update(lead_id=lead_id)
        b.lead_id = lead_id

    # The guard is the booking DATE, not merely "this lead has been on a visit".
    # A repeat buyer visited once per unit they bought, so a visit already logged on
    # some other day belongs to the other sale and is left alone while this booking
    # still gets its own. Only a visit already sitting on this booking's date means
    # there is nothing to add.
    already = SiteVisit.objects.filter(
        lead_id=lead_id, project_id=b.project_id, status='completed',
        visited_at__date=b.booking_date).exists()
    if already:
        return lead_id, None

    # booking_date is a date; visits are timestamped, so anchor it at midday local
    # time — a plain midnight can land on the previous day once rendered in another
    # timezone, which would put the visit before the booking it came from.
    visited = datetime.combine(b.booking_date, dt_time(12, 0))
    if timezone.is_naive(visited):
        visited = timezone.make_aware(visited, timezone.get_current_timezone())

    # The telecaller who worked this lead keeps the credit for the visit. Conversion
    # reporting counts a site visit through referred_by_telecaller — MQL→SV, SV Done,
    # the telecaller's own portal — so leaving it null quietly dropped every visit
    # created this way out of their numbers, and the visit read as if the STM had
    # sourced the buyer themselves.
    tc_id = Lead.objects.filter(pk=lead_id).values_list('telecaller_id', flat=True).first()

    sv = SiteVisit.objects.create(
        lead_id=lead_id, project_id=b.project_id, stm=b.stm,
        referred_by_telecaller_id=tc_id,
        scheduled_at=visited, visited_at=visited,
        status='completed', outcome='hot',
        remarks='Recorded automatically from booking #%s on approval.' % b.pk,
    )
    return lead_id, sv.id


class BookingActionView(APIView):
    """Approve / reject a pending booking (approver = admin or manager)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        company = _resolve_company(request)
        try:
            b = Booking.objects.get(pk=pk, company=company)
        except Booking.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        # Enforced here and not merely hidden in the list, or a manager could approve
        # another project's booking straight through the API. Two cases, mirroring the
        # list scoping above: someone named on any project is confined to those projects
        # (a project they don't approve is off limits even if it names nobody), and
        # someone named nowhere is blocked from projects that do name approvers.
        # Real admins are exempt.
        if not _can_approve_booking(request.user, b.project_id, b.project, b.lead_id, company, b.source):
            return Response(
                {'detail': 'You are not a booking approver for this project.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        action = request.data.get('action')
        is_rev = b.revision_no and b.revision_no > 0

        # Approving touches plots, the closure and the lead. Without one
        # transaction a failure halfway leaves the unit held with no closure,
        # or a closure with no lead — seen as "approved but not sold".
        with transaction.atomic():
            if action == 'approve':
                # The plot itself is NOT marked sold here — Sales/CP approval alone no
                # longer finalises the unit on the map; it stays 'hold' (spoken for, not
                # yet gone) until Accounts also signs off (see AccountsBookingActionView,
                # which is the only place that sets 'sold'). A revision re-approval can
                # find the plot already 'sold' from an earlier round — pull it back to
                # 'hold' to match accounts_status being reset to pending below.
                _pids = b.plot_ids or ([b.plot_id] if b.plot_id else [])
                # Checking again here, not only at submission. Two bookings can sit pending
                # on one unit and each be approved in turn, and a unit's status can be lost
                # to a re-import or a reset between the two — neither of which the
                # submission-time check can see. A revision legitimately reuses its own
                # unit, so the chain it belongs to is excluded.
                if _pids and not b.revision_of_id and not b.is_resale:
                    chain = _revision_chain_ids(b.id, company)
                    wanted = set(_pids)
                    clash = None
                    # plot_ids is JSON, not a Postgres array, so the overlap is worked out
                    # here rather than in the query — the same way the submission-time
                    # guard above does it, and portable to the test database.
                    for other in (Booking.objects.filter(company=company, status='sold')
                                  .exclude(id__in=chain)
                                  .only('id', 'plot_id', 'plot_ids', 'client_name')):
                        held = set(other.plot_ids or [])
                        if other.plot_id:
                            held.add(other.plot_id)
                        if held & wanted:
                            clash = other
                            break
                    if clash:
                        return Response(
                            {'detail': f'This unit is already sold to {clash.client_name} '
                                       f'(booking #{clash.id}). Cancel that booking first.'},
                            status=status.HTTP_409_CONFLICT,
                        )
                if _pids:
                    Plot.objects.filter(id__in=_pids).update(status='hold')
                b.status = 'sold'
                b.approval_status = ('REVISION R%d APPROVED' % b.revision_no) if is_rev else 'APPROVED'
                b.approved_at = timezone.now()
                b.approved_by = request.user
                # Sales/CP approval alone no longer makes this real to Accounts — it now
                # waits on a configured Accounts approver too (see AccountsBookingActionView).
                # Reset on every approval, including a revision: the amount may have
                # changed, so Accounts should sign off on it again rather than keeping
                # whatever stale accounts_status an earlier approval left behind.
                b.accounts_status = 'pending'
                b.accounts_rejected_reason = ''
                b.accounts_approved_by = None
                b.accounts_approved_at = None
                b.accounts_rejected_by = None
                b.accounts_rejected_at = None
                if b.closure_id:
                    # Existing closure (revision / re-approval) → just sync the amounts.
                    b.save(update_fields=['status', 'approval_status', 'approved_at', 'approved_by', 'accounts_status',
                                           'accounts_rejected_reason', 'accounts_approved_by', 'accounts_approved_at',
                                           'accounts_rejected_by', 'accounts_rejected_at'])
                    Closure.objects.filter(id=b.closure_id).update(
                        booking_amount=b.plot_basic or None, total_amount=b.final_amount or None)
                else:
                    # First approval of a new booking → mirror it into My Conversions now.
                    if b.lead_id:
                        # Both fields move to 'closed' together — status is the overall
                        # pipeline field (what All Leads and the dashboard's closed-count
                        # tile read; see Q(status='closed') in StatsView), stm_status is
                        # the STM portal's own field. Leaving status behind used to strand
                        # a lead auto-created at booking submission (status='new') on
                        # "new" forever once its booking was approved, even though
                        # _ensure_lead_and_site_visit_for_booking's backfill path already
                        # sets both together for the no-lead-at-all case below.
                        Lead.objects.filter(id=b.lead_id).update(stm=b.stm, stm_status='closed', status='closed')
                    closure = Closure.objects.create(
                        company_id=b.company_id, lead_id=b.lead_id, project_id=b.project_id, stm=b.stm,
                        client_name=b.client_name or '', client_phone=b.phone or '',
                        status='booked', closure_date=b.booking_date or timezone.now().date(),
                        unit_no=(b.plot_numbers or (b.plot.number if b.plot_id else b.area)),
                        unit_type=b.villa_type or b.bunglow_type or '',
                        booking_amount=b.plot_basic or None, total_amount=b.final_amount or None,
                    )
                    b.closure = closure
                    b.save(update_fields=['status', 'approval_status', 'approved_at', 'approved_by', 'closure', 'accounts_status',
                                           'accounts_rejected_reason', 'accounts_approved_by', 'accounts_approved_at',
                                           'accounts_rejected_by', 'accounts_rejected_at'])
                    # The sale is now real, so make sure the pipeline shows how it got
                    # here: a lead, and a completed site visit dated on the booking date.
                    try:
                        _lid, _svid = _ensure_lead_and_site_visit_for_booking(b)
                        if _svid and not closure.lead_id and _lid:
                            Closure.objects.filter(pk=closure.pk).update(lead_id=_lid)
                    except Exception:
                        # Never let this block an approval — the booking is what matters.
                        logger.exception('Could not back-fill lead/site visit for booking %s', b.pk)
                # Notify the STM (approved) and — on a fresh closure — their manager chain.
                from notifications import notify_many, reporting_chain
                _unit = (b.plot_numbers or (b.plot.number if b.plot_id else b.area))
                # Rep, the project's Sales/CP approvers, and its Accounts approvers
                # (who now have to act on it).
                _notify_booking_event(company, b, 'sales_approved', request.user,
                                      'approved by %s, awaiting Accounts' % (request.user.name or 'Sales'), request=request)
                if b.stm:
                    if not is_rev:
                        notify_many(reporting_chain(b.stm), 'closure', 'New Closure',
                                    f'{b.stm.name} closed {b.client_name or "a unit"} · Unit {_unit} · ₹{int(b.final_amount or 0)}',
                                    {'booking_id': b.id})
            elif action == 'reject':
                b.status = 'rejected'
                b.approval_status = ('REVISION R%d REJECTED' % b.revision_no) if is_rev else 'REJECTED'
                b.rejected_by = request.user
                b.rejected_at = timezone.now()
                # Remove the rejected signed LOI PDF from Supabase storage.
                if b.loi_document:
                    try: b.loi_document.delete(save=False)
                    except Exception: pass
                b.save(update_fields=['status', 'approval_status', 'loi_document',
                                      'rejected_by', 'rejected_at'])
                if not is_rev:
                    _pids = b.plot_ids or ([b.plot_id] if b.plot_id else [])
                    _release_plots(_pids, booking=b)
                    if b.closure_id:
                        Closure.objects.filter(id=b.closure_id).delete()
                _notify_booking_event(company, b, 'sales_rejected', request.user,
                                      'rejected by %s' % (request.user.name or 'Sales'), request=request)
            else:
                return Response({'detail': 'action must be approve or reject.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BookingSerializer(b).data)


class AccountsBookingActionView(APIView):
    """Approve / reject a booking's separate ACCOUNTS-stage sign-off — distinct
    from BookingActionView, which is the Sales/CP approval. A booking only
    reaches here once Sales/CP has already approved it (status='sold'); this
    endpoint is gated by the accounts_booking_approvers / accounts_cp_booking_approvers
    lists on the project, configured the same way as Sales' own approver lists
    (see _can_approve_accounts_booking). Rejecting requires a remarks string —
    it releases the plot(s) exactly like a Sales-level rejection does (same
    _release_plots) and notifies the Sales/CP approver(s) for the project."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        company = _resolve_company(request)
        try:
            b = Booking.objects.select_related('project', 'plot', 'stm').get(pk=pk, company=company)
        except Booking.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not _can_approve_accounts_booking(request.user, b.project_id, b.project, b.lead_id, company, b.source):
            return Response(
                {'detail': 'You are not an Accounts approver for this project.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if b.status != 'sold':
            return Response({'detail': 'This booking has not been approved by Sales/CP yet.'}, status=status.HTTP_400_BAD_REQUEST)
        if b.accounts_status != 'pending':
            return Response({'detail': 'This booking has already been processed by Accounts.'}, status=status.HTTP_400_BAD_REQUEST)

        action = request.data.get('action')
        # Units go 'sold' and the booking's accounts state flips together, or
        # the unit is sold on the map with the booking still pending.
        with transaction.atomic():
            if action == 'approve':
                # This is the ONLY point a unit actually becomes sold — Sales/CP approval
                # leaves it 'hold' precisely so the map doesn't show it as final until now.
                _pids = b.plot_ids or ([b.plot_id] if b.plot_id else [])
                if _pids:
                    Plot.objects.filter(id__in=_pids).update(status='sold')
                b.accounts_status = 'approved'
                b.accounts_approved_by = request.user
                b.accounts_approved_at = timezone.now()
                b.save(update_fields=['accounts_status', 'accounts_approved_by', 'accounts_approved_at'])
                _notify_booking_event(company, b, 'accounts_approved', request.user,
                                      'approved by %s (Accounts)' % (request.user.name or 'Accounts'), request=request)
            elif action == 'reject':
                reason = (request.data.get('reason') or '').strip()
                if not reason:
                    return Response({'detail': 'Remarks are required to reject.'}, status=status.HTTP_400_BAD_REQUEST)
                b.accounts_status = 'rejected'
                b.accounts_rejected_reason = reason
                b.accounts_rejected_by = request.user
                b.accounts_rejected_at = timezone.now()
                # Same fate as a Sales-level rejection: the plot(s) free up and the
                # booking itself is marked rejected, not just its accounts stage.
                b.status = 'rejected'
                if b.loi_document:
                    try: b.loi_document.delete(save=False)
                    except Exception: pass
                b.save(update_fields=['accounts_status', 'accounts_rejected_reason', 'accounts_rejected_by',
                                       'accounts_rejected_at', 'status', 'loi_document'])
                _pids = b.plot_ids or ([b.plot_id] if b.plot_id else [])
                _release_plots(_pids, booking=b)
                if b.closure_id:
                    Closure.objects.filter(id=b.closure_id).delete()
                _notify_booking_event(company, b, 'accounts_rejected', request.user,
                                      'rejected by %s (Accounts): %s' % (request.user.name or 'Accounts', reason), request=request)
            else:
                return Response({'detail': 'action must be approve or reject.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BookingSerializer(b).data)


class BookingLOIUrlView(APIView):
    """Returns a short-lived signed URL for a booking's confidential LOI PDF.
    Authorised viewers only (admin/manager or the booking's STM). The bucket is
    private, so this signed URL is the *only* way to open the document."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        b = scope_to_company(Booking.objects.all(), request.user).filter(pk=pk).first()
        if not b or not b.loi_document:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not _loi_enabled(b.company):
            return Response(
                {'detail': 'LOI / EOI documents are not enabled for this company.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not (is_admin_or_manager(request.user) or b.stm_id == request.user.id):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        from sales.supabase_storage import create_signed_url
        url = create_signed_url(b.loi_document.name, expires_in=120)
        if not url:
            # Local dev (FileSystem storage) fallback.
            try:
                url = request.build_absolute_uri(b.loi_document.url)
            except Exception:
                url = None
        if not url:
            return Response({'detail': 'LOI unavailable.'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'url': url})


# Largest media file accepted by MediaUploadView. Architects' floor-plan PDFs run
# well past the old 25 MB. Keep the web/app pickers' own limits in step with this —
# they check client-side purely to fail fast before the upload starts.
MEDIA_UPLOAD_MAX_MB = 100


class MediaUploadView(APIView):
    """Authenticated media upload to the public erp-media bucket via the service-role
    key. Lets the frontend stop using the anon key for writes (so anon INSERT can be
    revoked in Supabase). Returns {url, path}."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        import time, random, string
        from sales.supabase_storage import upload_public
        f = request.FILES.get('file')
        if not f:
            return Response({'detail': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)
        if f.size and f.size > MEDIA_UPLOAD_MAX_MB * 1024 * 1024:
            return Response({'detail': f'File too large (max {MEDIA_UPLOAD_MAX_MB} MB).'}, status=status.HTTP_400_BAD_REQUEST)
        folder = (request.data.get('folder') or 'erp/media').strip('/')
        ext = (f.name.rsplit('.', 1)[-1].lower() if '.' in (f.name or '') else 'bin')[:10]
        rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        path = f'{folder}/{int(time.time() * 1000)}_{rand}.{ext}'
        try:
            url = upload_public(f.read(), path, f.content_type or 'application/octet-stream')
        except Exception as e:
            return Response({'detail': str(e)[:200]}, status=status.HTTP_502_BAD_GATEWAY)
        if not url:
            return Response({'detail': 'Storage not configured.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'url': url, 'path': path})


class MediaDeleteView(APIView):
    """Delete a media object from erp-media via the service-role key (anon can't)."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from sales.supabase_storage import delete_object
        path = request.data.get('path')
        if not path:
            return Response({'detail': 'path required.'}, status=status.HTTP_400_BAD_REQUEST)
        delete_object(path)
        return Response({'ok': True})


class ClosureCancelView(APIView):
    """Cancel a closure: deletes the closure, frees the plot(s), removes the
    signed LOI PDFs from Supabase, and marks the related booking(s) cancelled."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        closure = scope_to_company(
            Closure.objects.filter(pk=pk).select_related('stm', 'project', 'lead'),
            request.user, 'company').first()
        if not closure:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        company = _resolve_company(request)
        # A closure has no Source field of its own — pull it from the booking that
        # was approved into this closure, if any, so a CP-sourced deal still routes
        # to the CP approvers at cancel time too.
        booking_source = Booking.objects.filter(closure_id=closure.pk).values_list('source', flat=True).first()
        # Two independent routes to cancel authority: the Sales/CP approver for this
        # project (admin/manager, same authority that approves/rejects it — the owning
        # STM can no longer self-cancel), OR the Accounts approver who signed off on it
        # at that separate stage — undoing either approval is cancelling the same sale,
        # so either side's approver may void it once it's Accounts-approved.
        is_accounts_approver = _can_approve_accounts_booking(
            request.user, closure.project_id, closure.project, closure.lead_id, company, booking_source)
        if not is_accounts_approver:
            if not is_admin_or_manager(request.user):
                return Response({'detail': 'Only an approver can cancel a booking.'}, status=status.HTTP_403_FORBIDDEN)
            if not _can_approve_booking(request.user, closure.project_id, closure.project, closure.lead_id, company, booking_source):
                return Response({'detail': 'You are not a booking approver for this project.'},
                                status=status.HTTP_403_FORBIDDEN)

        # Extract all notification data BEFORE deletion (closure.pk becomes None after delete).
        notif_stm      = closure.stm
        notif_project  = closure.project
        notif_unit     = (closure.unit_type + ' ' + (closure.unit_no or '')).strip() or '—'
        notif_client   = getattr(closure.lead, 'name', None) or closure.client_name or '—'
        notif_amount   = int(closure.total_amount or 0)
        notif_extra    = {'closure_id': closure.pk}

        linked_booking = Booking.objects.filter(closure=closure).only('id').first()
        if linked_booking:
            notif_extra['booking_id'] = linked_booking.pk

        # A cancellation is a record, not an erasure. The signed LOI stays in storage
        # and stays linked: it is the evidence of what the buyer agreed to, and
        # Accounts needs the cancelled deal and its PDF to reconcile against. Deleting
        # the document left nothing to show the day someone disputes a cancellation,
        # which is the one day it matters.
        for b in Booking.objects.filter(closure=closure):
            _pids = b.plot_ids or ([b.plot_id] if b.plot_id else [])
            _release_plots(_pids, booking=b)
            b.status = 'rejected'
            b.approval_status = 'CANCELLED'
            b.cancelled_by = request.user
            b.cancelled_at = timezone.now()
            b.save(update_fields=['status', 'approval_status', 'cancelled_by', 'cancelled_at'])
        if closure.lead_id:
            Lead.objects.filter(id=closure.lead_id).update(stm_status='')
        # Marked, not deleted — CLOSURE_STATUS has carried 'cancelled' all along.
        # Conversion counts exclude it (see the closure querysets in the dashboards),
        # so the numbers are unchanged while the row survives for Accounts.
        closure.status = 'cancelled'
        closure.save(update_fields=['status'])

        # The rep, the project's Sales/CP approvers and its Accounts approvers all
        # hear about it, whichever side cancelled.
        notif_booking = (Booking.objects.filter(closure_id=notif_extra['closure_id'])
                         .select_related('stm', 'project', 'plot', 'revision_of__stm')
                         .order_by('-revision_no', '-id').first())
        if notif_booking:
            _notify_booking_event(company, notif_booking, 'cancelled', request.user,
                                  'cancelled by %s' % (getattr(request.user, 'name', '') or 'an approver'), request=request)
        else:
            _notify_closure_cancellation(
                notif_stm, notif_project, company,
                notif_unit, notif_client, notif_amount,
                request.user, extra_data=notif_extra,
            )
            _notify_accounts_managers(
                company, 'accounts_booking_cancelled', 'Booking cancelled',
                f'{notif_client} · {getattr(notif_project, "name", "") or ""} Unit {notif_unit} · ₹{notif_amount} — cancelled by {getattr(request.user, "name", "")}',
                notif_extra,
            )
        return Response({'detail': 'Closure cancelled.'})


# ──────────────────────────────────────────────
#  Meta Lead Ads Webhook
# ──────────────────────────────────────────────

def _fetch_meta_lead_data(leadgen_id, page_access_token):
    """Call Meta Graph API to get lead field data and ad info."""
    try:
        url = f'https://graph.facebook.com/v19.0/{leadgen_id}'
        r = http_requests.get(url, params={
            'access_token': page_access_token,
            # form_id decides project routing — fetch it authoritatively here so we
            # don't depend on the webhook payload always including it.
            'fields': 'field_data,ad_id,ad_name,form_id',
        }, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        logger.exception('Meta: failed to fetch lead data for leadgen_id=%s', leadgen_id)
    return None


def _fetch_ad_campaign_info(ad_id, page_access_token):
    """Given an ad_id, fetch campaign name and adset name from Meta Graph API."""
    if not ad_id:
        return '', ''
    try:
        url = f'https://graph.facebook.com/v19.0/{ad_id}'
        r = http_requests.get(url, params={
            'access_token': page_access_token,
            'fields': 'campaign{name},adset{name}',
        }, timeout=10)
        if r.status_code == 200:
            data = r.json()
            campaign_name = (data.get('campaign') or {}).get('name', '')
            adset_name    = (data.get('adset') or {}).get('name', '')
            return campaign_name, adset_name
    except Exception:
        logger.exception('Meta: failed to fetch campaign info for ad_id=%s', ad_id)
    return '', ''


def _create_lead_from_meta(field_data, config, campaign_name='', adset_name='', ad_name='', form_id=''):
    """Parse Meta field_data list and create a Lead."""
    fields = {f['name']: f['values'][0] for f in field_data if f.get('values') and f.get('name')}
    name  = fields.get('full_name') or fields.get('name') or (fields.get('first_name', '') + ' ' + fields.get('last_name', '')).strip()
    phone = (fields.get('phone_number') or fields.get('phone') or '').strip()[:20]
    email = fields.get('email', '')[:254]
    if not name and not phone:
        return None

    # Resolve project: form mapping takes priority over default
    project = config.default_project
    if form_id:
        mapping = MetaFormMapping.objects.filter(form_id=form_id).select_related('project').first()
        if mapping:
            project = mapping.project
            MetaFormMapping.objects.filter(pk=mapping.pk).update(total_leads=mapping.total_leads + 1)

    # Tenant for the incoming lead: project's company → config's company
    company = (project.company if project and project.company_id else None) or config.company
    if company is None:
        return None  # Can't attribute to a tenant — drop rather than leak globally.

    source, _ = LeadSource.objects.get_or_create(
        company=company, name='meta', defaults={'is_active': True},
    )

    # Duplicate detection using last 10 digits, scoped to this company
    clean = ''.join(c for c in phone if c.isdigit())[-10:]
    existing = (
        Lead.objects.filter(company=company, phone_key=phone_blind_index(clean)).first()
        if clean else None
    )
    if existing:
        existing.duplicate_count = (existing.duplicate_count or 0) + 1
        existing.save(update_fields=['duplicate_count'])

    lead = Lead.objects.create(
        company=company,
        name=(name or 'Meta Lead')[:200],
        phone=phone,
        email=email,
        source=source,
        project=project,
        meta_campaign_name=campaign_name[:200] if campaign_name else '',
        meta_adset_name=adset_name[:200] if adset_name else '',
        meta_ad_name=ad_name[:200] if ad_name else '',
        meta_form_id=str(form_id or '')[:100],
        status='new',
        is_duplicate=bool(existing),
        duplicate_of=existing if existing else None,
    )
    MetaWebhookConfig.objects.filter(pk=config.pk).update(
        total_leads_received=config.total_leads_received + 1,
        last_lead_at=timezone.now(),
        is_active=True,
    )
    _record_lead_created(lead)  # source = 'meta'
    # Auto-assign the live lead to an available telecaller (window-gated).
    _run_distribution(company, 'telecaller')
    return lead


class MetaWebhookView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        """Meta webhook verification challenge."""
        mode      = request.GET.get('hub.mode')
        token     = request.GET.get('hub.verify_token')
        challenge = request.GET.get('hub.challenge')
        # Match any company's verify token (each tenant has its own config).
        if mode == 'subscribe' and token and MetaWebhookConfig.objects.filter(verify_token=token).exists():
            return HttpResponse(challenge, content_type='text/plain')
        return HttpResponse(status=403)

    def _config_for_page(self, page_id):
        """Find the tenant config that owns the given Meta page id."""
        configs = list(MetaWebhookConfig.objects.filter(page_access_token__gt=''))
        if page_id:
            for cfg in configs:
                for p in (cfg.pages_data or []):
                    if str(p.get('page_id')) == str(page_id):
                        return cfg
        return configs[0] if configs else None

    @staticmethod
    def _signature_ok(request, app_secret):
        """Verify Meta's X-Hub-Signature-256 over the raw request body.

        Meta signs every delivery with HMAC-SHA256 keyed on the app secret. Without
        this the endpoint is an open door: anyone who learns a page id could post a
        payload and make the ERP call the Graph API with that page's token.

        A config with no app_secret is not rejected -- that would silently drop real
        leads for a tenant mid-setup -- but it is logged so the gap is visible.
        """
        if not app_secret:
            logger.warning('Meta webhook: no app_secret configured — delivery accepted unverified')
            return True
        header = request.headers.get('X-Hub-Signature-256', '')
        if not header.startswith('sha256='):
            return False
        import hashlib, hmac
        expected = hmac.new(app_secret.encode(), request.body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, header[len('sha256='):])

    def post(self, request):
        """Receive lead notification from Meta."""
        try:
            data = request.data
            if data.get('object') != 'page':
                return Response({'ok': True})
            for entry in data.get('entry', []):
                config = self._config_for_page(entry.get('id'))
                if not config:
                    continue
                if not self._signature_ok(request, config.app_secret):
                    logger.warning('Meta webhook: bad signature for page %s — ignored', entry.get('id'))
                    continue
                for change in entry.get('changes', []):
                    if change.get('field') == 'leadgen':
                        val        = change.get('value', {})
                        leadgen_id = val.get('leadgen_id')
                        campaign   = val.get('campaign_name', '') or ''
                        adset      = val.get('adset_name', '') or val.get('adgroup_name', '') or ''
                        ad         = val.get('ad_name', '') or ''
                        form_id    = str(val.get('form_id', '') or '')
                        if leadgen_id:
                            meta_data = _fetch_meta_lead_data(leadgen_id, config.page_access_token)
                            if meta_data and meta_data.get('field_data'):
                                ad    = meta_data.get('ad_name') or ad
                                ad_id = meta_data.get('ad_id')
                                # Prefer the form_id from the Graph lead object; the
                                # webhook payload doesn't always include it.
                                form_id = str(meta_data.get('form_id') or form_id or '')
                                if ad_id and not campaign and not adset:
                                    campaign, adset = _fetch_ad_campaign_info(ad_id, config.page_access_token)
                                _create_lead_from_meta(meta_data['field_data'], config, campaign, adset, ad, form_id)
        except Exception:
            logger.exception('Meta webhook: unhandled error processing payload')
        return Response({'ok': True})


class MetaWebhookConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def _ensure_config(self, request):
        company = _resolve_company(request)
        config, created = MetaWebhookConfig.objects.get_or_create(
            company=company,
            defaults={'verify_token': secrets.token_urlsafe(32)},
        )
        if not config.verify_token:
            config.verify_token = secrets.token_urlsafe(32)
            config.save(update_fields=['verify_token'])
        return config

    def _fetch_pages_and_forms(self, pat):
        """Fetch all subscribed pages and their lead forms from Meta API."""
        pages_data, subscribed = [], []
        try:
            pages_r = http_requests.get(
                'https://graph.facebook.com/v19.0/me/accounts',
                params={'access_token': pat, 'limit': 50}, timeout=10
            )
            if pages_r.status_code == 200:
                for page in pages_r.json().get('data', []):
                    page_token = page.get('access_token')
                    page_id    = page.get('id')
                    page_name  = page.get('name', page_id)
                    if not page_token or not page_id:
                        continue
                    subscribed.append(page_name)
                    forms = []
                    try:
                        forms_r = http_requests.get(
                            f'https://graph.facebook.com/v19.0/{page_id}/leadgen_forms',
                            params={'access_token': page_token, 'fields': 'id,name', 'limit': 50},
                            timeout=10
                        )
                        if forms_r.status_code == 200:
                            forms = [{'id': f['id'], 'name': f.get('name', '')}
                                     for f in forms_r.json().get('data', [])]
                    except Exception:
                        logger.exception('Meta: failed to fetch forms for page_id=%s', page_id)
                    pages_data.append({'page_id': page_id, 'page_name': page_name, 'forms': forms})
        except Exception:
            logger.exception('Meta: failed to fetch pages list')
        return subscribed, pages_data

    def get(self, request):
        config = self._ensure_config(request)
        # Auto-refresh pages/forms if stale (older than 2 hours) or never fetched
        if config.page_access_token:
            stale = (
                not config.pages_refreshed_at or
                (timezone.now() - config.pages_refreshed_at).total_seconds() > 7200
            )
            if stale:
                subscribed, pages_data = self._fetch_pages_and_forms(config.page_access_token)
                if pages_data:
                    config.subscribed_pages  = subscribed
                    config.pages_data        = pages_data
                    config.pages_refreshed_at = timezone.now()
                    config.save(update_fields=['subscribed_pages', 'pages_data', 'pages_refreshed_at'])
        projects = list(
            scope_to_company(Project.objects.filter(is_active=True), request.user).values('id', 'name')
        )
        # Lead count per Meta form (mapped or not) so the UI can flag forms that are
        # bringing in leads but aren't yet routed to a project.
        form_lead_counts = {
            row['meta_form_id']: row['c']
            for row in scope_to_company(Lead.objects.exclude(meta_form_id=''), request.user)
                        .values('meta_form_id').annotate(c=Count('id'))
        }
        return Response({
            'verify_token':         config.verify_token,
            'page_access_token':    config.page_access_token,
            # Whether a secret is stored, never the secret itself.
            'app_secret_set':       bool(config.app_secret),
            'default_project_id':   config.default_project_id,
            'is_active':            config.is_active,
            'total_leads_received': config.total_leads_received,
            'last_lead_at':         config.last_lead_at,
            'subscribed_pages':     config.subscribed_pages or [],
            'pages_data':           config.pages_data or [],
            'form_lead_counts':     form_lead_counts,
            'projects':             projects,
        })

    def post(self, request):
        config = self._ensure_config(request)
        action = request.data.get('action')
        if action == 'debug_forms':
            pat = config.page_access_token
            debug = {}
            pages_r = http_requests.get('https://graph.facebook.com/v19.0/me/accounts',
                                        params={'access_token': pat, 'limit': 50}, timeout=10)
            debug['accounts_status'] = pages_r.status_code
            debug['pages'] = []
            if pages_r.status_code == 200:
                for page in pages_r.json().get('data', []):
                    page_id = page.get('id')
                    page_name = page.get('name', page_id)
                    page_tok = page.get('access_token')
                    forms_r = http_requests.get(
                        f'https://graph.facebook.com/v19.0/{page_id}/leadgen_forms',
                        params={'access_token': page_tok, 'fields': 'id,name', 'limit': 50}, timeout=10)
                    debug['pages'].append({
                        'page': page_name,
                        'page_id': page_id,
                        'forms_status': forms_r.status_code,
                        'forms_response': forms_r.json(),
                    })
            else:
                debug['accounts_error'] = pages_r.json()
            return Response(debug)
        if action == 'regenerate_token':
            config.verify_token = secrets.token_urlsafe(32)
            config.save(update_fields=['verify_token'])
            return Response({'verify_token': config.verify_token})
        if action == 'save':
            pat = request.data.get('page_access_token', '').strip()
            pid = request.data.get('default_project_id')
            if pid and not _project_in_scope(request, pid):
                return Response({'detail': 'Invalid project for your company.'}, status=400)
            config.page_access_token = pat
            # Optional but strongly recommended: without it deliveries can't be
            # verified. Only overwrite when a value is supplied, so saving other
            # settings doesn't wipe a secret already stored.
            secret = str(request.data.get('app_secret', '') or '').strip()
            if secret:
                config.app_secret = secret
            config.default_project_id = pid if pid else None
            config.is_active = bool(pat)
            config.save(update_fields=['page_access_token', 'app_secret',
                                       'default_project_id', 'is_active'])
            # Subscribe app to all accessible pages' leadgen events
            subscribed, failed, pages_data = [], [], []
            if pat:
                try:
                    pages_r = http_requests.get(
                        'https://graph.facebook.com/v19.0/me/accounts',
                        params={'access_token': pat, 'limit': 50}, timeout=10
                    )
                    if pages_r.status_code == 200:
                        for page in pages_r.json().get('data', []):
                            page_token = page.get('access_token')
                            page_id    = page.get('id')
                            page_name  = page.get('name', page_id)
                            if not page_token or not page_id:
                                continue
                            sub_r = http_requests.post(
                                f'https://graph.facebook.com/v19.0/{page_id}/subscribed_apps',
                                params={'access_token': page_token,
                                        'subscribed_fields': 'leadgen'}, timeout=10
                            )
                            if sub_r.status_code == 200 and sub_r.json().get('success'):
                                subscribed.append(page_name)
                            else:
                                failed.append(page_name)
                except Exception:
                    logger.exception('Meta: failed to subscribe pages to app')
            _, pages_data = self._fetch_pages_and_forms(pat) if pat else ([], [])
            config.subscribed_pages   = subscribed
            config.pages_data         = pages_data
            config.pages_refreshed_at = timezone.now()
            config.save(update_fields=['subscribed_pages', 'pages_data', 'pages_refreshed_at'])
            return Response({'ok': True, 'is_active': config.is_active,
                             'subscribed_pages': subscribed, 'failed_pages': failed,
                             'pages_data': pages_data})
        return Response({'detail': 'Unknown action'}, status=400)


def _backfill_form_mapping(company, form_id, project, page_access_token=None):
    """Assign `project` to existing UNMAPPED leads that belong to this form, so a
    mapping added/fixed after leads arrived also fixes those leads. Two passes:
      1) leads already tagged with this form_id (stored on the lead);
      2) best-effort — leads with no/blank project that match (by phone) a lead in
         this form on Meta, covering leads that arrived before form_id was stored
         or without a form_id in the webhook payload.
    Returns the number of leads updated."""
    fid = str(form_id)
    n = Lead.objects.filter(company=company, project__isnull=True, meta_form_id=fid).update(project=project)
    if page_access_token:
        try:
            import urllib.request, json as _json
            phones, url, pages = set(), (
                f'https://graph.facebook.com/v19.0/{fid}/leads?fields=field_data&limit=200&access_token={page_access_token}'), 0
            while url and pages < 6:
                d = _json.load(urllib.request.urlopen(url, timeout=25))
                for r in d.get('data', []):
                    for f in r.get('field_data', []):
                        if 'phone' in (f.get('name', '').lower()):
                            digits = ''.join(c for c in (f.get('values') or [''])[0] if c.isdigit())[-10:]
                            if len(digits) >= 10:
                                phones.add(digits)
                url = d.get('paging', {}).get('next'); pages += 1
            for digits in phones:
                # endswith (not a (^|\D)…$ boundary regex): a +91-prefixed number like
                # +919510188522 has its 10-digit core preceded by the '1' of +91, so a
                # \D boundary never matches. Last-10 endswith matches the same number.
                n += Lead.objects.filter(
                    company=company, project__isnull=True,
                    phone_key=phone_blind_index(digits),
                ).update(project=project, meta_form_id=fid)
        except Exception:
            logger.exception('Meta backfill failed for form_id=%s', fid)
    return n


class MetaFormMappingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = _resolve_company(request)
        mappings = MetaFormMapping.objects.select_related('project').filter(
            company=company
        ).order_by('-created_at')
        return Response([{
            'id':          m.id,
            'form_id':     m.form_id,
            'form_name':   m.form_name,
            'project_id':  m.project_id,
            'project_name':m.project.name,
            'total_leads': m.total_leads,
        } for m in mappings])

    def post(self, request):
        form_id   = request.data.get('form_id', '').strip()
        form_name = request.data.get('form_name', '').strip()
        project_id = request.data.get('project_id')
        if not form_id or not project_id:
            return Response({'detail': 'form_id and project_id are required.'}, status=400)
        company = _resolve_company(request)
        try:
            project = Project.objects.filter(company=company).get(pk=project_id)
        except Project.DoesNotExist:
            return Response({'detail': 'Project not found.'}, status=404)
        mapping, created = MetaFormMapping.objects.update_or_create(
            form_id=form_id,
            defaults={'form_name': form_name, 'project': project, 'company': project.company},
        )
        # Retroactively map existing unmapped leads from this form.
        cfg = MetaWebhookConfig.objects.filter(company=company).first()
        backfilled = _backfill_form_mapping(
            company, form_id, project, cfg.page_access_token if cfg else None)
        return Response({
            'id': mapping.id, 'form_id': mapping.form_id,
            'form_name': mapping.form_name, 'project_id': mapping.project_id,
            'project_name': mapping.project.name, 'total_leads': mapping.total_leads,
            'backfilled': backfilled,
        }, status=201 if created else 200)

    def delete(self, request):
        mid = request.data.get('id')
        MetaFormMapping.objects.filter(pk=mid, company=_resolve_company(request)).delete()
        return Response({'ok': True})


# ── User Project Assignments ──────────────────────────────────────────────────
class UserProjectAssignmentView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user_id = request.query_params.get('user_id')
        if not user_id:
            return Response({'detail': 'user_id required.'}, status=400)
        assigned = scope_to_company(
            UserProjectAssignment.objects.filter(user_id=user_id),
            request.user, 'user__company',
        ).values_list('project_id', flat=True)
        return Response(list(assigned))

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=403)
        user_id     = request.data.get('user_id')
        project_ids = request.data.get('project_ids', [])
        try:
            user = User.objects.get(pk=user_id, company=request.user.company)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=404)
        # Only allow assigning projects that belong to the requester's company.
        valid_ids = list(
            scope_to_company(Project.objects.filter(pk__in=project_ids), request.user)
            .values_list('id', flat=True)
        )
        UserProjectAssignment.objects.filter(user=user).delete()
        UserProjectAssignment.objects.bulk_create([
            UserProjectAssignment(user=user, project_id=pid) for pid in valid_ids
        ], ignore_conflicts=True)
        from activity.recorder import note
        names = sorted(Project.objects.filter(pk__in=valid_ids).values_list('name', flat=True))
        note(request, 'Set projects for %s: %s' % (user.name, ', '.join(names) or 'none'),
             action='updated', target_type='user', target_id=user.id)
        return Response({'user_id': user_id, 'project_ids': valid_ids})


# ── Bulk Plot Creation ────────────────────────────────────────────────────────
class PlotBulkCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=403)
        project_id = request.data.get('project_id')
        plots_data = request.data.get('plots', [])
        try:
            project = scope_to_company(Project.objects.all(), request.user).get(pk=project_id)
        except Project.DoesNotExist:
            return Response({'detail': 'Project not found.'}, status=404)
        # Tower floor-builder sends the whole unit record (floor, areas, facing, price),
        # not just a number — carry every field through instead of dropping them.
        def _floor(v):
            try: return int(v)
            except (TypeError, ValueError): return None
        plots = [
            Plot(
                project=project,
                number=p.get('number', ''),
                cluster_type=p.get('cluster_type', ''),
                size=p.get('size', '') or '',
                construction_area=p.get('construction_area', '') or '',
                terrace_area=p.get('terrace_area', '') or '',
                facing=p.get('facing', '') or '',
                price=p.get('price', '') or '',
                notes=p.get('notes', '') or '',
                floor=_floor(p.get('floor')),
                status='available',
            )
            for p in plots_data
            if p.get('number')
        ]
        # Re-running the builder for one floor must not blow up on units that already
        # exist (project+number is unique) — skip the clashes, report what landed.
        created = Plot.objects.bulk_create(plots, ignore_conflicts=True)
        n = Plot.objects.filter(project=project, number__in=[p.number for p in plots]).count()
        return Response({'created': len(plots), 'existing_total': n}, status=201)


class PlotBulkDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=403)
        project_id = request.data.get('project_id')
        if not project_id:
            return Response({'detail': 'project_id is required.'}, status=400)
        if not _project_in_scope(request, project_id):
            return Response({'detail': 'Project not found.'}, status=404)
        # Wiping the map does not cancel anything. Booking.plot is SET_NULL, so every
        # live sale would survive pointing at no unit, and rebuilding the floor would
        # bring those units back as available to be sold a second time — the same
        # double-sale this project has already seen, through a different door.
        # project_id is already scoped to the caller's company by _project_in_scope.
        held = [b for b in Booking.objects.filter(project_id=project_id,
                                                  status__in=('pending', 'sold'))
                .only('id', 'plot_id', 'plot_ids', 'client_name', 'status')
                if b.plot_id or b.plot_ids]
        if held:
            first = held[0]
            return Response(
                {'detail': f'{len(held)} unit(s) in this project are held by a live booking — '
                           f'{first.client_name} (#{first.id}) among them. Cancel those bookings '
                           f'first; deleting the map would leave the sales standing with no unit.'},
                status=status.HTTP_409_CONFLICT,
            )
        deleted, _ = Plot.objects.filter(project_id=project_id).delete()
        Project.objects.filter(pk=project_id).update(total_plots=0)
        return Response({'deleted': deleted})


class PlotRenameTypeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=403)
        project_id = request.data.get('project_id')
        old_name   = request.data.get('old_name', '').strip()
        new_name   = request.data.get('new_name', '').strip()
        if not project_id or not old_name or not new_name:
            return Response({'detail': 'project_id, old_name and new_name are required.'}, status=400)
        if not _project_in_scope(request, project_id):
            return Response({'detail': 'Project not found.'}, status=404)
        updated = Plot.objects.filter(project_id=project_id, cluster_type=old_name).update(cluster_type=new_name)
        return Response({'updated': updated})


class SalesDataResetView(APIView):
    """Admin-only, company-scoped: wipe TRIAL transactional data (leads + their
    history/follow-ups/site-visits/closures, bookings, distribution log,
    availability, notifications) and reset all plots to 'available'. KEEPS setup:
    company, users, projects, plot definitions, lead sources, team/distribution
    config. POST requires confirm='DELETE'. GET returns current counts."""
    permission_classes = [IsAuthenticated]

    def _is_admin(self, user):
        return bool(
            getattr(user, 'is_staff', False) or getattr(user, 'role', '') == 'Admin' or is_platform_admin(user)
            or 'Sales' in (getattr(user, 'admin_modules', None) or [])
        )

    def _counts(self, co):
        from accounts.models import Notification
        return {
            'leads':            Lead.objects.filter(company=co).count(),
            'follow_ups':       FollowUp.objects.filter(lead__company=co).count(),
            'site_visits':      SiteVisit.objects.filter(lead__company=co).count(),
            'bookings':         Booking.objects.filter(company=co).count(),
            'cancelled_bookings': Booking.objects.filter(company=co, approval_status='CANCELLED').count(),
            'closures':         Closure.objects.filter(company=co).count(),
            'lead_history':     LeadStatusHistory.objects.filter(lead__company=co).count(),
            'distribution_log': DistributionLog.objects.filter(company=co).count(),
            'availability':     UserAvailability.objects.filter(user__company=co).count(),
            'notifications':    Notification.objects.filter(recipient__company=co).count(),
            'plots_to_reset':   Plot.objects.filter(project__company=co).exclude(status='available').count(),
        }

    def get(self, request):
        if not self._is_admin(request.user):
            return Response({'detail': 'Admin only.'}, status=status.HTTP_403_FORBIDDEN)
        return Response(self._counts(_resolve_company(request)))

    def post(self, request):
        if not self._is_admin(request.user):
            return Response({'detail': 'Admin only.'}, status=status.HTTP_403_FORBIDDEN)
        if (request.data.get('confirm') or '') != 'DELETE':
            return Response({'detail': 'Type DELETE to confirm.'}, status=status.HTTP_400_BAD_REQUEST)
        # A second gate the app itself does not hold: the reset key lives in the
        # server environment, so a signed-in admin — or anyone who takes over an
        # admin session — still cannot wipe the company's data without it.
        #
        # Fails closed on purpose. If DATA_RESET_KEY is unset the reset is refused
        # outright rather than silently falling back to the DELETE box, because a
        # missing key must never mean "no protection" on something irreversible.
        expected = (os.getenv('DATA_RESET_KEY') or '').strip()
        if not expected:
            return Response(
                {'detail': 'Data reset is disabled: no DATA_RESET_KEY is configured on the server.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        supplied = str(request.data.get('reset_key') or '').strip()
        if not hmac.compare_digest(supplied, expected):
            logger.warning('Data reset refused: bad key from user %s', getattr(request.user, 'id', None))
            return Response({'detail': 'Incorrect reset key.'}, status=status.HTTP_403_FORBIDDEN)
        co = _resolve_company(request)
        before = self._counts(co)
        with_attendance = bool(request.data.get('with_attendance'))
        with_loi        = bool(request.data.get('with_loi_files'))

        # Which categories to clear. Defaults to ALL (legacy behaviour) when the
        # client doesn't send an explicit selection.
        all_keys = ['bookings', 'cancelled_bookings', 'closures', 'site_visits', 'follow_ups', 'lead_history',
                    'distribution_log', 'availability', 'notifications', 'leads', 'plots_to_reset']
        raw = request.data.get('targets')
        if isinstance(raw, list) and raw:
            targets = [k for k in all_keys if k in raw]
        else:
            targets = list(all_keys)
        sel = set(targets)
        # Deleting leads cascades their children in the DB — reflect that in the summary.
        # Closures are deliberately NOT in that set: they own their company FK and only
        # SET_NULL their lead, so clearing leads leaves the conversion history (and the
        # bookings that point at it) intact.
        cascades = {'site_visits', 'follow_ups', 'lead_history'}
        effective = set(sel) | (cascades if 'leads' in sel else set())
        # An approved booking mirrors itself into a Closure; drop those alongside the
        # bookings so the two counts can never disagree. Standalone closures (imported
        # or recorded by hand, with no booking) are only removed by ticking Closures.
        booking_closures = Closure.objects.filter(
            id__in=Booking.objects.filter(company=co).exclude(closure=None).values('closure_id')
        ) if 'bookings' in sel else Closure.objects.none()
        n_booking_closures = booking_closures.count()

        # Optionally purge confidential LOI PDFs from Supabase before deleting bookings.
        if with_loi and 'bookings' in sel:
            for b in Booking.objects.filter(company=co).exclude(loi_document=''):
                try: b.loi_document.delete(save=False)
                except Exception: pass

        from django.db import transaction
        from accounts.models import Notification
        with transaction.atomic():
            if 'bookings' in sel:
                Closure.objects.filter(id__in=list(booking_closures.values_list('id', flat=True))).delete()
                Booking.objects.filter(company=co).delete()
            # Purge only the cancelled booking records (the CANCELLED log entries).
            if 'cancelled_bookings' in sel and 'bookings' not in sel:
                Booking.objects.filter(company=co, approval_status='CANCELLED').delete()
            if 'closures' in sel:         Closure.objects.filter(company=co).delete()
            if 'site_visits' in sel:      SiteVisit.objects.filter(lead__company=co).delete()
            if 'follow_ups' in sel:       FollowUp.objects.filter(lead__company=co).delete()
            if 'lead_history' in sel:     LeadStatusHistory.objects.filter(lead__company=co).delete()
            if 'distribution_log' in sel: DistributionLog.objects.filter(company=co).delete()
            if 'availability' in sel:     UserAvailability.objects.filter(user__company=co).delete()
            if 'notifications' in sel:    Notification.objects.filter(recipient__company=co).delete()
            if 'leads' in sel:            Lead.objects.filter(company=co).delete()  # cascades children
            if 'plots_to_reset' in sel:   Plot.objects.filter(project__company=co).exclude(status='available').update(status='available')
            if with_attendance:
                from attendance.models import AttendanceRecord, LeaveApplication, LeaveTransaction, LeaveBalance
                AttendanceRecord.objects.filter(user__company=co).delete()
                LeaveApplication.objects.filter(user__company=co).delete()
                LeaveTransaction.objects.filter(user__company=co).delete()
                LeaveBalance.objects.filter(user__company=co).delete()
        deleted = {k: v for k, v in before.items() if k in effective}
        if n_booking_closures and 'closures' not in effective:
            deleted['closures'] = n_booking_closures  # the booking-mirrored ones only
        return Response({'detail': 'Trial data cleared.', 'deleted': deleted, 'targets': sorted(effective)})


class BackupSettingsView(APIView):
    """Platform-super-user-only: view/update the automatic backup schedule."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not is_platform_admin(request.user):
            return Response({'detail': 'Super admin only.'}, status=status.HTTP_403_FORBIDDEN)
        settings_row, _ = BackupSettings.objects.get_or_create(pk=1)
        return Response(BackupSettingsSerializer(settings_row).data)

    def patch(self, request):
        if not is_platform_admin(request.user):
            return Response({'detail': 'Super admin only.'}, status=status.HTTP_403_FORBIDDEN)
        settings_row, _ = BackupSettings.objects.get_or_create(pk=1)
        ser = BackupSettingsSerializer(settings_row, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        ser.save(updated_by=request.user)
        return Response(ser.data)


class BackupListView(APIView):
    """Platform-super-user-only: recent backup history."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not is_platform_admin(request.user):
            return Response({'detail': 'Super admin only.'}, status=status.HTTP_403_FORBIDDEN)
        records = BackupRecord.objects.select_related('triggered_by')[:50]
        return Response(BackupRecordSerializer(records, many=True).data)


class BackupRunNowView(APIView):
    """Platform-super-user-only: trigger a backup immediately, outside the schedule."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_platform_admin(request.user):
            return Response({'detail': 'Super admin only.'}, status=status.HTTP_403_FORBIDDEN)
        from .backup_service import run_backup
        record = run_backup(triggered_by=request.user)
        return Response(BackupRecordSerializer(record).data,
                         status=status.HTTP_201_CREATED if record.status == 'success' else status.HTTP_502_BAD_GATEWAY)


class BackupDownloadView(APIView):
    """Platform-super-user-only: a short-lived signed URL for one backup file."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not is_platform_admin(request.user):
            return Response({'detail': 'Super admin only.'}, status=status.HTTP_403_FORBIDDEN)
        record = BackupRecord.objects.filter(pk=pk, status='success').first()
        if not record or not record.file_path:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        from .backup_storage import signed_backup_url
        url = signed_backup_url(record.file_path)
        if not url:
            return Response({'detail': 'Could not generate a download link.'}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({'url': url})


# ─────────────────────────────────────────────────────────────────────────────
# Lead transfer: one STM hands a lead to another, held for approval
# ─────────────────────────────────────────────────────────────────────────────
def _transfer_approver_ids(company, project_id):
    """Who may sign off a transfer of a lead on this project.

    The same people who approve that project's bookings — the list an admin sets in
    Booking & Approvals. Deliberately not "any manager": a lead moving between reps
    changes whose numbers it lands in, so it needs the same named authority a booking
    does. A project with nobody named leaves only real admins, which is the same rule
    _can_approve_project applies.
    """
    ids = set()
    for p in Project.objects.filter(company=company).only('id', 'booking_approvers'):
        if project_id and p.id != project_id:
            continue
        ids.update(p.booking_approvers or [])
    return sorted(ids)


class LeadTransferListCreateView(APIView):
    """GET  — transfers this user can see (their own requests, plus the queue they approve).
    POST — request a transfer of one of your leads to another STM."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = _resolve_company(request)
        qs = (LeadTransfer.objects.filter(company=company)
              .select_related('lead', 'project', 'from_stm', 'to_stm', 'requested_by', 'decided_by')
              .defer(*PROJECT_BLOBS))
        if not _is_hard_admin(request.user):
            approver_pids = _approver_project_ids(request.user, company)
            qs = qs.filter(
                Q(requested_by=request.user) | Q(from_stm=request.user) | Q(to_stm=request.user)
                | Q(project_id__in=approver_pids)
            )
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        # The Channel Partner module shows only partner-sourced work. Transferring a
        # lead between STMs is a Sales activity, so its queue was arriving in the CP
        # module untouched — a CP approver was being asked to decide transfers for
        # leads that never came through a partner. Same test and same parameter the
        # rest of the module's lists use.
        if request.query_params.get('cp_only') == 'true' or is_cp_designated(request.user):
            qs = qs.filter(cp_lead_q(prefix='lead__'))
        return Response(LeadTransferSerializer(qs[:200], many=True).data)

    def post(self, request):
        company = _resolve_company(request)
        lead_id = request.data.get('lead')
        to_stm_id = request.data.get('to_stm')
        if not lead_id or not to_stm_id:
            return Response({'detail': 'Lead and destination STM are required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        lead = Lead.objects.filter(pk=lead_id, company=company).first()
        if not lead:
            return Response({'detail': 'Lead not found.'}, status=status.HTTP_404_NOT_FOUND)
        # Only the rep holding the lead may hand it on (admins may act for them).
        if not _is_hard_admin(request.user) and lead.stm_id != request.user.id:
            return Response({'detail': 'This lead is not assigned to you.'},
                            status=status.HTTP_403_FORBIDDEN)
        if str(to_stm_id) == str(lead.stm_id):
            return Response({'detail': 'That is already the assigned STM.'},
                            status=status.HTTP_400_BAD_REQUEST)
        to_stm = User.objects.filter(pk=to_stm_id, company=company, is_active=True).first()
        if not to_stm:
            return Response({'detail': 'Destination STM not found in your company.'},
                            status=status.HTTP_404_NOT_FOUND)
        if LeadTransfer.objects.filter(lead=lead, status='pending').exists():
            return Response({'detail': 'A transfer for this lead is already awaiting approval.'},
                            status=status.HTTP_409_CONFLICT)

        t = LeadTransfer.objects.create(
            company=company, lead=lead, project_id=lead.project_id,
            from_stm_id=lead.stm_id, to_stm=to_stm, requested_by=request.user,
            reason=(request.data.get('reason') or '').strip(),
        )
        _notify_transfer_requested(t)
        return Response(LeadTransferSerializer(t).data, status=status.HTTP_201_CREATED)


class LeadTransferActionView(APIView):
    """Approve or reject a pending transfer. Approval is what actually moves the lead."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        company = _resolve_company(request)
        t = LeadTransfer.objects.filter(pk=pk, company=company).select_related('lead').first()
        if not t:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        action = request.data.get('action')

        # The requester may withdraw their own request; everything else needs an approver.
        if action == 'cancel':
            if t.requested_by_id != request.user.id and not _is_hard_admin(request.user):
                return Response({'detail': 'Only the requester can withdraw this.'},
                                status=status.HTTP_403_FORBIDDEN)
        elif not _can_approve_project(request.user, t.project_id, company):
            return Response({'detail': 'You are not an approver for this project.'},
                            status=status.HTTP_403_FORBIDDEN)

        if t.status != 'pending':
            return Response({'detail': 'This request has already been %s.' % t.status},
                            status=status.HTTP_409_CONFLICT)
        if action not in ('approve', 'reject', 'cancel'):
            return Response({'detail': 'Unknown action.'}, status=status.HTTP_400_BAD_REQUEST)

        note = (request.data.get('note') or '').strip()
        if action == 'approve':
            with transaction.atomic():
                # Re-read under a lock: the lead may have moved since the request was
                # raised, and the record should say what it actually moved from.
                lead = Lead.objects.select_for_update().get(pk=t.lead_id)
                t.from_stm_id = lead.stm_id
                lead.stm_id = t.to_stm_id
                lead.stm_assigned_at = timezone.now()
                lead.save(update_fields=['stm', 'stm_assigned_at'])
                t.status = 'approved'
                t.decided_by = request.user
                t.decided_at = timezone.now()
                t.decision_note = note
                t.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_note',
                                      'from_stm', 'updated_at'])
        else:
            t.status = 'rejected' if action == 'reject' else 'cancelled'
            t.decided_by = request.user
            t.decided_at = timezone.now()
            t.decision_note = note
            t.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_note', 'updated_at'])

        _notify_transfer_decided(t)
        return Response(LeadTransferSerializer(t).data)


def _notify_transfer_requested(t):
    """Tell the approvers there is something to look at, and the receiving STM it is coming."""
    try:
        from notifications import notify, notify_many
        who = User.objects.filter(id__in=_transfer_approver_ids(t.company, t.project_id))
        body = '%s wants to transfer %s to %s' % (
            getattr(t.requested_by, 'name', 'An STM') or 'An STM',
            getattr(t.lead, 'name', 'a lead') or 'a lead',
            getattr(t.to_stm, 'name', 'another STM') or 'another STM')
        notify_many(who, 'lead_transfer_requested', 'Lead Transfer Request', body,
                    data={'transfer': t.id, 'lead': t.lead_id})
        if t.to_stm:
            notify(t.to_stm, 'lead_transfer_requested', 'Lead Coming Your Way',
                   '%s has asked to transfer %s to you — awaiting approval.' % (
                       getattr(t.requested_by, 'name', 'An STM') or 'An STM',
                       getattr(t.lead, 'name', 'a lead') or 'a lead'),
                   data={'transfer': t.id, 'lead': t.lead_id})
    except Exception:
        logger.exception('Could not notify for lead transfer %s', t.pk)


def _notify_transfer_decided(t):
    """Tell both reps the outcome — the one losing the lead and the one gaining it."""
    try:
        from notifications import notify
        verb = {'approved': 'approved', 'rejected': 'rejected', 'cancelled': 'withdrawn'}[t.status]
        lead_name = getattr(t.lead, 'name', 'a lead') or 'a lead'
        for person in {t.requested_by_id: t.requested_by, t.from_stm_id: t.from_stm,
                       t.to_stm_id: t.to_stm}.values():
            if person and person.id != t.decided_by_id:
                notify(person, 'lead_transfer_%s' % t.status, 'Lead Transfer %s' % verb.title(),
                       'The transfer of %s was %s.' % (lead_name, verb),
                       data={'transfer': t.id, 'lead': t.lead_id})
    except Exception:
        logger.exception('Could not notify the outcome of lead transfer %s', t.pk)
