#!/bin/bash
set -Eeuo pipefail

GREEN="\033[38;2;80;250;123m"
CYAN="\033[38;2;139;233;253m"
PINK="\033[38;2;255;102;217m"
PURPLE="\033[38;2;178;102;255m"
ORANGE="\033[38;2;255;184;108m"
RED="\033[38;2;255;85;85m"
RESET="\033[0m"

SERVICE="stacks"
IMAGE="stacks:latest"
PLATFORM="linux/amd64"
NO_CACHE=false
OUTPUT=""

# Keep this in sync with build-and-launch.sh so the exported image has the same ownership label.
FINGERPRINT="dfb58278-7000-469c-91be-84466af5f8e9"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VERSION="$(tr -d '[:space:]' < ./VERSION)"

usage() {
    echo "Usage: $0 [--platform linux/amd64] [--image stacks:latest] [--output file.tar] [--no-cache|-n]"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --platform)
            PLATFORM="${2:-}"
            shift 2
        ;;
        --image)
            IMAGE="${2:-}"
            shift 2
        ;;
        --output|-o)
            OUTPUT="${2:-}"
            shift 2
        ;;
        --no-cache|-n)
            NO_CACHE=true
            shift
        ;;
        --help|-h)
            usage
            exit 0
        ;;
        *)
            echo -e "${RED}Unknown argument: $1${RESET}"
            usage
            exit 1
        ;;
    esac
done

if [ -z "$PLATFORM" ] || [ -z "$IMAGE" ]; then
    echo -e "${RED}Platform and image cannot be empty.${RESET}"
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo -e "${RED}Docker is not installed or not on PATH.${RESET}"
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo -e "${RED}Docker is not running or is not reachable.${RESET}"
    exit 1
fi

PLATFORM_SLUG="${PLATFORM//\//-}"
if [ -z "$OUTPUT" ]; then
    OUTPUT="${SCRIPT_DIR}/${SERVICE}-${VERSION}-${PLATFORM_SLUG}-docker-image.tar"
elif [[ "$OUTPUT" != /* ]]; then
    OUTPUT="${SCRIPT_DIR}/${OUTPUT}"
fi

TMP_OUTPUT="${OUTPUT}.tmp"
cleanup() {
    rm -f "$TMP_OUTPUT"
}
trap cleanup EXIT

echo -e "${PURPLE}----------------------------------------${RESET}"
echo -e "${PINK}[${PURPLE}Ʌ${PINK}] Building STɅCKS Docker image tar${RESET}"
echo -e "${PURPLE}----------------------------------------${RESET}"
echo -e "${CYAN}Version:${RESET}  ${VERSION}"
echo -e "${CYAN}Image:${RESET}    ${IMAGE}"
echo -e "${CYAN}Platform:${RESET} ${PLATFORM}"
echo -e "${CYAN}Output:${RESET}   ${OUTPUT}"

BUILD_ARGS=(
    --platform "$PLATFORM"
    --tag "$IMAGE"
    --build-arg "VERSION=$VERSION"
    --build-arg "FINGERPRINT=$FINGERPRINT"
)

if [ "$NO_CACHE" = true ]; then
    BUILD_ARGS+=(--no-cache)
fi

echo -e "${PURPLE}► Building image...${RESET}"
docker build "${BUILD_ARGS[@]}" .

mkdir -p "$(dirname "$OUTPUT")"
rm -f "$TMP_OUTPUT"

echo -e "${PURPLE}► Saving image tar...${RESET}"
docker save "$IMAGE" --output "$TMP_OUTPUT"
mv "$TMP_OUTPUT" "$OUTPUT"

echo -e "${GREEN}[√] Docker image tar created:${RESET} ${OUTPUT}"
