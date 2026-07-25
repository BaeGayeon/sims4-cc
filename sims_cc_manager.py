#!/usr/bin/env python3
"""
Sims 4 CC Manager
- CC 폴더 스캔해서 썸네일 추출
- 로컬 웹서버로 갤러리 제공
- 브라우저에서 클릭 → 실제 삭제 (안전: 휴지통 폴더로 이동)
- 재스캔, 복원, 통계 지원
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
SIMS_ROOT = Path("/Users/bae/Games/Electronic Arts/The Sims 4")
MODS = SIMS_ROOT / "Mods"
CC_ROOT = MODS / "CC FeaturedCreators"
# 앱 캐시는 Sims 폴더 밖(macOS 표준 위치)에 저장 - 게임 데이터 오염 방지
APP_STATE = Path.home() / "Library" / "Application Support" / "Sims4CCManager"
THUMBS_DIR = APP_STATE / "thumbs"
MANIFEST_PATH = APP_STATE / "manifest.json"
# 휴지통은 Mods 안(빠른 이동/복원 위해) 유지, 단 dot-prefix라 심즈4가 무시함
TRASH_DIR = MODS / ".cc_trash"

# 옛 위치 → 새 위치 마이그레이션
_OLD_STATE = SIMS_ROOT / ".cc_manager"
if _OLD_STATE.exists() and not APP_STATE.exists():
    APP_STATE.parent.mkdir(parents=True, exist_ok=True)
    import shutil as _shutil
    _shutil.move(str(_OLD_STATE), str(APP_STATE))

APP_STATE.mkdir(parents=True, exist_ok=True)
THUMBS_DIR.mkdir(exist_ok=True)
TRASH_DIR.mkdir(exist_ok=True)

CAS_THUMB_TYPE = 0x3C1AF1F2
BUILDBUY_THUMB_TYPE = 0x0D338A3A
CASP_TYPE = 0x034AEECB

PORT = 8765

# CASP 내부 이름 → 카테고리 매핑 (심즈4 스튜디오 규칙)
# 예: "Hezeh_ymHair_..." 에서 "Hair" 추출
CASP_TYPE_MAP = {
    "hair":         ("헤어", "💇"),
    "hat":          ("모자", "🎩"),
    "head":         ("헤어", "💇"),  # 대부분 헤어 관련
    "top":          ("상의", "👕"),
    "body":         ("상의", "👕"),
    "shirt":        ("상의", "👕"),
    "bottom":       ("하의", "👖"),
    "pants":        ("하의", "👖"),
    "skirt":        ("하의", "👖"),
    "full":         ("전신", "👗"),
    "fullbody":     ("전신", "👗"),
    "outfit":       ("전신", "👗"),
    "dress":        ("전신", "👗"),
    "shoes":        ("신발", "👟"),
    "boots":        ("신발", "👟"),
    "sock":         ("신발", "👟"),
    "socks":        ("신발", "👟"),
    "skin":         ("스킨", "🧑"),
    "skintone":     ("스킨", "🧑"),
    "skindetail":   ("스킨디테일", "✨"),
    "freckledetail":("스킨디테일", "✨"),
    "eyes":         ("렌즈", "👁️"),
    "eye":          ("렌즈", "👁️"),
    "eyecolor":     ("렌즈", "👁️"),
    "eyebrow":      ("눈썹", "👁️‍🗨️"),
    "eyebrows":     ("눈썹", "👁️‍🗨️"),
    "eyelash":      ("눈속눈썹", "😊"),
    "eyelashes":    ("눈속눈썹", "😊"),
    "lips":         ("입술", "💋"),
    "lip":          ("입술", "💋"),
    "lipstick":     ("입술", "💋"),
    "lipgloss":     ("입술", "💋"),
    "blush":        ("메이크업", "💄"),
    "eyeliner":     ("메이크업", "💄"),
    "eyeshadow":    ("메이크업", "💄"),
    "mascara":      ("메이크업", "💄"),
    "makeup":       ("메이크업", "💄"),
    "facemakeup":   ("메이크업", "💄"),
    "eyemakeup":    ("메이크업", "💄"),
    "facepaint":    ("스킨디테일", "✨"),
    "contour":      ("메이크업", "💄"),
    "beard":        ("수염", "🧔"),
    "facialhair":   ("수염", "🧔"),
    "mustache":     ("수염", "🧔"),
    "necklace":     ("목걸이", "📿"),
    "neck":         ("목걸이", "📿"),
    "earring":      ("귀걸이", "💎"),
    "earrings":     ("귀걸이", "💎"),
    "ears":         ("귀걸이", "💎"),
    "ring":         ("반지", "💍"),
    "rings":        ("반지", "💍"),
    "ringl":        ("반지", "💍"),
    "ringr":        ("반지", "💍"),
    "bracelet":     ("팔찌/시계", "⌚"),
    "wrist":        ("팔찌/시계", "⌚"),
    "wristl":       ("팔찌/시계", "⌚"),
    "wristr":       ("팔찌/시계", "⌚"),
    "watch":        ("팔찌/시계", "⌚"),
    "forearm":      ("팔찌/시계", "⌚"),
    "forearml":     ("팔찌/시계", "⌚"),
    "forearmr":     ("팔찌/시계", "⌚"),
    "glasses":      ("안경", "👓"),
    "eyewear":      ("안경", "👓"),
    "sunglasses":   ("안경", "👓"),
    "bag":          ("가방", "👜"),
    "handbag":      ("가방", "👜"),
    "backpack":     ("가방", "👜"),
    "tattoo":       ("문신", "🎨"),
    "acc":          ("액세서리", "🎀"),
    "accessory":    ("액세서리", "🎀"),
    "tights":       ("신발", "👟"),
    "gloves":       ("액세서리", "🎀"),
    "teeth":        ("스킨디테일", "✨"),
    "nail":         ("액세서리", "🎀"),
    "nails":        ("액세서리", "🎀"),
}

# 내부 이름 패턴: 예) "Hezeh_ymHair_...", "obscurus_yuSkinDetail_..."
#   중간에 [소문자 1-3자][대문자시작+영문자] 패턴이 카테고리
import re
_CASP_NAME_RE = re.compile(r'[_-][a-z]{1,3}([A-Z][a-zA-Z]+?)[_0-9-]')


def extract_casp_type(name):
    """CASP 내부 이름에서 파트 타입 추출. 예: 'Hair', 'Bottom', 'SkinDetail'."""
    if not name:
        return None
    m = _CASP_NAME_RE.search(name)
    if m:
        return m.group(1)
    # 대체: 이름 안 어디에든 알려진 키워드 있는지
    lower = name.lower()
    for key in CASP_TYPE_MAP:
        if key in lower:
            return key
    return None

# ─────────── 카테고리 휴리스틱 ───────────
CATEGORIES = [
    # (표시 이름, 아이콘, 키워드 목록 [소문자 부분 매칭])
    ("헤어",       "💇", ["hair_", "hair no", "hair.", "hairstyle", "머리", "wig", "hair(", " hair", "hairline", "hairs_", "_hair", "untied", "tail_", "tails", "-tail"]),
    ("눈썹",       "👁️‍🗨️", ["eyebrow", "brow_", " brow", "eye brow"]),
    ("눈속눈썹",   "😊", ["eyelash", "lashes"]),
    # 메이크업을 먼저 (eyeliner/eyeshadow 등이 눈 카테고리로 잘못 가지 않게)
    ("입술",       "💋", ["lipstick", "lipgloss", "립스틱", "lip_", "lips_", "_lip", "_lips", " lip ", " lips", "lip("]),
    ("메이크업",   "💄", ["blush", "eyeliner", "eyeshadow", "mascara", "makeup", "liner_", " liner"]),
    # 눈(렌즈)는 아이라이너/섀도우 제외한 진짜 렌즈만
    ("렌즈",   "👁️", ["eyes_", "_eyes", "eye12", "eye ", "eyes.", "eye.", "eyes(", "eye(", "iris", "eyeball", "contact_lens", "contact lens", "sclera", "waterblue", "gpme-gold", "_lens"]),
    ("스킨",       "🧑", ["skintone", "skin_n", "skinoverlay", "skin overlay", "skin(", "_skin", "faceskin", "face skin"]),
    ("스킨디테일", "✨", ["skindetail", "skin detail", "freckle", "mole", "philtrum", "nose_mask", "nosemask", "contour", "dimple", "eyebag", "spot", "facepaint", "facemask", "face mask", "face_mask", "mouthcorner", "mouth details", "mouthdetail"]),
    ("수영복",     "👙", ["swim", "bikini", "swimsuit", "monokini", "swimwear"]),
    ("잠옷",       "🛌", ["pajama", "pyjama", "sleepwear", "nightgown", "nightwear"]),
    ("속옷",       "🩲", ["underwear", "bra_", " bra ", "panties", "thong", "boxer", "brief_"]),
    ("상의",       "👕", ["top(", " top ", "_top", "top.", "shirt", "sweater", "hoodie", "hoody", "sweat_top", "tee_", "tee(", "tee.", "tank", "blouse", "jacket", "coat", "cardigan", "knit", "turtle", "polo", "vest", "상의", "suits", " suit ", "suit_", "_suit"]),
    ("하의",       "👖", ["pants", "jean", "denim", "short(", "shorts", "skirt", "trouser", "legging", "slack", "sweatpants", "sweat pants", "치마", "바지", "culotte", "capri"]),
    ("전신",       "👗", ["dress", "outfit", "jumpsuit", "romper", "fullbody", "full body", "onepiece", "one piece", "hanbok", "-jhsuit"]),
    ("신발",       "👟", ["shoes", "boot", "sneaker", "sandal", "heel", "loafer", "wedge", "slipper", "flat.", "socks", "sock_", "신발", "mule"]),
    ("귀걸이",     "💎", ["earring", "earing", "ear ring", "piercing"]),
    ("목걸이",     "📿", ["necklace", "choker", "pendant", "chain_", " chain "]),
    ("반지",       "💍", ["ring_", " ring ", "ring.", "rings"]),
    ("팔찌/시계",  "⌚", ["bracelet", "watch", "wrist"]),
    ("안경",       "👓", ["glass", "eyewear", "sunglass", "goggle"]),
    ("모자",       "🎩", ["hat_", "hat.", " hat", "cap.", "cap_", " cap", "beanie", "beret"]),
    ("가방",       "👜", ["handbag", "backpack", "purse", "hipbag", "shoulderbag", "chestbag", "waistbag", "totebag", "clutch bag"]),
    ("수염",       "🧔", ["beard", "mustache", "stubble", "goatee"]),
    ("문신",       "🎨", ["tattoo", "tatoo"]),
    ("액세서리",   "🎀", ["accessory", "s4accessory", "brooch"]),
    ("슬라이더/프리셋", "🎚️", ["slider", "preset"]),
]


META_CATEGORIES = {
    "옷":       ("👔", ["상의", "하의", "전신", "잠옷", "속옷", "수영복"]),
    "헤어":     ("💇", ["헤어"]),
    "얼굴":     ("😊", ["눈썹", "눈속눈썹", "렌즈", "입술", "메이크업", "수염"]),
    "스킨":     ("🧑", ["스킨", "스킨디테일"]),
    "액세서리": ("💎", ["귀걸이", "목걸이", "반지", "팔찌/시계", "안경", "모자", "가방", "액세서리", "문신"]),
    "신발":     ("👟", ["신발"]),
    "유틸":     ("🎚️", ["슬라이더/프리셋"]),
    "기타":     ("❓", ["기타"]),
}


def categorize(filename, size=0):
    lower = filename.lower()
    matches = []
    for name, icon, keywords in CATEGORIES:
        for kw in keywords:
            if kw in lower:
                matches.append((name, icon))
                break
    if not matches:
        # 폴백 휴리스틱: 파일 크기·이름 힌트
        if size and size < 100 * 1024 and any(k in lower for k in ["slider", "preset"]):
            return [("슬라이더/프리셋", "🎚️")]
        return [("기타", "❓")]
    return matches

# ─────────── DBPF 파서 ───────────

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


def parse_casp_name(data):
    """CASP 리소스에서 이름 문자열만 추출. 실패 시 None."""
    try:
        if len(data) < 14:
            return None
        version = struct.unpack_from("<I", data, 0)[0]
        preset_count = struct.unpack_from("<I", data, 8)[0]
        if preset_count > 0:
            return None  # 프리셋 있는 CASP는 skip
        # uint16 length (bytes) + UTF-16 LE string
        name_len = struct.unpack_from("<H", data, 12)[0]
        if name_len < 4 or name_len > 500 or 14 + name_len > len(data):
            return None
        name = data[14:14+name_len].decode("utf-16-le", errors="replace")
        return name
    except (struct.error, IndexError):
        return None


def get_casp_category(pkg_path):
    """패키지의 첫 CASP를 파싱해서 카테고리 반환. 없으면 None."""
    for entry in parse_dbpf(pkg_path):
        if entry["type"] != CASP_TYPE:
            continue
        try:
            data = read_resource(pkg_path, entry)
        except Exception:
            continue
        name = parse_casp_name(data)
        if not name:
            continue
        casp_type = extract_casp_type(name)
        if casp_type:
            lower_type = casp_type.lower()
            if lower_type in CASP_TYPE_MAP:
                return CASP_TYPE_MAP[lower_type]
    return None


def extract_thumbs(pkg_path):
    seen = set()
    for entry in parse_dbpf(pkg_path):
        if entry["type"] not in (CAS_THUMB_TYPE, BUILDBUY_THUMB_TYPE):
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

IGNORED_DIRS = {".cc_trash", "TMEX-Settings", "Tmex-Settings", ".git", "__MACOSX"}
IGNORED_DIR_PREFIXES = ("GSM ",)  # 스크립트 모드 폴더 (mccc, ww 등 내부는 CC 아님)


def _iter_packages():
    """Mods 아래 모든 .package 파일 순회 (제외 규칙 적용)."""
    if not MODS.exists():
        return
    for root, dirs, files in os.walk(MODS):
        # 제외 폴더 정리
        dirs[:] = [
            d for d in dirs
            if d not in IGNORED_DIRS
            and not any(d.startswith(p) for p in IGNORED_DIR_PREFIXES)
        ]
        for f in files:
            if f.lower().endswith(".package"):
                yield Path(root) / f


def scan_cc(progress_cb=None):
    """전체 Mods 폴더 스캔 → 폴더 경로별 그룹."""
    if not MODS.exists():
        return {"folders": [], "error": f"Mods 폴더 없음: {MODS}"}

    # 폴더별 파일 그룹핑 - key는 Mods 기준 상대 폴더 경로
    from collections import defaultdict
    groups = defaultdict(list)
    all_pkgs = list(_iter_packages())
    total_pkgs = len(all_pkgs)
    total_thumbs = 0
    overrides = _load_overrides()

    for i, pkg in enumerate(all_pkgs, 1):
        rel_pkg = pkg.relative_to(MODS)
        folder = str(rel_pkg.parent) if rel_pkg.parent != Path(".") else "(최상위)"
        if progress_cb and (i % 100 == 0 or i == total_pkgs):
            progress_cb(i, total_pkgs, folder)

        # 카테고리 결정
        # 1) 수동 오버라이드 우선
        rel_pkg_str = str(rel_pkg)
        override_cat = overrides.get(rel_pkg_str)
        if override_cat:
            # 오버라이드 카테고리에 해당하는 아이콘 찾기
            icon = "📝"
            for name, ic, _ in CATEGORIES:
                if name == override_cat:
                    icon = ic; break
            cats = [(override_cat, icon)]
            is_override = True
            is_casp = False
        else:
            is_override = False
            casp_cat = get_casp_category(pkg)
            if casp_cat:
                cats = [casp_cat]
                is_casp = True
            else:
                cats = categorize(pkg.name, pkg.stat().st_size)
                is_casp = False
            # 파일명에서 추가 카테고리도 병합 (필터용, primary는 첫 번째만)
            fn_cats = categorize(pkg.name, pkg.stat().st_size)
            for fc in fn_cats:
                if fc[0] not in [c[0] for c in cats] and fc[0] != "기타":
                    cats.append(fc)

        item = {
            "file": pkg.name,
            "path": rel_pkg_str,
            "size": pkg.stat().st_size,
            "thumbs": [],
            "cats": [c[0] for c in cats],
            "primary_cat": cats[0][0],
            "cat_icon": cats[0][1],
            "casp": is_casp,
            "override": is_override,
        }
        for jpg_bytes, h in extract_thumbs(pkg):
            thumb_name = f"{h[:16]}.jpg"
            thumb_path = THUMBS_DIR / thumb_name
            if not thumb_path.exists():
                thumb_path.write_bytes(jpg_bytes)
            item["thumbs"].append(thumb_name)
            total_thumbs += 1
        groups[folder].append(item)

    folders = [{"name": name, "items": sorted(items, key=lambda x: x["file"])}
               for name, items in sorted(groups.items())]

    manifest = {
        "folders": folders,
        "creators": folders,  # 하위호환 alias
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


# ─────────── 파일 조작 ───────────

TRASH_MANIFEST_PATH = APP_STATE / "trash_manifest.json"
OVERRIDES_PATH = APP_STATE / "category_overrides.json"


def _load_overrides():
    if OVERRIDES_PATH.exists():
        try:
            return json.loads(OVERRIDES_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_overrides(m):
    OVERRIDES_PATH.write_text(json.dumps(m, ensure_ascii=False, indent=2))


def set_category_override(rel_path, category):
    """수동으로 카테고리 지정 (또는 해제)"""
    overrides = _load_overrides()
    if category is None or category == "":
        overrides.pop(rel_path, None)
    else:
        overrides[rel_path] = category
    _save_overrides(overrides)
    return {"ok": True}


def _load_trash_manifest():
    if TRASH_MANIFEST_PATH.exists():
        try:
            return json.loads(TRASH_MANIFEST_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_trash_manifest(m):
    TRASH_MANIFEST_PATH.write_text(json.dumps(m, ensure_ascii=False))


def _find_thumbs_for(path):
    """현재 manifest에서 이 파일의 썸네일 목록 찾기."""
    m = load_manifest()
    if not m:
        return []
    for c in m.get("creators", []):
        for it in c.get("items", []):
            if it["path"] == path:
                return it.get("thumbs", [])
    return []


def move_to_trash(rel_paths):
    moved, failed = [], []
    trash_m = _load_trash_manifest()
    for rel in rel_paths:
        src = MODS / rel
        if not src.exists():
            failed.append({"path": rel, "reason": "파일 없음"})
            continue
        # 썸네일 정보 저장 (삭제 전에)
        thumbs = _find_thumbs_for(rel)
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
            trash_rel = str(dst.relative_to(TRASH_DIR))
            trash_m[trash_rel] = {"original_path": rel, "thumbs": thumbs, "size": dst.stat().st_size}
            moved.append(rel)
        except Exception as e:
            failed.append({"path": rel, "reason": str(e)})
    _save_trash_manifest(trash_m)
    return {"moved": moved, "failed": failed}


def restore_from_trash(rel_paths):
    restored, failed = [], []
    trash_m = _load_trash_manifest()
    for rel in rel_paths:
        src = TRASH_DIR / rel
        info = trash_m.get(rel, {})
        original = info.get("original_path", rel)  # 원본 경로 있으면 그리로 복원
        dst = MODS / original
        if not src.exists():
            failed.append({"path": rel, "reason": "휴지통에 없음"})
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
            trash_m.pop(rel, None)
            restored.append(rel)
        except Exception as e:
            failed.append({"path": rel, "reason": str(e)})
    _save_trash_manifest(trash_m)
    return {"restored": restored, "failed": failed}


def list_trash():
    items = []
    total_size = 0
    trash_m = _load_trash_manifest()
    dirty = False
    for f in TRASH_DIR.rglob("*"):
        if f.is_file():
            rel = str(f.relative_to(TRASH_DIR))
            sz = f.stat().st_size
            info = trash_m.get(rel, {})
            thumbs = info.get("thumbs", [])
            # 트래시 매니페스트에 썸네일 정보 없으면 파일에서 직접 뽑기
            if not thumbs and f.suffix.lower() == ".package":
                extracted = []
                for jpg_bytes, h in extract_thumbs(f):
                    thumb_name = f"{h[:16]}.jpg"
                    thumb_path = THUMBS_DIR / thumb_name
                    if not thumb_path.exists():
                        thumb_path.write_bytes(jpg_bytes)
                    extracted.append(thumb_name)
                thumbs = extracted
                # 매니페스트 업데이트
                trash_m[rel] = {
                    "original_path": info.get("original_path", rel),
                    "thumbs": thumbs,
                    "size": sz,
                }
                dirty = True
            items.append({
                "path": rel,
                "size": sz,
                "thumbs": thumbs,
                "original_path": info.get("original_path", rel),
            })
            total_size += sz
    if dirty:
        _save_trash_manifest(trash_m)
    return {"items": items, "total_size": total_size}


def delete_from_trash(rel_paths):
    """휴지통 안의 특정 파일들만 완전 삭제"""
    count = 0
    total = 0
    trash_m = _load_trash_manifest()
    for rel in rel_paths:
        fp = TRASH_DIR / rel
        if fp.exists() and fp.is_file():
            try:
                total += fp.stat().st_size
                fp.unlink()
                count += 1
                trash_m.pop(rel, None)
            except OSError:
                pass
    _save_trash_manifest(trash_m)
    return {"count": count, "size_freed": total}


def empty_trash():
    count = 0
    total = 0
    for f in TRASH_DIR.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
            f.unlink()
            count += 1
    # 빈 폴더 제거
    for d in sorted([d for d in TRASH_DIR.rglob("*") if d.is_dir()], key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
        except OSError:
            pass
    return {"count": count, "size_freed": total}


# ─────────── HTTP 서버 ───────────

HTML_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Sims 4 CC Manager</title>
<link rel="stylesheet" as="style" crossorigin href="https://cdn.jsdelivr.net/gh/sun-typeface/SUITE/fonts/static/woff2/SUITE.css">
<style>
  * { box-sizing: border-box; }
  body { font-family: 'SUITE Variable', 'SUITE', -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; background: #f5f5f5; color: #222; }
  header { position: sticky; top: 0; background: white; border-bottom: 1px solid #ddd; padding: 8px 16px; z-index: 100; box-shadow: 0 2px 4px rgba(0,0,0,.05); }
  .title-row { display: flex; align-items: center; gap: 12px; }
  .title-row h1 { margin: 0; font-size: 16px; flex-shrink: 0; }
  .stats { color: #666; font-size: 12px; flex: 1; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; padding: 6px 0; border-top: 1px solid #f0f0f0; }
  .row:first-of-type { border-top: none; padding-top: 0; }
  .group { display: inline-flex; align-items: center; gap: 4px; background: #f7f7f7; padding: 3px 8px; border-radius: 6px; }
  .group .label { color: #666; font-size: 11px; margin: 0; }
  .divider { width: 1px; height: 20px; background: #ddd; margin: 0 4px; }
  .toggle-group { display: inline-flex; align-items: center; gap: 10px; }
  input[type=search] { padding: 6px 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 13px; min-width: 220px; }
  select { padding: 5px 8px; border: 1px solid #ccc; border-radius: 6px; font-size: 12px; background: white; }
  button { padding: 5px 10px; font-size: 12px; background: white; border: 1px solid #ccc; border-radius: 6px; cursor: pointer; }
  button:hover { background: #f4f4f4; }
  button.primary { background: #d9534f; color: white; border-color: #d43f3a; }
  button.primary:hover { background: #c9302c; }
  button.blue { background: #4a90e2; color: white; border-color: #357ab8; }
  button.blue:hover { background: #357ab8; }
  /* Segmented toggle */
  .seg { display: inline-flex; border: 1px solid #ccc; border-radius: 6px; overflow: hidden; }
  .seg button { border: none; border-radius: 0; padding: 5px 12px; background: white; font-size: 12px; }
  .seg button + button { border-left: 1px solid #ccc; }
  .seg button.on { background: #333; color: white; }
  /* 모드 토글은 특히 눈에 띄게 */
  #modeSeg { border: 2px solid #4a90e2; border-radius: 8px; padding: 2px; background: #eaf3fc; box-shadow: 0 0 0 2px rgba(74,144,226,.15); }
  #modeSeg button { padding: 6px 14px; font-size: 13px; font-weight: 600; border-radius: 5px; border-left: none !important; }
  #modeSeg button.on { background: #4a90e2; color: white; box-shadow: 0 1px 3px rgba(0,0,0,.15); }
  #modeSeg button:not(.on) { background: transparent; color: #4a90e2; }
  body.mode-category #modeSeg { border-color: #f0a500; background: #fff8e5; box-shadow: 0 0 0 2px rgba(240,165,0,.15); }
  body.mode-category #modeSeg button:not(.on) { color: #b8860b; }
  body.mode-category #modeSeg button.on { background: #f0a500; }
  /* Switch toggle */
  .switch { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: #444; cursor: pointer; user-select: none; }
  .switch input { appearance: none; -webkit-appearance: none; width: 32px; height: 18px; background: #ccc; border-radius: 10px; position: relative; cursor: pointer; transition: background .2s; margin: 0; }
  .switch input:checked { background: #4a90e2; }
  .switch input::before { content: ''; position: absolute; top: 2px; left: 2px; width: 14px; height: 14px; background: white; border-radius: 50%; transition: transform .2s; }
  .switch input:checked::before { transform: translateX(14px); }
  /* Chip filter */
  .chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .chip { padding: 4px 10px; border-radius: 14px; background: white; border: 1px solid #ddd; cursor: pointer; font-size: 12px; display: inline-flex; align-items: center; gap: 4px; }
  .chip:hover { background: #f4f4f4; }
  .chip.on { background: #4a90e2; color: white; border-color: #357ab8; }
  .chip .count { opacity: .7; font-size: 11px; }
  .subchips { padding-left: 16px; margin-top: 4px; padding-top: 4px; border-top: 1px dashed #e0e0e0; }
  .subchip { padding: 3px 8px; border-radius: 12px; background: #f4f4f4; border: 1px solid #e0e0e0; cursor: pointer; font-size: 11px; }
  .subchip:hover { background: #e8e8e8; }
  .subchip.on { background: #333; color: white; border-color: #333; }
  .label { font-size: 11px; color: #888; margin-right: 4px; }
  main { padding: 16px; }
  .creator { background: white; margin-bottom: 12px; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .creator-header { padding: 10px 16px; background: #fafafa; border-bottom: 1px solid #eee; display: flex; align-items: center; gap: 12px; cursor: pointer; user-select: none; }
  .creator-header h2 { margin: 0; font-size: 15px; flex: 1; }
  .creator-count { color: #666; font-size: 12px; }
  .creator-actions { display: flex; gap: 6px; }
  .creator-actions button { padding: 3px 8px; font-size: 11px; }
  .grid { padding: 10px; display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 6px; }
  .item { position: relative; border: 2px solid transparent; border-radius: 4px; cursor: pointer; background: #f9f9f9; overflow: visible; }
  .item .thumb-img, .item .no-thumb { border-radius: 2px 2px 0 0; overflow: hidden; }
  .item:hover { border-color: #4a90e2; z-index: 10; }
  .item.marked { border-color: #d9534f; background: #ffe8e8; box-shadow: 0 0 0 1px #d9534f inset; }
  .item.marked .thumb-img, .item.marked .no-thumb { filter: brightness(0.85) saturate(0.7); }
  .item.marked::after {
    content: '';
    position: absolute; top: 0; left: 0; right: 0; bottom: 0;
    background: linear-gradient(rgba(217,83,79,.15), rgba(217,83,79,.25));
    pointer-events: none; border-radius: 2px;
  }
  body.mode-category .item:not(.trashed):hover { border-color: #4a90e2; cursor: cell; }
  body.mode-category .item:not(.trashed).marked { opacity: 0.5; }
  .item.selected { border-color: #4a90e2 !important; box-shadow: 0 0 0 2px #4a90e2, 0 0 8px rgba(74,144,226,.5); }
  .item.selected::before { content: '✓'; position: absolute; top: 4px; left: 4px; background: #4a90e2; color: white; width: 20px; height: 20px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: bold; z-index: 3; }
  .chip.drop-target { background: #ffe066 !important; color: #333 !important; border-color: #f0a500 !important; transform: scale(1.1); }
  .item.dragging { opacity: 0.4; }
  #bulkBar { position: fixed; bottom: 44px; left: 0; right: 0; background: #333; color: white; padding: 8px 16px; z-index: 99; display: none; align-items: center; gap: 12px; box-shadow: 0 -2px 8px rgba(0,0,0,.2); }
  #footer { height: 44px; padding: 0 16px; }
  body { padding-bottom: 60px; }
  body.has-bulk { padding-bottom: 108px; }
  #bulkBar select { padding: 5px 8px; border-radius: 4px; }
  #bulkBar .info { flex: 1; font-size: 14px; }
  .item.trashed { opacity: 0.55; border-color: #999; background: #f0f0f0; }
  .item.trashed .thumb-img { filter: grayscale(0.6); }
  .trash-badge { position: absolute; top: 4px; left: 4px; background: rgba(0,0,0,.75); color: white; padding: 2px 6px; border-radius: 3px; font-size: 10px; }
  .cat-icon { position: absolute; bottom: 26px; right: 4px; background: rgba(255,255,255,.92); padding: 1px 5px; border-radius: 10px; font-size: 13px; box-shadow: 0 1px 2px rgba(0,0,0,.15); cursor: context-menu; z-index: 2; }
  .cat-icon.override { background: #ffd700; box-shadow: 0 0 0 1px #b8860b; }
  #ctxMenu { position: fixed; background: white; border: 1px solid #ccc; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,.15); padding: 4px; z-index: 1000; max-height: 400px; overflow-y: auto; min-width: 180px; display: none; }
  #ctxMenu .ctx-header { padding: 4px 8px; font-size: 11px; color: #666; border-bottom: 1px solid #eee; margin-bottom: 4px; }
  #ctxMenu .ctx-item { padding: 5px 10px; font-size: 13px; cursor: pointer; border-radius: 4px; display: flex; align-items: center; gap: 6px; }
  #ctxMenu .ctx-item:hover { background: #f4f4f4; }
  #ctxMenu .ctx-item.current { background: #e8f4ff; font-weight: 600; }
  #ctxMenu .ctx-item.reset { border-top: 1px solid #eee; margin-top: 4px; padding-top: 8px; color: #d9534f; }
  .creator-sub { padding: 0 6px 2px; font-size: 9px; color: #999; font-style: italic; }
  .item img { display: block; width: 100%; height: auto; }
  .item .no-thumb { padding: 40px 8px; text-align: center; color: #999; font-size: 11px; background: #eee; }
  .item .name { padding: 3px 6px; font-size: 10px; color: #555; word-break: break-all; line-height: 1.3; max-height: 2.6em; overflow: hidden; }
  /* 전체 파일명은 팝오버 - 레이아웃 흔들지 않음 */
  .item .name-full { display: none; position: absolute; top: 100%; left: 0; right: 0; margin-top: 2px; background: white; padding: 6px 8px; font-size: 11px; color: #222; word-break: break-all; line-height: 1.4; border-radius: 4px; box-shadow: 0 4px 12px rgba(0,0,0,.2); z-index: 200; pointer-events: none; }
  .item:hover .name-full { display: block; }
  .item .sz { position: absolute; top: 4px; left: 4px; background: rgba(0,0,0,.6); color: white; padding: 1px 5px; font-size: 10px; border-radius: 3px; pointer-events: none; z-index: 2; }
  .item.selected::before { left: 4px; }
  .item.selected .sz { display: none; }
  .thumb-img, .no-thumb { cursor: pointer; }
  .thumb-nav { position: absolute; top: 4px; left: 4px; right: 4px; display: flex; align-items: center; justify-content: space-between; opacity: 0; transition: opacity .15s; pointer-events: none; }
  .item:hover .thumb-nav { opacity: 1; pointer-events: auto; }
  .thumb-btn { background: rgba(0,0,0,.6); color: white; border: none; border-radius: 50%; width: 22px; height: 22px; font-size: 10px; padding: 0; cursor: pointer; display: flex; align-items: center; justify-content: center; }
  .thumb-btn:hover { background: rgba(0,0,0,.85); }
  .thumb-counter { background: rgba(0,0,0,.6); color: white; padding: 2px 6px; border-radius: 10px; font-size: 10px; font-variant-numeric: tabular-nums; }
  .collapsed .grid, .collapsed .creator-actions { display: none; }
  #footer { position: fixed; bottom: 0; left: 0; right: 0; background: white; border-top: 1px solid #ddd; display: flex; align-items: center; gap: 12px; z-index: 100; box-shadow: 0 -2px 4px rgba(0,0,0,.05); }
  #footer .count { flex: 1; font-size: 14px; }
  #footer .count b { color: #d9534f; }
  .toast { position: fixed; bottom: 90px; left: 50%; transform: translateX(-50%); background: #333; color: white; padding: 10px 20px; border-radius: 6px; opacity: 0; transition: opacity .3s; z-index: 200; }
  .toast.show { opacity: 1; }
  .badge { display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 10px; margin-left: 4px; }
  .badge.warn { background: #fff3cd; color: #856404; }
  .progress { background: #f0f0f0; padding: 20px; border-radius: 8px; margin: 40px auto; max-width: 500px; text-align: center; }
  .progress-bar { height: 8px; background: #ddd; border-radius: 4px; overflow: hidden; margin-top: 12px; }
  .progress-bar > div { height: 100%; background: #4a90e2; transition: width .3s; }
  dialog { border: none; border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,.2); padding: 20px; max-width: 500px; }
  dialog::backdrop { background: rgba(0,0,0,.5); }
  .trash-item { padding: 6px 8px; border-bottom: 1px solid #eee; display: flex; gap: 8px; font-size: 12px; align-items: center; }
  .trash-item input { margin: 0; }
</style>
</head><body>
<header>
  <div class="title-row">
    <h1>🎮 CC Manager</h1>
    <div class="stats" id="stats">로딩 중...</div>
    <div class="seg" id="modeSeg" title="선택 모드">
      <button data-v="delete" class="on">🗑️ 삭제 선택</button>
      <button data-v="category">🏷️ 카테고리 편집</button>
    </div>
    <button onclick="undo()" id="undoBtn" title="되돌리기 (Cmd+Z)" disabled>↶</button>
    <button onclick="rescan()" class="blue">🔄 재스캔</button>
    <button onclick="openTrash()">🗑️ 휴지통</button>
  </div>

  <div class="row">
    <input type="search" id="search" placeholder="🔍 파일명·창작자 검색" style="width: 260px;">
    <div class="group">
      <span class="label">그룹</span>
      <div class="seg" id="groupSeg">
        <button data-v="creator" class="on">폴더별</button>
        <button data-v="category">카테고리별</button>
      </div>
    </div>
    <div class="group">
      <span class="label">그룹정렬</span>
      <select id="sortBy">
        <option value="name">이름</option>
        <option value="size">크기</option>
        <option value="count">개수</option>
      </select>
    </div>
    <div class="group">
      <span class="label">아이템정렬</span>
      <select id="itemSortBy">
        <option value="name">이름</option>
        <option value="size">크기</option>
        <option value="category">카테고리</option>
      </select>
    </div>
  </div>

  <div class="row">
    <div class="toggle-group">
      <label class="switch"><input type="checkbox" id="tglMarked"> 🗑️ 지울 것만</label>
      <label class="switch"><input type="checkbox" id="tglOverride"> 🖐️ 수동 지정만</label>
      <label class="switch"><input type="checkbox" id="tglCollapsed"> 모두 접기</label>
    </div>
    <div class="divider"></div>
    <button onclick="selectAllFiltered()" title="지금 보이는 아이템 전부 다중선택에 추가">☑️ 전체 선택</button>
    <button onclick="clearBulkSel()" title="다중선택 해제">▢ 선택 해제</button>
    <div class="divider"></div>
    <button onclick="clearMarks()" title="삭제 표시 전부 해제">🗑️❌ 삭제표시 초기화</button>
  </div>

  <div class="row">
    <span class="label">카테고리</span>
    <div class="chips" id="metaChips" style="flex:1;"></div>
  </div>
  <div class="row" id="subRow" style="display:none;">
    <div class="chips subchips" id="subChips"></div>
  </div>
</header>
<main id="main"><div class="progress">로딩 중...</div></main>
<div id="footer">
  <div class="count" onclick="openMarkedDialog()" style="cursor:pointer; user-select:none;">
    삭제 표시: <b id="marked-count">0</b>개 · 절약 예상: <b id="marked-size">0 B</b>
    <span style="color:#888; font-size:12px; margin-left:8px;">▲ 목록 보기</span>
  </div>
  <button onclick="performDelete()" class="primary">🗑️ 휴지통으로 이동</button>
</div>
<dialog id="marked-dialog" style="max-width:720px; width:90vw;">
  <h3>🗑️ 삭제 표시된 아이템 <span id="marked-summary" style="font-weight:normal; color:#666; font-size:13px;"></span></h3>
  <div style="max-height:60vh; overflow-y:auto; margin:8px 0; border:1px solid #eee; border-radius:6px; padding:8px; background:#fafafa;">
    <div id="marked-list" style="display:grid; grid-template-columns:repeat(auto-fill, minmax(140px, 1fr)); gap:6px;"></div>
  </div>
  <div style="display:flex; gap:8px; justify-content:flex-end;">
    <button onclick="document.getElementById('marked-dialog').close()">닫기</button>
    <button onclick="clearMarksFromDialog()">전체 표시 해제</button>
    <button onclick="performDeleteFromDialog()" class="primary">🗑️ 휴지통으로 이동</button>
  </div>
</dialog>
<div id="toast" class="toast"></div>
<div id="ctxMenu"></div>
<div id="bulkBar">
  <span class="info">📌 <b id="bulkCount">0</b>개 선택됨</span>
  <select id="bulkCat"><option value="">카테고리 선택...</option></select>
  <button onclick="applyBulkCategory()" class="blue">✓ 카테고리 지정</button>
  <button onclick="clearBulkSel()">선택 해제</button>
</div>
<dialog id="failed-dialog" style="max-width: 600px; width: 90vw;">
  <h3>⚠️ 일부 파일 이동 실패</h3>
  <div id="failed-summary" style="color: #666; font-size: 13px; margin-bottom: 8px;"></div>
  <div style="max-height: 300px; overflow-y: auto; margin: 8px 0; border: 1px solid #eee; border-radius: 6px; padding: 8px; background: #fafafa;">
    <div id="failed-list"></div>
  </div>
  <div style="display: flex; gap: 8px; justify-content: flex-end;">
    <button onclick="clearFailedMarks()">실패한 것 표시 해제</button>
    <button onclick="document.getElementById('failed-dialog').close()">닫기</button>
  </div>
</dialog>
<dialog id="trash-dialog" style="max-width: 720px; width: 90vw;">
  <h3>휴지통 <span id="trash-summary" style="font-weight: normal; color: #666; font-size: 13px;"></span></h3>
  <div style="display: flex; gap: 8px; margin: 8px 0; align-items: center;">
    <button onclick="trashSelectAll(true)">전체 선택</button>
    <button onclick="trashSelectAll(false)">전체 해제</button>
    <span style="color:#666; font-size:12px;">선택: <b id="trash-selected-count">0</b>개</span>
  </div>
  <div style="max-height: 60vh; overflow-y: auto; margin: 8px 0; border: 1px solid #eee; border-radius: 6px; padding: 8px; background: #fafafa;">
    <div id="trash-list" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 6px;"></div>
  </div>
  <div style="display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap;">
    <button onclick="closeTrash()">닫기</button>
    <button onclick="restoreAll()" class="blue">↩️ 전체 복원</button>
    <button onclick="restoreSelected()" class="blue">↩️ 선택 복원</button>
    <button onclick="deleteSelectedFromTrash()" class="primary">🗑️ 선택 완전 삭제</button>
    <button onclick="emptyTrash()" class="primary">🗑️ 전체 완전 삭제</button>
  </div>
</dialog>
<script>
let MANIFEST = null;
const state = JSON.parse(localStorage.getItem('cc-marks') || '{}');
const bulkSel = new Set();  // 다중선택 (Cmd+클릭)
let currentFilter = '';
let showMarkedOnly = false;
let sortBy = 'name';
let itemSortBy = 'name';
let metaFilter = '';
let subFilter = '';
let groupBy = 'creator';
let collapsedMode = false;
let showOverrideOnly = false;
let editMode = 'delete';  // 'delete' | 'category'

function human(bytes) {
  const u = ['B','KB','MB','GB'];
  let i = 0, s = bytes;
  while (s >= 1024 && i < 3) { s /= 1024; i++; }
  return s.toFixed(1) + ' ' + u[i];
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2500);
}

async function loadManifest() {
  const res = await fetch('/api/manifest');
  MANIFEST = await res.json();
  // 표시된 것 중 이제 존재하지 않는 파일 제거
  const validPaths = new Set();
  for (const c of MANIFEST.creators)
    for (const it of c.items) validPaths.add(it.path);
  for (const k of Object.keys(state))
    if (!validPaths.has(k)) delete state[k];
  saveMarks();
  // 다중선택 dropdown 강제 재생성
  document.getElementById('bulkCat').innerHTML = '<option value="">카테고리 선택...</option>';
  render();
  updateBulkBar();
}

function saveMarks() {
  localStorage.setItem('cc-marks', JSON.stringify(state));
}

function creatorStats(c) {
  return {
    count: c.items.length,
    size: c.items.reduce((s, it) => s + it.size, 0),
  };
}

function populateCatFilter() {
  // 카테고리별 카운트
  const catCounts = {};
  for (const c of MANIFEST.creators)
    for (const it of c.items)
      for (const cat of (it.cats || ['기타']))
        catCounts[cat] = (catCounts[cat] || 0) + 1;

  // 대카테고리별 총 개수
  const meta = MANIFEST.meta_categories || {};
  const metaCounts = {};
  for (const [mname, mdata] of Object.entries(meta)) {
    let total = 0;
    for (const sub of mdata.subs) total += (catCounts[sub] || 0);
    metaCounts[mname] = total;
  }

  // 대카테고리 chip bar
  const metaBar = document.getElementById('metaChips');
  const metaHtml = [`<div class="chip ${metaFilter === '' ? 'on' : ''}" data-meta="">전체 <span class="count">${MANIFEST.total_pkgs}</span></div>`];
  for (const [mname, mdata] of Object.entries(meta)) {
    if (metaCounts[mname] === 0) continue;
    metaHtml.push(`<div class="chip ${metaFilter === mname ? 'on' : ''}" data-meta="${mname}">${mdata.icon} ${mname} <span class="count">${metaCounts[mname]}</span></div>`);
  }
  metaBar.innerHTML = metaHtml.join('');
  metaBar.querySelectorAll('.chip').forEach(el => {
    el.onclick = () => {
      const m = el.dataset.meta;
      if (metaFilter === m) { metaFilter = ''; subFilter = ''; }
      else { metaFilter = m; subFilter = ''; }
      render();
    };
  });

  // 소카테고리 chip bar (선택된 대카테고리에 여러 소카가 있을 때만 표시)
  const subRow = document.getElementById('subRow');
  const subBar = document.getElementById('subChips');
  if (metaFilter && meta[metaFilter] && meta[metaFilter].subs.length > 1) {
    subRow.style.display = 'flex';
    const subHtml = [`<div class="subchip ${subFilter === '' ? 'on' : ''}" data-sub="">전체</div>`];
    for (const sub of meta[metaFilter].subs) {
      const n = catCounts[sub] || 0;
      if (n === 0) continue;
      subHtml.push(`<div class="subchip ${subFilter === sub ? 'on' : ''}" data-sub="${sub}">${sub} <span style="opacity:.7">${n}</span></div>`);
    }
    subBar.innerHTML = subHtml.join('');
    subBar.querySelectorAll('.subchip').forEach(el => {
      el.onclick = () => {
        subFilter = subFilter === el.dataset.sub ? '' : el.dataset.sub;
        render();
      };
      // 드롭 타겟 - 아이템을 여기 놓으면 그 소카테고리로 지정
      const cat = el.dataset.sub;
      if (cat) {
        el.addEventListener('dragover', (e) => { e.preventDefault(); el.classList.add('drop-target'); });
        el.addEventListener('dragleave', () => el.classList.remove('drop-target'));
        el.addEventListener('drop', async (e) => {
          e.preventDefault();
          el.classList.remove('drop-target');
          try {
            const paths = JSON.parse(e.dataTransfer.getData('application/json'));
            await applyToPaths(paths, cat);
          } catch {}
        });
      }
    });
  } else {
    subRow.style.display = 'none';
  }
}

function matchesCatFilter(cats) {
  if (!metaFilter) return true;
  const meta = MANIFEST.meta_categories || {};
  const allowedSubs = meta[metaFilter]?.subs || [];
  if (subFilter) {
    return (cats || []).includes(subFilter);
  }
  return (cats || []).some(c => allowedSubs.includes(c));
}

function buildGroups() {
  const folders = MANIFEST.folders || MANIFEST.creators || [];
  if (groupBy === 'category') {
    const groups = {};
    for (const c of folders) {
      for (const it of c.items) {
        // 대표 카테고리 하나에만 배정 (중복 카운트 방지)
        const cat = it.primary_cat || (it.cats && it.cats[0]) || '기타';
        if (!groups[cat]) groups[cat] = { name: cat, items: [] };
        groups[cat].items.push({...it, _creator: c.name});
      }
    }
    return Object.values(groups);
  }
  return folders.map(c => ({...c}));
}

function render() {
  const main = document.getElementById('main');
  main.innerHTML = '';
  if (!MANIFEST || !MANIFEST.creators.length) {
    main.innerHTML = '<div class="progress">CC 없음. 재스캔을 눌러보세요.</div>';
    updateFooter();
    updateStats(0, 0);
    return;
  }
  populateCatFilter();
  let totalItems = 0, visibleItems = 0;
  const creators = buildGroups();
  creators.forEach(c => c._stats = creatorStats(c));
  if (sortBy === 'size') creators.sort((a,b) => b._stats.size - a._stats.size);
  else if (sortBy === 'count') creators.sort((a,b) => b._stats.count - a._stats.count);
  else creators.sort((a,b) => a.name.localeCompare(b.name));

  for (const c of creators) {
    let items = c.items.filter(it => {
      totalItems++;
      if (showMarkedOnly && !state[it.path]) return false;
      if (showOverrideOnly && !it.override) return false;
      if (!matchesCatFilter(it.cats)) return false;
      if (currentFilter && !it.file.toLowerCase().includes(currentFilter) && !c.name.toLowerCase().includes(currentFilter)) return false;
      return true;
    });
    if (itemSortBy === 'size') items = items.slice().sort((a,b) => b.size - a.size);
    else if (itemSortBy === 'category') items = items.slice().sort((a,b) => ((a.cats||[])[0]||'기타').localeCompare((b.cats||[])[0]||'기타') || a.file.localeCompare(b.file));
    else items = items.slice().sort((a,b) => a.file.localeCompare(b.file));
    if (!items.length) continue;
    visibleItems += items.length;
    const section = document.createElement('section');
    section.className = 'creator' + (collapsedMode ? ' collapsed' : '');
    const markedInCreator = items.filter(it => state[it.path]).length;
    section.innerHTML = `
      <div class="creator-header" onclick="if(!event.target.matches('button'))this.parentElement.classList.toggle('collapsed')">
        <h2>${escapeHtml(c.name)}</h2>
        <span class="creator-count">${items.length}개 · ${human(c._stats.size)}${markedInCreator ? ` <span class="badge warn">🗑️ ${markedInCreator}</span>`:''}</span>
        <div class="creator-actions">
          <button onclick="markCreator('${escapeAttr(c.name)}', true)">모두 표시</button>
          <button onclick="markCreator('${escapeAttr(c.name)}', false)">표시 해제</button>
        </div>
      </div>
      <div class="grid"></div>
    `;
    const grid = section.querySelector('.grid');
    for (const it of items) {
      const div = document.createElement('div');
      const classes = ['item'];
      if (it.trashed) classes.push('trashed');
      else if (state[it.path]) classes.push('marked');
      if (bulkSel.has(it.path)) classes.push('selected');
      div.className = classes.join(' ');
      div.draggable = !it.trashed;
      div.dataset.path = it.path;
      div.dataset.thumbIdx = '0';
      div.title = (it.trashed ? '휴지통에 있음 (클릭 → 복원)\\n' : '') + it.file + '\\n' + human(it.size);
      let thumbHtml;
      if (it.thumbs.length) {
        thumbHtml = `<img class="thumb-img" src="/thumbs/${it.thumbs[0]}" loading="lazy">`;
        if (it.thumbs.length > 1) {
          thumbHtml += `<div class="thumb-nav">
            <button class="thumb-btn thumb-prev" onclick="cycleThumb(event, this, -1)">◀</button>
            <span class="thumb-counter">1/${it.thumbs.length}</span>
            <button class="thumb-btn thumb-next" onclick="cycleThumb(event, this, 1)">▶</button>
          </div>`;
        }
      } else {
        thumbHtml = `<div class="no-thumb">썸네일 없음</div>`;
      }
      const trashBadge = it.trashed ? '<div class="trash-badge">🗑️ 휴지통</div>' : '';
      const overrideClass = it.override ? ' override' : '';
      const catTitle = it.override ? '수동 지정' : (it.casp ? 'CASP 파싱' : '파일명 추측') + ' (우클릭으로 변경)';
      const catIcon = it.cat_icon ? `<div class="cat-icon${overrideClass}" title="${escapeHtml(catTitle + ': ' + (it.cats||[]).join(', '))}" data-item-path="${escapeHtml(it.path)}" data-item-cat="${escapeHtml(it.primary_cat||'')}">${it.cat_icon}</div>` : '';
      const creatorSubtitle = it._creator ? `<div class="creator-sub">${escapeHtml(it._creator)}</div>` : '';
      div.dataset.thumbs = JSON.stringify(it.thumbs);
      div.innerHTML = thumbHtml + trashBadge + catIcon + `<div class="sz">${human(it.size)}</div>${creatorSubtitle}<div class="name">${escapeHtml(it.file)}</div><div class="name-full">${escapeHtml(it.file)}</div>`;
      const clickHandler = it.trashed
        ? async () => {
            if (!confirm(`복원할까요?\\n${it.file}`)) return;
            const res = await fetch('/api/restore', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({paths: [it.trash_path]}),
            });
            await res.json();
            toast('↩️ 복원됨');
            await loadManifest();
          }
        : (e) => {
            const isBulk = editMode === 'category' || (e && (e.metaKey || e.ctrlKey));
            if (isBulk) {
              if (bulkSel.has(it.path)) bulkSel.delete(it.path);
              else bulkSel.add(it.path);
              updateBulkBar();
              div.classList.toggle('selected', bulkSel.has(it.path));
            } else {
              toggle(it.path);
            }
          };
      div.querySelector('.thumb-img')?.addEventListener('click', clickHandler);
      div.querySelector('.no-thumb')?.addEventListener('click', clickHandler);
      div.querySelector('.name').addEventListener('click', clickHandler);
      div.querySelector('.sz').addEventListener('click', clickHandler);
      // 우클릭 or cat-icon 클릭 → 카테고리 변경 메뉴
      const catEl = div.querySelector('.cat-icon');
      if (catEl) {
        catEl.addEventListener('click', (e) => {
          e.stopPropagation();
          showCategoryMenu(e, it.path, it.primary_cat);
        });
      }
      div.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        showCategoryMenu(e, it.path, it.primary_cat);
      });
      // Drag: 카테고리 chip으로 드래그해서 카테고리 지정
      div.addEventListener('dragstart', (e) => {
        div.classList.add('dragging');
        // 선택된 것이 있으면 그 세트 전체 드래그, 아니면 이 아이템만
        const draggedPaths = bulkSel.has(it.path) ? [...bulkSel] : [it.path];
        e.dataTransfer.setData('application/json', JSON.stringify(draggedPaths));
        e.dataTransfer.effectAllowed = 'copy';
      });
      div.addEventListener('dragend', () => div.classList.remove('dragging'));
      grid.appendChild(div);
    }
    main.appendChild(section);
  }
  updateStats(totalItems, visibleItems);
  updateFooter();
}

function escapeHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function escapeAttr(s) { return s.replace(/'/g, "\\\\'"); }

function updateStats(total, visible) {
  document.getElementById('stats').textContent =
    `창작자 ${MANIFEST.creators.length}명 · 파일 ${total.toLocaleString()}개 · 표시 중 ${visible.toLocaleString()}개 · 썸네일 ${MANIFEST.total_thumbs.toLocaleString()}개`;
}

function toggle(path) {
  if (state[path]) delete state[path]; else state[path] = true;
  saveMarks();
  // 최소 렌더링: 해당 카드만 marked 클래스 토글
  document.querySelectorAll(`.item[data-path="${CSS.escape(path)}"]`).forEach(el => {
    el.classList.toggle('marked', !!state[path]);
  });
  updateFooter();
}

function updateBulkBar() {
  const bar = document.getElementById('bulkBar');
  const count = bulkSel.size;
  document.getElementById('bulkCount').textContent = count;
  bar.style.display = count > 0 ? 'flex' : 'none';
  document.body.classList.toggle('has-bulk', count > 0);
  // 드롭다운 채우기
  const sel = document.getElementById('bulkCat');
  if (sel.options.length <= 1 && MANIFEST) {
    const cats = MANIFEST.all_categories || [];
    sel.innerHTML = '<option value="">카테고리 선택...</option>' +
      cats.map(c => `<option value="${escapeHtml(c.name)}">${c.icon} ${c.name}</option>`).join('') +
      '<option value="__reset__">↺ 수동 지정 해제 (자동 재분류)</option>';
  }
}

async function applyBulkCategory() {
  const cat = document.getElementById('bulkCat').value;
  if (!cat) { toast('카테고리를 선택해주세요'); return; }
  if (bulkSel.size === 0) return;
  const paths = [...bulkSel];
  const catToSend = cat === '__reset__' ? '' : cat;
  const res = await fetch('/api/set-category-batch', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({paths, category: catToSend}),
  });
  await res.json();
  toast(catToSend
    ? `✅ ${paths.length}개 → ${catToSend}`
    : `↺ ${paths.length}개 수동 지정 해제`);
  bulkSel.clear();
  await loadManifest();
}

function clearBulkSel() {
  bulkSel.clear();
  document.querySelectorAll('.item.selected').forEach(el => el.classList.remove('selected'));
  updateBulkBar();
}

async function applyToPaths(paths, category) {
  if (!paths.length) return;
  const catToSend = category === '__reset__' ? '' : category;
  await fetch('/api/set-category-batch', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({paths, category: catToSend}),
  });
  toast(catToSend
    ? `✅ ${paths.length}개 → ${catToSend}`
    : `↺ ${paths.length}개 수동 지정 해제`);
  bulkSel.clear();
  await loadManifest();
}

function showCategoryMenu(event, path, currentCat) {
  const menu = document.getElementById('ctxMenu');
  const cats = MANIFEST.all_categories || [];
  let html = `<div class="ctx-header">카테고리 변경 <span style="color:#999">(${path.split('/').pop()})</span></div>`;
  for (const c of cats) {
    const cls = c.name === currentCat ? ' current' : '';
    html += `<div class="ctx-item${cls}" data-cat="${escapeHtml(c.name)}">${c.icon} ${c.name}</div>`;
  }
  html += `<div class="ctx-item reset" data-cat="">↺ 수동 지정 해제 (자동 재분류)</div>`;
  menu.innerHTML = html;
  // 위치
  const x = Math.min(event.clientX, window.innerWidth - 200);
  const y = Math.min(event.clientY, window.innerHeight - 300);
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
  menu.style.display = 'block';
  menu.querySelectorAll('.ctx-item').forEach(el => {
    el.onclick = async () => {
      menu.style.display = 'none';
      const cat = el.dataset.cat;
      const res = await fetch('/api/set-category', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path, category: cat}),
      });
      await res.json();
      if (cat) {
        toast(`✅ "${path.split('/').pop()}" → ${cat}`);
      } else {
        toast('↺ 수동 지정 해제 (재스캔 시 반영됨)');
      }
      await loadManifest();
    };
  });
}
document.addEventListener('click', (e) => {
  const menu = document.getElementById('ctxMenu');
  if (!menu.contains(e.target)) menu.style.display = 'none';
});

function cycleThumb(event, btn, dir) {
  event.stopPropagation();
  const item = btn.closest('.item');
  const thumbs = JSON.parse(item.dataset.thumbs);
  let idx = parseInt(item.dataset.thumbIdx || '0', 10);
  idx = (idx + dir + thumbs.length) % thumbs.length;
  item.dataset.thumbIdx = idx;
  item.querySelector('.thumb-img').src = `/thumbs/${thumbs[idx]}`;
  item.querySelector('.thumb-counter').textContent = `${idx+1}/${thumbs.length}`;
}

function markCreator(name, mark) {
  const c = MANIFEST.creators.find(x => x.name === name);
  if (!c) return;
  for (const it of c.items) {
    if (mark) state[it.path] = true;
    else delete state[it.path];
  }
  saveMarks();
  render();
}

function updateFooter() {
  const marked = Object.keys(state);
  document.getElementById('marked-count').textContent = marked.length;
  let sz = 0;
  if (MANIFEST) for (const c of MANIFEST.creators)
    for (const it of c.items) if (state[it.path]) sz += it.size;
  document.getElementById('marked-size').textContent = human(sz);
}

function showOnlyMarked() { showMarkedOnly = !showMarkedOnly; render(); }
function clearMarks() {
  if (!confirm('모든 표시를 초기화할까요?')) return;
  for (const k of Object.keys(state)) delete state[k];
  saveMarks();
  render();
}

function openMarkedDialog() {
  const paths = Object.keys(state);
  if (!paths.length) { toast('삭제 표시된 항목이 없어요.'); return; }
  const list = document.getElementById('marked-list');
  let sz = 0;
  const items = [];
  if (MANIFEST) {
    for (const f of (MANIFEST.folders || MANIFEST.creators || [])) {
      for (const it of f.items) {
        if (state[it.path]) { items.push(it); sz += it.size; }
      }
    }
  }
  document.getElementById('marked-summary').textContent = `(${items.length}개 · ${human(sz)})`;
  list.innerHTML = items.map(it => {
    const thumbHtml = it.thumbs && it.thumbs.length
      ? `<img src="/thumbs/${it.thumbs[0]}" style="width:100%; display:block;">`
      : `<div style="padding:40px 8px; text-align:center; color:#999; font-size:11px; background:#eee;">썸네일 없음</div>`;
    return `<div class="marked-tile" data-path="${escapeHtml(it.path)}" style="position:relative; border:2px solid #d9534f; border-radius:4px; background:white; overflow:hidden;">
      ${thumbHtml}
      <button style="position:absolute; top:4px; right:4px; background:rgba(255,255,255,.9); border:1px solid #ccc; border-radius:50%; width:22px; height:22px; padding:0; cursor:pointer; font-size:12px; z-index:2;" title="표시 해제" onclick="unmarkFromDialog('${escapeAttr(it.path)}')">✕</button>
      <div style="padding:3px 6px; font-size:10px; color:#555; word-break:break-all; line-height:1.3; max-height:2.6em; overflow:hidden;">${escapeHtml(it.file)}</div>
      <div style="position:absolute; bottom:24px; right:0; background:rgba(0,0,0,.6); color:white; padding:1px 4px; font-size:10px;">${human(it.size)}</div>
    </div>`;
  }).join('');
  document.getElementById('marked-dialog').showModal();
}

function unmarkFromDialog(path) {
  delete state[path];
  saveMarks();
  // 다이얼로그 아이템만 즉시 제거
  const tile = document.querySelector(`.marked-tile[data-path="${CSS.escape(path)}"]`);
  if (tile) tile.remove();
  // 갤러리도 반영
  document.querySelectorAll(`.item[data-path="${CSS.escape(path)}"]`).forEach(el => el.classList.remove('marked'));
  updateFooter();
  const remaining = Object.keys(state).length;
  if (remaining === 0) document.getElementById('marked-dialog').close();
}

function clearMarksFromDialog() {
  if (!confirm('삭제 표시 전체를 해제할까요?')) return;
  for (const k of Object.keys(state)) delete state[k];
  saveMarks();
  document.getElementById('marked-dialog').close();
  render();
}

async function performDeleteFromDialog() {
  document.getElementById('marked-dialog').close();
  await performDelete();
}

async function performDelete() {
  const paths = Object.keys(state);
  if (!paths.length) { toast('삭제 표시된 항목이 없어요.'); return; }
  if (!confirm(`${paths.length}개 파일을 휴지통으로 이동할까요?\\n(휴지통에서 복원 가능해요)`)) return;
  const res = await fetch('/api/delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({paths}),
  });
  const data = await res.json();
  // 성공한 것 표시 제거
  for (const p of data.moved) delete state[p];
  // 실패한 것 처리
  const missing = data.failed.filter(f => f.reason === '파일 없음');
  const realErrors = data.failed.filter(f => f.reason !== '파일 없음');
  // 이미 없는 파일은 표시에서 자동 제거
  for (const f of missing) delete state[f.path];
  saveMarks();
  if (realErrors.length) {
    showFailedDialog(data.moved.length, missing.length, realErrors);
  } else if (missing.length) {
    toast(`✅ ${data.moved.length}개 이동됨 · 이미 없던 ${missing.length}개 표시 정리됨`);
  } else {
    toast(`✅ ${data.moved.length}개 이동됨`);
  }
  await loadManifest();
}

async function rescan() {
  document.getElementById('main').innerHTML = '<div class="progress">🔄 스캔 중... (몇 초 걸림)<div class="progress-bar"><div style="width:50%"></div></div></div>';
  const res = await fetch('/api/scan', {method: 'POST'});
  await res.json();
  await loadManifest();
  toast('✅ 재스캔 완료');
}

async function openTrash() {
  const res = await fetch('/api/trash');
  const data = await res.json();
  const list = document.getElementById('trash-list');
  document.getElementById('trash-summary').textContent = `(${data.items.length}개 파일, ${human(data.total_size)})`;
  if (!data.items.length) {
    list.innerHTML = '<div style="padding: 40px; text-align: center; color: #999; grid-column: 1/-1;">휴지통이 비어있어요</div>';
  } else {
    list.innerHTML = data.items.map((it, i) => {
      const fileName = it.original_path.split('/').pop();
      const thumbs = it.thumbs || [];
      let thumbHtml;
      if (thumbs.length) {
        thumbHtml = `<img class="thumb-img" src="/thumbs/${thumbs[0]}" style="width:100%;display:block;">`;
        if (thumbs.length > 1) {
          thumbHtml += `<div class="thumb-nav">
            <button class="thumb-btn thumb-prev" onclick="cycleThumb(event, this, -1)">◀</button>
            <span class="thumb-counter">1/${thumbs.length}</span>
            <button class="thumb-btn thumb-next" onclick="cycleThumb(event, this, 1)">▶</button>
          </div>`;
        }
      } else {
        thumbHtml = `<div class="no-thumb" style="padding:40px 8px; text-align:center; color:#999; font-size:11px; background:#eee;">썸네일 없음</div>`;
      }
      return `
        <label class="trash-tile item" data-thumbs='${JSON.stringify(thumbs).replace(/'/g, "&#39;")}' data-thumb-idx="0" style="position:relative; border:2px solid transparent; border-radius:4px; cursor:pointer; background:white; overflow:hidden; display:block;">
          <input type="checkbox" value="${escapeHtml(it.path)}" onchange="updateTrashSelectedCount()" style="position:absolute; top:6px; left:6px; z-index:3; transform:scale(1.3);">
          ${thumbHtml}
          <div style="padding:3px 6px; font-size:10px; color:#555; word-break:break-all; line-height:1.3; max-height:2.6em; overflow:hidden;">${escapeHtml(fileName)}</div>
          <div style="position:absolute; bottom:24px; right:0; background:rgba(0,0,0,.6); color:white; padding:1px 4px; font-size:10px;">${human(it.size)}</div>
        </label>
      `;
    }).join('');
  }
  updateTrashSelectedCount();
  document.getElementById('trash-dialog').showModal();
}

let lastFailedPaths = [];
function showFailedDialog(movedCount, missingCount, errors) {
  lastFailedPaths = errors.map(e => e.path);
  const summary = `이동 성공: ${movedCount}개 · 이미 삭제된 파일 정리: ${missingCount}개 · 실패: ${errors.length}개`;
  document.getElementById('failed-summary').textContent = summary;
  document.getElementById('failed-list').innerHTML = errors.map(e => `
    <div style="padding: 6px; border-bottom: 1px solid #eee; font-size: 12px;">
      <div style="color: #d9534f; font-weight: 600;">${escapeHtml(e.reason)}</div>
      <div style="color: #666; margin-top: 2px; word-break: break-all;">${escapeHtml(e.path)}</div>
    </div>
  `).join('');
  document.getElementById('failed-dialog').showModal();
}
function clearFailedMarks() {
  for (const p of lastFailedPaths) delete state[p];
  saveMarks();
  document.getElementById('failed-dialog').close();
  render();
  toast(`✅ 실패한 ${lastFailedPaths.length}개 표시 해제됨`);
  lastFailedPaths = [];
}

function trashSelectAll(state) {
  document.querySelectorAll('#trash-list input[type=checkbox]').forEach(cb => cb.checked = state);
  updateTrashSelectedCount();
}

function updateTrashSelectedCount() {
  const n = document.querySelectorAll('#trash-list input:checked').length;
  document.getElementById('trash-selected-count').textContent = n;
}

function closeTrash() { document.getElementById('trash-dialog').close(); }

async function restoreSelected() {
  const paths = [...document.querySelectorAll('#trash-list input:checked')].map(el => el.value);
  if (!paths.length) { toast('선택된 파일이 없어요.'); return; }
  const res = await fetch('/api/restore', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({paths}),
  });
  const data = await res.json();
  toast(`✅ ${data.restored.length}개 복원됨`);
  await loadManifest();
  openTrash();
}

async function emptyTrash() {
  if (!confirm('휴지통을 완전히 비울까요? 되돌릴 수 없어요.')) return;
  const res = await fetch('/api/empty-trash', {method: 'POST'});
  const data = await res.json();
  toast(`✅ ${data.count}개 완전 삭제 (${human(data.size_freed)} 확보)`);
  await postBulkAction();
}

async function restoreAll() {
  const paths = [...document.querySelectorAll('#trash-list input')].map(el => el.value);
  if (!paths.length) { toast('휴지통이 비어있어요.'); return; }
  if (!confirm(`${paths.length}개 파일 전체를 원위치로 복원할까요?`)) return;
  const res = await fetch('/api/restore', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({paths}),
  });
  const data = await res.json();
  toast(`✅ ${data.restored.length}개 복원됨${data.failed.length ? ` (${data.failed.length}개 실패)`:''}`);
  await postBulkAction();
}

async function deleteSelectedFromTrash() {
  const paths = [...document.querySelectorAll('#trash-list input:checked')].map(el => el.value);
  if (!paths.length) { toast('선택된 파일이 없어요.'); return; }
  if (!confirm(`선택한 ${paths.length}개를 완전히 삭제할까요? 되돌릴 수 없어요.`)) return;
  const res = await fetch('/api/delete-from-trash', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({paths}),
  });
  const data = await res.json();
  toast(`✅ ${data.count}개 완전 삭제 (${human(data.size_freed)} 확보)`);
  await postBulkAction();
}

async function postBulkAction() {
  closeTrash();
  await loadManifest();
  if (confirm('게임에 반영하려면 CC 재스캔이 필요해요. 지금 재스캔할까요?')) {
    await rescan();
  }
}

document.getElementById('search').addEventListener('input', e => {
  currentFilter = e.target.value.toLowerCase();
  render();
});
document.getElementById('sortBy').addEventListener('change', e => {
  sortBy = e.target.value;
  render();
});
document.getElementById('itemSortBy').addEventListener('change', e => {
  itemSortBy = e.target.value;
  render();
});
document.getElementById('groupSeg').querySelectorAll('button').forEach(b => {
  b.onclick = () => {
    groupBy = b.dataset.v;
    document.querySelectorAll('#groupSeg button').forEach(x => x.classList.toggle('on', x === b));
    render();
  };
});
document.getElementById('modeSeg').querySelectorAll('button').forEach(b => {
  b.onclick = () => {
    editMode = b.dataset.v;
    document.querySelectorAll('#modeSeg button').forEach(x => x.classList.toggle('on', x === b));
    // 모드 표시 힌트
    document.body.classList.toggle('mode-category', editMode === 'category');
    toast(editMode === 'category' ? '🏷️ 카테고리 편집 모드' : '🗑️ 삭제 선택 모드');
  };
});
document.getElementById('tglMarked').addEventListener('change', e => {
  showMarkedOnly = e.target.checked;
  render();
});
document.getElementById('tglCollapsed').addEventListener('change', e => {
  collapsedMode = e.target.checked;
  render();
});
document.getElementById('tglOverride').addEventListener('change', e => {
  showOverrideOnly = e.target.checked;
  render();
});

function selectAllFiltered() {
  // 현재 렌더된 모든 아이템 (필터/그룹 상관없이 화면에 보이는 것)을 다중선택에 추가
  const visible = document.querySelectorAll('.item');
  let added = 0;
  visible.forEach(el => {
    const p = el.dataset.path;
    if (p && !el.classList.contains('trashed') && !bulkSel.has(p)) {
      bulkSel.add(p);
      el.classList.add('selected');
      added++;
    }
  });
  if (!added) { toast('추가된 아이템이 없어요 (이미 다 선택되어 있거나 없음)'); return; }
  toast(`✅ ${added}개 추가로 선택됨 (총 ${bulkSel.size}개)`);
  updateBulkBar();
}

// ─────── 드래그 박스 선택 ───────
(function initDragBox() {
  const main = document.getElementById('main');
  let box = null;
  let startX = 0, startY = 0;
  let baseline = null;
  main.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    if (e.target.closest('.item, .creator-header, button, a, input, select, label')) return;
    e.preventDefault();
    startX = e.clientX; startY = e.clientY;
    baseline = new Set(bulkSel);  // 기존 선택 유지 (Shift/Cmd 눌렀을 때) 아니면 replace
    if (!(e.shiftKey || e.metaKey || e.ctrlKey)) {
      bulkSel.clear();
      document.querySelectorAll('.item.selected').forEach(el => el.classList.remove('selected'));
      baseline = new Set();
    }
    box = document.createElement('div');
    box.style.cssText = 'position:fixed; border:1.5px dashed #4a90e2; background:rgba(74,144,226,.15); pointer-events:none; z-index:500;';
    document.body.appendChild(box);
  });
  window.addEventListener('mousemove', (e) => {
    if (!box) return;
    const x = Math.min(e.clientX, startX), y = Math.min(e.clientY, startY);
    const w = Math.abs(e.clientX - startX), h = Math.abs(e.clientY - startY);
    box.style.left = x + 'px'; box.style.top = y + 'px';
    box.style.width = w + 'px'; box.style.height = h + 'px';
    // hit test
    const rect = {left:x, top:y, right:x+w, bottom:y+h};
    document.querySelectorAll('.item').forEach(el => {
      if (el.classList.contains('trashed')) return;
      const r = el.getBoundingClientRect();
      const intersects = !(r.right < rect.left || r.left > rect.right || r.bottom < rect.top || r.top > rect.bottom);
      const path = el.dataset.path;
      if (intersects) {
        if (!baseline.has(path)) { bulkSel.add(path); el.classList.add('selected'); }
      } else {
        if (!baseline.has(path) && bulkSel.has(path)) { bulkSel.delete(path); el.classList.remove('selected'); }
      }
    });
    updateBulkBar();
  });
  window.addEventListener('mouseup', () => {
    if (box) { box.remove(); box = null; baseline = null; }
  });
})();

// ─────── 되돌리기 (Cmd+Z) ───────
const undoStack = [];  // {type, data} 각 액션
const MAX_UNDO = 30;

function pushUndo(action) {
  undoStack.push(action);
  if (undoStack.length > MAX_UNDO) undoStack.shift();
  document.getElementById('undoBtn').disabled = false;
}

async function undo() {
  const a = undoStack.pop();
  document.getElementById('undoBtn').disabled = undoStack.length === 0;
  if (!a) { toast('되돌릴 것 없음'); return; }
  if (a.type === 'mark') {
    if (a.wasMarked) state[a.path] = true; else delete state[a.path];
    saveMarks();
    document.querySelectorAll(`.item[data-path="${CSS.escape(a.path)}"]`).forEach(el => el.classList.toggle('marked', !!state[a.path]));
    updateFooter();
    toast('↶ 삭제 표시 되돌림');
  } else if (a.type === 'category') {
    // 각 파일의 이전 카테고리 복원
    for (const [p, prev] of Object.entries(a.prev)) {
      await fetch('/api/set-category', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: p, category: prev || ''}),
      });
    }
    toast(`↶ ${Object.keys(a.prev).length}개 카테고리 되돌림`);
    await loadManifest();
  } else if (a.type === 'delete') {
    // 휴지통에서 원래대로 복원
    const trashPaths = a.movedTrashPaths;
    if (trashPaths.length) {
      await fetch('/api/restore', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({paths: trashPaths}),
      });
      // 삭제 표시도 되살리기
      for (const p of a.originalPaths) state[p] = true;
      saveMarks();
      toast(`↶ ${trashPaths.length}개 파일 복원`);
      await loadManifest();
    }
  }
}

// Cmd+Z / Ctrl+Z 리스너
window.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'z' && !e.shiftKey) {
    if (e.target.matches('input, textarea')) return;
    e.preventDefault();
    undo();
  }
});

// 기존 toggle을 감싸서 undo 추적
const _origToggle = toggle;
toggle = function(path) {
  const wasMarked = !!state[path];
  _origToggle(path);
  pushUndo({type: 'mark', path, wasMarked});
};

// 배치 카테고리 지정 시 undo 추적
const _origApply = applyBulkCategory;
applyBulkCategory = async function() {
  const paths = [...bulkSel];
  const prev = {};
  if (MANIFEST) {
    for (const f of (MANIFEST.folders || MANIFEST.creators || [])) {
      for (const it of f.items) {
        if (paths.includes(it.path)) {
          prev[it.path] = it.override ? it.primary_cat : null;
        }
      }
    }
  }
  await _origApply();
  if (Object.keys(prev).length) pushUndo({type: 'category', prev});
};

// 개별 카테고리 지정 시 undo 추적
const _origApplyToPaths = applyToPaths;
applyToPaths = async function(paths, category) {
  const prev = {};
  if (MANIFEST) {
    for (const f of (MANIFEST.folders || MANIFEST.creators || [])) {
      for (const it of f.items) {
        if (paths.includes(it.path)) {
          prev[it.path] = it.override ? it.primary_cat : null;
        }
      }
    }
  }
  await _origApplyToPaths(paths, category);
  if (Object.keys(prev).length) pushUndo({type: 'category', prev});
};

// 삭제도 undo 추적
const _origPerformDelete = performDelete;
performDelete = async function() {
  const originalPaths = Object.keys(state);
  await _origPerformDelete();
  // 잘 이동된 것들만 undo 스택에 저장
  // (이미 loadManifest 후라 정보 얻으려면 별도 처리 필요)
  // 간단하게: 다시 스캔해서 trashPaths를 얻거나 API 응답에서 얻는 방식
  // 여기서는 originalPaths를 저장하고 restore 시 그대로 요청
  if (originalPaths.length) {
    pushUndo({type: 'delete', originalPaths, movedTrashPaths: originalPaths});
  }
};

loadManifest();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 조용히

    def _send(self, code, content, ctype="text/html; charset=utf-8"):
        if isinstance(content, str):
            content = content.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        if path == "/" or path == "/index.html":
            self._send(200, HTML_PAGE)
            return
        if path == "/api/manifest":
            m = load_manifest()
            if not m:
                m = scan_cc()
            m["meta_categories"] = {name: {"icon": icon, "subs": subs} for name, (icon, subs) in META_CATEGORIES.items()}
            m["all_categories"] = [{"name": name, "icon": icon} for name, icon, _ in CATEGORIES]
            self._json(m)
            return
        if path == "/api/trash":
            self._json(list_trash())
            return
        if path.startswith("/thumbs/"):
            name = path[len("/thumbs/"):]
            fp = THUMBS_DIR / name
            if fp.exists() and fp.is_file():
                self._send(200, fp.read_bytes(), "image/jpeg")
            else:
                self._send(404, "not found")
            return
        self._send(404, "not found")

    def do_POST(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        if path == "/api/delete":
            self._json(move_to_trash(data.get("paths", [])))
        elif path == "/api/restore":
            self._json(restore_from_trash(data.get("paths", [])))
        elif path == "/api/scan":
            self._json(scan_cc())
        elif path == "/api/empty-trash":
            self._json(empty_trash())
        elif path == "/api/delete-from-trash":
            self._json(delete_from_trash(data.get("paths", [])))
        elif path == "/api/set-category-batch":
            # {paths: [...], category: "..."}
            paths = data.get("paths", [])
            cat = data.get("category")
            for p in paths:
                set_category_override(p, cat)
            # 매니페스트 즉시 반영
            m = load_manifest()
            if m:
                path_set = set(paths)
                icon = "📝"
                for name, ic, _ in CATEGORIES:
                    if name == cat: icon = ic; break
                for folder in m.get("folders", []) + m.get("creators", []):
                    for it in folder.get("items", []):
                        if it["path"] in path_set:
                            if cat:
                                it["cats"] = [cat]
                                it["primary_cat"] = cat
                                it["cat_icon"] = icon
                                it["override"] = True
                                it["casp"] = False
                            else:
                                it["override"] = False
                MANIFEST_PATH.write_text(json.dumps(m, ensure_ascii=False))
            self._json({"count": len(paths), "category": cat})
        elif path == "/api/set-category":
            # {path, category (or "" to remove)}
            result = set_category_override(data.get("path"), data.get("category"))
            # 매니페스트에도 즉시 반영 (재스캔 없이)
            m = load_manifest()
            if m:
                target = data.get("path")
                cat = data.get("category")
                for folder in m.get("folders", []) + m.get("creators", []):
                    for it in folder.get("items", []):
                        if it["path"] == target:
                            if cat:
                                # 아이콘 찾기
                                icon = "📝"
                                for name, ic, _ in CATEGORIES:
                                    if name == cat: icon = ic; break
                                it["cats"] = [cat]
                                it["primary_cat"] = cat
                                it["cat_icon"] = icon
                                it["override"] = True
                                it["casp"] = False
                            else:
                                # 재계산 필요 - 이 파일만 다시 스캔
                                it["override"] = False
                            break
                MANIFEST_PATH.write_text(json.dumps(m, ensure_ascii=False))
            self._json(result)
        else:
            self._send(404, "not found")


def run():
    print("Sims 4 CC Manager")
    print("=" * 50)
    print(f"CC 폴더:  {CC_ROOT}")
    print(f"휴지통:   {TRASH_DIR}")
    print(f"캐시:     {APP_STATE}")
    print()

    if not load_manifest():
        print("첫 실행 - CC 스캔 중... (몇 초 걸림)")
        def cb(i, n, name):
            if i % 20 == 0 or i == n:
                print(f"  [{i}/{n}] {name}")
        scan_cc(progress_cb=cb)
        print("스캔 완료")
        print()

    url = f"http://localhost:{PORT}/"
    print(f"서버 시작: {url}")
    print("종료하려면 Ctrl+C")
    print()

    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        HTTPServer(("localhost", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n종료")


if __name__ == "__main__":
    run()
