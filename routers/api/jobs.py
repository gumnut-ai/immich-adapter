from fastapi import APIRouter

from routers.immich_models import (
    AllJobStatusResponseDto,
    JobCommandDto,
    JobCountsDto,
    JobCreateDto,
    JobName,
    JobStatusDto,
    QueueStatusDto,
)

router = APIRouter(
    prefix="/api/jobs",
    tags=["jobs"],
    responses={404: {"description": "Not found"}},
)


def create_fake_job_status() -> JobStatusDto:
    """Helper function to create a fake job status for stub responses."""
    return JobStatusDto(
        jobCounts=JobCountsDto(
            active=0,
            completed=0,
            delayed=0,
            failed=0,
            paused=0,
            waiting=0,
        ),
        queueStatus=QueueStatusDto(
            isActive=False,
            isPaused=False,
        ),
    )


@router.get("")
async def get_all_jobs_status() -> AllJobStatusResponseDto:
    """
    Get all jobs status.
    This is a stub implementation that returns fake job statuses.
    """
    fake_status = create_fake_job_status()

    return AllJobStatusResponseDto(
        backgroundTask=fake_status,
        backupDatabase=fake_status,
        duplicateDetection=fake_status,
        faceDetection=fake_status,
        facialRecognition=fake_status,
        library=fake_status,
        metadataExtraction=fake_status,
        migration=fake_status,
        notifications=fake_status,
        ocr=fake_status,
        search=fake_status,
        sidecar=fake_status,
        smartSearch=fake_status,
        storageTemplateMigration=fake_status,
        thumbnailGeneration=fake_status,
        videoConversion=fake_status,
    )


@router.post("", status_code=204)
async def create_job(request: JobCreateDto):
    """
    Create a job.
    This is a stub implementation that does not perform any action.
    """
    return


@router.put("/{id}")
async def send_job_command(id: JobName, request: JobCommandDto) -> JobStatusDto:
    """
    Send job command.
    This is a stub implementation that returns a fake job status.
    """
    return create_fake_job_status()
