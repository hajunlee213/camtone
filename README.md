# webcam-as-als-rgb

노트북 웹캠(`/dev/video0`)을 **주변광 조도 센서(ALS) + 색온도 센서(RGB/CCT)**처럼 쓰고, GNOME Wayland에서 화면 밝기와 Night Light 색온도를 주변 환경에 맞춰 자동으로 조절하는 프로젝트입니다. 색온도 센서가 없는 노트북을 위한 소프트웨어 대체 구현입니다.

v0.1.6부터 Python 데몬과 GNOME Shell 확장이 하나로 합쳐졌습니다. 퀵 설정의 토글 하나로 켜고 끄며, 끄면 프로세스가 완전히 사라지고 Night Light가 원래 값으로 돌아갑니다.

---

## 1. 동작 원리

일반적인 UVC 웹캠은 기본 상태(`auto_exposure=3`, `white_balance_automatic=1`)에서 내부 게인과 화이트밸런스를 자동 보정하므로, 주변 밝기나 조명 색과 무관하게 비슷한 영상을 출력합니다. 그래서 이 프로젝트는 다음 방식으로 측정합니다.

1. **수동 노출 + Auto-Ranging (`auto_exposure=1`)**
   * 자동 노출을 끄고 `exposure_time_absolute`를 직접 지정합니다.
   * 프레임 평균 휘도(`luma`)가 선형 적정 구간(`50~80`, 목표 `65.0`)에 들어올 때까지 노출을 최대 3단계로 조정합니다.
   * 블랙 레벨(`16.0`)을 뺀 뒤 선형화한 평균 휘도와 노출 시간 비율로 **상대 조도 지수 `ev_log = log2(luma_lin_mean / (exp / 100))`**를 계산합니다.
2. **수동 화이트밸런스 고정 + McCamy CCT (`white_balance_automatic=0`)**
   * 화이트밸런스를 기준 온도(`wb_ref_temp`, 기본 `4600K`)에 고정하고, YUV → sRGB → 선형 RGB → CIE XYZ 변환 후 McCamy 공식으로 주변 색온도(`amb_cct`)를 계산합니다.
   * **주의**: `amb_cct`는 절대 색온도가 아니라 고정 WB 기준 **상대값**입니다. 실제 색온도에 맞추려면 [캘리브레이션](#9-캘리브레이션-절차)을 하세요.
3. **측정 후 원복과 점유 충돌 방지**
   * 측정 전에 `fuser /dev/video0`로 다른 앱(Zoom, Meet 등)이 웹캠을 쓰고 있는지 확인하고, 쓰는 중이면 이번 회차를 건너뜁니다.
   * 측정이 끝나면 `finally`에서 측정 전 저장해 둔 카메라 컨트롤 값으로 즉시 되돌립니다.
4. **GNOME Wayland 제어**
   * **밝기 (GNOME 셸 백엔드, v0.1.8)**: GNOME 셸의 `org.gnome.Shell.Brightness.SetAutoBrightnessTarget`에 목표 밝기를 넘깁니다. 셸이 그 값을 기준으로 밝기를 설정하고 밝기 슬라이더가 따라갑니다. 슬라이더는 사용자 보정으로 쓰이며 가운데(50%)가 보정 없음입니다. 이 경우 데몬은 밝기 오프셋을 학습하지 않습니다. 자동 밝기를 끄거나 서비스를 끄면 `SetAutoBrightnessTarget(-1.0)`으로 제어를 GNOME에 돌려줍니다.
   * **밝기 (logind 백엔드, GNOME 48 이하 등)**: 셸 밝기 인터페이스가 없을 때 `systemd-logind` D-Bus `org.freedesktop.login1.Session.SetBrightness`(`sudo` 불필요)로 백라이트를 직접 씁니다. 시스템 슬라이더나 단축키로 밝기를 직접 바꾸면 그 차이를 `brightness-offset-pct`로 학습해 다음 측정부터 반영하고, 값은 GSettings에 저장되어 재시작 후에도 유지됩니다.
   * 현재 백엔드는 D-Bus 속성 `BrightnessBackend`(`shell` / `logind` / `none`)와 서비스 로그 `BRIGHTNESS: backend=…`에 나타납니다.
   * **색온도**: GNOME Night Light (`night-light-temperature`).
   * **부드러운 전환**: 밝기와 색온도 모두 현재 값에서 목표 값까지 `300ms × 4단계`(총 1.2초)로 나눠 적용합니다. 전환은 메인 루프에서 비동기로 진행되어 UI를 멈추지 않으며, 새 목표가 오면 현재 값에서 다시 보간합니다.

---

## 2. 요구 사항

* Linux, GNOME 50 Wayland 세션 (GNOME Shell 확장 사용)
* Python 3.10+, `numpy`, PyGObject (`gi`, `Gio`/`GLib` 타입 라이브러리)
* `v4l-utils`(`v4l2-ctl`), `psmisc`(`fuser`), `glib2`(`gsettings`, `glib-compile-schemas`), `systemd`(`systemctl --user`), `gnome-shell`(`gnome-extensions`)
* YUYV 출력을 지원하는 웹캠 (`/dev/video0`)

Ubuntu 기준 설치 예:

```bash
sudo apt install python3-numpy python3-gi gir1.2-glib-2.0 v4l-utils psmisc libglib2.0-bin
```

---

## 3. 설치와 제거

### 3.1 설치

```bash
cd /path/to/webcam-as-als-rgb
./install.sh
```

설치 스크립트는 다음을 확인하고 진행합니다.

* 필수 명령(`python3`, `glib-compile-schemas`, `gnome-extensions`, `systemctl`, `gsettings`, `fuser`, `v4l2-ctl`)과 `python3`의 `numpy`·`gi` 로드 여부 → 없으면 중단합니다.
* `/dev/video0`의 YUYV 포맷 지원 → 없으면 경고만 하고 계속합니다. 웹캠을 나중에 연결해도 됩니다.

설치 위치:

| 항목 | 위치 |
| :--- | :--- |
| Python 패키지 | `~/.local/share/webcam-als-rgb/webcam_als_rgb/` |
| 실행 파일 | `~/.local/bin/webcam-als-rgb` (패키지를 `python3 -m webcam_als_rgb`로 실행하는 셸 스크립트) |
| systemd 사용자 유닛 | `~/.config/systemd/user/webcam-als-rgb.service` |
| GNOME Shell 확장 | `~/.local/share/gnome-shell/extensions/webcam-als-rgb@hajun.github.io/` |
| GSettings 스키마 | 확장 폴더의 `schemas/` 와 `~/.local/share/glib-2.0/schemas/` 두 곳 |

설치 후 **Wayland에서는 로그아웃 후 다시 로그인해야** 새 확장이 로드됩니다.

> 서비스는 **로그인 시 자동으로 뜨지 않습니다.** 유닛에 `[Install]` 섹션이 없고 `systemctl --user enable`을 하지 않기 때문입니다. 켜고 끄는 일은 확장의 토글이 맡습니다.

이전 버전(`webcam-als-rgb-psr@hajun.github.io` 확장, 수동으로 만든 유닛)이 있으면 설치 중에 정지·비활성화하고 제거합니다.

### 3.2 개발용 설치 (`--dev`)

```bash
./install.sh --dev
```

확장 파일과 Python 패키지를 복사하지 않고 **이 저장소를 가리키는 심볼릭 링크**로 연결합니다. 저장소에서 고친 Python 코드는 다음 서비스 시작부터 바로 반영되고, 확장 JavaScript는 재로그인 후 반영됩니다. 복사 모드로 다시 설치하려면 `./install.sh`를 실행하면 됩니다.

`--dev`는 `extension/` 아래 최상위 항목을 하나씩 링크합니다. 그래서 **새 파일을 추가하면(예: v0.1.7의 `extension/prefs.js`) 링크가 없으므로 `./install.sh --dev`를 한 번 다시 실행해야** 설정 창이 보입니다. 기존 파일을 고친 것은 다시 실행하지 않아도 됩니다.

### 3.3 제거

```bash
./uninstall.sh
```

순서는 다음과 같습니다.

1. 서비스 정지 및 자동 시작 해제 → 데몬이 종료 신호를 받고 Night Light를 원래 값으로 되돌립니다.
2. 남은 Night Light 상태가 있으면 `--restore-night-light`로 한 번 더 복구합니다.
3. 확장 비활성화.
4. 설치 파일(실행 파일, 패키지, 유닛, 확장, 스키마)과 `$XDG_STATE_HOME/webcam-as-als-rgb/` 상태 파일 삭제.
5. `dconf`에 저장된 `/org/gnome/shell/extensions/webcam-als-rgb/` 값 초기화.

저장소 파일은 지우지 않습니다.

---

## 4. 사용법

### 4.1 퀵 설정 토글

GNOME 상단 퀵 설정의 토글이 이 프로젝트의 **유일한 on/off 스위치**입니다. 확장 설정(GSettings `enabled`)에 바인딩되어 있습니다.

| 조작 | 결과 |
| :--- | :--- |
| 토글 끔 (`enabled = false`) | 확장이 서비스를 정지합니다. 데몬이 Night Light를 원래 값으로 복구하고 종료합니다. 프로세스 0개, 카메라 사용 0. |
| 토글 켬 | 확장이 서비스를 시작합니다. 시작 직후 1회 측정합니다. |
| 확장 앱에서 확장 끔 | 서비스 정지, 토글과 PSR 갱신기 제거. |
| 확장 앱에서 확장 켬 | `enabled`가 true이면 서비스 시작. |
| 잠금 화면 진입 | 확장이 잠금 상태에서 꺼져도 **서비스는 멈추지 않습니다.** 데몬이 화면 잠금 신호를 받아 스스로 측정을 멈춥니다. 잠금을 풀면 다시 측정합니다. |

토글 부제목에는 상태가 나옵니다. 켜져 있으면 `밝기 48% · 4610K`, 일시정지 중이면 사유(`잠김`, `배터리 절약`, `카메라 사용 중`), 꺼져 있으면 `꺼짐`입니다. 서비스 시작에 실패하면 `설치 오류`로 표시되고 로그에 기록됩니다.

### 4.2 퀵 설정 메뉴

토글을 펼치면 다음 항목을 쓸 수 있습니다.

* **자동 밝기** / **자동 색온도**: 각 기능을 따로 켜고 끕니다 (`auto-brightness`, `auto-color-temp`).
* **주변 값**(비활성): 마지막 측정의 `ev`, 색온도, 측정 후 경과 시간.
* **지금 측정**: 측정 주기와 무관하게 즉시 1회 측정하고 주기를 기본값으로 되돌립니다.

### 4.3 측정 주기 정책

센서를 계속 켜 두면 웹캠 LED가 자주 켜지고 배터리도 씁니다. 그래서 측정 간격과 일시정지 조건을 다음처럼 둡니다.

**측정하지 않는 경우**

| 조건 | 감지 방법 | 동작 |
| :--- | :--- | :--- |
| 화면 잠금·화면 꺼짐 | 세션 버스 `org.gnome.ScreenSaver`의 `ActiveChanged` | 타이머를 멈추고 진행 중인 전환을 취소합니다. 잠금 해제 2초 뒤 1회 측정 후 재개합니다. |
| 절전 진입·복귀 | 시스템 버스 logind `PrepareForSleep` | 절전 진입 시 타이머를 멈춥니다. 복귀 2초 뒤 1회 측정합니다. |
| 배터리 + `pause-on-battery = true` | UPower `OnBattery` | 배터리 전환 시 멈추고, 전원 복귀 시 즉시 1회 측정합니다. |
| 다른 앱이 카메라 사용 중 | `fuser /dev/video0` | 이번 회차만 건너뜁니다. 이때 주기를 늘리지 않습니다. |

**측정 간격**

* 기본 간격은 전원 연결 시 `interval-ac-sec`(기본 60초), 배터리 시 `interval-battery-sec`(기본 120초)입니다.
* **적응형 증가**: 데드밴드 안이라 밝기와 색온도를 바꾸지 않은 회차가 3번 연속이면 간격을 2배로 늘리고, 상한은 `interval-max-sec`(기본 300초)입니다.
* 밝기나 색온도를 한 번이라도 바꾸면 기본 간격으로 되돌아갑니다.
* 전원 상태가 바뀌면 기본 간격을 다시 계산하고 1회 측정합니다.

이전 버전은 15초마다 고정으로 측정했습니다. 정상 사용에서는 측정 횟수가 크게 줄어듭니다. 실제 감소폭과 측정 1회의 상주 비용은 `docs/v0.1.6-change-report-packaging.md`와 데몬 보고서에 기록합니다.

### 4.4 설정 창 (v0.1.7)

퀵 설정 토글 메뉴의 **설정…**, 또는 앱 목록의 **Webcam ALS RGB 설정**으로 엽니다. 모든 값은 GSettings(`org.gnome.shell.extensions.webcam-als-rgb`)에 바로 저장되고, 데몬은 바뀐 값을 다음 회차부터 적용합니다. 범위는 스키마에 정의된 값을 따릅니다.

| 쪽 | 내용 |
| :--- | :--- |
| 밝기 | 서비스 사용, 자동 밝기, 최소/최대 밝기, 데드밴드, 학습된 밝기 오프셋(`초기화`로 0) |
| 색온도 | 자동 색온도, 화면 최소/최대 색온도, 색온도 오프셋, 데드밴드, PSR2 전체 갱신 |
| 측정·전원 | AC/배터리/최대 측정 간격, 배터리에서 측정 일시정지, 평활화 계수 |
| 보정 | 밝기 기준 ev(어두운/밝은), 주변 색온도 범위, 색온도 보정 점 목록 |
| 정보 | 확장 버전, 데몬 상태와 마지막 측정(5초마다 갱신), 로그 명령 복사 |

각 행 오른쪽의 되돌리기 버튼은 스키마 기본값으로 되돌립니다.

**보정 도우미** (보정 쪽)

* **현재 환경을 어두운 기준으로 / 밝은 기준으로**: 데몬에 지금 측정을 한 번 시키고, 나온 ev 값을 `ev-log-min` 또는 `ev-log-max`에 넣습니다. 어두운 기준이 밝은 기준보다 작아야 하므로, 순서가 맞지 않으면 값을 바꾸지 않고 이유를 알려 줍니다.
* **색온도 보정 점 추가**: 실제 조명의 색온도(K)를 입력하면, 지금 측정한 주변 색온도와 짝지어 `cct-calibration`에 추가합니다. 점이 1개면 그만큼 평행 이동하고, 2개 이상이면 점 사이를 선형으로 잇습니다.

두 도우미는 데몬이 실행 중이어야 합니다. 꺼져 있거나 일시정지 상태면 아무것도 쓰지 않고 토스트로 알려 줍니다.

---

## 5. 로그와 상태 확인

서비스는 systemd 사용자 서비스이므로 로그는 journal에 남습니다.

```bash
# 실시간 로그
journalctl --user -u webcam-als-rgb -f

# 오늘 로그 전체
journalctl --user -u webcam-als-rgb --since today

# 서비스 상태
systemctl --user status webcam-als-rgb.service
```

D-Bus 인터페이스(세션 버스 `io.github.hajunlee213.WebcamAlsRgb`)로 현재 상태를 확인할 수도 있습니다.

```bash
gdbus introspect --session \
  --dest io.github.hajunlee213.WebcamAlsRgb \
  --object-path /io/github/hajunlee213/WebcamAlsRgb

# 지금 바로 측정
gdbus call --session \
  --dest io.github.hajunlee213.WebcamAlsRgb \
  --object-path /io/github/hajunlee213/WebcamAlsRgb \
  --method io.github.hajunlee213.WebcamAlsRgb.MeasureNow
```

주요 속성은 `State`(`running`, `paused-locked`, `paused-sleep`, `paused-battery`, `camera-busy`), `EvLog`, `AmbientCct`, `TargetBrightness`, `TargetCct`, `LastMeasured`, `NextMeasureIn`, `BrightnessBackend`(`shell`, `logind`, `none`)입니다.

---

## 6. 설정

### 6.1 GSettings (사용자 설정의 단일 출처)

설정은 GSettings 스키마 `org.gnome.shell.extensions.webcam-als-rgb`에 있습니다. 바꾸면 데몬이 재시작 없이 바로 반영합니다.

```bash
gsettings set org.gnome.shell.extensions.webcam-als-rgb interval-ac-sec 30
gsettings get org.gnome.shell.extensions.webcam-als-rgb brightness-max-pct
```

| 키 | 기본값 | 설명 |
| :--- | :--- | :--- |
| `enabled` | `true` | 마스터 스위치 (토글과 같은 값) |
| `auto-brightness` | `true` | 자동 밝기 |
| `auto-color-temp` | `true` | 자동 색온도. 끄면 Night Light를 원래 값으로 되돌립니다. |
| `brightness-min-pct` / `brightness-max-pct` | `15` / `95` | 자동 밝기 범위(%) |
| `brightness-offset-pct` | `0` | 수동 밝기 오프셋(−50~50). logind 백엔드에서만 데몬이 학습해 저장합니다. 셸 백엔드에서는 쓰지 않습니다(GNOME 슬라이더가 보정). |
| `display-cct-min` / `display-cct-max` | `3400` / `6200` | Night Light 색온도 범위(K) |
| `cct-offset` | `0` | 색온도 오프셋(K) |
| `ev-log-min` / `ev-log-max` | `-8.8` / `-2.3` | 밝기 매핑의 조도 범위(추정치) |
| `ambient-cct-min` / `ambient-cct-max` | `2800` / `6500` | 주변 색온도 매핑 범위(K) |
| `cct-calibration` | `[]` | `[측정값, 실제값]` 보정점 목록 |
| `ema-alpha` | `0.35` | 지수이동평균 계수. 작을수록 천천히 변합니다. |
| `brightness-deadband-pct` / `cct-deadband-k` | `2` / `80` | 이 값보다 작게 바뀌면 적용하지 않습니다 (깜빡임 방지) |
| `interval-ac-sec` | `60` | 전원 연결 시 측정 간격(15~600초) |
| `interval-battery-sec` | `120` | 배터리 측정 간격(15~1200초) |
| `interval-max-sec` | `300` | 적응형 증가 상한(초) |
| `pause-on-battery` | `false` | `true`면 배터리에서 측정을 멈춥니다. |
| `psr-refresh` | `true` | PSR2 화면 갱신기 사용 (§7) |
| `show-indicator` | `false` | 상단바 아이콘 |

하드웨어 센서 설정(장치 경로, 해상도, 노출 범위 등)은 GSettings에 없습니다. 코드 기본값을 쓰고, 개발용으로만 JSON 덮어쓰기를 씁니다 (§8).

### 6.2 개발용 JSON 덮어쓰기 (`config.json`)

`config.example.json`을 복사해 `config.json`으로 만든 뒤 `--config`로 넘기면, GSettings 값 위에 덮어씁니다. 파일이 없어도 동작합니다. 주로 `sensor` 섹션(센서 튜닝)과 `controller`, `output` 섹션을 조정할 때 씁니다.

```bash
cp config.example.json config.json
webcam-als-rgb --config config.json --once --dry-run
```

| 섹션 | 키 | 기본값 | 설명 |
| :--- | :--- | :--- | :--- |
| `sensor` | `roi_mode` | `"full"` | `"full"`(전체 프레임) 또는 `"border"`(상단·좌우 테두리 위주. 화면 반사광과 얼굴 영향 감소) |
| `sensor` | `wb_ref_temp` | `4600` | 수동 화이트밸런스 기준 온도(K). 웹캠에 맞춰 4600~5500 사이로 조정합니다. |
| `sensor` | `max_exposure` | `8000` | 자동 노출 조절 상한 |
| `sensor` | `exposure_blacklist` | `[1250, 2500]` | 휘도가 급락하는 펌웨어 결함 노출 목록 (회피) |
| `sensor` | `gamma` | `1.0` | Y 휘도 선형화 감마. 다른 카메라는 `--sweep`으로 확인합니다. |
| `sensor` | `yuv_range` | `"limited"` | `"limited"`(Y 16~235) 또는 `"full"`(Y 0~255) |
| `sensor` | `target_luma_min` / `max` | `50.0` / `80.0` | 노출 자동 조절의 목표 휘도 구간 |
| `sensor` | `target_luma_setpoint` | `65.0` | 노출 비례 보정의 목표 휘도 |
| `controller` | `cct_calibration` | `[]` | 색온도 보정점. 알려진 조명에서 측정한 `amb_cct`를 채웁니다. |
| `output` | `transition_steps` | `4` | 전환 단계 수 |
| `output` | `transition_interval_sec` | `0.3` | 전환 단계 간격(초) |

---

## 7. PSR2 패널 화면 갱신 (선택)

Intel PSR2(Panel Self Refresh) 패널에서는 화면이 정지해 있으면 Night Light 색온도 변경이 화면 전체에 바로 반영되지 않을 수 있습니다. 확장에 들어 있는 **PSR 갱신기**는 색온도를 바꿀 때 전체화면 투명 액터를 짧게 토글해 화면을 다시 그리게 합니다. 동작은 gnome-adaptive-tone의 `PsrRefreshController`와 같습니다.

* 켜고 끄기: GSettings `psr-refresh` (기본 `true`).
* 이 패널에서 실제로 필요한지는 실기기 확인이 필요합니다. 확인 방법은 Night Light 온도를 `3500`과 `5500` 사이로 번갈아 바꾸면서 `psr-refresh`를 켠 상태와 끈 상태에서 화면 전체 색이 바로 바뀌는지 보는 것입니다.

---

## 8. 개발 명령

모든 명령은 설치된 `webcam-als-rgb` 또는 저장소에서 `python3 -m webcam_als_rgb`로 실행할 수 있습니다.

```bash
# 측정만 하고 결과(ev, 색온도, 목표 밝기·색온도)를 출력합니다. 화면 설정은 바꾸지 않습니다.
webcam-als-rgb --once --dry-run

# 1회 측정 후 실제로 적용합니다.
webcam-als-rgb --once

# 고정 노출 스윕 진단 (화면 설정과 Night Light를 바꾸지 않음)
webcam-als-rgb --sweep
webcam-als-rgb --sweep --sweep-order desc --sweep-exposures 200,400,800,1200,1600,2400,3200,4800

# 비정상 종료 등으로 남은 Night Light 상태를 원래 값으로 복구
webcam-als-rgb --restore-night-light

# 저장소에서 직접 실행 (설치 없이)
python3 -m webcam_als_rgb --once --dry-run
```

인자 없이 `webcam-als-rgb`를 실행하면 **서비스 모드**(데몬)로 뜹니다. 보통은 직접 실행할 일이 없고, 확장 토글이 systemd로 시작합니다. 서비스가 도는 중에 개발 명령으로 같은 카메라를 열면 측정이 실패하거나 서로 간섭할 수 있으므로, 개발 명령은 서비스를 멈춘 뒤에 실행하세요.

```bash
systemctl --user stop webcam-als-rgb.service
webcam-als-rgb --once --dry-run
```

---

## 9. 캘리브레이션 절차

다른 장소, 조명, 카메라에서 정확도를 맞추려면 다음 순서로 진행합니다.

1. **선형 구간과 노출 상한 확인 (`--sweep`)**
   ```bash
   webcam-als-rgb --sweep --sweep-order asc
   webcam-als-rgb --sweep --sweep-order desc
   ```
   `Linear luma range (est.)`와 `Effective exposure cap (est.)`, `ev_log` 편차(`PASS` 기준 `<= 0.30`)를 확인합니다. `Anomalous exposures`가 나오면 `sensor.exposure_blacklist`에 추가하세요.
2. **센서 파라미터 조정 (`gamma`, `target_luma_*`, `max_exposure`)**
   * `--sweep`에서 선형 구간의 `ev_log_gamma1`이 일정하면 `gamma: 1.0`을 유지하고, 체계적인 기울기가 보이면 `gamma`를 조정합니다.
   * `target_luma_*`는 선형 구간 안에 두고, `max_exposure`는 유효 노출 상한보다 약간 낮게 둡니다.
3. **밝기 매핑 범위 (`ev-log-min` / `ev-log-max`)**
   * 어두운 방과 밝은 창가에서 `webcam-als-rgb --once --dry-run`으로 `ev_log`를 측정한 뒤, `gsettings set org.gnome.shell.extensions.webcam-als-rgb ev-log-min <값>`처럼 `ev-log-min` / `ev-log-max`에 넣습니다.
4. **색온도 보정 (`cct-calibration`)**
   * 알려진 색온도의 조명(예: 2700K 전구색, 6500K 주광색)에서 `--once --dry-run`으로 `amb_cct`를 측정하고, `[[측정값, 실제값], ...]` 목록으로 `cct-calibration`에 넣습니다.

---

## 10. 알려진 한계

* **웹캠 LED 점등**: 측정할 때마다 웹캠 LED가 켜집니다. 밝은 환경에서는 0.5~1초 정도지만, 어두운 환경에서 노출이 길면 수 초에서 십수 초 켜질 수 있습니다. 측정 간격을 줄일수록 자주 켜집니다.
* **화면 반사광 피드백**: 어두운 방에서 밝은 화면을 띄우면 모니터 빛이 얼굴에 반사되어 실제보다 밝거나 다른 색온도로 읽힐 수 있습니다. `roi_mode: "border"`와 낮은 `ema-alpha`를 권장합니다.
* **Night Light 슬라이더 충돌**: `night-light-slider-updated` 같은 다른 확장이나 GNOME 설정 슬라이더로 색온도를 직접 바꾸면, 다음 측정에서 이 프로젝트가 덮어씁니다. 자동 색온도를 끄려면 `auto-color-temp`를 끄세요.
* **카메라 점유**: 다른 앱이 웹캠을 쓰는 동안은 측정을 건너뜁니다.
* **센서 결함**: 웹캠 펌웨어에 따라 특정 노출에서 값이 급락할 수 있습니다. `exposure_blacklist`로 회피합니다.
