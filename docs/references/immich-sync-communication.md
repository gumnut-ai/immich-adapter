---
title: "Immich Client<>Server Sync Communication"
last-updated: 2025-12-02
---

# Immich Client<>Server Sync Communication

## Setup

I am running the Immich iOS client and communicating with an Immich server running in the local network. I am using Proxyman on MacOS to proxy communication between the iOS app and the Immich server. The Immich server already has 49 assets and 7 albums defined.

## Initial Client Activity

This is the first time the client has connected to this server. After login, the client calls `http://192.168.1.187:2283/api/sync/stream` with a body of:

```json
{
  "reset": true,
  "types": [
    "AuthUsersV1",
    "UsersV1",
    "AssetsV1",
    "AssetExifsV1",
    "PartnersV1",
    "PartnerAssetsV1",
    "PartnerAssetExifsV1",
    "AlbumsV1",
    "AlbumUsersV1",
    "AlbumAssetsV1",
    "AlbumAssetExifsV1",
    "AlbumToAssetsV1",
    "MemoriesV1",
    "MemoryToAssetsV1",
    "StacksV1",
    "PartnerStacksV1",
    "UserMetadataV1",
    "PeopleV1",
    "AssetFacesV1"
  ]
}
```

The 20 possible values for `types` are defined in `SyncRequestType`. The Immich client is asking for all entity types to be synced.

The response is 275 lines and 140KB. Here is a subset:

