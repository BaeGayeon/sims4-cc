# 스크린샷 키트

README/위키용 스크린샷을 **실제 CC나 창작자 이름 없이** 찍기 위한 도구.
가짜 창작자 이름·가짜 파일명·생성한 플레이스홀더 이미지(색깔 도형)로 채운 목업
데이터로 앱을 별도 인스턴스로 띄운 다음, headless Chrome을 CDP로 직접 조작해서
스크린샷을 실제 PNG 파일로 저장한다.

## 필요한 것

- Python 3.8+ (표준 라이브러리만 사용, 이미 이 프로젝트 실행에 필요한 것과 동일)
- Node.js 18+ (`fetch`/`WebSocket` 내장 버전. 20 이상이면 확실함)
- Google Chrome (`/Applications/Google Chrome.app/...` 경로에 설치돼 있어야 함, macOS 기준)

> 왜 Node+CDP를 직접 쓰나: 브라우저 자동화 도구(MCP 등)의 스크린샷 기능은 대화 안에
> 이미지를 보여주기만 하고 실제 파일로 저장을 안 해준다. README/위키에 커밋할 파일이
> 필요하면 headless Chrome을 원격 디버깅 포트로 띄워서 `Page.captureScreenshot`을
> 직접 파일로 써야 한다.

## 전체 흐름

### 1. 목업 데이터 생성

```bash
cd tools/screenshot-kit
python3 gen_placeholder_thumbs.py      # ./mock-data/thumbs/*.png 생성
python3 gen_mock_manifest.py           # ./mock-data/manifest.json 등 생성
```

### 2. 앱을 목업 데이터로, 격리된 포트에 띄우기

실제 앱 데이터(`~/Library/Application Support/Sims4CCManager` 등)를 전혀 건드리지 않도록
환경 변수로 완전히 다른 위치/포트를 지정한다:

```bash
cd ../..   # 저장소 루트로
CC_MANAGER_APP_STATE="$(pwd)/tools/screenshot-kit/mock-data" \
CC_MANAGER_PORT=8766 \
CC_MANAGER_NO_AUTOOPEN=1 \
python3 sims_cc_manager.py &
```

`http://localhost:8766` 로 목업 갤러리가 뜨는지 확인 (`curl -s http://localhost:8766/api/manifest`).

### 3. Chrome을 원격 디버깅 모드로 띄우기

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu --hide-scrollbars \
  --remote-debugging-port=9333 --window-size=1440,900 \
  --user-data-dir=/tmp/cc-manager-chrome-profile \
  about:blank &
```

### 4. 스크린샷 캡처

```bash
node capture.mjs "http://localhost:8766/" "" gallery.png 1440 1800
```

세 번째 인자는 `Page.captureScreenshot` 실행 직전에 페이지에서 돌릴 JS 표현식이다
(다이얼로그를 열거나 배지를 클릭하는 등). 아래는 실제로 위키에 쓴 스크린샷들의
정확한 명령어 예시:

| 스크린샷 | 명령 |
|---|---|
| 전체 갤러리 | `node capture.mjs "http://localhost:8766/" "" gallery.png 1440 1800` |
| 통계 | `node capture.mjs "http://localhost:8766/" "document.getElementById('statsBtn')?.click(); (function(){const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='통계'); if(b) b.click();})();" stats.png` |
| 중복 의심 팝오버 | `node capture.mjs "http://localhost:8766/" "document.querySelector('.dup-badge').click();" dup-popover.png` |
| CAS 충돌 팝오버 | `node capture.mjs "http://localhost:8766/" "const b=document.querySelector('.conflict-badge'); b.scrollIntoView({block:'center'}); b.click();" conflict-popover.png` |
| 설정 화면 | `node capture.mjs "http://localhost:8766/" "document.getElementById('settings-dialog').showModal();" settings.png` |
| 라이트박스(확대) | `node capture.mjs "http://localhost:8766/" "const z=document.querySelector('.zoom-btn'); z.style.opacity='1'; z.style.pointerEvents='auto'; z.click();" lightbox.png` |
| 삭제 표시 | `node capture.mjs "http://localhost:8766/" "[...document.querySelectorAll('.item .thumb-img, .item .no-thumb')].slice(0,2).forEach(img=>img.click());" delete-marking.png` |
| 스캔 진행률 | 아래 "스캔/휴지통 화면은 조금 더 손이 간다" 참고 |
| 휴지통 | 아래 참고 |
| 카테고리 변경 메뉴(우클릭) | 아래 참고 |

### 스캔/휴지통/우클릭 화면은 조금 더 손이 간다

**스캔 진행률**: 목업 데이터가 몇 개 안 돼서 스캔이 순식간에 끝나버려 진행 중인 순간을
자연스럽게 잡기 어렵다. 진행률 오버레이 함수를 직접 불러서 데모 값을 채워 넣는다:

```js
Object.keys(state).forEach(k=>delete state[k]); saveMarks(); render();
showScanOverlay(true);
document.getElementById('scanOvBar').style.width='63%';
document.getElementById('scanOvName').textContent='CC FeaturedCreators/StudioNoir/StudioNoir_LeatherJacket.package';
document.getElementById('scanOvCount').textContent='11 / 18';
```

**휴지통**: 목업 파일은 실제로 디스크에 없어서 `/api/delete`가 "파일 없음"으로 실패한다.
`mock-data/manifest.json`의 아이템 하나에 `"trashed": true, "trash_path": "<path>"`를 직접
써넣고, `mock-data/trash_manifest.json`에도 같은 경로로 항목을 추가한 다음,
`mock-data/trash/<그 경로>`에 더미 파일(예: 2~3MB 정도의 0바이트 채움)을 만들어 두면
휴지통 API가 정상적으로 인식한다. 그다음:

```js
openTrash();
```

**카테고리 변경 메뉴**: 우클릭(`contextmenu`)이라 `.click()`으로는 안 뜬다. 이벤트를
직접 만들어서 쏴야 한다:

```js
const el = document.querySelector('.item[data-path="..."]');
const r = el.getBoundingClientRect();
el.dispatchEvent(new MouseEvent('contextmenu', {
  bubbles: true, clientX: r.left + r.width/2, clientY: r.top + r.height/2,
}));
```

## 다 찍고 나면

```bash
# 프로세스 정리
lsof -ti :8766 | xargs kill
lsof -ti :9333 | xargs kill
rm -rf mock-data /tmp/cc-manager-chrome-profile
```

`mock-data/`는 `.gitignore`에 걸려있어서 실수로 커밋되진 않지만, 그래도 안 남기는 게 깔끔하다.

## 완성된 스크린샷은 어디로

- 저장소 루트 `screenshots/` — README에서 씀
- 위키 저장소(`sims4-cc.wiki`, 별도 git repo)의 `images/` — 위키 페이지들에서 씀.
  위키는 `git clone https://github.com/BaeGayeon/sims4-cc.wiki.git` 로 따로 받아서
  커밋/푸시해야 한다 (메인 저장소랑 별개 repo).
