# Building a new Nexora module

Everything a new module needs in order to behave like AR and Club 1000: admins decide
per company who may do what, which screens each designation sees, which dashboard
opens for them, and every change is recorded in the log.

The worked example is **Purchase** (vendors and purchase orders). Replace `purchase`
with your module's short name throughout.

The permission system has two layers, and they answer different questions:

| Layer | Question | Where it is set |
|---|---|---|
| Module access | Which modules may this person enter? | User Management, per person (`modules`, `manager_modules`, `admin_modules`) |
| Designation settings | Inside a module: what may they do, which screens, which dashboard, whose records? | Designation Master → Permissions, per designation, per company |

Build the screens first. Wire the permissions once the module works.

---

## 1. Backend

### 1.1 Register the module name

`vistaraweb/src/lib/moduleAccess.js` → `ALL_MODULES`, and the same list in the app, so
"Purchase" appears in User Management and the sidebar.

Nothing in the Permissions editor itself needs changing. The three tabs build
themselves from the lists below, which the editor reads through
`GET /api/auth/designations/capabilities/`: the Actions tab groups `CAPABILITIES`
by module, the Menu tab groups `SCREENS` by module, and the Dashboard tab lists
`DASHBOARDS`. Add your rows and your module appears as its own card in each tab,
on the website and in the app.

### 1.2 Declare the actions

`accounts/capabilities.py` → `CAPABILITIES`. One row per thing a person can *do*, with a
label and one line of help — both are shown in the editor, so write them for the admin,
not for yourself:

```python
('purchase.po.create',    'Raise purchase orders', 'Purchase', 'Create a PO against a vendor.'),
('purchase.po.approve',   'Approve purchase orders', 'Purchase', 'Sign off a PO so it can be sent.'),
('purchase.vendor.manage','Add and edit vendors',  'Purchase', 'Create a vendor and change its details.'),
```

Anything everyone with the module could already do (because the module is new, that is
usually all of it) also goes in `DEFAULT_ON`, so existing designations keep it.

### 1.3 Declare the screens

`accounts/capabilities.py` → `SCREENS`. One row per menu item:

```python
('purchase.screen.dashboard', 'Dashboard', 'Purchase'),
('purchase.screen.orders',    'Purchase Orders', 'Purchase'),
('purchase.screen.vendors',   'Vendors', 'Purchase'),
```

Add the usual menu for each job to `PRESET_SCREENS` so the presets stay useful.

**Adding screens to a module that already exists?** A designation someone has
already configured has none of your new keys ticked, so its menu would empty out
the day you deploy. Write a data migration that adds them to the rows that
already imply them — `accounts/migrations/0016_seed_cp_screens.py` is the worked
example (it gave every designation holding `sales.screen.cp` the eight new
Channel Partner keys). A brand-new module needs no migration: nobody can have
configured it yet, and an unset designation sees everything.

### 1.4 Declare the dashboards

Each module has a Dashboard with a **role filter** on top: one dashboard per role
level, the roles being the ones User Management creates people with
(`DASHBOARD_ROLES` — Director, General Manager, Manager, Employee, Intern).

Write the views, then declare them in `accounts/capabilities.py` → `DASHBOARDS`:

```python
('purchase_buyer',   'Buyer — their own orders',   'Purchase', 'Employee'),
('purchase_manager', 'Manager — the whole desk',   'Purchase', 'Manager'),
('purchase_director','Director — company figures', 'Purchase', 'Director'),
```

In the module's dashboard page, list the same keys and render the matching view.
`app/sales/page.js` is the worked example:

```jsx
export const PURCHASE_DASHBOARDS = [
  { key: 'purchase_buyer',   role: 'Employee', label: 'Buyer' },
  { key: 'purchase_manager', role: 'Manager',  label: 'Manager' },
];

const chosen = preview || dashboardFor(user);     // what an admin is looking at,
const body = viewFor(chosen) || <YourDefault />;  // else what they are pinned to
return isAdmin ? <><DashboardRoleFilter options={PURCHASE_DASHBOARDS}
  value={chosen} onChange={setPreview} />{body}</> : body;
```

The filter shows for admins only — it is how you see what each role gets before
pinning it. Designation Master → Permissions → Dashboard lists the same rows,
filtered by role, and that is what decides which one a designation opens.

Whichever view opens, the figures stay scoped by role and the reporting tree:
`accounts/test_permission_matrix.py::DashboardFollowsTheTreeTests` proves a
telecaller pinned to the Manager dashboard still counts only their own leads.

### 1.5 Gate the actions

One helper per module — the module stays the gate, capabilities refine it. Copy
`receivables/permissions.py`:

```python
def purchase_can(user, key):
    from accounts.capabilities import user_can
    return has_purchase_access(user) and user_can(user, key)
```

