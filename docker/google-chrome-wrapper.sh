#!/bin/sh
set -eu

# Chromium cannot create its user-namespace sandbox under Docker. This process
# is already isolated inside the dedicated, unprivileged, localhost-only
# publisher container.
extension=/opt/local-supporter-google-sites
exec /usr/bin/chromium --no-sandbox "$@" \
  --disable-extensions-except="$extension" \
  --load-extension="$extension"
