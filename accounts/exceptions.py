from django.core.exceptions import RequestDataTooBig
from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.response import Response
from rest_framework import status


def exception_handler(exc, context):
    """Wraps DRF's default handler to turn RequestDataTooBig (raised when a
    request body exceeds DATA_UPLOAD_MAX_MEMORY_SIZE) into a clean JSON error
    instead of a bare Django error page."""
    if isinstance(exc, RequestDataTooBig):
        return Response(
            {'detail': 'File too large.'},
            status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
    return drf_exception_handler(exc, context)
