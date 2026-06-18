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


class UserSerializer(serializers.ModelSerializer):
    company_code      = serializers.SerializerMethodField()
    company_name      = serializers.SerializerMethodField()
    reporting_manager = ReportingManagerSerializer(read_only=True)

    def get_company_code(self, obj):
        return obj.company.code if obj.company else ''

    def get_company_name(self, obj):
        return obj.company.name if obj.company else ''

    class Meta:
        model  = User
        fields = [
            'id', 'user_code', 'name', 'email', 'phone',
            'role', 'department', 'designation', 'avatar_url',
            'modules', 'manager_modules',
            'company_code', 'company_name', 'is_staff',
            'reporting_manager',
        ]


class DesignationSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Designation
        fields = ['id', 'name', 'module']


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
            'id', 'user_code', 'name', 'email', 'role', 'designation',
            'modules', 'manager_modules', 'module_count', 'is_manager', 'is_active',
            'company_code', 'company_name', 'reporting_manager',
        ]


class UserCreateSerializer(serializers.ModelSerializer):
    password              = serializers.CharField(write_only=True, min_length=6)
    user_code_prefix      = serializers.CharField(write_only=True, required=False, max_length=10, default='USR')
    company_id            = serializers.IntegerField(write_only=True, required=False)
    reporting_manager_id  = serializers.IntegerField(write_only=True, required=False, allow_null=True)

    class Meta:
        model  = User
        fields = ['name', 'email', 'password', 'role', 'designation', 'modules', 'manager_modules', 'user_code_prefix', 'company_id', 'reporting_manager_id']

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
        fields = ['name', 'email', 'user_code', 'password', 'role', 'designation', 'modules', 'manager_modules', 'is_active', 'reporting_manager_id']

    def validate_email(self, value):
        if User.objects.filter(email=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError('A user with this email already exists.')
        return value

    def validate_user_code(self, value):
        value   = value.upper().strip()
        company = self.instance.company
        if User.objects.filter(company=company, user_code=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError('This user code is already taken.')
        return value

    def update(self, instance, validated_data):
        if 'reporting_manager_id' in validated_data:
            instance.reporting_manager_id = validated_data.pop('reporting_manager_id')
        password = validated_data.pop('password', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance
