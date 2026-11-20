import re
from collections import defaultdict
from itertools import combinations

from app.clock import instant
from app.errors import DomainError
from app.modules.intake.reader import records

FIELDS = ('station', 'revision', 'start', 'end', 'slots', 'note')


def parse_rows(raw, shift, source):
    """解析单个容量来源，返回按 (站, 修订号) 分组的候选与隔离行。

    同一来源内同站同修订的重叠区间整体隔离；来源之间不做重叠隔离，
    统一交给修订号高低裁决，使原始文件与补报文件都能独立保留、独立勾稽。
    """
    rows, encoding = records(raw, FIELDS)
    valid, excluded = defaultdict(list), []

    def exclude(row, code):
        excluded.append({'line': row['line'], 'raw': row['raw'], 'code': code, 'source': source})

    for row in rows:
        if row['code']:
            exclude(row, row['code'])
            continue
        fields = row['cells']
        try:
            if fields['station'] not in shift.stations:
                raise DomainError('UNKNOWN_STATION')
            if not all(re.fullmatch(r'[0-9]{1,3}', fields[key]) for key in ('slots', 'revision')):
                raise DomainError('BAD_CAPACITY')
            start, end = instant(fields['start']), instant(fields['end'])
            if end <= start:
                raise DomainError('CAPACITY_INTERVAL')
            if start >= shift.end or end <= shift.start:
                raise DomainError('OUTSIDE_SHIFT')
            rev = int(fields['revision'])
            valid[fields['station'], rev].append(dict(
                row, start=start, end=end, revision=rev, slots=int(fields['slots']),
                note=fields['note'], source=source))
        except DomainError as error:
            exclude(row, error.code)
    survivors = defaultdict(list)
    for (station, _), group in valid.items():
        conflict = set()
        for left, right in combinations(group, 2):
            if left['end'] > right['start'] and left['start'] < right['end']:
                conflict.update((left['line'], right['line']))
        for row in group:
            if row['line'] in conflict:
                exclude(row, 'CAPACITY_CONFLICT')
            else:
                survivors[station].append(row)
    return survivors, sorted(excluded, key=lambda row: (row['source'], row['line'])), len(rows), encoding


def revisions(raw, shift):
    return parse_rows(raw, shift, 'capacity')


def merged_sources(raw_sources, shift):
    """解析原始容量表与补报表，合并候选并检查跨源同修订号冲突。"""
    combined, excluded, totals, encodings = defaultdict(list), [], {}, {}
    for source, raw in raw_sources:
        survivors, source_excluded, count, encoding = parse_rows(raw, shift, source)
        totals[source] = count
        encodings[source] = encoding
        excluded.extend(source_excluded)
        for station, rows in survivors.items():
            combined[station].extend(rows)
    for station in shift.stations:
        by_revision = defaultdict(list)
        for row in combined[station]:
            by_revision[row['revision']].append(row)
        for revision, group in by_revision.items():
            sources = {row['source'] for row in group}
            if len(sources) > 1:
                for row in group:
                    combined[station].remove(row)
                    excluded.append({'line': row['line'], 'raw': row['raw'],
                                     'code': 'SUPPLEMENT_REVISION_COLLISION', 'source': row['source']})
    return combined, sorted(excluded, key=lambda row: (row['source'], row['line'])), totals, encodings
