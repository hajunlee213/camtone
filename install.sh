#!/bin/bash
#
# webcam-as-als-rgb 설치 스크립트 (v0.1.6)
#
#   ./install.sh        파일을 ~/.local 아래로 복사해 설치 (일반 사용)
#   ./install.sh --dev  복사 대신 이 저장소의 파일을 심볼릭 링크로 연결 (개발용)
#
# 서비스는 로그인 시 자동 시작하지 않는다 (systemctl --user enable 하지 않음).
# 시작·정지는 GNOME 퀵 설정 토글(확장)이 담당한다.
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
step()  { echo -e "${BLUE}[STEP]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
사용법: ./install.sh [--dev]
  --dev   확장과 Python 패키지를 복사하지 않고 이 저장소에 심볼릭 링크로 연결합니다.
EOF
}

DEV_MODE=false
for arg in "$@"; do
    case "$arg" in
        --dev) DEV_MODE=true ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; error "알 수 없는 옵션: $arg" ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${HOME:?HOME 환경 변수가 설정되어 있지 않습니다}"

EXTENSION_UUID="webcam-als-rgb@hajun.github.io"
LEGACY_PSR_UUID="webcam-als-rgb-psr@hajun.github.io"
SERVICE_NAME="webcam-als-rgb.service"
SCHEMA_FILE="org.gnome.shell.extensions.webcam-als-rgb.gschema.xml"

DATA_DIR="$HOME/.local/share/webcam-als-rgb"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/webcam-als-rgb"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$SYSTEMD_USER_DIR/$SERVICE_NAME"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$EXTENSION_UUID"
LEGACY_EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$LEGACY_PSR_UUID"
SCHEMAS_USER_DIR="$HOME/.local/share/glib-2.0/schemas"

echo "================================================================="
echo "  webcam-as-als-rgb 설치 (v0.1.6)$([[ "$DEV_MODE" == true ]] && echo ' [--dev: 심볼릭 링크 모드]')"
echo "================================================================="
echo ""

# 1. 의존성과 필수 파일 확인
step "1/7: 의존성과 저장소 파일 확인..."
missing=""
for cmd in python3 glib-compile-schemas gnome-extensions systemctl gsettings fuser v4l2-ctl; do
    command -v "$cmd" >/dev/null 2>&1 || missing="$missing $cmd"
done
[[ -z "$missing" ]] || error "필수 명령이 없습니다:$missing"

if ! python3 -c '
import gi
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib
import numpy
' 2>/dev/null; then
    error "python3에서 numpy 또는 PyGObject(gi)를 불러오지 못했습니다. (Ubuntu: sudo apt install python3-numpy python3-gi gir1.2-glib-2.0)"
fi
info "python3 / numpy / gi 확인 완료."

if [[ -e /dev/video0 ]]; then
    formats="$(v4l2-ctl -d /dev/video0 --list-formats-ext 2>/dev/null || true)"
    if [[ "$formats" == *YUYV* ]]; then
        info "/dev/video0: YUYV 포맷 지원 확인."
    else
        warn "/dev/video0에서 YUYV 포맷을 찾지 못했습니다. 센서가 동작하지 않을 수 있습니다."
    fi
else
    warn "/dev/video0이 없습니다. 웹캠을 연결한 뒤 데몬 로그를 확인하세요."
fi

[[ -f "$SCRIPT_DIR/webcam_als_rgb/__init__.py" ]] || error "webcam_als_rgb/ 패키지를 찾을 수 없습니다. 저장소 루트에서 실행하세요."
[[ -f "$SCRIPT_DIR/webcam-als-rgb.service" ]] || error "webcam-als-rgb.service 파일이 없습니다."
[[ -f "$SCRIPT_DIR/extension/metadata.json" && -f "$SCRIPT_DIR/extension/extension.js" ]] \
    || error "extension/ 에 metadata.json 또는 extension.js가 없습니다."
[[ -f "$SCRIPT_DIR/extension/schemas/$SCHEMA_FILE" ]] || error "스키마 파일이 없습니다: extension/schemas/$SCHEMA_FILE"

# 2. 이전 설치 정리: 실행 중인 데몬을 먼저 멈춘다 (SIGTERM 경로에서 Night Light가 복구됨)
step "2/7: 이전 서비스·PSR 전용 확장 정리..."
systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
# 수동 설치 때 enable했던 유닛이 있으면 로그인 자동 시작 링크를 제거한다.
systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true
gnome-extensions disable "$LEGACY_PSR_UUID" 2>/dev/null || true
rm -rf "$LEGACY_EXT_DIR"

# 3. Python 패키지와 실행 파일
step "3/7: Python 패키지와 실행 파일 설치..."
mkdir -p "$DATA_DIR" "$BIN_DIR" "$SYSTEMD_USER_DIR" "$SCHEMAS_USER_DIR"

rm -rf "$DATA_DIR/webcam_als_rgb"
if [[ "$DEV_MODE" == true ]]; then
    ln -s "$SCRIPT_DIR/webcam_als_rgb" "$DATA_DIR/webcam_als_rgb"
else
    cp -R "$SCRIPT_DIR/webcam_als_rgb" "$DATA_DIR/webcam_als_rgb"
    find "$DATA_DIR/webcam_als_rgb" -name __pycache__ -type d -prune -exec rm -rf {} +
fi

# 설치 시점의 python3 절대 경로로 고정한다 (numpy/gi를 확인한 바로 그 인터프리터).
PYTHON3="$(command -v python3)"
rm -f "$LAUNCHER"
cat > "$LAUNCHER" <<EOF
#!/bin/sh
# webcam-as-als-rgb 실행 스크립트 (install.sh가 생성). 인자 없이 실행하면 서비스 모드.
export PYTHONPATH="$DATA_DIR"
exec "$PYTHON3" -m webcam_als_rgb "\$@"
EOF
chmod +x "$LAUNCHER"
info "실행 파일: $LAUNCHER"

# 4. GNOME Shell 확장과 GSettings 스키마
step "4/7: GNOME Shell 확장과 GSettings 스키마 설치..."
rm -rf "$EXT_DIR"
mkdir -p "$EXT_DIR"
if [[ "$DEV_MODE" == true ]]; then
    # 최상위 항목은 저장소로 연결한다. 스키마 디렉터리는 실제 디렉터리로 만들고
    # xml만 연결해서 gschemas.compiled가 저장소 안에 생기지 않게 한다.
    for entry in "$SCRIPT_DIR"/extension/*; do
        name="$(basename "$entry")"
        [[ "$name" == schemas ]] && continue
        ln -s "$entry" "$EXT_DIR/$name"
    done
    mkdir -p "$EXT_DIR/schemas"
    ln -s "$SCRIPT_DIR/extension/schemas/$SCHEMA_FILE" "$EXT_DIR/schemas/$SCHEMA_FILE"
else
    cp -R "$SCRIPT_DIR/extension/." "$EXT_DIR/"
fi
glib-compile-schemas "$EXT_DIR/schemas"

# 데몬은 확장 밖에서도 스키마를 찾아야 하므로 사용자 스키마 디렉터리에도 둔다.
rm -f "$SCHEMAS_USER_DIR/$SCHEMA_FILE"
if [[ "$DEV_MODE" == true ]]; then
    ln -s "$SCRIPT_DIR/extension/schemas/$SCHEMA_FILE" "$SCHEMAS_USER_DIR/$SCHEMA_FILE"
else
    cp "$SCRIPT_DIR/extension/schemas/$SCHEMA_FILE" "$SCHEMAS_USER_DIR/$SCHEMA_FILE"
fi
glib-compile-schemas "$SCHEMAS_USER_DIR"
info "확장: $EXT_DIR"

# 5. systemd 사용자 유닛 (enable하지 않는다: 로그인 시 자동 시작 금지)
step "5/7: systemd 사용자 유닛 설치 (자동 시작은 설정하지 않음)..."
rm -f "$UNIT_PATH"
cp "$SCRIPT_DIR/webcam-als-rgb.service" "$UNIT_PATH"
systemctl --user daemon-reload 2>/dev/null || warn "systemctl --user daemon-reload 실패. 다음 로그인 후 적용됩니다."
info "유닛: $UNIT_PATH"

# 6. 확장 활성화
step "6/7: GNOME Shell 확장 등록..."
EXT_UUID="$EXTENSION_UUID" LEGACY_UUID="$LEGACY_PSR_UUID" python3 - <<'PY' \
    || warn "확장 목록을 자동으로 갱신하지 못했습니다. 확장 앱에서 직접 켜 주세요."
import os
import sys

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio

ext_uuid = os.environ["EXT_UUID"]
legacy_uuid = os.environ["LEGACY_UUID"]

source = Gio.SettingsSchemaSource.get_default()
if source is None or source.lookup("org.gnome.shell", True) is None:
    print("[WARN] org.gnome.shell 스키마가 없어 확장 목록을 건너뜁니다 (GNOME 세션이 아닌 환경).")
    sys.exit(0)

settings = Gio.Settings.new("org.gnome.shell")
if settings.get_boolean("disable-user-extensions"):
    settings.set_boolean("disable-user-extensions", False)
    print("[INFO] 사용자 확장 전체 스위치를 켰습니다 (disable-user-extensions = false).")

before = list(settings.get_strv("enabled-extensions"))
after = [u for u in before if u != legacy_uuid]
if ext_uuid not in after:
    after.append(ext_uuid)
if after != before:
    settings.set_strv("enabled-extensions", after)
    print(f"[INFO] enabled-extensions 갱신: {ext_uuid} 추가"
          + (f", {legacy_uuid} 제거" if legacy_uuid in before else ""))
PY
gnome-extensions enable "$EXTENSION_UUID" 2>/dev/null || true

# 7. 완료 안내
step "7/7: 완료."
echo ""
info "================================================================="
info " webcam-as-als-rgb v0.1.6 설치가 끝났습니다."
info "================================================================="
echo "다음 단계:"
echo "  1. Wayland에서는 로그아웃 후 다시 로그인해야 새 확장이 로드됩니다."
echo "  2. 로그인 후 퀵 설정의 토글을 켜면 서비스가 시작되고, 끄면 Night Light를 복구하고 멈춥니다."
echo "     서비스는 로그인 시 자동으로 뜨지 않습니다."
echo "  3. 로그 확인:   journalctl --user -u webcam-als-rgb -f"
echo "  4. 개발 명령:   webcam-als-rgb --once --dry-run"
echo "  5. 완전 제거:   ./uninstall.sh"
echo ""
