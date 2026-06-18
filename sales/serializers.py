from rest_framework import serializers
from .models import LeadSource, Project, Plot, Lead, FollowUp, SiteVisit, Closure, LeadStatusHistory


class LeadSourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeadSource
        fields = ['id', 'name', 'is_active', 'created_at']


class ProjectSerializer(serializers.ModelSerializer):
    lead_count = serializers.IntegerField(read_only=True, default=0)
    plot_counts = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            'id', 'name', 'description', 'location', 'project_type', 'is_active',
            'tagline', 'rera', 'total_area', 'total_plots', 'price_range', 'possession',
            'cover_image_url', 'master_plan_url', 'site_map_image_url', 'site_map_zones',
            'lead_count', 'plot_counts', 'created_at', 'updated_at',
        ]

    def get_plot_counts(self, obj):
        plots = obj.plots.all()
        return {
            'total':     plots.count(),
            'available': plots.filter(status='available').count(),
            'hold':      plots.filter(status='hold').count(),
            'sold':      plots.filter(status='sold').count(),
        }


class PlotSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plot
        fields = ['id', 'project', 'number', 'status', 'size', 'cluster_type', 'facing', 'price', 'notes']
        read_only_fields = ['id', 'project']


class LeadUserSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    user_code = serializers.CharField()


class LeadListSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True, default=None)
    source_name = serializers.CharField(source='source.name', read_only=True, default=None)
    telecaller_name = serializers.CharField(source='telecaller.name', read_only=True, default=None)
    stm_name = serializers.CharField(source='stm.name', read_only=True, default=None)

    class Meta:
        model = Lead
        fields = [
            'id', 'name', 'phone', 'alt_phone', 'email',
            'project', 'project_name', 'source', 'source_name',
            'status', 'telecaller', 'telecaller_name', 'telecaller_status',
            'stm', 'stm_name', 'stm_status',
            'is_duplicate', 'created_at', 'updated_at',
        ]


class LeadDetailSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True, default=None)
    source_name = serializers.CharField(source='source.name', read_only=True, default=None)
    telecaller_name = serializers.CharField(source='telecaller.name', read_only=True, default=None)
    stm_name = serializers.CharField(source='stm.name', read_only=True, default=None)

    class Meta:
        model = Lead
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class LeadCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Lead
        fields = [
            'name', 'phone', 'alt_phone', 'email',
            'project', 'source',
            'meta_campaign_name', 'meta_adset_name', 'meta_ad_name',
            'status',
        ]

    def validate_phone(self, value):
        return value.strip()


class LeadUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Lead
        fields = [
            'name', 'phone', 'alt_phone', 'email',
            'project', 'source', 'status',
            'telecaller', 'telecaller_status', 'telecaller_remarks', 'telecaller_assigned_at',
            'stm', 'stm_status', 'stm_remarks', 'stm_assigned_at',
            'budget_min', 'budget_max', 'requirement', 'preferred_location',
        ]


class FollowUpSerializer(serializers.ModelSerializer):
    lead_name = serializers.CharField(source='lead.name', read_only=True)
    assigned_to_name = serializers.CharField(source='assigned_to.name', read_only=True)

    class Meta:
        model = FollowUp
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class SiteVisitSerializer(serializers.ModelSerializer):
    lead_name = serializers.CharField(source='lead.name', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True, default=None)
    stm_name = serializers.CharField(source='stm.name', read_only=True, default=None)

    class Meta:
        model = SiteVisit
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class ClosureSerializer(serializers.ModelSerializer):
    lead_name = serializers.CharField(source='lead.name', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True, default=None)
    stm_name = serializers.CharField(source='stm.name', read_only=True, default=None)

    class Meta:
        model = Closure
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class LeadStatusHistorySerializer(serializers.ModelSerializer):
    changed_by_name = serializers.CharField(source='changed_by.name', read_only=True, default=None)

    class Meta:
        model = LeadStatusHistory
        fields = ['id', 'field_changed', 'old_value', 'new_value', 'remarks', 'changed_by_name', 'created_at']
