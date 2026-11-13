# prefs.js 실행 하이네스

실제 Libadwaita 창에서 `extension/prefs.js`의 `fillPreferencesWindow`를 호출해 창 구성이 예외 없이 끝나는지 확인합니다. `stub.js`는 셸 전용 `ExtensionPreferences` import만 대신하고, GSettings는 설치된 실제 스키마를 씁니다. 창은 6초 뒤 스스로 닫히며, 값은 바꾸지 않습니다. 현재 디스플레이에 창이 뜹니다.

```bash
D=$(mktemp -d)
cp tools/prefs-harness/run.js tools/prefs-harness/stub.js "$D"
sed "s#resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js#./stub.js#" extension/prefs.js > "$D/prefs.js"
(cd "$D" && gjs -m run.js)
```

정상이면 `FILL_OK`만 출력되고 stderr에 Gtk/Adw 경고가 없습니다.

`props-check.js`는 `prefs.js`가 쓰는 생성자 속성과 메서드가 실제 Adw/Gtk 클래스에 있는지 확인합니다 (창을 띄우지 않음). 실행은 `(cd tools/prefs-harness && gjs -m props-check.js)`이고, 정상이면 `PROPS_OK`를 출력합니다. 속성 목록은 `prefs.js`와 맞춰 직접 관리해야 합니다.
