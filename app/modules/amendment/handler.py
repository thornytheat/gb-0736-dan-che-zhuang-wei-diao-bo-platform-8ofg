from flask import Blueprint, Response, jsonify, request

from app.config import MAX_INPUT_BYTES
from app.errors import DomainError
from app.web import store
from .service import amend, list_amendments, receipt_for, supplement_source

bp = Blueprint('amendment', __name__)


@bp.get('/api/amendments')
def index():
    run = request.args.get('run')
    return jsonify(amendments=list_amendments(store(), run))


@bp.get('/api/amendments/<run_id>/receipt')
def show(run_id):
    return jsonify(receipt_for(store(), run_id))


@bp.get('/api/amendments/<run_id>/supplement.csv')
def export(run_id):
    raw, encoding = supplement_source(store(), run_id)
    return Response(raw, mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="supplement-{run_id[:12]}.csv"',
                             'X-Source-Encoding': encoding})


@bp.post('/api/amendments')
def submit():
    file = request.files.get('supplement')
    if file is None:
        raise DomainError('UPLOAD_REQUIRED')
    expected = request.form.get('expected')
    if not expected:
        raise DomainError('BAD_REQUEST', field='expected')
    raw = file.stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise DomainError('BODY_TOO_LARGE', 413)
    return jsonify(amend(store(), expected, raw))
