'use strict';
// Share the runner's nonroot file ownership across the payload writer, reader,
// and cleanup process. Docker mounts still control which files each can see.
const uid = process.getuid?.();
const gid = process.getgid?.();
if (!Number.isInteger(uid) || uid <= 0 || !Number.isInteger(gid) || gid < 0) throw new Error('A nonroot POSIX review runner is required');
module.exports = { uid, gid, user: `${uid}:${gid}` };
