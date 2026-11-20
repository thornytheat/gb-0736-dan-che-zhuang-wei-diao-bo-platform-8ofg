from time import perf_counter

from app.clock import stamp
from app.errors import DomainError
from app.manifest import manifest
from app.modules.amendment.receipt import SUPPLEMENT_SOURCE, build_receipt
from app.modules.capacity.service import capacity, merged_capacity
from app.modules.demand.service import demand
from app.modules.dispatch.service import evaluate
from app.modules.intake.service import intake
from app.modules.inventory.service import reconcile
from app.modules.replay.service import replay
from app.shift import load_shift


def run_shift(sources, draft, base_snapshot=None):
    started = perf_counter()
    shift = load_shift(sources['shift'])
    received = intake(sources['events'], shift)
    if not received['events']:
        raise DomainError('NO_ACCEPTED_EVENTS', reasons=received['reasons'], quarantined=received['excluded'])
    if SUPPLEMENT_SOURCE in sources:
        limits = merged_capacity([('capacity', sources['capacity']),
                                  (SUPPLEMENT_SOURCE, sources[SUPPLEMENT_SOURCE])], shift)
        supplement_encoding = limits['sources'][SUPPLEMENT_SOURCE]['encoding']
    else:
        limits = capacity(sources['capacity'], shift)
        supplement_encoding = None
    base = replay(shift, received['events'], limits['segments'])
    candidate, dispatch = evaluate(shift, received['events'], limits['segments'], base, draft)
    audits = {name: reconcile(shift, result, received, limits)
              for name, result in [('base', base), ('plan', candidate)]}
    if not all(entry['ok'] for entry in audits.values()):
        raise DomainError('INVENTORY_MISMATCH', 409)
    snapshot = {
        'manifest': manifest(sources, dispatch['legs'], round((perf_counter() - started) * 1000, 3)),
        'shift': {'name': shift.name, 'start': stamp(shift.start), 'end': stamp(shift.end),
                  'stations': shift.stations, 'trucks': shift.trucks,
                  'min_travel_minutes': shift.min_travel_minutes},
        'intake': received, 'capacity': limits, 'replay': {'base': base, 'plan': candidate},
        'demand': {'base': demand(shift, base), 'plan': demand(shift, candidate)},
        'dispatch': dispatch, 'inventory': audits,
    }
    if SUPPLEMENT_SOURCE in sources:
        if base_snapshot is None:
            raise DomainError('AMENDMENT_BASE_MISSING', 409)
        snapshot['amendment'] = build_receipt(base_snapshot, snapshot,
                                              sources[SUPPLEMENT_SOURCE], supplement_encoding)
    return snapshot