```json
{"type":"AuthUserV1","data":{"id":"5ccc983a-db97-4f49-b29b-a832f1d3d2b5","name":"Taggart","email":"taggartgorman@yahoo.com","avatarColor":null,"deletedAt":null,"profileChangedAt":"2025-09-15T22:55:19.045Z","isAdmin":true,"pinCode":null,"oauthId":"","storageLabel":"admin","quotaSizeInBytes":null,"quotaUsageInBytes":72837546,"hasProfileImage":false},"ack":"AuthUserV1|019ad8ec-f87b-7542-9111-45758cac8ff9"}
{"type":"UserV1","data":{"id":"b1158ad9-b579-4b0d-a454-4edc0bc85945","name":"John","email":"jtaggartgorman@gmail.com","avatarColor":null,"deletedAt":null,"profileChangedAt":"2025-09-20T17:49:49.668Z","hasProfileImage":false},"ack":"UserV1|019ad8ec-f879-7c39-9626-50b1bd5dac6a"}
{"type":"UserV1","data":{"id":"5ccc983a-db97-4f49-b29b-a832f1d3d2b5","name":"Taggart","email":"taggartgorman@yahoo.com","avatarColor":null,"deletedAt":null,"profileChangedAt":"2025-09-15T22:55:19.045Z","hasProfileImage":false},"ack":"UserV1|019ad8ec-f87b-7542-9111-45758cac8ff9"}
{"type":"AssetV1","data":{"id":"bb90997e-7dc0-4398-a401-d40f791e4ef2","ownerId":"5ccc983a-db97-4f49-b29b-a832f1d3d2b5","originalFileName":"DSC_0982.jpg","fileCreatedAt":"2025-01-30T00:13:07.230Z","fileModifiedAt":"2025-01-30T04:18:36.290Z","localDateTime":"2025-01-29T16:13:07.230Z","type":"IMAGE","deletedAt":null,"isFavorite":false,"visibility":"timeline","duration":null,"livePhotoVideoId":null,"stackId":null,"libraryId":null,"checksum":"vCbD4DZ3BqQibzmnh3qO1uEpLBA=","thumbhash":"lbYFDQLUmHaHWIeIeJ90qAh4ioCn"},"ack":"AssetV1|01994f97-f1fd-7d4f-ab66-c5624fe665d7"}
[...]
{"type":"SyncAckV1","data":{},"ack":"AlbumAssetUpdateV1|019adb7b-eaef-7a5f-a073-a5e2331de138"}
{"type":"AlbumAssetCreateV1","data":{"id":"ec6cadf9-dc8a-40ce-b6e7-c3f6df2827cf","ownerId":"5ccc983a-db97-4f49-b29b-a832f1d3d2b5","originalFileName":"DSC_4506.jpg","fileCreatedAt":"2025-01-31T00:02:00.430Z","fileModifiedAt":"2025-04-14T21:13:43.787Z","localDateTime":"2025-01-30T16:02:00.430Z","type":"IMAGE","deletedAt":null,"isFavorite":false,"visibility":"timeline","duration":null,"livePhotoVideoId":null,"stackId":null,"libraryId":null,"checksum":"K8yLrSN5c1rLLcNw5p5eZgTcdfY=","thumbhash":"qbYFDYCUiol2eIivVId3UElwkCMI"},"ack":"AlbumAssetCreateV1|01994f98-b16c-703e-8b61-959cb9c3ee76"}
[...]
{"type":"AlbumV1","data":{"id":"bc6724a1-1136-4e80-a74a-a52e788eb488","ownerId":"5ccc983a-db97-4f49-b29b-a832f1d3d2b5","name":"Red Flag 25-01","description":"","createdAt":"2025-09-15T22:57:01.054Z","updatedAt":"2025-09-15T22:57:19.506Z","thumbnailAssetId":"e526140d-48bc-4772-9107-5b7cc17f24f0","isActivityEnabled":true,"order":"desc"},"ack":"AlbumV1|01994f98-d292-73b9-b27a-154e90d4a2f3"}
[...]
{"type":"AlbumToAssetV1","data":{"assetId":"ec6cadf9-dc8a-40ce-b6e7-c3f6df2827cf","albumId":"bc6724a1-1136-4e80-a74a-a52e788eb488"},"ack":"AlbumToAssetV1|01994f98-b16c-703e-8b61-959cb9c3ee76"}
[...]
{"type":"AssetExifV1","data":{"assetId":"ec6cadf9-dc8a-40ce-b6e7-c3f6df2827cf","description":"","exifImageWidth":1600,"exifImageHeight":1066,"fileSizeInByte":251102,"orientation":null,"dateTimeOriginal":"2025-01-31T00:02:00.430Z","modifyDate":"2025-04-14T21:13:43.787Z","timeZone":"UTC-7","latitude":null,"longitude":null,"projectionType":null,"city":null,"state":null,"country":null,"make":"NIKON CORPORATION","model":"NIKON Z 8","lensModel":"NIKKOR Z 180-600mm f/5.6-6.3 VR","fNumber":6.3,"focalLength":600,"iso":125,"exposureTime":"1/500","profileDescription":"sRGB IEC61966-2.1","rating":null,"fps":null},"ack":"AssetExifV1|01994f97-f030-749f-8d14-7aa4d5bbb92b"}
[...]
{"type":"SyncAckV1","data":{},"ack":"AlbumAssetExifUpdateV1|019adb7b-eaef-7a5f-a073-a5e2331de138"}
{"type":"AlbumAssetExifCreateV1","data":{"assetId":"ec6cadf9-dc8a-40ce-b6e7-c3f6df2827cf","description":"","exifImageWidth":1600,"exifImageHeight":1066,"fileSizeInByte":251102,"orientation":null,"dateTimeOriginal":"2025-01-31T00:02:00.430Z","modifyDate":"2025-04-14T21:13:43.787Z","timeZone":"UTC-7","latitude":null,"longitude":null,"projectionType":null,"city":null,"state":null,"country":null,"make":"NIKON CORPORATION","model":"NIKON Z 8","lensModel":"NIKKOR Z 180-600mm f/5.6-6.3 VR","fNumber":6.3,"focalLength":600,"iso":125,"exposureTime":"1/500","profileDescription":"sRGB IEC61966-2.1","rating":null,"fps":null},"ack":"AlbumAssetExifCreateV1|01994f98-b16c-703e-8b61-959cb9c3ee76"}
[...]
{"type":"MemoryDeleteV1","data":{"memoryId":"f0fecdc5-a3da-466a-9757-b5e106a69f40"},"ack":"MemoryDeleteV1|019ac453-886e-7f05-adba-44ca13ad94c5"}
{"type":"MemoryV1","data":{"id":"3d3cd24d-4cd2-47e3-9843-325a990ed43c","createdAt":"2025-11-25T08:00:00.165Z","updatedAt":"2025-11-25T08:00:00.165Z","deletedAt":null,"ownerId":"5ccc983a-db97-4f49-b29b-a832f1d3d2b5","type":"on_this_day","data":{"year":2017},"isSaved":false,"memoryAt":"2017-11-28T00:00:00.000Z","seenAt":null,"showAt":"2025-11-28T00:00:00.000Z","hideAt":"2025-11-28T23:59:59.999Z"},"ack":"MemoryV1|019aba06-d0a7-7c88-989f-38bfe4bbc5c3"}
{"type":"MemoryToAssetV1","data":{"memoryId":"3d3cd24d-4cd2-47e3-9843-325a990ed43c","assetId":"02b7e3e3-5d89-4564-9908-902908ea998a"},"ack":"MemoryToAssetV1|019aba06-d0a8-7e98-a2e7-6b38915dcb23"}
{"type":"AssetFaceV1","data":{"id":"96e8bd0b-cf42-4c96-b9fb-6ec3fe371da6","assetId":"0c56b89c-e87a-4f95-bead-4424b8413207","personId":null,"imageWidth":1920,"imageHeight":1440,"boundingBoxX1":817,"boundingBoxY1":361,"boundingBoxX2":1181,"boundingBoxY2":807,"sourceType":"machine-learning"},"ack":"AssetFaceV1|01995b89-248f-7c6e-bb41-da484abd0661"}
[...]
{"type":"UserMetadataV1","data":{"userId":"5ccc983a-db97-4f49-b29b-a832f1d3d2b5","key":"preferences","value":{}},"ack":"UserMetadataV1|01994f97-5d43-7399-9d53-29e9e7be3359"}
{"type":"UserMetadataV1","data":{"userId":"5ccc983a-db97-4f49-b29b-a832f1d3d2b5","key":"onboarding","value":{"isOnboarded":true}},"ack":"UserMetadataV1|01994f97-93e2-7252-89c8-63f6ed57e0fb"}
{"type":"SyncCompleteV1","data":{},"ack":"SyncCompleteV1|019adb7b-eaef-7a5f-a073-a5e2331de138"}
```

