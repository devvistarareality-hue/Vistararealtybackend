from django.db import transaction
from rest_framework import serializers
from .models import User, Designation

VRL_CODE = 'VRL'


class ReportingManagerSerializer(serializers.ModelSerializer):
    class Meta:
        model  = User
        fields = ['id', 'name', 'user_code', 'role', 'designation']


class LoginSerializer(serializers.Serializer):
    company_code = serializers.CharField(max_length=20)
    user_code    = serializers.CharField(max_length=20)
    password     = serializers.CharField(write_only=True)
    platform     = serializers.ChoiceField(choices=['app', 'web'], default='app')


class UserSerializer(serializers.ModelSerializer):
    company_code      = serializers.SerializerMethodField()
    company_name      = serializers.SerializerMethodField()
    reporting_manager = ReportingManagerSerializer(read_only=True)
    is_approver       = serializers.SerializerMethodField()
    # What this person may do, resolved from their company's designation settings.
    capabilities      = serializers.SerializerMethodField()

    def get_capabilities(self, obj):
        from .capabilities import capabilities_for
        return sorted(capabilities_for(obj))

    # The menu this person sees (null keeps the old role-based menu) and which
    # dashboard opens for them.
    screens = serializers.SerializerMethodField()
    dashboard = serializers.SerializerMethodField()

    def get_screens(self, obj):
        from .capabilities import screens_for
        allowed = screens_for(obj)
        return None if allowed is None else sorted(allowed)

    # The modules that saved menu speaks for. The browser filters its own sidebar
    # too, so it needs the same rule: a module not named here keeps its defaults.
    screen_modules = serializers.SerializerMethodField()

    def get_screen_modules(self, obj):
        from .capabilities import screen_modules_for
        named = screen_modules_for(obj)
        return None if named is None else sorted(named)

    def get_dashboard(self, obj):
        from .capabilities import dashboard_for
        return dashboard_for(obj)

    # What their ROLE opens in each module (Copy to role, on a module's
    # Dashboard). The designation's own pin, above, still wins.
    role_dashboards = serializers.SerializerMethodField()

    def get_role_dashboards(self, obj):
        from .capabilities import role_dashboards
        return role_dashboards(obj)

    def get_company_code(self, obj):
        return obj.company.code if obj.company else ''

    def get_company_name(self, obj):
        return obj.company.name if obj.company else ''

    def get_is_approver(self, obj):
        # Can this user action leave requests? Admins/staff, or anyone who is a
        # reporting manager for at least one other user.
        if obj.is_staff or getattr(obj, 'role', '') == 'Admin':
            return True
        return obj.subordinates.exists()

    class Meta:
        model  = User
        fields = [
            'id', 'user_code', 'name', 'email', 'phone',
            'role', 'department', 'designation', 'avatar_url',
            'modules', 'manager_modules', 'admin_modules',
            'company_code', 'company_name', 'is_staff',
            'reporting_manager', 'is_approver', 'can_export_bookings',
            'capabilities', 'screens', 'screen_modules', 'dashboard', 'role_dashboards',
        ]


class DesignationSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source='company.code', read_only=True)
    company_name = serializers.CharField(source='company.name', read_only=True)
    # What this designation grants right now: its ticks, or — when a company has
    # never opened the screen — what the title already implied.
    effective_capabilities = serializers.SerializerMethodField()

    def get_effective_capabilities(self, obj):
        from .capabilities import legacy_capabilities
        return sorted(obj.capabilities or []) if obj.capabilities_set else sorted(legacy_capabilities(obj.name, obj.module))

    # Which menu the editor should show ticked before anyone configures it.
    effective_screens = serializers.SerializerMethodField()

    def get_effective_screens(self, obj):
        from .capabilities import preset_screens
        return sorted(obj.screens or []) if obj.screens_set else preset_screens(obj.name, obj.module)

    # Which modules that menu speaks for. Older rows say nothing, so they read as
    # the module the designation was configured in — see screen_modules_for.
    effective_screen_modules = serializers.SerializerMethodField()

    def get_effective_screen_modules(self, obj):
        from .capabilities import SCREEN_MODULE, modules_of
        named = sorted(obj.screens_modules or [])
        if named or not obj.screens_set:
            return named
        return sorted({SCREEN_MODULE[k] for k in (obj.screens or []) if k in SCREEN_MODULE}
                      | set(modules_of(obj.module)))

    class Meta:
        model  = Designation
        fields = ['id', 'name', 'module', 'company_code', 'company_name',
                  'capabilities', 'capabilities_set', 'effective_capabilities', 'data_scope',
                  'screens', 'screens_set', 'effective_screens',
                  'screens_modules', 'effective_screen_modules', 'dashboard']


class UserListSerializer(serializers.ModelSerializer):
    module_count      = serializers.SerializerMethodField()
    is_manager        = serializers.SerializerMethodField()
    company_code      = serializers.CharField(source='company.code', read_only=True)
    company_name      = serializers.CharField(source='company.name', read_only=True)
    reporting_manager = ReportingManagerSerializer(read_only=True)

    def get_module_count(self, obj):
        return len(obj.modules) if obj.modules else 0

    def get_is_manager(self, obj):
        return bool(obj.manager_modules)

    class Meta:
        model  = User
        fields = [
            'id', 'user_code', 'name', 'email', 'phone', 'role', 'designation',
            'modules', 'manager_modules', 'admin_modules', 'module_count', 'is_manager', 'is_active',
            'can_export_bookings', 'company_code', 'company_name', 'reporting_manager',
        ]


