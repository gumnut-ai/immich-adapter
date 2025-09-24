from fastapi import APIRouter
from pydantic import AnyUrl
from routers.immich_models import (
    AudioCodec,
    CLIPConfig,
    CQMode,
    Colorspace,
    DatabaseBackupConfig,
    DuplicateDetectionConfig,
    FacialRecognitionConfig,
    ImageFormat,
    JobSettingsDto,
    LogLevel,
    OAuthTokenEndpointAuthMethod,
    SystemConfigBackupsDto,
    SystemConfigDto,
    SystemConfigFFmpegDto,
    SystemConfigFacesDto,
    SystemConfigGeneratedFullsizeImageDto,
    SystemConfigGeneratedImageDto,
    SystemConfigImageDto,
    SystemConfigJobDto,
    SystemConfigLibraryDto,
    SystemConfigLibraryScanDto,
    SystemConfigLibraryWatchDto,
    SystemConfigLoggingDto,
    SystemConfigMachineLearningDto,
    SystemConfigMapDto,
    SystemConfigMetadataDto,
    SystemConfigNewVersionCheckDto,
    SystemConfigNightlyTasksDto,
    SystemConfigNotificationsDto,
    SystemConfigOAuthDto,
    SystemConfigPasswordLoginDto,
    SystemConfigReverseGeocodingDto,
    SystemConfigServerDto,
    SystemConfigSmtpDto,
    SystemConfigSmtpTransportDto,
    SystemConfigStorageTemplateDto,
    SystemConfigTemplateEmailsDto,
    SystemConfigTemplatesDto,
    SystemConfigTemplateStorageOptionDto,
    SystemConfigThemeDto,
    SystemConfigTrashDto,
    SystemConfigUserDto,
    ToneMapping,
    TranscodeHWAccel,
    TranscodePolicy,
    VideoCodec,
    VideoContainer,
)


router = APIRouter(
    prefix="/api/system-config",
    tags=["system-config"],
    responses={404: {"description": "Not found"}},
)