The entities are roughly ordered by `SyncRequestType` (though it is complicated by the fact that each `SyncRequestType` has its own handler which determines the actual order of the entities) and the UUID v7 time-ordered `updateId`. (I was incorrect when I stated that entities are ordered by `updateId`.)

Let's look at the detail for the first AssetV1 entity:

```json
{
    "type": "AssetV1",
    "data": {
        "id": "bb90997e-7dc0-4398-a401-d40f791e4ef2",
        "ownerId": "5ccc983a-db97-4f49-b29b-a832f1d3d2b5",
        "originalFileName": "DSC_0982.jpg",
        "fileCreatedAt": "2025-01-30T00:13:07.230Z",
        "fileModifiedAt": "2025-01-30T04:18:36.290Z",
        "localDateTime": "2025-01-29T16:13:07.230Z",
        "type": "IMAGE",
        "deletedAt": null,
        "isFavorite": false,
        "visibility": "timeline",
        "duration": null,
        "livePhotoVideoId": null,
        "stackId": null,
        "libraryId": null,
        "checksum": "vCbD4DZ3BqQibzmnh3qO1uEpLBA=",
        "thumbhash": "lbYFDQLUmHaHWIeIeJ90qAh4ioCn"
    },
    "ack": "AssetV1|01994f97-f1fd-7d4f-ab66-c5624fe665d7"
}
```

It contains just the details of the asset. EXIF comes later in a `AssetExifV1` record. Note that there is no binary image - the client retrieves the thumbnail a bit later as we'll see below.

## The Full Initial Conversation

This is the complete set of requests starting with the call to `/sync/stream`:

| Path | Method | Request Summary |
|------|--------|-----------------|
| /api/sync/stream | POST | Body detailed above |
| /api/sync/ack | POST | AuthUserV1 |
| /api/sync/ack | POST | UserV1 |
| /api/sync/ack | POST | AssetV1 |
| /api/sync/ack | POST | AlbumAssetUpdateV1 |
| /api/assets/0c56b89c-e87a-4f95-bead-4424b8413207/thumbnail | GET | WEBP image thumbnail |
| /api/assets/3df1f6a0-8a01-4ff8-9f02-d7c3d3de8618/thumbnail | GET | WEBP image thumbnail |
| /api/assets/7b5d073c-1304-4082-9ed3-030a1b2bbbae/thumbnail | GET | WEBP image thumbnail |
| /api/assets/d687118b-eed6-4a49-a0c5-1b42d3df872e/thumbnail | GET | WEBP image thumbnail |
| /api/assets/028e983d-3353-47df-82ff-4f83de672dc7/thumbnail | GET | WEBP image thumbnail |
| /api/assets/e526140d-48bc-4772-9107-5b7cc17f24f0/thumbnail | GET | WEBP image thumbnail |
| /api/assets/1fe1616c-4803-490f-851c-bd7875195625/thumbnail | GET | WEBP image thumbnail |
| /api/assets/ec6cadf9-dc8a-40ce-b6e7-c3f6df2827cf/thumbnail | GET | WEBP image thumbnail |
| /api/assets/37cf2ca5-13cf-4aa6-91dc-e8e911b0fd56/thumbnail | GET | WEBP image thumbnail |
| /api/assets/bb90997e-7dc0-4398-a401-d40f791e4ef2/thumbnail | GET | WEBP image thumbnail |
| /api/assets/02b7e3e3-5d89-4564-9908-902908ea998a/thumbnail | GET | WEBP image thumbnail |
| /api/assets/f8e63cec-6673-4e87-85fe-0ae96d9eca10/thumbnail | GET | WEBP image thumbnail |
| /api/sync/ack | POST | AlbumAssetCreateV1 |
| /api/sync/ack | POST | AlbumV1 |
| /api/sync/ack | POST | AlbumToAssetV1 |
| /api/sync/ack | POST | AssetExifV1 |
| /api/sync/ack | POST | AlbumAssetExifUpdateV1 |
| /api/sync/ack | POST | AlbumAssetExifCreateV1 |
| /api/sync/ack | POST | MemoryDeleteV1 |
| /api/sync/ack | POST | MemoryV1 |
| /api/sync/ack | POST | MemoryToAssetV1 |
| /api/sync/ack | POST | AssetFaceV1 |
| /api/sync/ack | POST | UserMetadataV1 |
| /api/sync/ack | POST | SyncCompleteV1 |

Generally there will be just one ack for each SyncEntityType, however the mobile client has a batch processing limit of 5,000 response lines. This can result in multiple acks for a single type if there are more than 5,000 lines with the same type, or if the lines for a sync extend across a batch "border."

## Additional Activity

After the initial sync was complete, the mobile client will check for changes on events such as (re-)launching or resuming the app, or the user forcing a sync by tapping "Sync Remote" in Settings or tapping on the cloud icon at the top of the Photos screen.

### No Changes

When updating with no changes, we see just two calls:

| Path | Method | Request Summary | Response |
|------|--------|-----------------|----------|
| /api/sync/stream | POST | `{"reset": false, "types": [/* sync types as above */]}` | `{"type": "SyncCompleteV1", "data": {}, "ack": "SyncCompleteV1\|019adc6a-81c5-7c33-9874-feab3030c5c1"}` |
| /api/sync/ack | POST | `{"acks": ["SyncCompleteV1\|019adc6a-81c5-7c33-9874-feab3030c5c1"]}` | (empty) |

### New Image Added

A new image was added via the Immich web client, and then the mobile client was updated:

| Path | Method | Request Summary |
|------|--------|-----------------|
| /api/sync/stream | POST | `{"reset": false, "types": [/* sync types as above */]}` |

The response body is:

