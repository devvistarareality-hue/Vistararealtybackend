from django.apps import AppConfig


class ActivityConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'activity'

    def ready(self):
        # Compare every save made during a logged API write with the stored row,
        # so the log shows what actually changed (see activity/changes.py).
        from django.db.models.signals import pre_save, post_save, post_delete
        from . import changes
        pre_save.connect(changes.on_pre_save, dispatch_uid='activity_pre_save')
        post_save.connect(changes.on_post_save, dispatch_uid='activity_post_save')
        post_delete.connect(changes.on_post_delete, dispatch_uid='activity_post_delete')
