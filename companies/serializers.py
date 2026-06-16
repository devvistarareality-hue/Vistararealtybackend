from rest_framework import serializers
from .models import Company


class CompanyVerifySerializer(serializers.Serializer):
    company_code = serializers.CharField(max_length=20)


class CompanySerializer(serializers.ModelSerializer):
    class Meta:
        model  = Company
        fields = ['code', 'name', 'logo_url']


class CompanyAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Company
        fields = ['id', 'code', 'name', 'email', 'phone', 'is_active', 'created_at']


class CompanyCodeUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Company
        fields = ['code', 'name']

    def validate_code(self, value):
        value = value.upper().strip()
        if Company.objects.filter(code=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError('This company code is already taken.')
        return value


class CompanyCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Company
        fields = ['code', 'name', 'email', 'phone']

    def validate_code(self, value):
        value = value.upper().strip()
        if Company.objects.filter(code=value).exists():
            raise serializers.ValidationError('This company code is already taken.')
        return value