```json
{"type":"AuthUserV1","data":{"id":"5ccc983a-db97-4f49-b29b-a832f1d3d2b5","name":"Taggart","email":"taggartgorman@yahoo.com","avatarColor":null,"deletedAt":null,"profileChangedAt":"2025-09-15T22:55:19.045Z","isAdmin":true,"pinCode":null,"oauthId":"","storageLabel":"admin","quotaSizeInBytes":null,"quotaUsageInBytes":83212978,"hasProfileImage":false},"ack":"AuthUserV1|019adc6a-abb8-7191-af07-f65f5e5bd628"}
{"type":"UserV1","data":{"id":"5ccc983a-db97-4f49-b29b-a832f1d3d2b5","name":"Taggart","email":"taggartgorman@yahoo.com","avatarColor":null,"deletedAt":null,"profileChangedAt":"2025-09-15T22:55:19.045Z","hasProfileImage":false},"ack":"UserV1|019adc6a-abb8-7191-af07-f65f5e5bd628"}
{"type":"AssetV1","data":{"id":"1ee6d743-54fd-43de-9164-b99fb18caf36","ownerId":"5ccc983a-db97-4f49-b29b-a832f1d3d2b5","originalFileName":"DSC_8766.jpg","fileCreatedAt":"2010-10-08T22:16:30.900Z","fileModifiedAt":"2025-09-17T02:41:46.000Z","localDateTime":"2010-10-08T15:16:30.900Z","type":"IMAGE","deletedAt":null,"isFavorite":false,"visibility":"timeline","duration":null,"livePhotoVideoId":null,"stackId":null,"libraryId":null,"checksum":"1HHXeKxJMUSpQsIvJJVdZuBOm+0=","thumbhash":"XoUJHoT4t0mGl0iMdnR2uFiYCHaDgDc="},"ack":"AssetV1|019adc6a-af34-73d3-ad9d-6e7622d7f211"}
{"type":"AssetExifV1","data":{"assetId":"1ee6d743-54fd-43de-9164-b99fb18caf36","description":"","exifImageWidth":2400,"exifImageHeight":1920,"fileSizeInByte":1058587,"orientation":null,"dateTimeOriginal":"2010-10-08T22:16:30.900Z","modifyDate":"2025-09-17T02:41:46.000Z","timeZone":"UTC-7","latitude":null,"longitude":null,"projectionType":null,"city":null,"state":null,"country":null,"make":"NIKON CORPORATION","model":"NIKON D60","lensModel":"AF-S Nikkor 300mm f/4D IF-ED","fNumber":4,"focalLength":300,"iso":110,"exposureTime":"1/1000","profileDescription":"sRGB IEC61966-2.1","rating":5,"fps":null},"ack":"AssetExifV1|019adc6a-ad2b-7813-bc0a-6e2f059fcd60"}
{"type":"SyncCompleteV1","data":{},"ack":"SyncCompleteV1|019adc6c-3ba4-7f57-a7e0-9b67da6b27d3"}
```

And the rest of the communication chain:

| Path | Method | Request Summary |
|------|--------|-----------------|
| /api/sync/ack | POST | AuthUserV1 |
| /api/sync/ack | POST | UserV1 |
| /api/sync/ack | POST | AssetV1 |
| /api/sync/ack | POST | AssetExifV1 |
| /api/sync/ack | POST | SyncCompleteV1 |

### Adding Image to Existing Album

The response body for `/api/sync/stream` is:

