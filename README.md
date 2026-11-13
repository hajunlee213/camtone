# webcam-as-als-rgb

노트북 웹캠(`/dev/video0`)을 **주변광 조도 센서(ALS) + 색온도 센서(RGB/CCT)**로 활용해, GNOME Wayland 환경에서 화면 밝기와 색온도(True Tone / Adaptive Tone 스타일)를 자동으로 조절하는 프로토타입입니다.

---

## 1. 왜 기본 `clight`와 다른가요?

일반적인 UVC 웹캠은 기본 상태(`auto_exposure=3`, `white_balance_automatic=1`)에서 내부 게인(ISO)과 화이트밸런스를 자동으로 보정해 **주변이 어둡든 밝든, 조명이 노랗든 파랗든 항상 비슷한 중간 밝기·중립 색감의 영상**을 출력합니다. 게다가 일부 웹캠(예: LG Gram 내장 `LG Camera`)은 자동 모드에서 변경된 노출/색온도 값을 V4L2로 리드백해 주지 않습니다.

이 프로젝트는 다음 방식으로 이 문제를 우회합니다:

1. **수동 노출 + Auto-Ranging (`auto_exposure=1`)**:
   * 자동 노출을 끄고 `exposure_time_absolute`를 직접 지정합니다.
   * 프레임 평균 휘도(`luma`)가 적정 구간(`65~155`)에 들어올 때까지 노출 시간을 최대 3단계로 자동 조절합니다.
   * 블랙 레벨(`16.0`) 및 감마(`gamma`, 기본 `2.2`)를 선형화한 유효 휘도와 노출 시간의 비율로 **상대 조도 지수(`ev_log = log2(luma_lin / (exp / 100))`)**를 계산합니다.
2. **수동 화이트밸런스 고정 + McCamy CCT (`white_balance_automatic=0`)**:
   * 화이트밸런스를 기준 온도(`4600K`)에 고정한 뒤, YUV→sRGB→Linear RGB→CIE XYZ 변환을 거쳐 McCamy 공식으로 주변광 색온도(CCT)를 산출합니다.
   * **주의**: 이때 산출되는 `amb_cct`는 실제 절대 색온도가 아니라 **고정 WB 기준(`wb_ref_temp`) 대비 상대값**입니다(예: 4600K 조명 아래 중립 피사체는 sRGB 기준 약 6500K로 측정됨). 실제 색온도로 맞추려면 `--dry-run`으로 알려진 조명(예: 2700K 전구색, 6500K 주광색)을 측정한 뒤 `controller.cct_calibration`에 `[측정값, 실제값]` 보정점 리스트를 등록하세요.
3. **측정 직후 자동 복구 & 점유 충돌 방지**:
   * 샘플링 전에 `fuser /dev/video0`로 다른 앱(Zoom, Meet 등)이 웹캠을 사용 중인지 확인해 충돌 시 해당 주기를 건너뜁니다.
   * 샘플링이 끝나면 `finally` 블록에서 즉시 `auto_exposure=3, white_balance_automatic=1` 기본값으로 원복합니다.
4. **Wayland GNOME 네이티브 제어**:
   * **밝기**: `systemd-logind` D-Bus (`org.freedesktop.login1.Session.SetBrightness`) 사용 (`sudo` 불필요)
   * **색온도**: GNOME Night Light (`gsettings` `night-light-temperature`) 실시간 갱신

---

## 2. 요구 사항

* Linux (Ubuntu / GNOME Wayland 환경 기준)
* Python 3.10+ 및 `numpy`
* `v4l-utils` (`v4l2-ctl`), `fuser` (`psmisc`), `gdbus`, `gsettings`

---

## 3. 빠른 시작

```bash
cd /home/hajun/Project/webcam-as-als-rgb

# 1) 드라이런 1회 실행 (화면 설정을 바꾸지 않고 측정값과 목표값만 확인)
python3 run.py --once --dry-run

# 2) 실제 1회 적용 테스트
python3 run.py --once

# 3) 데몬 모드로 주기적 실행 (기본 15초 간격)
python3 run.py --interval 15

# 4) 밝기 제어만 또는 색온도 제어만 끄고 실행
python3 run.py --no-brightness
python3 run.py --no-color
```

---

