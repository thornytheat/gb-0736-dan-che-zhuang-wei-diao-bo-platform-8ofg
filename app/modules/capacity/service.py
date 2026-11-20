from app.clock import stamp
from app.errors import DomainError
from .versions import merged_sources, revisions

SUPPLEMENT_SOURCE = 'capacity_supplement'


def build_segments(shift, versions):
    """从候选容量记录切出每个站点的生效区间，缺失有效版本时报 CAPACITY_GAP。"""
    segments = []
    for station in shift.stations:
        candidates = versions[station]
        edges = {shift.start, shift.end}
        for row in candidates:
            edges.update((max(row['start'], shift.start), min(row['end'], shift.end)))
        edges = sorted(edges)
        for start, end in zip(edges, edges[1:]):
            active = [row for row in candidates if row['start'] <= start < row['end']]
            if not active:
                raise DomainError('CAPACITY_GAP', station=station, at=stamp(start))
            chosen = max(active, key=lambda row: row['revision'])
            if segments and segments[-1]['line'] == chosen['line'] \
                    and segments[-1]['source'] == chosen['source'] \
                    and segments[-1]['station'] == station:
                segments[-1]['end'] = stamp(end)
                continue
            segments.append({'station': station, 'start': stamp(start), 'end': stamp(end),
                             **{key: chosen[key] for key in
                                ('revision', 'slots', 'line', 'note', 'raw', 'source')}})
    return segments


def capacity(raw, shift):
    versions, excluded, count, encoding = revisions(raw, shift)
    return {'segments': build_segments(shift, versions), 'input_rows': count, 'excluded': excluded,
            'accepted': sum(map(len, versions.values())), 'encoding': encoding,
            'sources': {'capacity': {'input_rows': count, 'accepted': sum(map(len, versions.values())),
                                     'excluded': len(excluded), 'encoding': encoding}}}


def merged_capacity(raw_sources, shift):
    """以单一合并容量视图生成生效区间，保证下游容量与库存不按新旧单混用。"""
    versions, excluded, totals, encodings = merged_sources(raw_sources, shift)
    segments = build_segments(shift, versions)
    per_source = {}
    for source, count in totals.items():
        source_excluded = [row for row in excluded if row['source'] == source]
        per_source[source] = {
            'input_rows': count,
            'accepted': count - len(source_excluded),
            'excluded': len(source_excluded),
            'encoding': encodings[source],
        }
    return {'segments': segments, 'input_rows': sum(totals.values()), 'excluded': excluded,
            'accepted': sum(item['accepted'] for item in per_source.values()),
            'encoding': '+'.join(encodings[source] for source, _ in raw_sources),
            'sources': per_source}
