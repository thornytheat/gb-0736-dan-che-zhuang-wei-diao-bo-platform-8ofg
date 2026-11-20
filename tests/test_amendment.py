import io
import json
from concurrent.futures import ThreadPoolExecutor

from app.manifest import canonical, digest
from app.modules.amendment.receipt import SUPPLEMENT_SOURCE

SUPPLEMENT = ('station,revision,start,end,slots,note\n'
              '004,5,2026-11-20T08:35:00+08:00,2026-11-20T09:10:00+08:00,0,交班后补报临时关站\n').encode()


def submit(client, run, raw=SUPPLEMENT):
    return client.post('/api/amendments',
                       data={'expected': run, 'supplement': (io.BytesIO(raw), 'supplement.csv')},
                       content_type='multipart/form-data')


def test_amendment_publishes_receipt_and_keeps_original_run(client, run_id):
    response = submit(client, run_id)
    assert response.status_code == 200
    result = response.json
    revised = result['id']
    assert result['created'] is True and result['changed'] is True and revised != run_id
    receipt = result['receipt']
    assert receipt['base_run_id'] == run_id and receipt['revised_run_id'] == revised
    # 补报从何时影响班次
    assert (receipt['effective_from'], receipt['effective_to']) == \
           ('2026-11-20T00:35:00Z', '2026-11-20T01:10:00Z')
    assert [(row['station'], row['slots_before'], row['slots_after']) for row in receipt['windows']] == \
           [('004', 6, 0)]
    # 哪些借还尝试需要改判
    assert [(row['id'], row['before'], row['after']) for row in receipt['judgement']['changes']] == [
        ('0028', None, 'STATION_CLOSED'), ('0029', None, 'STATION_CLOSED'),
        ('0030', None, 'STATION_CLOSED'), ('0031', None, 'STATION_CLOSED'),
        ('0032', None, 'STATION_CLOSED')]
    assert (receipt['judgement']['delta'], receipt['judgement']['newly_missed']) == (5, 5)
    # 原发布结论完整保留，且不含修订内容
    original = client.get(f'/api/runs/{run_id}/evidence.json').json['evidence']
    assert original['manifest']['id'] == run_id and 'amendment' not in original
    assert SUPPLEMENT_SOURCE not in original['manifest']['inputs']
    # 原容量原文仍逐字封存
    assert client.get('/api/runs').json['history'] and len(
        client.get('/api/runs').json['history']) == 2


def test_supplement_raw_text_is_embedded_and_downloadable(client, run_id):
    revised = submit(client, run_id).json['id']
    receipt = client.get(f'/api/amendments/{revised}/receipt').json['receipt']
    assert receipt['supplement']['sha256'] == digest(SUPPLEMENT)
    assert receipt['supplement']['raw_text'].encode() == SUPPLEMENT
    download = client.get(f'/api/amendments/{revised}/supplement.csv')
    assert download.status_code == 200 and download.data == SUPPLEMENT


def test_retry_same_supplement_creates_no_new_revision(client, run_id):
    first = submit(client, run_id).json
    second = submit(client, run_id).json
    assert first['id'] == second['id']
    assert (first['created'], second['created']) == (True, False)
    assert len(client.get('/api/runs').json['history']) == 2
    assert len(client.get('/api/amendments').json['amendments']) == 1


def test_receipt_verifies_and_detects_tampering(client, run_id, app):
    revised = submit(client, run_id).json['id']
    verification = client.get(f'/api/amendments/{revised}/receipt').json['verification']
    assert verification['ok'] is True and len(verification['checks']) == 6
    assert all(check['pass'] for check in verification['checks'])

    db = app.extensions['shift_store']
    with db.connect() as conn:
        payload = json.loads(conn.execute('SELECT payload FROM runs WHERE id=?', (revised,)).fetchone()[0])
        payload['amendment']['judgement']['delta'] = 999
        conn.execute('UPDATE runs SET payload=? WHERE id=?', (canonical(payload), revised))
    failed = client.get(f'/api/amendments/{revised}/receipt').json['verification']
    assert failed['ok'] is False
    assert [check['code'] for check in failed['checks'] if not check['pass']] == ['RECEIPT_DETERMINISTIC']

    with db.connect() as conn:
        conn.execute('UPDATE sources SET raw=? WHERE run_id=? AND name=?',
                     (b'tampered', revised, SUPPLEMENT_SOURCE))
    failed = client.get(f'/api/amendments/{revised}/receipt').json['verification']
    assert failed['ok'] is False
    assert {check['code'] for check in failed['checks'] if not check['pass']} == \
           {'SUPPLEMENT_DIGEST', 'RECEIPT_DETERMINISTIC'}


def test_fleet_total_stays_balanced_under_single_capacity_view(client, run_id):
    revised = submit(client, run_id).json['id']
    receipt = client.get(f'/api/amendments/{revised}/receipt').json['receipt']
    fleet = receipt['fleet']
    assert fleet['start_fleet'] == fleet['end_fleet_before'] == fleet['end_fleet_after'] == 33
    # 时间线上所有容量切换都能在合并容量视图中溯源，不存在半旧半新的容量
    snapshot = client.get(f'/api/replay?run={revised}').json['data']
    view = client.get(f'/api/capacity?run={revised}').json['data']
    view_keys = {(row['source'], row['line']) for row in view['segments']}
    timeline_keys = {(row['source'], row['line']) for row in snapshot['plan']['timeline']
                     if row['kind'] == 'capacity'}
    assert timeline_keys <= view_keys
    # 勾稽场景全部相符，全网车辆总量守恒
    audit = client.get(f'/api/inventory?run={revised}').json['data']['plan']
    assert audit['ok'] is True and audit['fleet_gap'] == 0
    # 受影响站库存变化单列，未受影响站不变
    changes = {row['station']: row for row in receipt['inventory_changes']}
    assert (changes['004']['closing_before'], changes['004']['closing_after']) == (4, 1)
    assert changes['001']['closing_before'] == changes['001']['closing_after']
    assert all(row['gap_after'] == 0 for row in receipt['inventory_changes'])


def test_amended_run_rerun_is_idempotent_and_evidence_stable(client, run_id):
    revised = submit(client, run_id).json['id']
    export = f'/api/runs/{revised}/evidence.json'
    original = client.get(export).data
    for _ in range(2):
        response = client.post('/api/runs/rerun', json={'expected': revised})
        assert response.json == {'id': revised, 'created': False, 'changed': False}
    assert client.get(export).data == original
    assert client.get(f'/api/amendments/{revised}/receipt').json['verification']['ok'] is True


def test_second_amendment_on_amended_run_rejected(client, run_id):
    revised = submit(client, run_id).json['id']
    response = submit(client, revised)
    assert response.status_code == 409 and response.json['code'] == 'AMENDMENT_LIMIT'


def test_revision_collision_with_original_is_rejected(client, run_id):
    raw = ('station,revision,start,end,slots,note\n'
           '004,1,2026-11-20T09:00:00+08:00,2026-11-20T09:05:00+08:00,0,撞原单修订号\n').encode()
    response = submit(client, run_id, raw)
    assert response.status_code == 422 and response.json['code'] == 'SUPPLEMENT_REVISION_COLLISION'
    # 失败不留半成品
    assert len(client.get('/api/runs').json['history']) == 1


def test_all_quarantined_supplement_rejected_without_partial_run(client, run_id):
    raw = ('station,revision,start,end,slots,note\n'
           '009,1,2026-11-20T09:00:00+08:00,2026-11-20T09:05:00+08:00,0,未知站\n').encode()
    response = submit(client, run_id, raw)
    assert response.status_code == 422 and response.json['code'] == 'SUPPLEMENT_NO_EFFECT'
    assert response.json['details']['input_rows'] == 1
    assert len(client.get('/api/runs').json['history']) == 1


