from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from companies.models import Company


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError('Email is required')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault('is_staff', True)
        extra.setdefault('is_superuser', True)
        return self.create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    company      = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='users', null=True, blank=True)
    user_code    = models.CharField(max_length=20, blank=True)
    name         = models.CharField(max_length=100, blank=True)
    email        = models.EmailField(unique=True)
    phone        = models.CharField(max_length=20, blank=True)
    role         = models.CharField(max_length=100, blank=True)
    department   = models.CharField(max_length=100, blank=True)
    designation  = models.CharField(max_length=100, blank=True)
    avatar_url      = models.URLField(blank=True)
    modules         = models.JSONField(default=list, blank=True)
    manager_modules = models.JSONField(default=list, blank=True)
    is_active    = models.BooleanField(default=True)
    is_staff     = models.BooleanField(default=False)
    date_joined  = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD  = 'email'
    REQUIRED_FIELDS = []

    class Meta:
        unique_together = ('company', 'user_code')

    def __str__(self):
        return f'{self.user_code} ({self.company.code if self.company else "admin"})'
