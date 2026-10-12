"""The hook every module's list screen uses to serve Ask Nexora (sales/assistant.py).

The assistant reads data by calling a module's real list view in-process, as the
person asking. A view calls `ai_capture(request, qs=...)` (or `rows=...` when its
list is computed rather than a queryset) right after it has applied its own
company / role / team scoping; the assistant gets exactly what that person's screen
would show, with no second copy of the rules to drift. Only an in-process request
carries the marker — a real HTTP request never does, so it is a no-op for them.
"""


def ai_capture(request, qs=None, rows=None):
    box = getattr(getattr(request, '_request', request), '_ai_capture', None)
    if box is None:
        return False
    if rows is not None:
        box['rows'] = rows
    else:
        box['qs'] = qs
    return True
