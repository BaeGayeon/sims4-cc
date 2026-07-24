#!/usr/bin/env python3
"""
Sims 4 CC Manager (v0.1)
- CC FeaturedCreators 폴더 스캔
- .package 파일에서 CAS 썸네일(JPEG) 추출
- 창작자별로 그룹핑된 정적 HTML 갤러리 생성 (/tmp/cc_gallery/index.html)
- 브라우저에서 지울 항목 체크 → delete_cc.sh 스크립트 내보내기

아직 서버 없음. 스크립트 실행 → HTML 열기 → 체크 → 스크립트 저장 → 터미널에서 실행.
"""
import os
import struct
import zlib
import hashlib
import html
import webbrowser
from pathlib import Path

# ─────────── 경로 설정 ───────────
SIMS_ROOT = Path.home() / "Games" / "Electronic Arts" / "The Sims 4"
MODS = SIMS_ROOT / "Mods"
CC_ROOT = MODS / "CC FeaturedCreators"

OUT_DIR = Path("/tmp/cc_gallery")
THUMBS_DIR = OUT_DIR / "thumbs"
INDEX_HTML = OUT_DIR / "index.html"

CAS_THUMB_TYPE = 0x3C1AF1F2


# ─────────── DBPF v2 파서 ───────────

def parse_dbpf(path):
    """DBPF v2 파일의 인덱스 엔트리를 순회한다."""
    try:
        with open(path, "rb") as f:
            hdr = f.read(96)
            if hdr[:4] != b"DBPF":
                return
            if struct.unpack("<I", hdr[4:8])[0] != 2:
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
                    yield {
                        "type": t,
                        "instance": (ihi << 32) | ilo,
                        "offset": offset,
                        "size": size,
                        "compression": compression,
                    }
                except struct.error:
                    return
    except OSError:
        return


def read_resource(path, entry):
    """엔트리의 raw 데이터를 읽고 필요하면 zlib 해제."""
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
    """패키지에서 중복 제거된 CAS 썸네일들을 (jpg_bytes, md5) 로 yield."""
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
        yield data, h


# ─────────── 스캔 ───────────

def scan():
    """CC FeaturedCreators 아래를 순회, 창작자(하위 폴더)별로 아이템 리스트 반환."""
    if not CC_ROOT.exists():
        print(f"[!] 대상 폴더가 없다: {CC_ROOT}")
        return []

    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    creators = {}
    total_pkgs = 0
    total_thumbs = 0

    for pkg in sorted(CC_ROOT.rglob("*.package")):
        rel = pkg.relative_to(CC_ROOT)
        creator = rel.parts[0] if len(rel.parts) > 1 else "(최상위)"
        total_pkgs += 1
        if total_pkgs % 100 == 0:
            print(f"    ... {total_pkgs}개 처리 중 ({creator})")

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
            "abs_path": str(pkg),
            "size": pkg.stat().st_size,
            "thumbs": thumbs,
        })

    print(f"[+] 스캔 완료: {total_pkgs}개 패키지, {total_thumbs}개 썸네일")
    return sorted(
        [{"name": name, "items": sorted(items, key=lambda x: x["file"])}
         for name, items in creators.items()],
        key=lambda c: c["name"].lower(),
    )


# ─────────── HTML 생성 ───────────

def _fmt_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


PAGE_CSS = """
body { font-family: -apple-system, sans-serif; margin: 0; background: #f5f5f5; }
header { position: sticky; top: 0; background: white; padding: 12px 20px; border-bottom: 1px solid #ddd; z-index: 10; }
header h1 { margin: 0 0 4px; font-size: 18px; }
header .stats { color: #666; font-size: 12px; }
main { padding: 16px; padding-bottom: 80px; }
.creator { background: white; margin-bottom: 16px; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.creator h2 { margin: 0; padding: 12px 16px; background: #fafafa; border-bottom: 1px solid #eee; font-size: 15px; }
.grid { padding: 12px; display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
.item { position: relative; background: #fafafa; border: 2px solid transparent; border-radius: 4px; overflow: hidden; cursor: pointer; }
.item.marked { border-color: #d9534f; }
.item.marked img { filter: grayscale(0.6) brightness(0.7); }
.item img, .no-thumb { display: block; width: 100%; height: auto; }
.no-thumb { padding: 40px 8px; text-align: center; color: #999; font-size: 11px; background: #eee; }
.item .name { padding: 4px 6px; font-size: 10px; color: #444; word-break: break-all; line-height: 1.3; }
.item .sz { position: absolute; top: 4px; left: 4px; background: rgba(0,0,0,.6); color: white; padding: 1px 5px; font-size: 10px; border-radius: 3px; }
.item input { position: absolute; top: 4px; right: 4px; z-index: 2; transform: scale(1.3); }
#footer { position: fixed; bottom: 0; left: 0; right: 0; background: white; border-top: 1px solid #ddd; padding: 10px 20px; display: flex; align-items: center; gap: 12px; }
#footer .count { flex: 1; font-size: 14px; }
button { padding: 8px 14px; font-size: 13px; background: #d9534f; color: white; border: none; border-radius: 4px; cursor: pointer; }
button:hover { background: #c9302c; }
"""

