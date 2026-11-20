import {$, api, clock, kind, kpis, label, node, outcome, table, time} from './view.js';

let receipts = [];

export async function loadAmendments(meta) {
  receipts = (await api('/api/amendments')).amendments;
  const stationName = id => meta.shift.stations[id]?.name || id;
  const earliest = receipts.length ? min(receipts.map(row => row.effective_from)) : null;
  kpis([['补报修订', receipts.length, '份'],
    ['当前班次含补报', receipts.some(row => row.active) ? '是' : '否'],
    ['最早影响时点', earliest ? time(earliest) : '—'],
    ['改判合计', receipts.reduce((sum, row) => sum + row.changes, 0), '笔', 'warn']]);
  $('amendment-base').textContent = `原发布班次 ${meta.manifest.id.slice(0, 12)} · 提交后原结论原样留档，不覆盖`;
  table('amendment-list', ['修订班次', '原班次', '影响起', '影响止', '改判', '未成交差量', '状态', '操作'],
    receipts.map(row => {
      const button = node('button', '核对回执', 'btn');
      button.addEventListener('click', () => openReceipt(row.run_id, stationName));
      return [row.run_id.slice(0, 12), row.base_run_id.slice(0, 12), time(row.effective_from), time(row.effective_to),
        row.changes, row.delta, row.active ? '当前班次' : '历史留档', button];
    }), '暂无容量补报修订');
  if (receipts.length) openReceipt(receipts[0].run_id, stationName);
  else $('amendment-detail-panel').hidden = true;
}

function min(values) {
  return values.reduce((acc, value) => acc === null || value < acc ? value : acc, null);
}

async function openReceipt(runId, stationName) {
  const {receipt, verification} = await api(`/api/amendments/${runId}/receipt`);
  $('amendment-detail-panel').hidden = false;
  $('amendment-detail-id').textContent = `回执 ${receipt.id.slice(0, 12)} · 修订班次 ${runId.slice(0, 12)}`;
  $('amendment-verify').textContent = verification.ok ? '再次核对：全部相符' : '再次核对：存在不符项';
  $('amendment-verify').className = verification.ok ? 'subtle ok' : 'subtle bad';
  $('amendment-checks').replaceChildren(...verification.checks.map(check => {
    const cell = node('div', '', 'diff-cell');
    cell.append(node('span', label(check.code), 'subtle'),
      node('strong', check.pass ? '相符' : '不符', check.pass ? 'ok' : 'bad'));
    return cell;
  }));
  table('amendment-windows', ['站点', '开始', '结束', '修订号', '原单容量', '补报容量', '源行', '备注'],
    receipt.windows.map(row => [stationName(row.station), time(row.start), time(row.end), row.revision,
      row.slots_before, row.slots_after, row.line, row.note]));
  table('amendment-changes', ['时点', '站点', '事件号', '尝试', '原判定', '改判后', '源行'],
    receipt.judgement.changes.map(row =>
      [clock(row.at), stationName(row.station), row.id, kind(row.kind), outcome(row.before), outcome(row.after), row.line]));
  table('amendment-inventory', ['站点', '期初', '原期末', '新期末', '原桩上', '新桩上', '原暂存', '新暂存',
    '原借出', '新借出', '原还入', '新还入', '差额'],
    receipt.inventory_changes.map(row => [stationName(row.station), row.opening, row.closing_before, row.closing_after,
      row.docked_before, row.docked_after, row.held_before, row.held_after, row.rent_before, row.rent_after,
      row.return_before, row.return_after, row.gap_after]));
  $('amendment-raw').textContent = receipt.supplement.raw_text;
  $('amendment-download').href = `/api/amendments/${runId}/supplement.csv`;
}
