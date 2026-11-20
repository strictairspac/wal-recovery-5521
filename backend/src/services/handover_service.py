import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select

from src.models.entities import CrashCase, HandoverDrill, LogSet
from src.services.short_log import build_short_log
from src.utils.serialize import record


def _signature(log_set_id, points):
    payload = json.dumps({'log_set_id': log_set_id, 'points': sorted(set(points))},
                         sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def public_view(obj):
    """复测通过才带出短日志与位置对照；未通过只给退回原因。原账始终另走恢复队列可查。"""
    out = record(obj)
    if obj.status != 'passed':
        out['result'] = {
            'retest_passed': False,
            'checks': obj.result.get('checks', []),
            'reject_reasons': obj.result.get('reject_reasons', []),
            'input_sha256': obj.result.get('input_sha256'),
            'model_version': obj.result.get('model_version'),
        }
    return out


def list_handover_drills(db):
    rows = db.scalars(select(HandoverDrill).order_by(HandoverDrill.id.desc()).limit(100))
    return [public_view(x) for x in rows]


def get_handover_drill(db, ident):
    obj = db.get(HandoverDrill, ident)
    if obj is None:
        raise HTTPException(404, '交班复测记录不存在')
    return obj


def create_handover_drill(db, data):
    logs = db.get(LogSet, data.log_set_id)
    if logs is None:
        raise HTTPException(404, '日志集不存在')
    cases = list(db.scalars(select(CrashCase).where(CrashCase.id.in_(data.crash_case_ids))))
    found = {c.id: c for c in cases}
    missing = [cid for cid in data.crash_case_ids if cid not in found]
    if missing:
        raise HTTPException(404, f'故障场景不存在: {missing}')
    foreign = [cid for cid, case in found.items() if case.log_set_id != logs.id]
    if foreign:
        raise HTTPException(422, '所选断电点必须属于同一日志集')
    points = sorted({found[cid].crash_after for cid in data.crash_case_ids})
    signature = _signature(logs.id, points)

    # 同一次申请重试：同日志集且断电点集合相同则直接复用，不重新生成。
    existing = db.scalar(select(HandoverDrill).where(
        HandoverDrill.log_set_id == logs.id,
        HandoverDrill.signature == signature))
    if existing is not None:
        out = public_view(existing)
        out['reused'] = True
        return out

    try:
        result = build_short_log(logs.initial, logs.records, points)
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    obj = HandoverDrill(
        log_set_id=logs.id, label=data.label, crash_points=points,
        signature=signature,
        status='passed' if result['retest_passed'] else 'failed',
        result=result)
    db.add(obj)
    db.flush()
    out = public_view(obj)
    out['reused'] = False
    return out
