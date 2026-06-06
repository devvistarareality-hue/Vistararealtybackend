from django.utils import timezone
from rest_framework import serializers

from .models import Lead, LeadActivity, Project


class LeadActivitySerializer(serializers.ModelSerializer):
    time = serializers.SerializerMethodField()

    class Meta:
        model  = LeadActivity
        fields = ['id', 'type', 'note', 'time']

    def get_time(self, obj):
        return obj.created_at.strftime('%-d %b, %-I:%M %p')


class LeadSerializer(serializers.ModelSerializer):
    activities       = LeadActivitySerializer(many=True, read_only=True)
    project_name     = serializers.SerializerMethodField()
    assigned_to_name = serializers.SerializerMethodField()
    time_ago         = serializers.SerializerMethodField()
    created_at_str   = serializers.SerializerMethodField()

    class Meta:
        model  = Lead
        fields = [
            'id', 'name', 'phone', 'email',
            'project', 'project_name',
            'source', 'status',
            'assigned_to', 'assigned_to_name',
            'budget', 'notes', 'next_followup',
            'created_at', 'created_at_str', 'time_ago',
            'activities',
        ]
        read_only_fields = ['id', 'created_at']

    def get_project_name(self, obj):
        return obj.project.name if obj.project else ''

    def get_assigned_to_name(self, obj):
        return obj.assigned_to.name if obj.assigned_to else ''

    def get_time_ago(self, obj):
        diff  = timezone.now() - obj.created_at
        days  = diff.days
        hours = diff.seconds // 3600
        if days >= 7:
            return f'{days // 7}w ago'
        if days >= 1:
            return f'{days}d ago'
        if hours >= 1:
            return f'{hours}h ago'
        return 'Just now'

    def get_created_at_str(self, obj):
        return obj.created_at.strftime('%-d %b %Y')


class ProjectSerializer(serializers.ModelSerializer):
    lead_count = serializers.SerializerMethodField()

    class Meta:
        model  = Project
        fields = [
            'id', 'name', 'location', 'type', 'units',
            'price_range', 'status', 'description',
            'lead_count', 'created_at',
        ]
        read_only_fields = ['id', 'created_at']

    def get_lead_count(self, obj):
        if hasattr(obj, '_lead_count'):
            return obj._lead_count
        return obj.lead_count
