"""Generate the Pratishtha 2 price books from each unit's areas.

Every Pratishtha 2 unit had price_book = {}, so the booking form fell through to a
rate-based branch that has no pratishtha formulas and priced bookings at zero. This
fills the books from the rates in sales/pricing/pratishtha2.py.

Areas are read off the Plot rows (`size`, `terrace_area`) — a unit with no area is
reported and skipped rather than written with a zero price.

    python manage.py load_pratishtha2_price_books --project "Pratishtha 2" --dry-run
    python manage.py load_pratishtha2_price_books --project "Pratishtha 2"
"""

import re

from django.core.management.base import BaseCommand

from sales.models import Project, Plot
from sales.pricing import pratishtha2


def _area(raw):
    """'60 sqyrd' / '60' / 60 -> 60.0. None when there is no number in there."""
    if raw is None:
        return None
    m = re.search(r'\d+(?:\.\d+)?', str(raw))
    return float(m.group()) if m else None


class Command(BaseCommand):
    help = "Fill Pratishtha 2 unit price books from their areas."

    def add_arguments(self, parser):
        parser.add_argument('--project', default='Pratishtha 2')
        parser.add_argument('--company', help='Company code, if the name is ambiguous.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change without writing.')
        parser.add_argument('--overwrite', action='store_true',
                            help='Also rewrite units that already have a price book.')

    def handle(self, *args, **o):
        qs = Project.objects.filter(name=o['project'])
        if o.get('company'):
            qs = qs.filter(company__code=o['company'])
        projects = list(qs)
        if not projects:
            self.stderr.write(self.style.ERROR('No project named %r.' % o['project']))
            return
        if len(projects) > 1:
            self.stderr.write(self.style.ERROR(
                'Several projects named %r — pass --company.' % o['project']))
            return
        project = projects[0]
        if project.formula_set != 'pratishtha':
            self.stderr.write(self.style.ERROR(
                "%s has formula_set=%r, not 'pratishtha'." % (project.name, project.formula_set)))
            return

        written = skipped_area = skipped_name = kept = 0
        no_area = []
        for plot in Plot.objects.filter(project=project).order_by('number'):
            if plot.price_book and not o['overwrite']:
                kept += 1
                continue
            if not pratishtha2.parse_unit(plot.number):
                skipped_name += 1
                no_area.append('%s (unrecognised number)' % plot.number)
                continue
            # `size` carries the area for both kinds — sq.yd for a flat, sq.ft for a
            # shop. Shops have no area fallback on purpose (see price_book_for).
            area = _area(plot.size)
            book = pratishtha2.price_book_for(
                plot.number,
                flat_area=area,
                terrace_area=_area(plot.terrace_area) or 0,
                sq_feet=area,
            )
            if not book:
                skipped_area += 1
                no_area.append(plot.number)
                continue
            if not o['dry_run']:
                plot.price_book = book
                plot.save(update_fields=['price_book'])
            written += 1

        verb = 'would write' if o['dry_run'] else 'wrote'
        self.stdout.write(self.style.SUCCESS(
            '%s %d price book(s) for %s.' % (verb.capitalize(), written, project.name)))
        if kept:
            self.stdout.write('  %d unit(s) already had a book (use --overwrite to redo).' % kept)
        if skipped_area or skipped_name:
            self.stdout.write(self.style.WARNING(
                '  %d unit(s) skipped for want of an area — they stay unbookable:'
                % (skipped_area + skipped_name)))
            for n in no_area[:20]:
                self.stdout.write('    %s' % n)
            if len(no_area) > 20:
                self.stdout.write('    … and %d more' % (len(no_area) - 20))
            self.stdout.write(
                '  Fill each unit\'s Area (and Terrace Area) on the plot, then re-run.')
