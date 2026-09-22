from django import template

register = template.Library()


@register.filter
def inr(value):
    """12345678 → '₹ 1,23,45,678' (Indian grouping); negatives as '−₹ …'."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return value
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ','.join(groups) + ',' + tail
    return ('−₹ ' if n < 0 else '₹ ') + s
