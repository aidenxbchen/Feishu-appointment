#!/bin/bash
# Auto-sync appointment repo with GitHub
# Pull remote changes; push any committed-but-unpushed local commits

cd /home/admin/appointment

# Pull remote changes (fast-forward only — won't clobber local uncommitted work)
git fetch origin main 2>&1 | logger -t appointment-sync
git merge --ff-only origin/main 2>&1 | logger -t appointment-sync

# Push any commits that are ahead of remote
git push origin main 2>&1 | logger -t appointment-sync