def test_partially_quarantined_supplement_keeps_isolation_trace(client, run_id):
    raw = SUPPLEMENT + '009,1,2026-11-20T09:00:00+08:00,2026-11-20T09:05:00+08:00,0,未知站\n'.encode()
    result = submit(client, run_id, raw).json
    assert [row['code'] for row in result['supplement_excluded']] == ['UNKNOWN_STATION']
    capacity = client.get(f'/api/capacity?run={result["id"]}').json['data']
    assert capacity['sources'][SUPPLEMENT_SOURCE]['excluded'] == 1
    assert all(row['source'] in ('capacity', SUPPLEMENT_SOURCE) for row in capacity['excluded'])


def test_different_supplement_against_stale_run_is_rejected(client, run_id):
    revised = submit(client, run_id).json['id']
    other = ('station,revision,start,end,slots,note\n'
             '004,6,2026-11-20T09:20:00+08:00,2026-11-20T09:30:00+08:00,0,另一份补报\n').encode()
    response = submit(client, run_id, other)
    assert response.status_code == 409 and response.json['code'] == 'STALE_RUN'
    assert response.json['details']['current'] == revised


def test_restore_then_same_supplement_returns_same_revision(client, run_id):
    revised = submit(client, run_id).json['id']
    client.post('/api/runs/restore', json={'target': run_id, 'expected': revised})
    again = submit(client, run_id).json
    assert again['id'] == revised and again['created'] is False
    assert len(client.get('/api/runs').json['history']) == 2


def test_concurrent_equal_amendments_yield_one_revision(client, run_id, app):
    def submit_one(_):
        return submit(app.test_client(), run_id).json
    with ThreadPoolExecutor(2) as workers:
        one, two = workers.map(submit_one, range(2))
    assert one['id'] == two['id'] and sorted([one['created'], two['created']]) == [False, True]
    assert len(client.get('/api/runs').json['history']) == 2


def test_amendment_list_and_missing_receipt_404(client, run_id):
    assert client.get('/api/amendments').json['amendments'] == []
    missing = client.get(f'/api/amendments/{run_id}/receipt')
    assert missing.status_code == 404 and missing.json['code'] == 'AMENDMENT_NOT_FOUND'
    revised = submit(client, run_id).json['id']
    listing = client.get('/api/amendments').json['amendments']
    assert len(listing) == 1 and listing[0]['run_id'] == revised
    assert listing[0]['base_run_id'] == run_id and listing[0]['delta'] == 5
    scoped = client.get(f'/api/amendments?run={revised}').json['amendments']
    assert len(scoped) == 1
    assert client.get(f'/api/amendments?run={run_id}').json['amendments'] == []


def test_upload_requires_file_and_expected(client, run_id):
    no_file = client.post('/api/amendments', data={'expected': run_id},
                          content_type='multipart/form-data')
    assert no_file.json['code'] == 'UPLOAD_REQUIRED'
    no_expected = client.post('/api/amendments',
                              data={'supplement': (io.BytesIO(SUPPLEMENT), 's.csv')},
                              content_type='multipart/form-data')
    assert no_expected.json['code'] == 'BAD_REQUEST'


def test_amended_snapshot_evidence_contains_receipt_and_input_digest(client, run_id):
    revised = submit(client, run_id).json['id']
    evidence = client.get(f'/api/runs/{revised}/evidence.json').json
    payload = evidence['evidence']
    assert digest(canonical(payload).encode()) == evidence['evidence_sha256']
    assert payload['manifest']['inputs'][SUPPLEMENT_SOURCE]['sha256'] == digest(SUPPLEMENT)
    assert payload['amendment']['id'] == client.get(
        f'/api/amendments/{revised}/receipt').json['receipt']['id']
