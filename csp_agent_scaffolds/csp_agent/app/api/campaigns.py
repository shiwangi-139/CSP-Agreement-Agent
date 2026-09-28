from fastapi import APIRouter

router = APIRouter()

@router.get("/")
def list_campaigns():
    return {"status": "ok"}

