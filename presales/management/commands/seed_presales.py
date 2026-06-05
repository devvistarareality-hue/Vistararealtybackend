from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User
from companies.models import Company
from presales.models import Lead, LeadActivity, Project


class Command(BaseCommand):
    help = 'Seed presales demo data: 3 projects, 3 sales persons, 5 leads'

    def handle(self, *args, **options):
        company = Company.objects.first()
        if not company:
            self.stderr.write('No company found. Create one via /admin first.')
            return

        # ── 3 Sales Persons ──────────────────────────────────────────
        sales_data = [
            {'email': 'arun.kumar@vistara.com',  'name': 'Arun Kumar',  'role': 'STM',             'user_code': 'SP001'},
            {'email': 'neha.shah@vistara.com',   'name': 'Neha Shah',   'role': 'Sales Executive', 'user_code': 'SP002'},
            {'email': 'karan.desai@vistara.com', 'name': 'Karan Desai', 'role': 'Sales Executive', 'user_code': 'SP003'},
        ]
        sales_persons = []
        for sd in sales_data:
            user, created = User.objects.get_or_create(
                email=sd['email'],
                defaults={
                    'name':      sd['name'],
                    'role':      sd['role'],
                    'user_code': sd['user_code'],
                    'company':   company,
                    'is_active': True,
                },
            )
            if created:
                user.set_password('Sales@123')
                user.save()
                self.stdout.write(f'  Created user: {user.name}')
            else:
                self.stdout.write(f'  Exists: {user.name}')
            sales_persons.append(user)

        arun, neha, karan = sales_persons

        # ── 3 Projects ───────────────────────────────────────────────
        project_data = [
            {
                'name': 'Vistara Heights',
                'location': 'Ahmedabad, Gujarat',
                'type': 'Residential',
                'units': 120,
                'price_range': '₹45L – ₹85L',
                'status': 'Active',
                'description': '2 & 3 BHK premium apartments with modern amenities.',
            },
            {
                'name': 'Skyline Business Park',
                'location': 'Surat, Gujarat',
                'type': 'Commercial',
                'units': 50,
                'price_range': '₹1.2Cr – ₹2.5Cr',
                'status': 'Active',
                'description': 'Grade-A commercial offices in prime Surat business district.',
            },
            {
                'name': 'Green Valley Residency',
                'location': 'Gandhinagar, Gujarat',
                'type': 'Residential',
                'units': 80,
                'price_range': '₹35L – ₹65L',
                'status': 'Upcoming',
                'description': 'Affordable township with green spaces and club house.',
            },
        ]
        projects = []
        for pd in project_data:
            proj, created = Project.objects.get_or_create(
                name=pd['name'],
                defaults={**pd, 'created_by': arun},
            )
            if created:
                self.stdout.write(f'  Created project: {proj.name}')
            else:
                self.stdout.write(f'  Exists: {proj.name}')
            projects.append(proj)

        vistara_heights, skyline, green_valley = projects

        # ── 5 Leads (one per status + one extra) ─────────────────────
        leads_data = [
            {
                'name': 'Rajesh Sharma', 'phone': '+91 98765 43210',
                'email': 'rajesh.sharma@gmail.com',
                'project': vistara_heights, 'source': 'Walk-in',
                'status': 'New', 'assigned_to': neha,
                'budget': '₹60L – ₹70L',
                'notes': 'Interested in 2BHK on higher floors. Wants site visit.',
                'activities': [
                    ('Enquiry', 'Walk-in enquiry at site office.'),
                ],
            },
            {
                'name': 'Priya Mehta', 'phone': '+91 87654 32109',
                'email': 'priya.mehta@gmail.com',
                'project': green_valley, 'source': 'Online',
                'status': 'Cold', 'assigned_to': karan,
                'budget': '₹40L – ₹55L',
                'notes': 'Enquired about 3BHK via website. Not responding to calls.',
                'activities': [
                    ('Enquiry', 'Online enquiry via website form.'),
                    ('Call', 'Called twice — no response.'),
                    ('Status Change', 'Status changed from New to Cold.'),
                ],
            },
            {
                'name': 'Amit Patel', 'phone': '+91 76543 21098',
                'email': 'amit.patel@gmail.com',
                'project': vistara_heights, 'source': 'Reference',
                'status': 'Warm', 'assigned_to': arun,
                'budget': '₹65L – ₹80L',
                'notes': 'Referred by existing client. Very interested, site visit scheduled.',
                'activities': [
                    ('Enquiry', 'Referred by existing client Rajesh Sharma.'),
                    ('Call', 'Follow-up call — confirmed interest in 3BHK.'),
                    ('Transfer', f'Lead transferred to {arun.name} ({arun.role}) and marked Warm.'),
                ],
            },
            {
                'name': 'Sunita Joshi', 'phone': '+91 65432 10987',
                'email': 'sunita.joshi@gmail.com',
                'project': skyline, 'source': 'Phone',
                'status': 'Lost', 'assigned_to': karan,
                'budget': '₹1.5Cr – ₹2Cr',
                'notes': 'Opted for a competitor project in the same area.',
                'activities': [
                    ('Call', 'Interested in commercial unit, 800 sq ft.'),
                    ('Call', 'Second call — hesitant about pricing.'),
                    ('Status Change', 'Status changed to Lost — opted for competitor.'),
                ],
            },
            {
                'name': 'Vivek Nair', 'phone': '+91 54321 09876',
                'email': 'vivek.nair@gmail.com',
                'project': green_valley, 'source': 'Walk-in',
                'status': 'New', 'assigned_to': neha,
                'budget': '₹38L – ₹50L',
                'notes': 'First-time buyer, needs detailed info about loan options.',
                'activities': [
                    ('Walk-in', 'Visited gallery office, collected brochure.'),
                ],
            },
        ]

        for ld in leads_data:
            activities = ld.pop('activities')
            lead, created = Lead.objects.get_or_create(
                phone=ld['phone'],
                defaults={**ld, 'created_by': arun},
            )
            if created:
                for act_type, act_note in activities:
                    LeadActivity.objects.create(
                        lead=lead, type=act_type, note=act_note, created_by=arun,
                    )
                self.stdout.write(f'  Created lead: {lead.name} ({lead.status})')
            else:
                self.stdout.write(f'  Exists: {lead.name}')

        self.stdout.write(self.style.SUCCESS('\nSeed complete: 3 projects, 3 sales persons, 5 leads.'))
