from fastapi import APIRouter

router = APIRouter(
    prefix="/api/partners",
    tags=["partners"],
    responses={404: {"description": "Not found"}},
)


fake_partners = []


@router.get("")
async def get_partners():
    return fake_partners