# Roles that can sit at the top of the tree: leadership, and the unattended kiosk
# account, which belongs to no one by design. Everyone else — STM, Telecaller, CP
# Executive — must report to somebody.
TOP_LEVEL_ROLES = {'Admin', 'Director', 'General Manager', 'Manager', 'Kiosk'}

_NO_MANAGER = (
    'Select a Reporting Manager. Visibility runs on the reporting tree, so someone '
    'at this level with no manager is invisible to every manager in the company — '
    'their leads and bookings appear in nobody\'s list.'
)


def validate_reporting_manager(role, reporting_manager_id, is_active=True):
    """A non-leadership user with no manager is a hole in the org tree, not a
    preference. It cost us an STM with 78 bookings that no manager could see: his
    work surfaced only where a rule reached past the hierarchy (the CP pool), which
    is why the same figure read 67 in one module and 65 in another.

    Deactivating is exempt — closing an account should not require fixing the tree
    first, and an inactive user is outside every visibility rule anyway.
    """
    if not is_active or (role or '') in TOP_LEVEL_ROLES or reporting_manager_id:
        return
    raise serializers.ValidationError({'reporting_manager_id': _NO_MANAGER})


class UserCreateSerializer(serializers.ModelSerializer):
    password              = serializers.CharField(write_only=True, min_length=6)
    user_code_prefix      = serializers.CharField(write_only=True, required=False, max_length=10, default='USR')
    company_id            = serializers.IntegerField(write_only=True, required=False)
    reporting_manager_id  = serializers.IntegerField(write_only=True, required=False, allow_null=True)

    class Meta:
        model  = User
        fields = ['name', 'email', 'phone', 'password', 'role', 'designation', 'modules', 'manager_modules', 'admin_modules', 'can_export_bookings', 'user_code_prefix', 'company_id', 'reporting_manager_id']

    def validate(self, attrs):
        validate_reporting_manager(attrs.get('role'), attrs.get('reporting_manager_id'))
        return attrs

    def create(self, validated_data):
        from companies.models import Company as CompanyModel
        request              = self.context['request']
        company_id           = validated_data.pop('company_id', None)
        password             = validated_data.pop('password')
        prefix               = validated_data.pop('user_code_prefix', 'USR').upper().strip() or 'USR'
        reporting_manager_id = validated_data.pop('reporting_manager_id', None)

        is_padmin = (
            request.user.is_staff or (
                getattr(request.user, 'company', None) and
                getattr(request.user.company, 'code', '').upper() == VRL_CODE and
                getattr(request.user, 'role', '') == 'Admin'
            )
        )

        if company_id and is_padmin:
            try:
                company = CompanyModel.objects.get(pk=company_id)
            except CompanyModel.DoesNotExist:
                raise serializers.ValidationError({'company_id': 'Company not found.'})
        else:
            company = request.user.company

        modules = validated_data.get('modules', [])
        role    = validated_data.get('role', '')
        validated_data['department'] = ' + '.join(modules) if modules and role != 'Admin' else ''

        with transaction.atomic():
            # Lock the company row so concurrent user-creation requests for the
            # same company can't both read the same count and generate the same code.
            company.__class__.objects.select_for_update().get(pk=company.pk)

            count     = User.objects.filter(company=company).count()
            user_code = f"{prefix}{str(count + 1).zfill(3)}"
            while User.objects.filter(company=company, user_code=user_code).exists():
                count    += 1
                user_code = f"{prefix}{str(count + 1).zfill(3)}"

            user = User(company=company, user_code=user_code, **validated_data)
            if reporting_manager_id:
                user.reporting_manager_id = reporting_manager_id
            user.set_password(password)
            user.save()

        return user


class UserUpdateSerializer(serializers.ModelSerializer):
    password             = serializers.CharField(write_only=True, min_length=6, required=False, allow_blank=True)
    reporting_manager_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)

    class Meta:
        model  = User
        fields = ['name', 'email', 'phone', 'user_code', 'password', 'role', 'designation', 'modules', 'manager_modules', 'admin_modules', 'can_export_bookings', 'is_active', 'reporting_manager_id']

    def validate_email(self, value):
        # email is encrypted; uniqueness lives on the blind index.
        from sales.fields import text_blind_index
        if User.objects.filter(email_key=text_blind_index(value)).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError('A user with this email already exists.')
        return value

    def validate_user_code(self, value):
        value   = value.upper().strip()
        company = self.instance.company
        if User.objects.filter(company=company, user_code=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError('This user code is already taken.')
        return value

    def validate(self, attrs):
        # Whatever the row will look like once this payload lands, not what was sent.
        validate_reporting_manager(
            attrs.get('role', self.instance.role),
            attrs['reporting_manager_id'] if 'reporting_manager_id' in attrs
            else self.instance.reporting_manager_id,
            attrs.get('is_active', self.instance.is_active),
        )
        return attrs

    def update(self, instance, validated_data):
        if 'reporting_manager_id' in validated_data:
            instance.reporting_manager_id = validated_data.pop('reporting_manager_id')
        password = validated_data.pop('password', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        # Keep department in sync with the first module; admins have no department
        modules = instance.modules or []
        instance.department = ' + '.join(modules) if modules and instance.role != 'Admin' else ''
        instance.save()
        return instance
