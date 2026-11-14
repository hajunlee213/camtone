#!/bin/bash
#
# webcam-as-als-rgb 제거 스크립트 (v0.1.9)
#
# 순서: 서비스 정지 → Night Light 복구 → 확장 비활성화 → 파일·dconf 설정 삭제.
# 저장소 자체는 지우지 않는다.
set -euo pipefail

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}[INFO]${NC} $*"; }
step() { echo -e "${BLUE}[STEP]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }

: "${HOME:?HOME 환경 변수가 설정되어 있지 않습니다}"

EXTENSION_UUID="webcam-als-rgb@hajun.github.io"
LEGACY_PSR_UUID="webcam-als-rgb-psr@hajun.github.io"
SERVICE_NAME="webcam-als-rgb.service"
SCHEMA_FILE="org.gnome.shell.extensions.webcam-als-rgb.gschema.xml"
STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}"

DATA_DIR="$HOME/.local/share/webcam-als-rgb"
LAUNCHER="$HOME/.local/bin/webcam-als-rgb"
UNIT_PATH="$HOME/.config/systemd/user/$SERVICE_NAME"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$EXTENSION_UUID"
LEGACY_EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$LEGACY_PSR_UUID"
SCHEMAS_USER_DIR="$HOME/.local/share/glib-2.0/schemas"
APPS_DIR="$HOME/.local/share/applications"
DESKTOP_NAME="webcam-als-rgb-prefs.desktop"

echo "================================================================="
echo "             webcam-as-als-rgb 제거 (v0.1.9)                     "
echo "================================================================="

# 1. 서비스 정지: 데몬은 SIGTERM을 받으면 Night Light를 원래 값으로 되돌리고 종료한다.
step "1/5: 서비스 정지 및 자동 시작 해제..."
systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true

# 2. 남은 Night Light 상태가 있으면 복구 (정지 경로에서 이미 복구했다면 no-op)
step "2/5: Night Light 설정 복구 확인..."
if [[ -x "$LAUNCHER" ]]; then
    "$LAUNCHER" --restore-night-light 2>/dev/null || warn "Night Light 복구 명령이 실패했습니다. 필요하면 수동으로 확인하세요."
else
    warn "실행 파일($LAUNCHER)이 없어 복구 명령을 건너뜁니다."
fi

# 3. 확장 비활성화 (PSR 전용 확장 이름도 함께 정리)
step "3/5: GNOME Shell 확장 비활성화..."
gnome-extensions disable "$EXTENSION_UUID" 2>/dev/null || true
gnome-extensions disable "$LEGACY_PSR_UUID" 2>/dev/null || true

# 4. 설치된 파일 삭제 (dev 모드에서 생긴 심볼릭 링크는 링크만 지워지고 저장소는 남는다)
step "4/5: 설치된 파일 삭제..."
rm -f "$LAUNCHER"
rm -rf "$DATA_DIR"
rm -f "$UNIT_PATH"
systemctl --user daemon-reload 2>/dev/null || true
rm -rf "$EXT_DIR"
rm -rf "$LEGACY_EXT_DIR"
rm -f "$SCHEMAS_USER_DIR/$SCHEMA_FILE"
rm -f "$APPS_DIR/$DESKTOP_NAME"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
fi
if [[ -d "$SCHEMAS_USER_DIR" ]]; then
    glib-compile-schemas "$SCHEMAS_USER_DIR" 2>/dev/null || true
fi
rm -rf "$STATE_ROOT/webcam-as-als-rgb"

# 5. 저장된 GSettings 값 초기화
step "5/5: GSettings 값 초기화..."
if command -v dconf >/dev/null 2>&1; then
    dconf reset -f /org/gnome/shell/extensions/webcam-als-rgb/ 2>/dev/null || true
fi

echo ""
info "================================================================="
info " webcam-as-als-rgb가 제거되었습니다."
info " (저장소 파일은 그대로 남아 있습니다. 다시 설치하려면 ./install.sh)"
info "================================================================="
