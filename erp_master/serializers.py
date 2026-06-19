from rest_framework import serializers
from .models import Vendor, Material, GLAccount, ERPProject, WBSActivity, DocumentTrail


class VendorSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Vendor
        fields = ['id', 'code', 'name', 'contact_name', 'phone', 'email',
                  'address', 'gstin', 'pan', 'payment_terms', 'is_active']


class GLAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model  = GLAccount
        fields = ['id', 'code', 'name', 'account_type', 'cost_center', 'is_active']


class MaterialSerializer(serializers.ModelSerializer):
    gl_account_name = serializers.CharField(source='gl_account.name', read_only=True, default='')

    class Meta:
        model  = Material
        fields = ['id', 'item_code', 'name', 'description', 'uom',
                  'category', 'gl_account', 'gl_account_name', 'is_active']


class ERPProjectSerializer(serializers.ModelSerializer):
    project_manager_name = serializers.CharField(source='project_manager.name', read_only=True, default='')

    class Meta:
        model  = ERPProject
        fields = ['id', 'code', 'name', 'client_name', 'location',
                  'start_date', 'end_date', 'project_manager', 'project_manager_name',
                  'status', 'is_active', 'created_at']
        read_only_fields = ['created_at']


class WBSActivitySerializer(serializers.ModelSerializer):
    project_name  = serializers.CharField(source='project.name', read_only=True)
    item_name     = serializers.CharField(source='item_code.name', read_only=True, default='')
    children      = serializers.SerializerMethodField()

    def get_children(self, obj):
        return WBSActivitySerializer(obj.children.filter(is_active=True), many=True).data

    class Meta:
        model  = WBSActivity
        fields = ['id', 'project', 'project_name', 'parent_activity', 'wbs_code',
                  'description', 'item_code', 'item_name', 'uom',
                  'budgeted_qty', 'unit_rate', 'budgeted_cost', 'is_active', 'children']
        read_only_fields = ['budgeted_cost']


class WBSActivityFlatSerializer(serializers.ModelSerializer):
    """Flat list version — no nested children, for dropdowns."""
    project_name = serializers.CharField(source='project.name', read_only=True)
    item_name    = serializers.CharField(source='item_code.name', read_only=True, default='')

    class Meta:
        model  = WBSActivity
        fields = ['id', 'project', 'project_name', 'wbs_code', 'description',
                  'item_code', 'item_name', 'uom', 'budgeted_qty', 'unit_rate', 'budgeted_cost']
        read_only_fields = ['budgeted_cost']


class DocumentTrailSerializer(serializers.ModelSerializer):
    class Meta:
        model  = DocumentTrail
        fields = ['id', 'doc_type', 'doc_no', 'ref_doc_type', 'ref_doc_no', 'created_at', 'notes']
        read_only_fields = ['created_at']
