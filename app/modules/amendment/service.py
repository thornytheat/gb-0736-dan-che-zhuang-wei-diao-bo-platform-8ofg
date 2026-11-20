from app.errors import DomainError
from app.modules.amendment.receipt import SUPPLEMENT_SOURCE, verify_receipt
from app.modules.archive.pipeline import run_shift
from app.modules.capacity.versions import parse_rows
from app.shift import load_shift


def _amended(store, run_id):
    snapshot = store.load(run_id)
    return snapshot if SUPPLEMENT_SOURCE in snapshot['manifest']['inputs'] else None


def amend(store, expected, raw):
    """提交容量补报并生成修订班次与修订回执；同一补报重试不新增修订。"""
    before = store.load(expected)
    if SUPPLEMENT_SOURCE in before['manifest']['inputs']:
        raise DomainError('AMENDMENT_LIMIT', 409, run=expected)
    sources = store.sources(expected)
    shift = load_shift(sources['shift'])
    # 先独立解析补报表：全部隔离或零有效行时直接拒绝，不产生半成品归档。
    survivors, excluded, count, encoding = parse_rows(raw, shift, SUPPLEMENT_SOURCE)
    if not any(rows for rows in survivors.values()):
        raise DomainError('SUPPLEMENT_NO_EFFECT', excluded=excluded, input_rows=count)
    # 跨源同修订号预检：撞号时给出明确原因，避免在合并切段时退化成笼统的容量缺口。
    original_keys = {(row['station'], row['revision'])
                     for row in before['capacity']['segments']}
    supplement_keys = {(station, row['revision']) for station, rows in survivors.items() for row in rows}
    collisions = sorted(original_keys & supplement_keys)
    if collisions:
        raise DomainError('SUPPLEMENT_REVISION_COLLISION',
                         collisions=[{'station': station, 'revision': revision}
                                     for station, revision in collisions])
    revised_sources = {**sources, SUPPLEMENT_SOURCE: raw}
    snapshot = run_shift(revised_sources, before['dispatch']['legs'], base_snapshot=before)
    created = store.publish(snapshot, revised_sources, expected)
    new_id = snapshot['manifest']['id']
    return {'id': new_id, 'base_run_id': expected, 'created': created,
            'changed': new_id != expected,
            'supplement_excluded': [row for row in snapshot['capacity']['excluded']
                                    if row['source'] == SUPPLEMENT_SOURCE],
            'receipt': snapshot['amendment']}


def list_amendments(store, run_id=None):
    """列出班次链上的补报修订回执；可按 run 限定。"""
    entries = []
    for record in store.history():
        target = record['id']
        if run_id is not None and target != run_id:
            continue
        snapshot = _amended(store, target)
        if snapshot is None:
            continue
        receipt = snapshot['amendment']
        entries.append({
            'id': receipt['id'], 'run_id': target, 'base_run_id': receipt['base_run_id'],
            'generated_at': record['generated_at'], 'active': bool(record['active']),
            'effective_from': receipt['effective_from'], 'effective_to': receipt['effective_to'],
            'delta': receipt['judgement']['delta'],
            'changes': len(receipt['judgement']['changes']),
            'supplement_sha256': receipt['supplement']['sha256'],
        })
    return entries


def receipt_for(store, run_id):
    snapshot = store.load(run_id)
    if SUPPLEMENT_SOURCE not in snapshot['manifest']['inputs']:
        raise DomainError('AMENDMENT_NOT_FOUND', 404, run=run_id)
    verification = verify_receipt(store, snapshot)
    return {'receipt': snapshot['amendment'], 'verification': verification}


def supplement_source(store, run_id):
    snapshot = store.load(run_id)
    if SUPPLEMENT_SOURCE not in snapshot['manifest']['inputs']:
        raise DomainError('AMENDMENT_NOT_FOUND', 404, run=run_id)
    return store.sources(run_id)[SUPPLEMENT_SOURCE], snapshot['amendment']['supplement']['encoding']
