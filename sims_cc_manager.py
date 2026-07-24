#!/usr/bin/env python3
"""
Sims 4 CC Manager (v0.2)
- 로컬 HTTP 서버 (port 8765)로 갤러리 서빙
- 브라우저에서 클릭 → 실제 삭제 (Mods/.cc_trash/ 로 이동, 복원 가능)
- 재스캔, 휴지통 복원/비우기 지원
- 스캔 결과는 .cc_manager/manifest.json 에 캐시됨
"""
import json
import os
import struct
import zlib
import hashlib
import shutil
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, unquote

# ─────────── 경로 설정 ───────────
SIMS_ROOT = Path.home() / "Games" / "Electronic Arts" / "The Sims 4"
MODS = SIMS_ROOT / "Mods"
CC_ROOT = MODS / "CC FeaturedCreators"
APP_STATE = SIMS_ROOT / ".cc_manager"
THUMBS_DIR = APP_STATE / "thumbs"
MANIFEST_PATH = APP_STATE / "manifest.json"
TRASH_DIR = MODS / ".cc_trash"

APP_STATE.mkdir(exist_ok=True)
THUMBS_DIR.mkdir(exist_ok=True)
TRASH_DIR.mkdir(exist_ok=True)

CAS_THUMB_TYPE = 0x3C1AF1F2
PORT = 8765


# ─────────── DBPF v2 파서 ───────────

def parse_dbpf(path):
    try:
        with open(path, "rb") as f:
            hdr = f.read(96)
            if hdr[:4] != b"DBPF" or struct.unpack("<I", hdr[4:8])[0] != 2:
                return
            index_count = struct.unpack("<I", hdr[36:40])[0]
            index_offset = struct.unpack("<I", hdr[64:68])[0]
            if index_offset == 0 or index_count == 0:
                return
            f.seek(index_offset)
            flags = struct.unpack("<I", f.read(4))[0]
            ct = struct.unpack("<I", f.read(4))[0] if flags & 1 else 0
            cg = struct.unpack("<I", f.read(4))[0] if flags & 2 else 0
            ch = struct.unpack("<I", f.read(4))[0] if flags & 4 else 0
            for _ in range(index_count):
                try:
                    t = ct if flags & 1 else struct.unpack("<I", f.read(4))[0]
                    g = cg if flags & 2 else struct.unpack("<I", f.read(4))[0]
                    ihi = ch if flags & 4 else struct.unpack("<I", f.read(4))[0]
                    ilo = struct.unpack("<I", f.read(4))[0]
                    offset = struct.unpack("<I", f.read(4))[0]
                    size = struct.unpack("<I", f.read(4))[0] & 0x7FFFFFFF
                    _ = f.read(4)
                    compression = struct.unpack("<H", f.read(2))[0]
                    _ = f.read(2)
                    yield {"type": t, "instance": (ihi << 32) | ilo,
                           "offset": offset, "size": size, "compression": compression}
                except struct.error:
                    return
    except OSError:
        return


def read_resource(path, entry):
    with open(path, "rb") as f:
        f.seek(entry["offset"])
        data = f.read(entry["size"])
    if entry["compression"] == 0x5A42:
        try:
            data = zlib.decompress(data)
        except zlib.error:
            pass
    return data


def extract_thumbs(pkg_path):
    seen = set()
    for entry in parse_dbpf(pkg_path):
        if entry["type"] != CAS_THUMB_TYPE:
            continue
        try:
            data = read_resource(pkg_path, entry)
        except Exception:
            continue
        if not data.startswith(b"\xff\xd8\xff"):
            continue
        h = hashlib.md5(data).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        yield (data, h)


# ─────────── 스캔 ───────────

def scan_cc():
    """CC FeaturedCreators 스캔 → 창작자(하위 폴더)별 아이템."""
    if not CC_ROOT.exists():
        return {"creators": [], "error": f"폴더 없음: {CC_ROOT}"}

    creators = {}
    total_pkgs = 0
    total_thumbs = 0

    for pkg in sorted(CC_ROOT.rglob("*.package")):
        rel_pkg = pkg.relative_to(MODS)
        rel_in_cc = pkg.relative_to(CC_ROOT)
        creator = rel_in_cc.parts[0] if len(rel_in_cc.parts) > 1 else "(최상위)"
        total_pkgs += 1
        if total_pkgs % 100 == 0:
            print(f"    ... {total_pkgs}개 처리 중")

        thumbs = []
        for jpg, h in extract_thumbs(pkg):
            thumb_name = f"{h[:16]}.jpg"
            tp = THUMBS_DIR / thumb_name
            if not tp.exists():
                tp.write_bytes(jpg)
            thumbs.append(thumb_name)
            total_thumbs += 1

        creators.setdefault(creator, []).append({
            "file": pkg.name,
            "path": str(rel_pkg),  # Mods 기준 상대 경로
            "size": pkg.stat().st_size,
            "thumbs": thumbs,
        })

    creators_list = sorted(
        [{"name": name, "items": sorted(items, key=lambda x: x["file"])}
         for name, items in creators.items()],
        key=lambda c: c["name"].lower(),
    )
    manifest = {
        "creators": creators_list,
        "total_pkgs": total_pkgs,
        "total_thumbs": total_thumbs,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False))
    return manifest


def load_manifest():
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return None


# ─────────── 파일 조작 (휴지통) ───────────

def move_to_trash(rel_paths):
    moved, failed = [], []
    for rel in rel_paths:
        src = MODS / rel
        if not src.exists():
            failed.append({"path": rel, "reason": "파일 없음"})
            continue
        dst = TRASH_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            i = 1
            while True:
                candidate = dst.with_stem(f"{dst.stem}_{i}")
                if not candidate.exists():
                    dst = candidate
                    break
                i += 1
        try:
            shutil.move(str(src), str(dst))
            moved.append(rel)
        except Exception as e:
            failed.append({"path": rel, "reason": str(e)})
    return {"moved": moved, "failed": failed}


def restore_from_trash(rel_paths):
    restored, failed = [], []
    for rel in rel_paths:
        src = TRASH_DIR / rel
        dst = MODS / rel
        if not src.exists():
            failed.append({"path": rel, "reason": "휴지통에 없음"})
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
            restored.append(rel)
        except Exception as e:
            failed.append({"path": rel, "reason": str(e)})
    return {"restored": restored, "failed": failed}


def list_trash():
    items = []
    total_size = 0
    for f in TRASH_DIR.rglob("*"):
        if f.is_file():
            sz = f.stat().st_size
            items.append({
                "path": str(f.relative_to(TRASH_DIR)),
                "size": sz,
            })
            total_size += sz
    return {"items": items, "total_size": total_size}


def empty_trash():
    count = 0
    total = 0
    for f in TRASH_DIR.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
            try:
                f.unlink()
                count += 1
            except OSError:
                pass
    # 빈 하위 폴더 제거
    for d in sorted([d for d in TRASH_DIR.rglob("*") if d.is_dir()],
                    key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
        except OSError:
            pass
    return {"count": count, "size_freed": total}


# ─────────── HTTP 서버 ───────────

HTML_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Sims 4 CC Manager</title>
<style>
  body { font-family: -apple-system, sans-serif; margin: 0; background: #f5f5f5; color: #222; }
  header { position: sticky; top: 0; background: white; padding: 10px 16px; border-bottom: 1px solid #ddd; z-index: 10; display: flex; align-items: center; gap: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.05); }
  header h1 { margin: 0; font-size: 16px; }
  .stats { color: #666; font-size: 12px; flex: 1; }
  button { padding: 6px 12px; font-size: 13px; background: white; border: 1px solid #ccc; border-radius: 4px; cursor: pointer; }
  button:hover { background: #f0f0f0; }
  button.primary { background: #d9534f; color: white; border-color: #d43f3a; }
  button.primary:hover { background: #c9302c; }
  button.blue { background: #4a90e2; color: white; border-color: #357ab8; }
  main { padding: 16px; padding-bottom: 80px; }
  .creator { background: white; margin-bottom: 12px; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .creator h2 { margin: 0; padding: 10px 16px; background: #fafafa; border-bottom: 1px solid #eee; font-size: 15px; display: flex; align-items: center; gap: 8px; }
  .creator h2 .count { color: #666; font-size: 12px; font-weight: normal; }
  .grid { padding: 10px; display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
  .item { position: relative; background: #fafafa; border: 2px solid transparent; border-radius: 4px; overflow: hidden; cursor: pointer; }
  .item:hover { border-color: #4a90e2; }
  .item.marked { border-color: #d9534f; background: #ffe8e8; }
  .item.marked img, .item.marked .no-thumb { filter: grayscale(0.5) brightness(0.7); }
  .item img, .no-thumb { display: block; width: 100%; height: auto; }
  .no-thumb { padding: 40px 8px; text-align: center; color: #999; font-size: 11px; background: #eee; }
  .item .name { padding: 4px 6px; font-size: 10px; color: #444; word-break: break-all; line-height: 1.3; max-height: 2.6em; overflow: hidden; }
  .item:hover .name { max-height: 20em; background: white; }
  .item .sz { position: absolute; top: 4px; left: 4px; background: rgba(0,0,0,.6); color: white; padding: 1px 5px; font-size: 10px; border-radius: 3px; }
  #footer { position: fixed; bottom: 0; left: 0; right: 0; background: white; border-top: 1px solid #ddd; padding: 10px 16px; display: flex; align-items: center; gap: 12px; box-shadow: 0 -1px 3px rgba(0,0,0,.05); }
  #footer .count { flex: 1; font-size: 14px; }
  #footer .count b { color: #d9534f; }
  input[type=search] { padding: 6px 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 13px; }
  .toast { position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%); background: #333; color: white; padding: 10px 20px; border-radius: 6px; opacity: 0; transition: opacity .3s; z-index: 200; }
  .toast.show { opacity: 1; }
  dialog { border: none; border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,.2); padding: 20px; max-width: 500px; }
  dialog::backdrop { background: rgba(0,0,0,.5); }
  .trash-item { padding: 6px 8px; border-bottom: 1px solid #eee; display: flex; gap: 8px; font-size: 12px; align-items: center; }
  .progress { background: #f0f0f0; padding: 20px; border-radius: 8px; margin: 40px auto; max-width: 500px; text-align: center; }
</style>
</head><body>
<header>
  <h1>🎮 CC Manager</h1>
  <div class="stats" id="stats">로딩 중...</div>
  <input type="search" id="search" placeholder="🔍 검색...">
  <button onclick="rescan()" class="blue">🔄 재스캔</button>
  <button onclick="openTrash()">🗑️ 휴지통</button>
</header>
<main id="main"><div class="progress">로딩 중...</div></main>
<div id="footer">
  <div class="count">삭제 표시: <b id="marked-count">0</b>개 · 절약 예상: <b id="marked-size">0 B</b></div>
  <button onclick="performDelete()" class="primary">🗑️ 휴지통으로 이동</button>
</div>
<dialog id="trash-dialog">
  <h3>🗑️ 휴지통</h3>
  <div id="trash-summary" style="color:#666; font-size:13px; margin-bottom:8px;"></div>
  <div id="trash-list" style="max-height:50vh; overflow-y:auto; margin:8px 0; border:1px solid #eee; border-radius:4px;"></div>
  <div style="display:flex; gap:8px; justify-content:flex-end;">
    <button onclick="restoreSelected()">↩️ 선택 복원</button>
    <button onclick="emptyTrash()" class="primary">완전 비우기</button>
    <button onclick="document.getElementById('trash-dialog').close()">닫기</button>
  </div>
</dialog>
<div id="toast" class="toast"></div>
<script>
let manifest = null;
const marks = new Set();

function fmtSize(n) {
  const u = ['B','KB','MB','GB','TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + ' ' + u[i];
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2200);
}

function updateFooter() {
  document.getElementById('marked-count').textContent = marks.size;
  let sz = 0;
  if (manifest) {
    const map = {};
    manifest.creators.forEach(c => c.items.forEach(it => { map[it.path] = it.size; }));
    marks.forEach(p => { sz += map[p] || 0; });
  }
  document.getElementById('marked-size').textContent = fmtSize(sz);
}

function render() {
  const q = document.getElementById('search').value.toLowerCase();
  const main = document.getElementById('main');
  if (!manifest || !manifest.creators.length) {
    main.innerHTML = '<div class="progress">스캔된 항목이 없다. 재스캔을 눌러라.</div>';
    return;
  }
  const parts = [];
  let total = 0, shown = 0, totalSize = 0;
  for (const c of manifest.creators) {
    const filtered = q
      ? c.items.filter(it => it.file.toLowerCase().includes(q) || c.name.toLowerCase().includes(q))
      : c.items;
    total += c.items.length;
    if (!filtered.length) continue;
    parts.push('<div class="creator"><h2>' + escapeHtml(c.name)
      + ' <span class="count">(' + filtered.length + ')</span></h2><div class="grid">');
    for (const it of filtered) {
      shown++;
      totalSize += it.size;
      const cls = 'item' + (marks.has(it.path) ? ' marked' : '');
      const img = it.thumbs.length
        ? '<img src="/thumb/' + it.thumbs[0] + '" loading="lazy">'
        : '<div class="no-thumb">썸네일 없음</div>';
      parts.push('<div class="' + cls + '" data-path="' + escapeAttr(it.path) + '">'
        + '<span class="sz">' + fmtSize(it.size) + '</span>'
        + img
        + '<div class="name" title="' + escapeAttr(it.file) + '">' + escapeHtml(it.file) + '</div>'
        + '</div>');
    }
    parts.push('</div></div>');
  }
  main.innerHTML = parts.join('');
  document.getElementById('stats').textContent =
    manifest.creators.length + '명 · ' + total + '개 · ' + fmtSize(totalSize) + ' (표시 ' + shown + '개)';
}

function escapeHtml(s) { return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function escapeAttr(s) { return escapeHtml(s).replace(/"/g, '&quot;'); }

document.addEventListener('click', e => {
  const it = e.target.closest('.item');
  if (!it) return;
  const path = it.dataset.path;
  if (marks.has(path)) { marks.delete(path); it.classList.remove('marked'); }
  else { marks.add(path); it.classList.add('marked'); }
  updateFooter();
});

document.getElementById('search').addEventListener('input', render);

async function api(url, body) {
  const opt = { method: body ? 'POST' : 'GET' };
  if (body) { opt.headers = {'Content-Type':'application/json'}; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  return r.json();
}

async function loadManifest() {
  manifest = await api('/api/manifest');
  if (manifest && manifest.creators) render(); else await rescan();
}

async function rescan() {
  document.getElementById('main').innerHTML = '<div class="progress">스캔 중...</div>';
  manifest = await api('/api/scan', {});
  marks.clear();
  updateFooter();
  render();
  toast('스캔 완료: ' + (manifest.total_pkgs||0) + '개');
}

async function performDelete() {
  if (!marks.size) return toast('선택된 항목이 없다');
  if (!confirm(marks.size + '개 파일을 휴지통으로 이동한다. 계속?')) return;
  const res = await api('/api/delete', { paths: [...marks] });
  toast((res.moved?.length || 0) + '개 이동됨' + (res.failed?.length ? (', ' + res.failed.length + '개 실패') : ''));
  marks.clear();
  await rescan();
}

async function openTrash() {
  const dlg = document.getElementById('trash-dialog');
  const data = await api('/api/trash');
  document.getElementById('trash-summary').textContent =
    data.items.length + '개 · ' + fmtSize(data.total_size);
  document.getElementById('trash-list').innerHTML = data.items.map(it =>
    '<label class="trash-item"><input type="checkbox" value="' + escapeAttr(it.path) + '">'
    + '<span style="flex:1;">' + escapeHtml(it.path) + '</span>'
    + '<span style="color:#888;">' + fmtSize(it.size) + '</span></label>'
  ).join('') || '<div style="padding:20px; text-align:center; color:#999;">휴지통 비어있음</div>';
  dlg.showModal();
}

async function restoreSelected() {
  const boxes = document.querySelectorAll('#trash-list input:checked');
  if (!boxes.length) return toast('선택된 항목이 없다');
  const paths = [...boxes].map(b => b.value);
  const res = await api('/api/restore', { paths });
  toast((res.restored?.length || 0) + '개 복원됨');
  document.getElementById('trash-dialog').close();
  await rescan();
}

async function emptyTrash() {
  if (!confirm('휴지통을 완전히 비운다. 되돌릴 수 없다. 계속?')) return;
  const res = await api('/api/empty-trash', {});
  toast(res.count + '개 완전 삭제됨 (' + fmtSize(res.size_freed) + ' 확보)');
  document.getElementById('trash-dialog').close();
}

loadManifest();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return  # 조용히

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0:
            return {}
        raw = self.rfile.read(n).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/" or p == "/index.html":
            self._bytes(HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if p.startswith("/thumb/"):
            name = unquote(p[len("/thumb/"):])
            fp = THUMBS_DIR / name
            if fp.exists() and fp.is_file():
                self._bytes(fp.read_bytes(), "image/jpeg")
            else:
                self.send_error(404)
            return
        if p == "/api/manifest":
            m = load_manifest() or {"creators": [], "total_pkgs": 0, "total_thumbs": 0}
            self._json(m)
            return
        if p == "/api/trash":
            self._json(list_trash())
            return
        self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path
        body = self._read_body()
        if p == "/api/scan":
            self._json(scan_cc())
            return
        if p == "/api/delete":
            self._json(move_to_trash(body.get("paths", [])))
            return
        if p == "/api/restore":
            self._json(restore_from_trash(body.get("paths", [])))
            return
        if p == "/api/empty-trash":
            self._json(empty_trash())
            return
        self.send_error(404)


# ─────────── 런처 ───────────

LAUNCHER_NAME = "CC Manager.command"


def ensure_launcher():
    launcher = Path(__file__).parent / LAUNCHER_NAME
    if launcher.exists():
        return
    launcher.write_text(
        '#!/bin/bash\n'
        '# Sims 4 CC Manager 실행기 — 이 파일을 Finder에서 더블클릭하면 실행됨\n'
        'cd "$(dirname "$0")"\n'
        'python3 sims_cc_manager.py\n'
    )
    os.chmod(launcher, 0o755)


def main():
    ensure_launcher()
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"[+] CC Manager 서버 시작: {url}")
    print(f"[i] Mods: {MODS}")
    print(f"[i] 휴지통: {TRASH_DIR}")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[i] 종료")
        server.server_close()


if __name__ == "__main__":
    main()
