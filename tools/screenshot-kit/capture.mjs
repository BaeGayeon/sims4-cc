// Chrome DevTools Protocol로 페이지를 열고, 선택적으로 JS를 실행한 뒤(다이얼로그 열기,
// 배지 클릭 등) 스크린샷을 PNG 파일로 저장한다.
//
// 왜 이 방식인가: 브라우저 자동화 MCP 도구의 screenshot 액션은 실제 파일로 저장을 안 해주고
// 대화 안에 이미지만 보여준다. README/위키에 넣을 실제 파일이 필요하면 이 스크립트처럼
// headless Chrome을 직접 CDP로 제어해서 Page.captureScreenshot 결과를 파일로 써야 한다.
//
// 사전 준비 (한 번만):
//   1) Chrome을 원격 디버깅 포트로 headless 실행:
//      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
//        --headless --disable-gpu --hide-scrollbars \
//        --remote-debugging-port=9333 --window-size=1440,900 \
//        --user-data-dir=/tmp/cc-manager-chrome-profile about:blank &
//   2) CC Manager를 목업 데이터로 띄워둠 (README.md 참고)
//
// 사용법:
//   node capture.mjs <url> <evalExpr|""> <출력.png> [width=1440] [height=900] [cdpPort=9333]
//
// 예시 (중복 배지 팝오버):
//   node capture.mjs "http://localhost:8766/" \
//     "document.querySelector('.dup-badge').click();" out.png

const [, , url, evalExpr, outPath, width, height, cdpPort] = process.argv;
const CDP_BASE = `http://localhost:${cdpPort || 9333}`;

if (!url || !outPath) {
  console.error("사용법: node capture.mjs <url> <evalExpr|\"\"> <출력.png> [width] [height] [cdpPort]");
  process.exit(1);
}

async function main() {
  const res = await fetch(`${CDP_BASE}/json/new?${encodeURIComponent(url)}`, { method: "PUT" });
  const target = await res.json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();

  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve);
    ws.addEventListener("error", reject);
  });
  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
    }
  });

  function send(method, params = {}) {
    const myId = ++id;
    ws.send(JSON.stringify({ id: myId, method, params }));
    return new Promise((resolve) => pending.set(myId, resolve));
  }

  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", {
    width: Number(width) || 1440,
    height: Number(height) || 900,
    deviceScaleFactor: 2,  // 레티나 수준 선명도
    mobile: false,
  });
  await new Promise((r) => setTimeout(r, 1500));  // 페이지/데이터 로드 대기

  if (evalExpr) {
    await send("Runtime.evaluate", { expression: evalExpr, awaitPromise: false });
    await new Promise((r) => setTimeout(r, 500));  // 다이얼로그/애니메이션 안착 대기
  }

  const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const fs = await import("node:fs");
  fs.writeFileSync(outPath, Buffer.from(shot.result.data, "base64"));
  console.log("saved:", outPath, Buffer.from(shot.result.data, "base64").length, "bytes");

  await send("Page.close");
  ws.close();
}

main().catch((e) => { console.error(e); process.exit(1); });
