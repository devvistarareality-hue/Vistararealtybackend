from rest_framework import serializers
from .models import User


class LoginSerializer(serializers.Serializer):
    company_code = serializers.CharField(max_length=20)
    user_code    = serializers.CharField(max_length=20)
    password     = serializers.CharField(write_only=True)


class UserSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source='company.code', read_only=True)
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model  = User
        fields = [
            'id', 'user_code', 'name', 'email', 'phone',
            'role', 'department', 'designation', 'avatar_url',
            'modules', 'manager_modules',
            'company_code', 'company_name',
        ]


class UserListSerializer(serializers.ModelSerializer):
    module_count = serializers.SerializerMethodField()
    is_manager   = serializers.SerializerMethodField()

    def get_module_count(self, obj):
        return len(obj.modules) if obj.modules else 0

    def get_is_manager(self, obj):
        return bool(obj.manager_modules)

    class Meta:
        model  = User
        fields = [
            'id', 'user_code', 'name', 'email', 'role',
            'modules', 'manager_modules', 'module_count', 'is_manager', 'is_active',
        ]


class UserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=6)

    class Meta:
        model  = User
        fields = ['name', 'email', 'password', 'role', 'modules', 'manager_modules']

    def create(self, validated_data):
        company  = self.context['request'].user.company
        password = validated_data.pop('password')

        count     = User.objects.filter(company=company).count()
        user_code = f"USR{str(count + 1).zfill(3)}"
        while User.objects.filter(company=company, user_code=user_code).exists():
            count    += 1
            user_code = f"USR{str(count + 1).zfill(3)}"

        user = User(company=company, user_code=user_code, **validated_data)
        user.set_password(password)
        user.save()
        return user


class UserUpdateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=6, required=False, allow_blank=True)

    class Meta:
        model  = User
        fields = ['name', 'email', 'user_code', 'password', 'role', 'modules', 'manager_modules', 'is_active']

    def validate_email(self, value):
        if User.objects.filter(email=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError('A user with this email already exists.')
        return value

    def validate_user_code(self, value):
        value = value.upper().strip()
        company = self.instance.company
        if User.objects.filter(company=company, user_code=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError('This user code is already taken.')
        return value

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance
