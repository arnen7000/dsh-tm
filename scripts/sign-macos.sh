#!/bin/bash
# Sign a CI-built app and staple its notarization ticket.
set -euo pipefail
umask 077

if [[ $# != 2 || ! -d "$1" || "${1##*/}" != DSH-TM.app ]]; then
    echo 'Usage: bash scripts/sign-macos.sh /path/to/DSH-TM.app arm64|x64' >&2
    exit 2
fi
case "$2" in arm64|x64) ;; *) exit 2 ;; esac
app="$1"
arch="$2"
script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"

: "${APPLE_CERTIFICATE_P12_BASE64:?Set the signing environment secret APPLE_CERTIFICATE_P12_BASE64}"
: "${APPLE_CERTIFICATE_PASSWORD:?Set the signing environment secret APPLE_CERTIFICATE_PASSWORD}"
: "${APPLE_NOTARY_KEY_BASE64:?Set the signing environment secret APPLE_NOTARY_KEY_BASE64}"
: "${APPLE_NOTARY_KEY_ID:?Set the signing environment secret APPLE_NOTARY_KEY_ID}"
: "${APPLE_NOTARY_ISSUER_ID:?Set the signing environment secret APPLE_NOTARY_ISSUER_ID}"

work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/dsh-tm-sign.XXXXXX")"
keychain="$work/signing.keychain-db"
cleanup() {
    /usr/bin/security delete-keychain "$keychain" >/dev/null 2>&1 || true
    /bin/rm -rf -- "$work"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf '%s' "$APPLE_CERTIFICATE_P12_BASE64" | /usr/bin/base64 -D > "$work/signing.p12"
printf '%s' "$APPLE_NOTARY_KEY_BASE64" | /usr/bin/base64 -D > "$work/notary.p8"
credentials=(--key "$work/notary.p8" --key-id "$APPLE_NOTARY_KEY_ID" --issuer "$APPLE_NOTARY_ISSUER_ID")
unset APPLE_CERTIFICATE_P12_BASE64 APPLE_NOTARY_KEY_BASE64 APPLE_NOTARY_KEY_ID APPLE_NOTARY_ISSUER_ID
keychain_password="$(/usr/bin/openssl rand -hex 32)"
/usr/bin/security create-keychain -p "$keychain_password" "$keychain"
/usr/bin/security set-keychain-settings -lut 3600 "$keychain"
/usr/bin/security unlock-keychain -p "$keychain_password" "$keychain"
/usr/bin/security import "$work/signing.p12" -k "$keychain" -f pkcs12 \
    -P "$APPLE_CERTIFICATE_PASSWORD" -T /usr/bin/codesign
unset APPLE_CERTIFICATE_PASSWORD
/bin/rm -- "$work/signing.p12"
/usr/bin/security set-key-partition-list -S apple-tool:,apple:,codesign: -s \
    -k "$keychain_password" "$keychain" >/dev/null
unset keychain_password
identity="$(/usr/bin/security find-identity -v -p codesigning "$keychain" |
    /usr/bin/awk '/"Developer ID Application:/{print $2}')"
if [[ ! "$identity" =~ ^[[:xdigit:]]{40}$ ]]; then
    echo 'The certificate export must contain one valid Developer ID Application identity.' >&2
    exit 1
fi

# The JavaScript runtime needs V8 JIT and loadable native addons installed per slot.
cat > "$work/node-entitlements.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.cs.allow-jit</key>
    <true/>
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
    <true/>
    <key>com.apple.security.cs.disable-library-validation</key>
    <true/>
</dict>
</plist>
PLIST
/usr/bin/python3 "$script_dir/package-macos.py" --app "$app" --arch "$arch" \
    --sign "$identity" "$keychain" "$work/node-entitlements.plist"
/usr/bin/ditto -c -k --keepParent "$app" "$work/notary.zip"

submission_exit=0
/usr/bin/xcrun notarytool submit "$work/notary.zip" "${credentials[@]}" \
    --wait --timeout 25m --output-format json > "$work/result.json" || submission_exit=$?
if [[ ! -s "$work/result.json" ]]; then
    echo "Apple notarization returned no result (exit $submission_exit)." >&2
    exit 1
fi
status="$(/usr/bin/python3 -c 'import json, sys; print(json.load(sys.stdin)["status"])' < "$work/result.json")"
request="$(/usr/bin/python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])' < "$work/result.json")"
if [[ "$status" != Accepted || "$submission_exit" != 0 ]]; then
    echo "Apple notarization $request returned $status" >&2
    /usr/bin/xcrun notarytool log "$request" "${credentials[@]}"
    exit 1
fi
/usr/bin/xcrun stapler staple "$app"
