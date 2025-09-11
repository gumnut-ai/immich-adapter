# from uuid import UUID

from fastapi import APIRouter
# , Depends, HTTPException
# from sqlalchemy import select
# from sqlalchemy.ext.asyncio import AsyncSession

# from database.config import get_async_session
# from database.models.person import Person
# from routers.immich.models import build_immich_person

router = APIRouter(
    prefix="/api/people",
    tags=["people"],
    responses={404: {"description": "Not found"}},
)


# @router.get("")
# async def get_people(db: AsyncSession = Depends(get_async_session)):
#     """
#     Get all people with pagination information.
#     """
#     result = await db.execute(select(Person))
#     people = result.scalars().all()

#     # Count total and hidden people
#     total_count = len(people)
#     hidden_count = sum(1 for person in people if person.is_hidden)

#     # Convert to Immich format
#     immich_people = [build_immich_person(person) for person in people]

#     # Return in the expected format
#     return {
#         "people": immich_people,
#         "hasNextPage": False,
#         "total": total_count,
#         "hidden": hidden_count,
#     }


# @router.get("/{person_uuid}")
# async def get_person(person_uuid: UUID, db: AsyncSession = Depends(get_async_session)):
#     """
#     Get details for a specific person.
#     """
#     person_id = Person.uuid_to_id(person_uuid)

#     result = await db.execute(select(Person).where(Person.id == person_id))
#     person = result.scalar_one_or_none()

#     if not person:
#         raise HTTPException(status_code=404, detail="Person not found")

#     return build_immich_person(person)


# @router.get("/{person_uuid}/statistics")
# async def get_person_statistics(
#     person_uuid: UUID, db: AsyncSession = Depends(get_async_session)
# ):
#     """
#     Get statistics for a specific person.
#     """
#     # person_id = Person.uuid_to_id(person_uuid)

#     # person = await db.execute(
#     #     select(Person).where(Person.id == person_id)
#     # )
#     # person = person.scalar_one_or_none()

#     # if not person:
#     #     raise HTTPException(status_code=404, detail="Person not found")

#     # # Count assets associated with this person
#     # # This assumes you have a relationship between Person and Asset
#     # # You might need to adjust this query based on your actual data model
#     # asset_count = (
#     #     await db.execute(
#     #         select(func.count(Asset.id))
#     #         .filter(Asset.people.any(Person.id == person_id))
#     #     )
#     #     .scalar()
#     #     or 0
#     # )

#     # return {"assets": asset_count}
#     # TODO: implement relationship between Person and Asset
#     return {"assets": 0}
