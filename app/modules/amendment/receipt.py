from app.manifest import canonical, digest
from app.modules.capacity.service import SUPPLEMENT_SOURCE
from app.modules.dispatch.compare import compare


def _old_slots(before, station, at):
    for segment in before['capacity']['segments']:
        if segment['station'] == station and segment['start'] <= at < segment['end']:
            return segment['slots']
    return None


def windows(before, after):
    """补报实际影响班次的区间：仅取合并视图中来自补报表的生效段，并对照原单容量。"""
    result = []
    for segment in after['capacity']['segments']:
        if segment['source'] != SUPPLEMENT_SOURCE:
            continue
        result.append({
            'station': segment['station'], 'start': segment['start'], 'end': segment['end'],
            'revision': segment['revision'], 'slots_after': segment['slots'],
            'slots_before': _old_slots(before, segment['station'], segment['start']),
            'line': segment['line'], 'note': segment['note'],
        })
    return sorted(result, key=lambda row: (row['start'], row['station'], row['line']))


class _ShiftView:
    """compare 只遍历 shift.stations 的顺序，用轻量视图避免依赖完整 Shift 对象。"""

    def __init__(self, snapshot):
        self.stations = tuple(snapshot['shift']['stations'])


def _inventory_changes(before, after):
    old = {row['station']: row for row in before['inventory']['plan']['stations']}
    rows = []
    for current in after['inventory']['plan']['stations']:
        previous = old[current['station']]
        rows.append({
            'station': current['station'],
            'opening': current['opening'],
            'closing_before': previous['closing'], 'closing_after': current['closing'],
            'docked_before': previous['docked'], 'docked_after': current['docked'],
            'held_before': previous['held'], 'held_after': current['held'],
            'rent_before': previous['rent'], 'rent_after': current['rent'],
            'return_before': previous['return'], 'return_after': current['return'],
            'gap_after': current['gap'],
        })
    return rows


def _attempts(snapshot):
    return {row['id']: row for row in snapshot['replay']['plan']['timeline']
            if row['kind'] in {'rent', 'return'}}


def _capacity_lines(snapshot):
    return {(row['source'], row['line']) for row in snapshot['capacity']['segments']}


def _timeline_capacity_lines(snapshot):
    return {(row['source'], row['line'])
            for row in snapshot['replay']['plan']['timeline'] if row['kind'] == 'capacity'}


def _decode_text(raw, encoding):
    codec = 'utf-8-sig' if encoding == 'UTF-8 BOM' else encoding.lower()
    return raw.decode(codec)


def build_receipt(before, after, supplement_raw, encoding):
    """由原班次快照与修订快照确定性派生容量补报修订回执（不含任何当前时点字段）。"""
    impact = windows(before, after)
    judgement = compare(_ShiftView(before), before['replay']['plan'], after['replay']['plan'])
    plan_after = after['inventory']['plan']
    before_attempts, after_attempts = _attempts(before), _attempts(after)
    body = {
        'base_run_id': before['manifest']['id'],
        'revised_run_id': after['manifest']['id'],
        'supplement': {
            'sha256': digest(supplement_raw), 'bytes': len(supplement_raw),
            'encoding': encoding,
            'raw_text': _decode_text(supplement_raw, encoding),
        },
        'effective_from': min((row['start'] for row in impact), default=None),
        'effective_to': max((row['end'] for row in impact), default=None),
        'windows': impact,
        'judgement': judgement,
        'inventory_changes': _inventory_changes(before, after),
        'fleet': {
            'start_fleet': plan_after['start_fleet'],
            'end_fleet_before': before['inventory']['plan']['end_fleet'],
            'end_fleet_after': plan_after['end_fleet'],
            'road_end_after': plan_after['road_end'],
            'transit_end_after': plan_after['in_transit_end'],
        },
        'checks': [
            {'code': 'SUPPLEMENT_DIGEST', 'pass': digest(supplement_raw)
             == after['manifest']['inputs'][SUPPLEMENT_SOURCE]['sha256']},
            {'code': 'FLEET_CONSERVATION', 'pass': plan_after['ok']
             and plan_after['end_fleet'] == plan_after['start_fleet']
             and plan_after['end_fleet'] == before['inventory']['plan']['start_fleet']},
            {'code': 'SINGLE_CAPACITY_VIEW',
             'pass': _timeline_capacity_lines(after) <= _capacity_lines(after)},
            {'code': 'ATTEMPT_ALIGNMENT',
             'pass': set(before_attempts) == set(after_attempts)},
        ],
    }
    body['id'] = digest(canonical(body).encode())
    return body


def verify_receipt(store, snapshot):
    """基于封存原文重新核对回执，返回逐项通过情况与总判定，任何一项失败都不得放行。"""
    receipt = snapshot['amendment']
    raw = store.sources(snapshot['manifest']['id'])[SUPPLEMENT_SOURCE]
    fresh_digest = digest(raw)
    identity = {'inputs': snapshot['manifest']['inputs'],
                'rule_version': snapshot['manifest']['rule_version'], 'plan': snapshot['manifest']['plan']}
    before = store.load(receipt['base_run_id'])
    before_attempts, after_attempts = _attempts(before), _attempts(snapshot)
    change_consistent = all(
        before_attempts[row['id']]['code'] == row['before']
        and after_attempts[row['id']]['code'] == row['after']
        for row in receipt['judgement']['changes'])
    stored = {check['code']: check['pass'] for check in receipt['checks']}
    checks = [
        {'code': 'SUPPLEMENT_DIGEST', 'pass': fresh_digest == receipt['supplement']['sha256']
         and fresh_digest == snapshot['manifest']['inputs'][SUPPLEMENT_SOURCE]['sha256']},
        {'code': 'RUN_IDENTITY', 'pass': digest(canonical(identity).encode()) == snapshot['manifest']['id']},
        {'code': 'FLEET_CONSERVATION', 'pass': stored['FLEET_CONSERVATION']},
        {'code': 'SINGLE_CAPACITY_VIEW', 'pass': stored['SINGLE_CAPACITY_VIEW']
         and _timeline_capacity_lines(snapshot) <= _capacity_lines(snapshot)},
        {'code': 'ATTEMPT_ALIGNMENT', 'pass': set(before_attempts) == set(after_attempts)
         and change_consistent},
        {'code': 'RECEIPT_DETERMINISTIC', 'pass': receipt['id'] == digest(
            canonical({key: value for key, value in receipt.items() if key != 'id'}).encode())},
    ]
    return {'ok': all(check['pass'] for check in checks), 'checks': checks,
            'receipt_sha256': receipt['id']}