```json
{"type":"SyncAckV1","data":{},"ack":"AlbumAssetUpdateV1|019adc74-ea58-751b-9139-1054075c249a"}
{"type":"AlbumAssetCreateV1","data":{"id":"1ee6d743-54fd-43de-9164-b99fb18caf36","ownerId":"5ccc983a-db97-4f49-b29b-a832f1d3d2b5","originalFileName":"DSC_8766.jpg","fileCreatedAt":"2010-10-08T22:16:30.900Z","fileModifiedAt":"2025-09-17T02:41:46.000Z","localDateTime":"2010-10-08T15:16:30.900Z","type":"IMAGE","deletedAt":null,"isFavorite":false,"visibility":"timeline","duration":null,"livePhotoVideoId":null,"stackId":null,"libraryId":null,"checksum":"1HHXeKxJMUSpQsIvJJVdZuBOm+0=","thumbhash":"XoUJHoT4t0mGl0iMdnR2uFiYCHaDgDc="},"ack":"AlbumAssetCreateV1|019adc74-c908-70c5-908c-e71c68e4a396"}
{"type":"AlbumV1","data":{"id":"0142f336-3ae4-4b33-bafd-31442ae5e835","ownerId":"5ccc983a-db97-4f49-b29b-a832f1d3d2b5","name":"Fleet Week","description":"","createdAt":"2025-12-01T23:15:52.074Z","updatedAt":"2025-12-02T00:27:12.552Z","thumbnailAssetId":"4eb5665e-1cd3-4da0-82b4-e989061d2692","isActivityEnabled":true,"order":"desc"},"ack":"AlbumV1|019adc74-c928-779f-8c84-8ae4aa40943a"}
{"type":"AlbumToAssetV1","data":{"assetId":"1ee6d743-54fd-43de-9164-b99fb18caf36","albumId":"0142f336-3ae4-4b33-bafd-31442ae5e835"},"ack":"AlbumToAssetV1|019adc74-c908-70c5-908c-e71c68e4a396"}
{"type":"SyncAckV1","data":{},"ack":"AlbumAssetExifUpdateV1|019adc74-ea58-751b-9139-1054075c249a"}
{"type":"AlbumAssetExifCreateV1","data":{"assetId":"1ee6d743-54fd-43de-9164-b99fb18caf36","description":"","exifImageWidth":2400,"exifImageHeight":1920,"fileSizeInByte":1058587,"orientation":null,"dateTimeOriginal":"2010-10-08T22:16:30.900Z","modifyDate":"2025-09-17T02:41:46.000Z","timeZone":"UTC-7","latitude":null,"longitude":null,"projectionType":null,"city":null,"state":null,"country":null,"make":"NIKON CORPORATION","model":"NIKON D60","lensModel":"AF-S Nikkor 300mm f/4D IF-ED","fNumber":4,"focalLength":300,"iso":110,"exposureTime":"1/1000","profileDescription":"sRGB IEC61966-2.1","rating":5,"fps":null},"ack":"AlbumAssetExifCreateV1|019adc74-c908-70c5-908c-e71c68e4a396"}
{"type":"SyncCompleteV1","data":{},"ack":"SyncCompleteV1|019adc74-ea58-751b-9139-1054075c249a"}
```

---

## How the Client Handles the Response

Once the mobile client calls `/api/sync/stream`, it begins processing the response lines in batch sizes of 5,000 lines. Until 5,000 lines have been read or the response finishes, the client does not start to parse the lines. If the batch size exceeds 5,000 lines, then reading the response is paused and the batch is parsed and handled, and the response reading is resumed.

### Sample Timeline

```text
Server                          Client
  |                               |
  |---- chunk 1 (lines 1-100) --->| accumulate
  |---- chunk 2 (lines 101-300) ->| accumulate
  |         ...                   |
  |---- chunk N (lines 4900-5100)>| 5000 reached -> process -> ack
  |<------- POST /sync/ack -------|
  |---- chunk N+1 --------------->| accumulate
  |         ...                   |
  |---- (stream continues) ------>|
  |                               |
```

Note: This timeline assumes all the lines from the server are of the same type.

### Batch Processing

The lines in a batch are parsed into events. The events are processed in a group of the same type and then an ack is sent to the server when the type group is finished processing.

---

## SyncRequestTypes and SyncEntityTypes

### Type Mapping

The 20 SyncRequestTypes generate 45 SyncEntityTypes in specific orders.

