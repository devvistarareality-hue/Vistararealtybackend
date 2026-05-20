from rest_framework import serializers
from .models import AttendanceRecord, LeaveBalance, LeaveApplication, LeaveTransaction


class AttendanceRecordSerializer(serializers.ModelSerializer):
    in_time  = serializers.SerializerMethodField()
    out_time = serializers.SerializerMethodField()
    total    = serializers.SerializerMethodField()
    day      = serializers.SerializerMethodField()

    class Meta:
        model  = AttendanceRecord
        fields = ['date', 'day', 'in_time', 'out_time', 'total']

    def _fmt_time(self, t):
        if t is None:
            return '00:00'
        return t.strftime('%H:%M')

    def _fmt_hours(self, hours):
        if not hours:
            return '00:00'
        h = int(hours)
        m = int((float(hours) - h) * 60)
        return f'{h:02d}:{m:02d}'

    def get_in_time(self, obj):
        return self._fmt_time(obj.in_time)

    def get_out_time(self, obj):
        return self._fmt_time(obj.out_time)

    def get_total(self, obj):
        return self._fmt_hours(obj.total_hours)

    def get_day(self, obj):
        return obj.date.strftime('%a')


class LeaveBalanceSerializer(serializers.ModelSerializer):
    class Meta:
        model  = LeaveBalance
        fields = ['available', 'utilised']


class LeaveTransactionSerializer(serializers.ModelSerializer):
    date        = serializers.SerializerMethodField()
    leave_date  = serializers.SerializerMethodField()
    leave_type  = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    change      = serializers.FloatField()
    balance     = serializers.FloatField()

    class Meta:
        model  = LeaveTransaction
        fields = ['id', 'date', 'leave_date', 'leave_type', 'description', 'change', 'balance']

    def get_date(self, obj):
        return obj.date.strftime('%-d %b %Y %-I:%M %p')

    def get_leave_date(self, obj):
        return obj.leave_date.strftime('%-d %b %Y') if obj.leave_date else None

    def get_leave_type(self, obj):
        return obj.get_leave_type_display()

    def get_description(self, obj):
        return obj.get_description_display()


class LeaveApplicationSerializer(serializers.ModelSerializer):
    class Meta:
        model  = LeaveApplication
        fields = ['id', 'work_type', 'leave_type', 'day_type', 'session',
                  'from_date', 'to_date', 'description', 'status', 'applied_on']
        read_only_fields = ['id', 'status', 'applied_on']

    def validate(self, data):
        if data.get('day_type') == 'half_day' and not data.get('session'):
            raise serializers.ValidationError({'session': 'Session is required for Half Day leave.'})
        if data.get('day_type') == 'full_day' and not data.get('to_date'):
            raise serializers.ValidationError({'to_date': 'End date is required for Full Day leave.'})
        if data.get('to_date') and data['to_date'] < data['from_date']:
            raise serializers.ValidationError({'to_date': 'End date cannot be before start date.'})
        return data
