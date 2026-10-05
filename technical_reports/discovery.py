"""Recognize archived publications before requesting their originals."""
import re

from .store import canonical_url, report_id


def _paper(item):
    paper = item.get('paper_id') or ''
    if not paper:
        match = re.search(r'arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})',
                          item.get('report_url', '') + ' ' + item.get('url', ''))
        paper = match.group(1) if match else ''
    return re.sub(r'v\d+$', '', paper)


class KnownReports:
    def __init__(self, archive):
        self.ids = set(archive.get('aliases', {})) | set(archive.get('excluded_reports', {}))
        self.urls = {}
        records = list(archive.get('items', []))
        for field in ('merged_reports', 'excluded_reports'):
            records.extend(row for row in archive.get(field, {}).values() if isinstance(row, dict))
        for row in records:
            if not row.get('vendor') or not row.get('url'):
                continue
            self.ids.update((row.get('id', report_id(row)), report_id(row)))
            urls = [row.get(field) for field in ('url', 'report_url', 'canonical_url', 'original_url')]
            urls.extend(p.get('url') for p in row.get('related_publications', []))
            for url in filter(None, urls):
                self.urls.setdefault((row['vendor'], canonical_url(url)), set()).add(_paper(row))

    def __call__(self, item):
        if report_id(item) in self.ids:
            return True
        paper = _paper(item)
        for field in ('url', 'report_url', 'canonical_url', 'original_url'):
            url = item.get(field)
            if url:
                papers = self.urls.get((item['vendor'], canonical_url(url)), set())
                # A new paper can share an announcement with an archived one.
                if papers and (not paper or paper in papers or papers == {''}):
                    return True
        return False