| SyncRequestType | SyncEntityTypes (in order) |
|-----------------|---------------------------|
| **AuthUsersV1** | `AuthUserV1` |
| **UsersV1** | `UserDeleteV1`, `UserV1` |
| **PartnersV1** | `PartnerDeleteV1`, `PartnerV1` |
| **AssetsV1** | `AssetDeleteV1`, `AssetV1` |
| **StacksV1** | `StackDeleteV1`, `StackV1` |
| **PartnerAssetsV1** | `PartnerAssetDeleteV1`, `PartnerAssetBackfillV1`, `SyncAckV1`\*, `PartnerAssetV1` |
| **PartnerStacksV1** | `PartnerStackDeleteV1`, `PartnerStackBackfillV1`, `SyncAckV1`\*, `PartnerStackV1` |
| **AlbumAssetsV1** | `AlbumAssetBackfillV1`, `SyncAckV1`\*, `AlbumAssetUpdateV1`, `SyncAckV1`\*\*, `AlbumAssetCreateV1` |
| **AlbumsV1** | `AlbumDeleteV1`, `AlbumV1` |
| **AlbumUsersV1** | `AlbumUserDeleteV1`, `AlbumUserBackfillV1`, `SyncAckV1`\*, `AlbumUserV1` |
| **AlbumToAssetsV1** | `AlbumToAssetDeleteV1`, `AlbumToAssetBackfillV1`, `SyncAckV1`\*, `AlbumToAssetV1` |
| **AssetExifsV1** | `AssetExifV1` |
| **AlbumAssetExifsV1** | `AlbumAssetExifBackfillV1`, `SyncAckV1`\*, `AlbumAssetExifUpdateV1`, `SyncAckV1`\*\*, `AlbumAssetExifCreateV1` |
| **PartnerAssetExifsV1** | `PartnerAssetExifBackfillV1`, `SyncAckV1`\*, `PartnerAssetExifV1` |
| **MemoriesV1** | `MemoryDeleteV1`, `MemoryV1` |
| **MemoryToAssetsV1** | `MemoryToAssetDeleteV1`, `MemoryToAssetV1` |
| **PeopleV1** | `PersonDeleteV1`, `PersonV1` |
| **AssetFacesV1** | `AssetFaceDeleteV1`, `AssetFaceV1` |
| **UserMetadataV1** | `UserMetadataDeleteV1`, `UserMetadataV1` |
| **AssetMetadataV1** | `AssetMetadataDeleteV1`, `AssetMetadataV1` |

Notes (discussed below):
- `SyncAckV1`\* = Backfill completion marker (via `sendEntityBackfillCompleteAck()`, one per entity)
- `SyncAckV1`\*\* = Phase transition marker (before first CREATE, marks updates complete)

## Special SyncEntityTypes

### SyncAckV1

SyncAckV1 does not correlate to a specific entity type - instead it is used as a marker that certain milestones have been achieved in specific sync flows.

**Purpose 1: Backfill Completion Marker**

Backfill is the process of sending historical data that already existed before the client gained access to it, either via a shared album or becoming a partner. (We're not planning on supporting shared albums or partners at this time, so this use case is for notation only.)

**What it does:** Marks that ALL historical data for a specific entity (album or partner) has been sent.
**When sent:** AFTER all backfill records for that entity.
**Why it exists:** On the next sync, the server checks if the client acked the backfill completion marker, and if so, the server skips re-sending backfill for that album. This enables resumable backfills - if the client disconnects mid-backfill, only the incomplete albums need to be re-sent.

**Purpose 2: Phase Transition Marker**

These are ONLY in `syncAlbumAssetsV1` and `syncAlbumAssetExifsV1`. These methods have a three-phase structure:

Phase 1: BACKFILL -- Historical data for newly-joined albums
Phase 2: UPDATES -- Changes to assets the client already knows about
Phase 3: CREATES -- New album-asset associations

**What it does:** Marks that Phase 2 (UPDATES) is complete and Phase 3 (CREATES) is beginning.
**When sent:** Before the first CREATE record (only if creates exist).
**Why it exists:** If the client disconnects in the middle of CREATES, it allows the server to know to resume from creates only.

**Why Only AlbumAssets and AlbumAssetExifs?** These are the only sync methods with three distinct record types (BACKFILL, UPDATE, CREATE). Other methods like `syncAssetsV1` or `syncAlbumsV1` only have deletes + upserts, so they don't need the phase transition marker.

### SyncResetV1

SyncResetV1 is a "wipe and start over" signal from the server to the client.

The server sends SyncResetV1 in two scenarios:
- The session is flagged for reset
- If the client's last successful sync was more than 30 days ago (Immich server tracks deletions in audit logs that are deleted after 30 days - without the audit records, the server cannot tell the user what entities were deleted)

### SyncCompleteV1

SyncCompleteV1 is sent by the server at the end of each response to `/sync/stream`, even if there are no entities to stream. The client does not do any processing when encountering this SyncEntityType. The Immich server generates a new UUID v7 timestamp and returns that as the ack parameter of the SyncCompleteV1 response.
