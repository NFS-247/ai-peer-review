#!/usr/bin/env bash
# One-shot: push this extracted repo to NFS-247/ai-peer-review and tag v1.
# Run on your VM (the one where `gh auth login` is already done) AFTER you have
# extracted the tarball and cd'd into this directory.
#
#   tar xzf ai-peer-review-export.tgz
#   cd ai-peer-review-export
#   bash PUSH.sh
#
set -euo pipefail

REMOTE="https://github.com/NFS-247/ai-peer-review.git"

# This dir is already a committed git repo (branch: main, one commit).
git remote remove origin 2>/dev/null || true
git remote add origin "$REMOTE"

echo ">> pushing main to $REMOTE"
git push -u origin main

echo ">> tagging v1"
git tag -f v1
git push -f origin v1

echo
echo "Done. https://github.com/NFS-247/ai-peer-review"
echo "Next: add repo secrets (see README.md) and onboard your first project."
