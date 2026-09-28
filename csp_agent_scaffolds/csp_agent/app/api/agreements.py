from fastapi import APIRouter

router = APIRouter()

@router.get("/")
def list_agreements():
    return {"status": "ok"}