PAGE_JS = """
const marks = new Set();
function toggle(el) {
  const path = el.dataset.path;
  const cb = el.querySelector('input');
  if (marks.has(path)) { marks.delete(path); el.classList.remove('marked'); cb.checked = false; }
  else { marks.add(path); el.classList.add('marked'); cb.checked = true; }
  document.getElementById('cnt').textContent = marks.size;
}
function exportScript() {
  if (marks.size === 0) { alert('선택된 항목이 없다.'); return; }
  const lines = ['#!/bin/bash',
                 '# Sims 4 CC 삭제 스크립트 (' + marks.size + '개)',
                 '# 실행 전에 반드시 백업하세요!',
                 'set -e', ''];
  marks.forEach(p => lines.push('rm -v ' + JSON.stringify(p)));
  const blob = new Blob([lines.join('\\n') + '\\n'], {type: 'text/x-sh'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'delete_cc.sh';
  a.click();
  URL.revokeObjectURL(a.href);
}
document.addEventListener('click', e => {
  const it = e.target.closest('.item');
  if (!it) return;
  if (e.target.tagName === 'INPUT') return;
  toggle(it);
});
"""


def build_html(creators):
    total_items = sum(len(c["items"]) for c in creators)
    total_size = sum(it["size"] for c in creators for it in c["items"])

    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<title>Sims 4 CC Gallery</title>",
        f"<style>{PAGE_CSS}</style></head><body>",
        "<header>",
        "<h1>🎮 Sims 4 CC Gallery</h1>",
        f"<div class='stats'>{len(creators)}명 창작자 · {total_items}개 패키지 · {_fmt_size(total_size)}</div>",
        "</header>",
        "<main>",
    ]

    for c in creators:
        parts.append("<div class='creator'>")
        parts.append(
            f"<h2>{html.escape(c['name'])} "
            f"<span style='color:#888; font-size:12px;'>({len(c['items'])})</span></h2>"
        )
        parts.append("<div class='grid'>")
        for it in c["items"]:
            path_attr = html.escape(it["abs_path"], quote=True)
            fname = html.escape(it["file"])
            size = _fmt_size(it["size"])
            if it["thumbs"]:
                img_html = f"<img src='thumbs/{it['thumbs'][0]}' loading='lazy'>"
            else:
                img_html = "<div class='no-thumb'>썸네일 없음</div>"
            parts.append(
                f"<div class='item' data-path='{path_attr}'>"
                f"<input type='checkbox'>"
                f"<span class='sz'>{size}</span>"
                f"{img_html}"
                f"<div class='name' title='{fname}'>{fname}</div>"
                f"</div>"
            )
        parts.append("</div></div>")

    parts.append("</main>")
    parts.append(
        "<div id='footer'>"
        "<div class='count'>지울 항목: <b id='cnt'>0</b>개</div>"
        "<button onclick='exportScript()'>💾 delete_cc.sh 저장</button>"
        "</div>"
    )
    parts.append(f"<script>{PAGE_JS}</script>")
    parts.append("</body></html>")
    return "".join(parts)


# ─────────── 메인 ───────────

def main():
    print(f"[i] 대상 폴더: {CC_ROOT}")
    creators = scan()
    if not creators:
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_HTML.write_text(build_html(creators), encoding="utf-8")
    print(f"[+] 갤러리 생성: {INDEX_HTML}")
    print("[i] 브라우저에서 여는 중...")
    webbrowser.open(f"file://{INDEX_HTML}")


if __name__ == "__main__":
    main()
