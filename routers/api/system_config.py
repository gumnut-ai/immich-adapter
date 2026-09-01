from fastapi import APIRouter
from pydantic import AnyUrl
from routers.api.constants import FIXED_LOGIN_CONFIG
from routers.immich_models import (
    AdminConfigBackupsDto,
    AdminConfigClipDto,
    AdminConfigDatabaseBackupDto,
    AdminConfigDto,
    AdminConfigDuplicateDetectionDto,
    AdminConfigFFmpegDto,
    AdminConfigFFmpegRealtimeDto,
    AdminConfigFacesDto,
    AdminConfigFacialRecognitionDto,
    AdminConfigGeneratedFullsizeImageDto,
    AdminConfigGeneratedImageDto,
    AdminConfigImageDto,
    AdminConfigIntegrityChecksDto,
    AdminConfigIntegrityChecksumJobDto,
    AdminConfigIntegrityJobDto,
    AdminConfigJobDto,
    AdminConfigJobSettingsDto,
    AdminConfigLibraryDto,
    AdminConfigLibraryScanDto,
    AdminConfigLibraryWatchDto,
    AdminConfigLoggingDto,
    AdminConfigMachineLearningAvailabilityChecksDto,
    AdminConfigMachineLearningDto,
    AdminConfigMapDto,
    AdminConfigMetadataDto,
    AdminConfigNewVersionCheckDto,
    AdminConfigNightlyTasksDto,
    AdminConfigNotificationsDto,
    AdminConfigOAuthDto,
    AdminConfigOcrDto,
    AdminConfigPasswordLoginDto,
    AdminConfigReverseGeocodingDto,
    AdminConfigServerDto,
    AdminConfigSmtpDto,
    AdminConfigSmtpTransportDto,
    AdminConfigStorageTemplateDto,
    AdminConfigTemplateEmailsDto,
    AdminConfigTemplatesDto,
    AdminConfigThemeDto,
    AdminConfigTrashDto,
    AdminConfigUserDto,
    AudioCodec,
    CQMode,
    Colorspace,
    HlsVideoResolution,
    ImageFormat,
    LogLevel,
    OAuthTokenEndpointAuthMethod,
    ReleaseChannel,
    SystemConfigTemplateStorageOptionDto,
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
async def get_config() -> AdminConfigDto:
    """
    Get system configuration.
    This is a stub implementation that properly creates all nested DTOs.
    """

    # Create all nested DTOs from deepest level up

    # Level 3 - Deepest nested DTOs
    smtp_transport = AdminConfigSmtpTransportDto(
        host="localhost",
        ignoreCert=False,
        password="",
        port=587,
        username="",
        secure=False,
    )

    # Level 2 - Second level DTOs
    database_backup = AdminConfigDatabaseBackupDto(
        cronExpression="0 2 * * *",
        enabled=False,
        keepLastAmount=7,
    )

    job_settings_bg = AdminConfigJobSettingsDto(concurrency=5)
    job_settings_face = AdminConfigJobSettingsDto(concurrency=2)
    job_settings_lib = AdminConfigJobSettingsDto(concurrency=5)
    job_settings_meta = AdminConfigJobSettingsDto(concurrency=5)
    job_settings_mig = AdminConfigJobSettingsDto(concurrency=5)
    job_settings_notif = AdminConfigJobSettingsDto(concurrency=5)
    job_settings_search = AdminConfigJobSettingsDto(concurrency=5)
    job_settings_sidecar = AdminConfigJobSettingsDto(concurrency=5)
    job_settings_smart = AdminConfigJobSettingsDto(concurrency=2)
    job_settings_thumb = AdminConfigJobSettingsDto(concurrency=5)
    job_settings_video = AdminConfigJobSettingsDto(concurrency=1)
    job_settings_ocr = AdminConfigJobSettingsDto(concurrency=1)
    job_settings_workflow = AdminConfigJobSettingsDto(concurrency=5)
    job_settings_integrity = AdminConfigJobSettingsDto(concurrency=1)

    fullsize_image = AdminConfigGeneratedFullsizeImageDto(
        enabled=True,
        format=ImageFormat.jpeg,
        quality=80,
    )

    preview_image = AdminConfigGeneratedImageDto(
        format=ImageFormat.jpeg,
        quality=80,
        size=1440,
    )

    thumbnail_image = AdminConfigGeneratedImageDto(
        format=ImageFormat.webp,
        quality=80,
        size=250,
    )

    library_scan = AdminConfigLibraryScanDto(
        cronExpression="0 0 * * *",
        enabled=True,
    )

    library_watch = AdminConfigLibraryWatchDto(enabled=False)

    clip_config = AdminConfigClipDto(
        enabled=True,
        modelName="ViT-B-32::openai",
    )

    duplicate_detection = AdminConfigDuplicateDetectionDto(
        enabled=True,
        maxDistance=0.01,
    )

    facial_recognition = AdminConfigFacialRecognitionDto(
        enabled=True,
        maxDistance=0.6,
        minFaces=3,
        minScore=0.7,
        modelName="buffalo_l",
    )

    faces_config = AdminConfigFacesDto(**{"import": True})

    smtp_config = AdminConfigSmtpDto(
        enabled=False,
        replyTo="noreply@example.com",
        transport=smtp_transport,
        **{"from": "immich@example.com"},
    )

    email_templates = AdminConfigTemplateEmailsDto(
        albumInviteTemplate="",
        albumUpdateTemplate="",
        welcomeTemplate="",
    )

    # Level 1 - Primary nested DTOs
    backup_config = AdminConfigBackupsDto(database=database_backup)

    ffmpeg_config = AdminConfigFFmpegDto(
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
        # Real-time HLS transcoding is an intentional gap: enabled=False keeps
        # both clients on direct playback. The resolution/codec lists are inert
        # while disabled but are required by the schema as of Immich v3.0.2, so
        # they mirror the upstream server defaults.
        realtime=AdminConfigFFmpegRealtimeDto(
            enabled=False,
            resolutions=[
                HlsVideoResolution.integer_480,
                HlsVideoResolution.integer_720,
                HlsVideoResolution.integer_1080,
            ],
            videoCodecs=[VideoCodec.h264, VideoCodec.hevc],
        ),
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

    image_config = AdminConfigImageDto(
        colorspace=Colorspace.p3,
        extractEmbedded=False,
        fullsize=fullsize_image,
        preview=preview_image,
        thumbnail=thumbnail_image,
    )

    # Integrity-check schedules mirror the Immich v3 server defaults, including
    # the zero-padded cron form of "every day at 3am".
    integrity_checks = AdminConfigIntegrityChecksDto(
        checksumFiles=AdminConfigIntegrityChecksumJobDto(
            cronExpression="0 03 * * *",
            enabled=True,
            percentageLimit=1,
            timeLimit=3600000,
        ),
        missingFiles=AdminConfigIntegrityJobDto(
            cronExpression="0 03 * * *",
            enabled=True,
        ),
        untrackedFiles=AdminConfigIntegrityJobDto(
            cronExpression="0 03 * * *",
            enabled=True,
        ),
    )

    job_config = AdminConfigJobDto(
        backgroundTask=job_settings_bg,
        editor=job_settings_bg,
        faceDetection=job_settings_face,
        integrityCheck=job_settings_integrity,
        library=job_settings_lib,
        metadataExtraction=job_settings_meta,
        migration=job_settings_mig,
        notifications=job_settings_notif,
        search=job_settings_search,
        sidecar=job_settings_sidecar,
        smartSearch=job_settings_smart,
        thumbnailGeneration=job_settings_thumb,
        videoConversion=job_settings_video,
        ocr=job_settings_ocr,
        workflow=job_settings_workflow,
    )

    library_config = AdminConfigLibraryDto(
        scan=library_scan,
        watch=library_watch,
    )

    logging_config = AdminConfigLoggingDto(
        enabled=True,
        level=LogLevel.log,
    )

    ml_availabilityChecks = AdminConfigMachineLearningAvailabilityChecksDto(
        enabled=False, interval=1, timeout=1
    )

    ocr_config = AdminConfigOcrDto(
        enabled=False,
        maxResolution=1,
        minDetectionScore=0.5,
        minRecognitionScore=0.5,
        modelName="",
    )

    ml_config = AdminConfigMachineLearningDto(
        availabilityChecks=ml_availabilityChecks,
        clip=clip_config,
        duplicateDetection=duplicate_detection,
        enabled=True,
        facialRecognition=facial_recognition,
        # v3 requires at least one URL; no ML service sits behind the adapter,
        # so mirror the Immich default placeholder.
        urls=["http://immich-machine-learning:3003"],
        ocr=ocr_config,
    )

    map_config = AdminConfigMapDto(
        darkStyle=AnyUrl("https://api.mapbox.com/styles/v1/mapbox/dark-v9"),
        enabled=True,
        lightStyle=AnyUrl("https://api.mapbox.com/styles/v1/mapbox/light-v9"),
    )

    metadata_config = AdminConfigMetadataDto(faces=faces_config)

    new_version_check = AdminConfigNewVersionCheckDto(
        channel=ReleaseChannel.stable,
        enabled=True,
    )

    nightly_tasks = AdminConfigNightlyTasksDto(
        clusterNewFaces=True,
        databaseCleanup=True,
        generateMemories=True,
        missingThumbnails=True,
        startTime="02:00",
        syncQuotaUsage=True,
    )

    notifications_config = AdminConfigNotificationsDto(smtp=smtp_config)

    oauth_config = AdminConfigOAuthDto(
        allowInsecureRequests=False,
        autoLaunch=FIXED_LOGIN_CONFIG.oauth_auto_launch,
        autoRegister=True,
        buttonText=FIXED_LOGIN_CONFIG.oauth_button_text,
        clientId="",
        clientSecret="",
        defaultStorageQuota=0,
        enabled=FIXED_LOGIN_CONFIG.oauth_enabled,
        endSessionEndpoint="",
        issuerUrl="",
        mobileOverrideEnabled=False,
        mobileRedirectUri="https://example.com/oauth/redirect",
        profileSigningAlgorithm="RS256",
        prompt="",
        roleClaim="preferred_username",
        scope="openid email profile",
        signingAlgorithm="RS256",
        storageLabelClaim="preferred_username",
        storageQuotaClaim="immich_quota",
        timeout=10000,
        tokenEndpointAuthMethod=OAuthTokenEndpointAuthMethod.client_secret_post,
    )

    password_login = AdminConfigPasswordLoginDto(
        enabled=FIXED_LOGIN_CONFIG.password_login_enabled
    )

    reverse_geocoding = AdminConfigReverseGeocodingDto(enabled=True)

    server_config = AdminConfigServerDto(
        externalDomain="https://example.com",
        loginPageMessage=FIXED_LOGIN_CONFIG.login_page_message,
        publicUsers=False,
    )

    storage_template = AdminConfigStorageTemplateDto(
        enabled=False,
        hashVerificationEnabled=True,
        template="{{y}}/{{y}}-{{MM}}-{{dd}}/{{filename}}",
    )

    templates_config = AdminConfigTemplatesDto(email=email_templates)

    theme_config = AdminConfigThemeDto(customCss=FIXED_LOGIN_CONFIG.custom_css)

    trash_config = AdminConfigTrashDto(
        days=30,
        enabled=True,
    )

    user_config = AdminConfigUserDto(deleteDelay=7)

    return AdminConfigDto(
        backup=backup_config,
        ffmpeg=ffmpeg_config,
        image=image_config,
        integrityChecks=integrity_checks,
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


@router.put("", response_model=AdminConfigDto)
async def update_config(config: AdminConfigDto) -> AdminConfigDto:
    """
    Update system configuration.
    This is a stub implementation that returns the same config.
    """
    return config


@router.get("/defaults", response_model=AdminConfigDto)
async def get_config_defaults() -> AdminConfigDto:
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