Then each write endpoint checks its key:

```python
def post(self, request):
    if not purchase_can(request.user, 'purchase.po.create'):
        return Response({'detail': 'You cannot raise purchase orders.'}, status=403)
```

### 1.6 Scope the records

If the module lists records owned by people, honour the designation's scope:

```python
from accounts.capabilities import SCOPE_COMPANY, SCOPE_OWN, SCOPE_TEAM, data_scope
```

`''` means "as before" — keep your existing role/reporting-tree rule for that case.
`sales/views.py::scope_leads_to_role` is the reference implementation.

### 1.7 Record who did what

Required for every module (see the rule in the project memory).

- Add the app to `TRACKED_APPS` in `activity/changes.py` and map the URL prefix in
  `MODULES` in `activity/recorder.py`.
- Add your models to `record_label()` and `TYPE_OF` so log lines name the record
  ("PO-1042 — Shah Traders"), not `#17`.
- Where a view writes with `queryset.update()` or does something the URL cannot
  describe, call `note(request, summary, action=…, target_type=…, target_id=…)`.
  Preview or dry-run endpoints call `skip(request)`.

### 1.8 Tests

- One test per action: allowed by default, refused when the designation unticks it.
- One test that an unconfigured designation keeps today's behaviour.
- One test that the module's main action writes a named line to the activity log.

`accounts/test_capabilities.py` and `sales/test_full_erp_roles.py` are the patterns.

---

## 2. Web

- **Menu:** give every nav item a `screen:` key and filter with
  `canSee(user, item.screen)` — see `app/m/[module]/layout.js`. Every item needs
  its own key: one key covering a whole sub-menu means an admin who unticks it
  changes nothing (that was the Channel Partner bug).
- **The address, too:** hiding an item has to hide the page, or anyone who
  remembers the URL walks back in. Match the path against the nav list and send a
  blocked person to the first screen they can see — or show a note when there is
  none. `app/m/[module]/layout.js` and `app/sales/layout.js` both do this.
- **Dashboard:** pick the view from `dashboardFor(user)`, falling back to your own
  default — see `app/sales/page.js`.
- **Buttons:** hide what the person cannot do with `can(user, 'purchase.po.approve')`
  from `lib/moduleAccess.js`. The server still enforces it; this only avoids offering
  something that will be refused.
- **Log tab:** admin-only, `<ActivityLogView modules={['Purchase']} />`.

## 3. App

Mirror the web, every time (project rule):

- `lib/roles.js` provides the same `can`, `canSee` and `dashboardFor`.
- Menu tiles carry `screen:` keys and filter the same way — see `SalesCRMScreen`,
  `ChannelPartnerHubScreen`, `Club1000HubScreen`, `ARDashboardScreen`.
- Gate the same buttons with `can(user, …)` — `ARLedgerScreen` and
  `Club1000PayoutsScreen` are the patterns.
- Admin-only Log screen: `ActivityLog` with `modules` and `title` params.
- The app reloads the profile from `/api/auth/me/` on launch and the web on every
  session check, so a permissions change reaches people without signing out. Read
  permissions from the Redux user, never from a copy you cached yourself.

---

## 4. Rolling it out

1. Deploy. Nothing changes yet: a designation nobody has configured keeps today's
   behaviour, and new actions are on by default.
2. Admin → Designation Master → shield on each designation: tick the actions and
   screens, choose the dashboard and the record scope. Per company.
3. User Management: tick the module for the people who need it and give them a
   designation. That is the whole of adding a new joiner.

## 5. Things that catch people out

- **A designation is per company.** Company A's "Purchase Executive" and company B's
  are separate rows with separate ticks.
- **A person's designation must match a Designation Master row** (matched by name,
  ignoring case). A title typed freehand falls back to the old rules.
- **Module access is not a capability.** Ticking `purchase.po.create` does nothing for
  someone without the Purchase module.
- **Adding a capability that everybody already had?** Put it in `DEFAULT_ON`, or
  existing designations will lose it the moment someone saves the editor.
- **Log text is encrypted at rest.** Never put a password, OTP or token in a summary.
- **An empty tick list is an answer.** `screens_set` / `capabilities_set` say the
  company has configured the designation; `screens = []` means "no menu at all",
  not "not set up yet". Never treat an empty list as unset.
- **Put the guard's hook above every early return.** A `useEffect` added after a
  layout's `if (!user) return …` crashes the page with a hook-order error.
- **The app's hub screen is its dashboard.** A `*.screen.dashboard` key can hide
  the item on the website, but in the app that screen is where the module opens —
  don't filter the hub out from under the person.
- **Module access is checked first, everywhere.** `purchase_can` = has the module
  AND holds the capability. `accounts/test_permission_matrix.py` is the test to
  copy for a new module: every capability ticked but no module must still be refused.