## 4. 설정 파일 (`config.json`) 튜닝 가이드

`config.example.json`을 복사해 `config.json`으로 만든 뒤 `--config config.json`으로 실행할 수 있습니다.

```bash
cp config.example.json config.json
python3 run.py --config config.json
```

### 주요 파라미터 설명

| 섹션 | 키 | 기본값 | 설명 |
| :--- | :--- | :--- | :--- |
| `sensor` | `roi_mode` | `"full"` | `"full"`(전체 프레임) 또는 `"border"`(상단/좌우 테두리 위주 — 화면 반사광·얼굴 영향 감소) |
| `sensor` | `wb_ref_temp` | `4600` | 수동 화이트밸런스 기준 온도(K). 사용 중인 웹캠에 맞춰 `4600`~`5500` 사이로 조정 가능 |
| `sensor` | `gamma` | `2.2` | Y 휘도 선형화 감마 지수. 카메라 특성에 맞게 조정 가능 |
| `sensor` | `yuv_range` | `"limited"` | `"limited"`(`Y: 16~235`) 또는 `"full"`(`Y: 0~255`). 사용 중인 웹캠 출력 범위에 맞게 선택 |
| `controller` | `ev_log_min` / `ev_log_max` | `-10.0` / `-3.5` | **추정치(감마 보정 반영)**. `--dry-run`으로 어두운 방/밝은 창가 `ev_log`를 측정해 최소/최대 매핑 범위를 다시 맞추세요. |
| `controller` | `brightness_min_pct` / `max_pct` | `15` / `95` | 자동 조절될 화면 밝기 범위(%) |
| `controller` | `cct_calibration` | `[]` | `[[측정값, 실제값], ...]` 보정점 리스트(빈 리스트면 보정 없음). 알려진 조명에서 `--dry-run`의 `amb_cct`를 측정해 채우면 절대 색온도 매핑이 정확해집니다. |
| `controller` | `display_cct_min` / `max` | `3400` / `6200` | GNOME Night Light로 적용할 최소(따뜻함)~최대(중립) 색온도(K) |
| `controller` | `ema_alpha` | `0.35` | 지수이동평균(EMA) 계수. 작을수록 천천히 부드럽게 변함 |
| `controller` | `brightness_deadband_pct` | `2` | 이전 적용값 대비 2% 미만 변화는 무시 (깜빡임 방지) |
| `controller` | `cct_deadband_k` | `80` | 이전 적용값 대비 80K 미만 변화는 무시 |
| `(최상위)` | `interval_sec` | `15.0` | 측정 주기(초). 짧을수록 반응이 빠르지만 웹캠 LED가 자주 켜집니다. |

---

## 5. `systemd --user` 서비스로 백그라운드 등록 (선택)

로그인 시 자동 실행하려면 `~/.config/systemd/user/webcam-als-rgb.service`를 생성합니다:

```ini
[Unit]
Description=Webcam as ALS and RGB Sensor
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
WorkingDirectory=/home/hajun/Project/webcam-as-als-rgb
# 가상환경(venv/conda 등)을 사용하는 경우 해당 환경의 python3 절대 경로로 수정하세요.
ExecStart=/usr/bin/python3 /home/hajun/Project/webcam-as-als-rgb/run.py --config /home/hajun/Project/webcam-as-als-rgb/config.json
KillSignal=SIGTERM
TimeoutStopSec=10
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
```

활성화 및 상태 확인:

```bash
systemctl --user daemon-reload
systemctl --user enable --now webcam-als-rgb.service
journalctl --user -u webcam-als-rgb.service -f
```

---

## 6. 알려진 한계

* **웹캠 LED 점등**: 샘플링 동안 웹캠 하드웨어 LED가 켜집니다. 밝은 환경에서는 ~0.5~1초 수준이지만, 어두운 환경에서 긴 노출이 필요할 때는 수 초~십수 초 동안 켜질 수 있습니다.
* **화면 반사광 피드백**: 매우 어두운 방에서 흰 화면을 띄우면 모니터 빛이 얼굴에 반사되어 웹캠이 실제보다 밝거나/다른 색온도로 오인할 수 있습니다. 필요 시 `roi_mode: "border"`와 낮은 `ema_alpha`를 권장합니다.
