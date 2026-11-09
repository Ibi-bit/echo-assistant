#!/usr/bin/env bash
# Build the APK without installing Gradle system-wide.
set -euo pipefail
cd "$(dirname "$0")"

if [ -z "${JAVA_HOME:-}" ] && [ -x "$HOME/.local/jdk17/bin/java" ]; then
    export JAVA_HOME="$HOME/.local/jdk17"
fi
if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/java" ]; then
    export PATH="$JAVA_HOME/bin:$PATH"
fi

if ! command -v java >/dev/null 2>&1; then
    echo "JDK 17 is required. On Debian/Ubuntu: sudo apt install openjdk-17-jdk" >&2
    exit 1
fi

if [ -z "${ANDROID_HOME:-}" ] && [ -d "$HOME/Android/Sdk" ]; then
    export ANDROID_HOME="$HOME/Android/Sdk"
fi
if [ -z "${ANDROID_HOME:-}" ]; then
    echo "Set ANDROID_HOME to your Android SDK (see README)." >&2
    exit 1
fi

GRADLE_VERSION=8.11.1
DIST="$PWD/.gradle-dist/gradle-$GRADLE_VERSION"
if [ ! -x "$DIST/bin/gradle" ]; then
    mkdir -p .gradle-dist
    echo "Downloading Gradle $GRADLE_VERSION..."
    curl -L --fail --silent --show-error \
        "https://services.gradle.org/distributions/gradle-$GRADLE_VERSION-bin.zip" \
        -o .gradle-dist/gradle.zip
    unzip -q .gradle-dist/gradle.zip -d .gradle-dist
    rm .gradle-dist/gradle.zip
fi

exec "$DIST/bin/gradle" "$@"
