from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(
    prefix="",
    tags=["static"],
    responses={404: {"description": "Not found"}},
)


@router.get("/custom.css")
async def get_css():
    return FileResponse("routers/static/custom.css")

