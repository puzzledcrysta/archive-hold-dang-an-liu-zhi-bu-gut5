from fastapi import APIRouter
from app.inputs import Review, TransferForm
from .repository import history
from .service import decide, detail, form

router = APIRouter()


@router.post('/api/transfer')
def create(request: TransferForm):
    return form(request.run_id, request.week, request.rows)


@router.get('/api/transfer')
def lists():
    return history()


@router.get('/api/transfer/{list_id}')
def list_detail(list_id: str):
    return detail(list_id)


@router.post('/api/transfer/{list_id}/review')
def review_list(list_id: str, review: Review):
    return decide(list_id, review.version, review.decision, review.note)
