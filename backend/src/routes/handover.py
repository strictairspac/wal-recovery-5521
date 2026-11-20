import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from src.config.database import get_db
from src.controllers.actions import audit
from src.middlewares.auth import current_user, writable
from src.schemas.handover import HandoverInput
from src.services.handover_service import (
    create_handover_drill,
    get_handover_drill,
    list_handover_drills,
    public_view,
)

router = APIRouter()


@router.get('/handover-drills')
def listing(db=Depends(get_db), user=Depends(current_user)):
    return list_handover_drills(db)


@router.post('/handover-drills', status_code=201)
def create(data: HandoverInput, db=Depends(get_db), user=Depends(writable)):
    out = create_handover_drill(db, data)
    audit(db, user, f'handover:{"reuse" if out.get("reused") else "create"}:{out["id"]}')
    db.commit()
    return out


@router.get('/handover-drills/{ident}')
def detail(ident: int, db=Depends(get_db), user=Depends(current_user)):
    return public_view(get_handover_drill(db, ident))


@router.get('/handover-drills/{ident}/export')
def export(ident: int, db=Depends(get_db), user=Depends(current_user)):
    obj = get_handover_drill(db, ident)
    if obj.status != 'passed':
        raise HTTPException(409, '复测未通过，短日志与位置对照不予发放；原账可在恢复队列查看')
    result = obj.result
    payload = {
        'id': obj.id,
        'label': obj.label,
        'log_set_id': obj.log_set_id,
        'crash_points': obj.crash_points,
        'status': obj.status,
        'short_records': result['short_records'],
        'position_map': result['position_map'],
        'minimality': result['minimality'],
        'checks': result['checks'],
        'short_log_sha256': result['short_log_sha256'],
        'input_sha256': result['input_sha256'],
        'model_version': result['model_version'],
        'boundary': result['boundary'],
    }
    audit(db, user, f'handover:export:{obj.id}')
    db.commit()
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        media_type='application/json',
        headers={'Content-Disposition': f'attachment; filename="handover-drill-{ident}.json"'})