@router.get("")
async def get_config() -> SystemConfigDto:
    """
    Get system configuration.
    This is a stub implementation that properly creates all nested DTOs.
    """

    # Create all nested DTOs from deepest level up

    # Level 3 - Deepest nested DTOs
    smtp_transport = SystemConfigSmtpTransportDto(
        host="localhost",
        ignoreCert=False,
        password="",
        port=587,
        username="",
    )

    # Level 2 - Second level DTOs
    database_backup = DatabaseBackupConfig(
        cronExpression="0 2 * * *",
        enabled=False,
        keepLastAmount=7.0,
    )

    job_settings_bg = JobSettingsDto(concurrency=5)
    job_settings_face = JobSettingsDto(concurrency=2)
    job_settings_lib = JobSettingsDto(concurrency=5)
    job_settings_meta = JobSettingsDto(concurrency=5)
    job_settings_mig = JobSettingsDto(concurrency=5)
    job_settings_notif = JobSettingsDto(concurrency=5)
    job_settings_search = JobSettingsDto(concurrency=5)
    job_settings_sidecar = JobSettingsDto(concurrency=5)
    job_settings_smart = JobSettingsDto(concurrency=2)
    job_settings_thumb = JobSettingsDto(concurrency=5)
    job_settings_video = JobSettingsDto(concurrency=1)

    fullsize_image = SystemConfigGeneratedFullsizeImageDto(
        enabled=True,
        format=ImageFormat.jpeg,
        quality=80,
    )

    preview_image = SystemConfigGeneratedImageDto(
        format=ImageFormat.jpeg,
        quality=80,
        size=1440,
    )

    thumbnail_image = SystemConfigGeneratedImageDto(
        format=ImageFormat.webp,
        quality=80,
        size=250,
    )

    library_scan = SystemConfigLibraryScanDto(
        cronExpression="0 0 * * *",
        enabled=True,
    )

    library_watch = SystemConfigLibraryWatchDto(enabled=False)

    clip_config = CLIPConfig(
        enabled=True,
        modelName="ViT-B-32::openai",
    )

    duplicate_detection = DuplicateDetectionConfig(
        enabled=True,
        maxDistance=0.01,
    )

    facial_recognition = FacialRecognitionConfig(
        enabled=True,
        maxDistance=0.6,
        minFaces=3,
        minScore=0.7,
        modelName="buffalo_l",
    )

    faces_config = SystemConfigFacesDto(**{"import": True})

    smtp_config = SystemConfigSmtpDto(
        enabled=False,
        replyTo="noreply@example.com",
        transport=smtp_transport,
        **{"from": "immich@example.com"},
    )

    email_templates = SystemConfigTemplateEmailsDto(
        albumInviteTemplate="",
        albumUpdateTemplate="",
        welcomeTemplate="",
    )

    # Level 1 - Primary nested DTOs
    backup_config = SystemConfigBackupsDto(database=database_backup)

    ffmpeg_config = SystemConfigFFmpegDto(
        accel=TranscodeHWAccel.disabled,
        accelDecode=False,
        acceptedAudioCodecs=[AudioCodec.aac],
        acceptedContainers=[VideoContainer.mp4],
        acceptedVideoCodecs=[VideoCodec.h264],
        bframes=-1,
        cqMode=CQMode.auto,
        crf=23,
        gopSize=0,
        maxBitrate="0",
        preferredHwDevice="auto",
        preset="faster",
        refs=0,
        targetAudioCodec=AudioCodec.aac,
        targetResolution="720",
        targetVideoCodec=VideoCodec.h264,
        temporalAQ=False,
        threads=0,
        tonemap=ToneMapping.hable,
        transcode=TranscodePolicy.required,
        twoPass=False,
    )

    image_config = SystemConfigImageDto(
        colorspace=Colorspace.p3,
        extractEmbedded=False,
        fullsize=fullsize_image,
        preview=preview_image,
        thumbnail=thumbnail_image,
    )

    job_config = SystemConfigJobDto(
        backgroundTask=job_settings_bg,
        faceDetection=job_settings_face,
        library=job_settings_lib,
        metadataExtraction=job_settings_meta,
        migration=job_settings_mig,
        notifications=job_settings_notif,
        search=job_settings_search,
        sidecar=job_settings_sidecar,
        smartSearch=job_settings_smart,
        thumbnailGeneration=job_settings_thumb,
        videoConversion=job_settings_video,
    )

    library_config = SystemConfigLibraryDto(
        scan=library_scan,
        watch=library_watch,
    )

    logging_config = SystemConfigLoggingDto(
        enabled=True,
        level=LogLevel.log,
    )

    ml_config = SystemConfigMachineLearningDto(
        clip=clip_config,
        duplicateDetection=duplicate_detection,
        enabled=True,
        facialRecognition=facial_recognition,
        urls=[],
    )

    map_config = SystemConfigMapDto(
        darkStyle=AnyUrl("https://api.mapbox.com/styles/v1/mapbox/dark-v9"),
        enabled=True,
        lightStyle=AnyUrl("https://api.mapbox.com/styles/v1/mapbox/light-v9"),
    )

    metadata_config = SystemConfigMetadataDto(faces=faces_config)

    new_version_check = SystemConfigNewVersionCheckDto(enabled=True)

    nightly_tasks = SystemConfigNightlyTasksDto(
        clusterNewFaces=True,
        databaseCleanup=True,
        generateMemories=True,
        missingThumbnails=True,
        startTime="02:00",
        syncQuotaUsage=True,
    )

    notifications_config = SystemConfigNotificationsDto(smtp=smtp_config)

    oauth_config = SystemConfigOAuthDto(
        autoLaunch=False,
        autoRegister=True,
        buttonText="Login with OAuth",
        clientId="",
        clientSecret="",
        defaultStorageQuota=0,
        enabled=False,
        issuerUrl="",
        mobileOverrideEnabled=False,
        mobileRedirectUri=AnyUrl("https://example.com/oauth/redirect"),
        profileSigningAlgorithm="RS256",
        roleClaim="preferred_username",
        scope="openid email profile",
        signingAlgorithm="RS256",
        storageLabelClaim="preferred_username",
        storageQuotaClaim="immich_quota",
        timeout=10000,
        tokenEndpointAuthMethod=OAuthTokenEndpointAuthMethod.client_secret_post,
    )

    password_login = SystemConfigPasswordLoginDto(enabled=True)

    reverse_geocoding = SystemConfigReverseGeocodingDto(enabled=True)

    server_config = SystemConfigServerDto(
        externalDomain=AnyUrl("https://example.com"),
        loginPageMessage="",
        publicUsers=False,
    )

    storage_template = SystemConfigStorageTemplateDto(
        enabled=False,
        hashVerificationEnabled=True,
        template="{{y}}/{{y}}-{{MM}}-{{dd}}/{{filename}}",
    )

    templates_config = SystemConfigTemplatesDto(email=email_templates)

    theme_config = SystemConfigThemeDto(customCss="")

    trash_config = SystemConfigTrashDto(
        days=30,
        enabled=True,
    )

    user_config = SystemConfigUserDto(deleteDelay=7)

    # Root SystemConfigDto
    return SystemConfigDto(
        backup=backup_config,
        ffmpeg=ffmpeg_config,
        image=image_config,
        job=job_config,
        library=library_config,
        logging=logging_config,
        machineLearning=ml_config,
        map=map_config,
        metadata=metadata_config,
        newVersionCheck=new_version_check,
        nightlyTasks=nightly_tasks,
        notifications=notifications_config,
        oauth=oauth_config,
        passwordLogin=password_login,
        reverseGeocoding=reverse_geocoding,
        server=server_config,
        storageTemplate=storage_template,
        templates=templates_config,
        theme=theme_config,
        trash=trash_config,
        user=user_config,
    )


@router.put("", response_model=SystemConfigDto)
async def update_config(config: SystemConfigDto) -> SystemConfigDto:
    """
    Update system configuration.
    This is a stub implementation that returns the same config.
    """
    return config


@router.get("/defaults", response_model=SystemConfigDto)
async def get_config_defaults() -> SystemConfigDto:
    """
    Get default system configuration.
    This is a stub implementation that returns the same as get_config.
    """
    return await get_config()


@router.get("/storage-template-options")
async def get_storage_template_options() -> SystemConfigTemplateStorageOptionDto:
    """
    Get storage template options.
    This is a stub implementation that returns dummy template options.
    """
    return SystemConfigTemplateStorageOptionDto(
        dayOptions=["01", "02", "03"],
        hourOptions=["00", "01", "02"],
        minuteOptions=["00", "01", "02"],
        monthOptions=["01", "02", "03"],
        presetOptions=["preset1", "preset2"],
        secondOptions=["00", "01", "02"],
        weekOptions=["1", "2", "3"],
        yearOptions=["2023", "2024", "2025"],
    )
