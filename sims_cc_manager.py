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
# 앱 관련 모든 데이터를 Sims 폴더 밖(macOS 표준 위치)에 저장
APP_STATE = Path.home() / "Library" / "Application Support" / "Sims4CCManager"
_CONFIG_PATH = APP_STATE / "config.json"


def find_sims_root():
    """Sims 4 폴더 자동 감지. 캐시 → 환경변수 → 표준 경로 → 심볼릭 링크 순."""
    # 캐시된 경로
    try:
        if _CONFIG_PATH.exists():
            cfg = json.loads(_CONFIG_PATH.read_text())
            p = Path(cfg.get("sims_root", ""))
            if p.exists() and (p / "Mods").exists():
                return p
    except Exception:
        pass
    candidates = []
    env = os.environ.get("SIMS4_PATH")
    if env:
        candidates.append(Path(env))
    home = Path.home()
    candidates.append(home / "Documents" / "Electronic Arts" / "The Sims 4")
    candidates.append(home / "Games" / "Electronic Arts" / "The Sims 4")
    # 심볼릭 링크 따라가기
    ea = home / "Documents" / "Electronic Arts"
    if ea.exists():
        try:
            for child in ea.iterdir():
                if child.name == "The Sims 4" or child.name.startswith("The Sims 4"):
                    try:
                        candidates.append(child.resolve())
                    except Exception:
                        candidates.append(child)
        except Exception:
            pass
    for p in candidates:
        try:
            if p and p.exists() and (p / "Mods").exists():
                # 캐시 저장
                try:
                    APP_STATE.mkdir(parents=True, exist_ok=True)
                    _CONFIG_PATH.write_text(json.dumps({"sims_root": str(p)}, indent=2))
                except Exception:
                    pass
                return p
        except Exception:
            continue
    print("\n" + "=" * 60)
    print("  Sims 4 폴더를 찾을 수 없습니다.")
    print("=" * 60)
    print("  다음 중 하나를 시도해 주세요:")
    print("   1) 환경변수로 지정:  export SIMS4_PATH=\"/경로/The Sims 4\"")
    print("   2) 표준 위치에 설치: ~/Documents/Electronic Arts/The Sims 4")
    print("   3) 설정 파일 편집:   " + str(_CONFIG_PATH))
    print("      예: {\"sims_root\": \"/경로/The Sims 4\"}")
    print("=" * 60 + "\n")
    raise SystemExit(1)


SIMS_ROOT = find_sims_root()
MODS = SIMS_ROOT / "Mods"
CC_ROOT = MODS / "CC FeaturedCreators"
THUMBS_DIR = APP_STATE / "thumbs"
MANIFEST_PATH = APP_STATE / "manifest.json"
TRASH_DIR = APP_STATE / "trash"

# 옛 위치들 → 새 위치 마이그레이션
_OLD_STATE = SIMS_ROOT / ".cc_manager"
if _OLD_STATE.exists() and not APP_STATE.exists():
    APP_STATE.parent.mkdir(parents=True, exist_ok=True)
    import shutil as _shutil
    _shutil.move(str(_OLD_STATE), str(APP_STATE))

APP_STATE.mkdir(parents=True, exist_ok=True)
THUMBS_DIR.mkdir(exist_ok=True)
TRASH_DIR.mkdir(exist_ok=True)

# 옛 휴지통 (Mods 안) → 새 위치로 옮기기
_OLD_TRASH = MODS / ".cc_trash"
if _OLD_TRASH.exists():
    import shutil as _shutil
    for item in _OLD_TRASH.rglob("*"):
        if item.is_file():
            rel = item.relative_to(_OLD_TRASH)
            dst = TRASH_DIR / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                _shutil.move(str(item), str(dst))
    # 빈 폴더 정리
    for d in sorted([d for d in _OLD_TRASH.rglob("*") if d.is_dir()], key=lambda p: -len(p.parts)):
        try: d.rmdir()
        except OSError: pass
    try: _OLD_TRASH.rmdir()
    except OSError: pass

CAS_THUMB_TYPE = 0x3C1AF1F2       # CAS 썸네일 (표준, 작은 사이즈)
CAS_THUMB_MEDIUM = 0x0D338A3A     # CAS 썸네일 (중간)
CAS_THUMB_LARGE = 0x0D338A3B      # CAS 썸네일 (큼)
BUILDBUY_THUMB_TYPE = 0x08D31226  # Build/Buy 썸네일
GENERIC_THUMB = 0x00B2D882        # 일반 썸네일 리소스
PNG_TYPE = 0x2F7D0004             # 임베디드 PNG
CASP_TYPE = 0x034AEECB

# 썸네일로 인식할 모든 리소스 타입 (확장 후)
THUMB_TYPES = {CAS_THUMB_TYPE, CAS_THUMB_MEDIUM, CAS_THUMB_LARGE,
               BUILDBUY_THUMB_TYPE, GENERIC_THUMB, PNG_TYPE}

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
    """.package 안에서 썸네일 이미지(JPEG 또는 PNG) 추출. yield (data, hash, ext)"""
    seen = set()
    for entry in parse_dbpf(pkg_path):
        if entry["type"] not in THUMB_TYPES:
            continue
        try:
            data = read_resource(pkg_path, entry)
        except Exception:
            continue
        if data.startswith(b"\xff\xd8\xff"):
            ext = ".jpg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = ".png"
        else:
            continue
        h = hashlib.md5(data).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        yield (data, h, ext)


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

        stat = pkg.stat()
        item = {
            "file": pkg.name,
            "path": rel_pkg_str,
            "size": stat.st_size,
            "mtime": stat.st_mtime,   # 파일 수정 시각 (최근순/오래된순 정렬용)
            "thumbs": [],
            "cats": [c[0] for c in cats],
            "primary_cat": cats[0][0],
            "cat_icon": cats[0][1],
            "casp": is_casp,
            "override": is_override,
        }
        for img_bytes, h, ext in extract_thumbs(pkg):
            thumb_name = f"{h[:16]}{ext}"
            thumb_path = THUMBS_DIR / thumb_name
            if not thumb_path.exists():
                thumb_path.write_bytes(img_bytes)
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


def _update_manifest_items(paths, updates):
    """매니페스트의 특정 파일들에 필드 업데이트 (재스캔 없이 즉시 반영)."""
    m = load_manifest()
    if not m: return
    path_set = set(paths)
    trash_rel_set = set()  # 휴지통 안 상대 경로도 매칭용
    for folder in m.get("folders", []) + m.get("creators", []):
        for it in folder.get("items", []):
            if it["path"] in path_set or it.get("trash_path") in path_set:
                for k, v in updates.items():
                    it[k] = v
    MANIFEST_PATH.write_text(json.dumps(m, ensure_ascii=False))


def move_to_trash(rel_paths):
    moved, failed = [], []
    trash_m = _load_trash_manifest()
    for rel in rel_paths:
        src = MODS / rel
        if not src.exists():
            failed.append({"path": rel, "reason": "파일 없음"})
            continue
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
    # 매니페스트에 상태 반영 (재스캔 안 해도 목록에 표시됨)
    if moved:
        _update_manifest_items(moved, {"trashed": True, "perma_deleted": False})
    return {"moved": moved, "failed": failed}


def restore_from_trash(rel_paths):
    restored, failed = [], []
    restored_originals = []
    trash_m = _load_trash_manifest()
    for rel in rel_paths:
        src = TRASH_DIR / rel
        info = trash_m.get(rel, {})
        original = info.get("original_path", rel)
        dst = MODS / original
        if not src.exists():
            failed.append({"path": rel, "reason": "휴지통에 없음"})
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
            trash_m.pop(rel, None)
            restored.append(rel)
            restored_originals.append(original)
        except Exception as e:
            failed.append({"path": rel, "reason": str(e)})
    _save_trash_manifest(trash_m)
    # 복원 → trashed 플래그 제거
    if restored_originals:
        _update_manifest_items(restored_originals, {"trashed": False, "perma_deleted": False})
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
                for img_bytes, h, ext in extract_thumbs(f):
                    thumb_name = f"{h[:16]}{ext}"
                    thumb_path = THUMBS_DIR / thumb_name
                    if not thumb_path.exists():
                        thumb_path.write_bytes(img_bytes)
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
    perma_originals = []
    trash_m = _load_trash_manifest()
    for rel in rel_paths:
        fp = TRASH_DIR / rel
        if fp.exists() and fp.is_file():
            try:
                total += fp.stat().st_size
                fp.unlink()
                count += 1
                info = trash_m.pop(rel, {})
                orig = info.get("original_path", rel)
                perma_originals.append(orig)
            except OSError:
                pass
    _save_trash_manifest(trash_m)
    if perma_originals:
        _update_manifest_items(perma_originals, {"trashed": False, "perma_deleted": True})
    return {"count": count, "size_freed": total}


def empty_trash():
    count = 0
    total = 0
    trash_m = _load_trash_manifest()
    perma_originals = [info.get("original_path", rel) for rel, info in trash_m.items()]
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
    _save_trash_manifest({})
    # 매니페스트에 perma_deleted 반영
    if perma_originals:
        _update_manifest_items(perma_originals, {"trashed": False, "perma_deleted": True})
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
  /* 모드 토글: 삭제 = 빨강 (위험), 카테고리 편집 = 파랑 (안전) */
  #modeSeg { border: 2px solid #d9534f !important; border-radius: 8px; padding: 2px; background: #fbecec !important; box-shadow: 0 0 0 2px rgba(217,83,79,.15); }
  #modeSeg button { padding: 6px 14px !important; font-size: 13px !important; font-weight: 700 !important; border-radius: 5px !important; border-left: none !important; }
  #modeSeg button.on { background: #d9534f !important; color: white !important; box-shadow: 0 1px 3px rgba(0,0,0,.15); }
  #modeSeg button:not(.on) { background: transparent !important; color: #d9534f !important; }
  body.mode-category #modeSeg { border-color: #4a90e2 !important; background: #eaf3fc !important; box-shadow: 0 0 0 2px rgba(74,144,226,.15); }
  body.mode-category #modeSeg button:not(.on) { color: #4a90e2 !important; background: transparent !important; }
  body.mode-category #modeSeg button.on { background: #4a90e2 !important; color: white !important; }
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
  .grid { padding: 10px; display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 6px; align-items: start; }
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
  .item.selected::before { content: '✓'; position: absolute; top: 4px; left: 4px; background: #4a90e2; color: white; width: 20px; height: 20px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: bold; z-index: 3; box-shadow: 0 1px 2px rgba(0,0,0,.2); }
  /* 삭제표시 + 다중선택 둘 다: 파란 테두리 + 빨간 배경 tint 만 (별도 배지 X) */
  .item.marked.selected { background: #ffe8e8 !important; }
  .item.marked.selected .thumb-img { filter: brightness(0.9) saturate(0.85); }
  .chip.drop-target { background: #ffe066 !important; color: #333 !important; border-color: #f0a500 !important; transform: scale(1.1); }
  .item.dragging { opacity: 0.4; }
  #bulkBar { position: fixed; bottom: 44px; left: 0; right: 0; background: #333; color: white; padding: 8px 16px; z-index: 99; display: none; align-items: center; gap: 12px; box-shadow: 0 -2px 8px rgba(0,0,0,.2); }
  #footer { height: 44px; padding: 0 16px; }
  body { padding-bottom: 60px; }
  body.has-bulk { padding-bottom: 108px; }
  #bulkBar select { padding: 5px 8px; border-radius: 4px; }
  #bulkBar .info { flex: 1; font-size: 14px; }
  .item.trashed { opacity: 0.6; border-color: #999; background: #f0f0f0; }
  .item.trashed .thumb-img { filter: grayscale(0.7); }
  .item.perma-deleted { opacity: 0.4; border-color: #600; background: #f5eaea; }
  .item.perma-deleted .thumb-img { filter: grayscale(1) brightness(0.6); }
  .item.perma-deleted .name, .item.perma-deleted .name-full { text-decoration: line-through; color: #999; }
  .trash-badge { position: absolute; top: 4px; left: 4px; background: rgba(0,0,0,.75); color: white; padding: 2px 6px; border-radius: 3px; font-size: 10px; z-index: 3; }
  /* 카테고리 아이콘: 썸네일 이미지 안쪽 우하단 (이름과 안 겹침) */
  .cat-icon { position: absolute; top: 4px; right: 4px; background: rgba(255,255,255,.92); padding: 2px 6px; border-radius: 10px; font-size: 13px; box-shadow: 0 1px 3px rgba(0,0,0,.2); cursor: context-menu; z-index: 2; transition: opacity .15s; }
  .item:hover .cat-icon { opacity: 0.2; }  /* hover 시 흐리게 → thumb nav 안 가림 */
  .item:hover .cat-icon:hover { opacity: 1; }  /* 아이콘 자체 hover 시 다시 진하게 */
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
  /* 기본: 2줄까지 잘리지 않고 항상 완전히 보임. 한 줄이면 한 줄 높이 유지 (min-height 없음). */
  .item .name { padding: 3px 6px; font-size: 10px; color: #555; word-break: break-all; line-height: 1.35; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; text-overflow: ellipsis; }
  /* 3줄 이상 긴 이름만 hover 시 팝오버로 위쪽 확장 - 원본은 그대로 유지, 그 위에 덮음 */
  .item .name-full { display: none; position: absolute; bottom: 0; left: 0; right: 0; padding: 3px 6px; font-size: 10px; color: #222; word-break: break-all; line-height: 1.35; background: white; z-index: 100; box-shadow: 0 -2px 6px rgba(0,0,0,.15); border-radius: 0 0 2px 2px; }
  /* JS로 짧은 이름은 name-full 을 아예 안 그리게 함 */
  .item:hover .name-full { display: block; }
  .item .sz { position: absolute; top: 4px; left: 4px; background: rgba(0,0,0,.6); color: white; padding: 1px 5px; font-size: 10px; border-radius: 3px; pointer-events: none; z-index: 2; }
  .item.selected::before { left: 4px; }
  .item.selected .sz { display: none; }
  .thumb-img, .no-thumb { cursor: pointer; }
  /* Thumb nav은 썸네일 아래쪽 (이름 바로 위) */
  .thumb-nav { position: absolute; bottom: 32px; left: 50%; transform: translateX(-50%); display: flex; align-items: center; gap: 4px; opacity: 0; transition: opacity .15s; pointer-events: none; background: rgba(0,0,0,.65); border-radius: 14px; padding: 2px 4px; }
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
  /* 모달 우측 상단 X 닫기 버튼 통일 스타일 */
  .dlg-close { position: absolute; top: 10px; right: 10px; width: 28px; height: 28px; padding: 0; font-size: 14px; border-radius: 50%; background: white; border: 1px solid #ddd; cursor: pointer; z-index: 10; }
  kbd { background: #f0f0f0; border: 1px solid #ccc; border-bottom-width: 2px; border-radius: 3px; padding: 1px 6px; font-size: 11px; font-family: -apple-system, monospace; }
  .dlg-close:hover { background: #f4f4f4; }
  dialog { position: relative; }
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
    <button onclick="openHelp()" title="사용법 & 단축키" style="border-radius:50%; width:32px; padding:0; font-size:16px;">❓</button>
  </div>

  <div class="row">
    <input type="search" id="search" placeholder="🔍 파일명·창작자 검색" style="width: 260px;">
    <div class="group">
      <span class="label">그룹</span>
      <div class="seg" id="groupSeg">
        <button data-v="creator" class="on">폴더별</button>
        <button data-v="category">카테고리별</button>
        <button data-v="date">날짜별</button>
      </div>
    </div>
    <div class="group">
      <span class="label">그룹정렬</span>
      <select id="sortBy">
        <option value="name">이름</option>
        <option value="size">크기</option>
        <option value="count">개수</option>
        <option value="recent">최근 파일순</option>
        <option value="oldest">오래된 파일순</option>
      </select>
    </div>
    <div class="group">
      <span class="label">아이템정렬</span>
      <select id="itemSortBy">
        <option value="name">이름</option>
        <option value="size">크기</option>
        <option value="category">카테고리</option>
        <option value="recent">최근순</option>
        <option value="oldest">오래된순</option>
      </select>
    </div>
  </div>

  <div class="row">
    <div class="toggle-group">
      <label class="switch"><input type="checkbox" id="tglMarked"> 🗑️ 지울 것만</label>
      <label class="switch"><input type="checkbox" id="tglOverride"> 🖐️ 수동 지정만</label>
      <label class="switch"><input type="checkbox" id="tglHideTrashed"> 휴지통 항목 숨기기</label>
      <label class="switch"><input type="checkbox" id="tglHidePerma" checked> 완전삭제 숨기기</label>
      <label class="switch"><input type="checkbox" id="tglCollapsed"> 모두 접기</label>
    </div>
    <div class="divider"></div>
    <button onclick="selectAllFiltered()" title="지금 화면에 보이는 아이템 모두를 다중선택에 추가">☑️ 전체 선택</button>
    <button onclick="clearBulkSel()" title="다중선택(파란 테두리) 전체 해제">▢ 전체 선택 해제</button>
    <div class="divider"></div>
    <button onclick="clearMarks()" title="삭제 표시(빨간 테두리) 전체 해제">🗑️ 삭제표시 전체 해제</button>
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
  <button class="dlg-close" onclick="document.getElementById('marked-dialog').close()">✕</button>
  <h3>🗑️ 삭제 표시된 아이템 <span id="marked-summary" style="font-weight:normal; color:#666; font-size:13px;"></span></h3>
  <div style="max-height:60vh; overflow-y:auto; margin:8px 0; border:1px solid #eee; border-radius:6px; padding:8px; background:#fafafa;">
    <div id="marked-list" style="display:grid; grid-template-columns:repeat(auto-fill, minmax(140px, 1fr)); gap:6px;"></div>
  </div>
  <div style="display:flex; gap:8px; justify-content:flex-end;">
    <button onclick="clearMarksFromDialog()">전체 표시 해제</button>
    <button onclick="performDeleteFromDialog()" class="primary">🗑️ 휴지통으로 이동</button>
  </div>
</dialog>
<div id="toast" class="toast"></div>
<div id="ctxMenu"></div>
<dialog id="help-dialog" style="max-width: 640px; width: 90vw; padding: 0;">
  <button class="dlg-close" onclick="document.getElementById('help-dialog').close()">✕</button>
  <div style="padding: 16px 22px; border-bottom: 1px solid #eee;">
    <h2 style="margin: 0; font-size: 17px;">🎮 사용법</h2>
  </div>
  <div style="padding: 16px 22px; max-height: 72vh; overflow-y: auto; font-size: 13px; line-height: 1.55;">

    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 18px;">
      <div style="border: 2px solid #d9534f; background: #fbecec; border-radius: 8px; padding: 10px 12px;">
        <div style="font-weight: 700; color: #d9534f; margin-bottom: 4px;">🗑️ 삭제 모드</div>
        <div style="font-size: 12px;">클릭해서 지울 것 <b>표시</b> → 하단 "🗑️ 휴지통으로 이동" 버튼</div>
      </div>
      <div style="border: 2px solid #4a90e2; background: #eaf3fc; border-radius: 8px; padding: 10px 12px;">
        <div style="font-weight: 700; color: #4a90e2; margin-bottom: 4px;">🏷️ 카테고리 모드</div>
        <div style="font-size: 12px;">클릭해서 <b>다중 선택</b> → 하단 드롭다운으로 카테고리 지정</div>
      </div>
    </div>

    <h3 style="margin: 12px 0 6px; font-size: 14px;">🎨 상태 색깔</h3>
    <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 6px; font-size: 12px;">
      <div><span style="display:inline-block; width:14px; height:14px; background:#ffe8e8; border:2px solid #d9534f; border-radius:3px; vertical-align:middle;"></span> 삭제 표시됨</div>
      <div><span style="display:inline-block; width:14px; height:14px; background:white; border:2px solid #4a90e2; border-radius:3px; box-shadow:0 0 4px #4a90e2; vertical-align:middle;"></span> 다중 선택됨</div>
      <div><span style="display:inline-block; width:14px; height:14px; background:#f0f0f0; border:2px solid #999; border-radius:3px; vertical-align:middle;"></span> 🗑️ 휴지통 (복원 가능)</div>
      <div><span style="display:inline-block; width:14px; height:14px; background:#f5eaea; border:2px solid #600; border-radius:3px; vertical-align:middle;"></span> ✖ 완전 삭제됨</div>
    </div>

    <h3 style="margin: 16px 0 6px; font-size: 14px;">⌨️ 단축키</h3>
    <table style="width:100%; border-collapse: collapse; font-size: 12px;">
      <tr><td style="padding:4px 8px; color:#666; width:120px;"><kbd>Cmd+Z</kbd></td><td>되돌리기</td></tr>
      <tr><td style="padding:4px 8px; color:#666;"><kbd>Cmd+클릭</kbd></td><td>모드 상관없이 다중 선택</td></tr>
      <tr><td style="padding:4px 8px; color:#666;">빈 공간 드래그</td><td>박스 다중 선택</td></tr>
      <tr><td style="padding:4px 8px; color:#666;">우클릭</td><td>단일 카테고리 변경 메뉴</td></tr>
      <tr><td style="padding:4px 8px; color:#666;">이름 클릭</td><td>파일명 복사 (검색용)</td></tr>
      <tr><td style="padding:4px 8px; color:#666;">이름 hover</td><td>긴 파일명 전체 표시</td></tr>
    </table>

    <h3 style="margin: 16px 0 6px; font-size: 14px;">💡 팁</h3>
    <ul style="margin: 0; padding-left: 20px; font-size: 12px;">
      <li>대량 카테고리 지정: Cmd+클릭 다중선택 → 하단 드롭다운, 또는 카테고리 chip으로 드래그</li>
      <li>휴지통 이동은 안전. 언제든 복원 가능. <b>완전 삭제만 되돌릴 수 없음</b></li>
      <li>날짜별 그룹 → "3년 이상 전" 오래된 것 대량 정리</li>
      <li>수동 지정한 카테고리는 재스캔 시에도 유지</li>
    </ul>

    <p style="margin: 14px 0 0; padding: 8px; background: #f7f7f7; border-radius: 4px; font-size: 11px; color: #666;">
      📂 앱 데이터: <code>~/Library/Application Support/Sims4CCManager/</code> (Sims 4 폴더 안 건드림)
    </p>

  </div>
</dialog>
<div id="bulkBar">
  <span class="info">📌 <b id="bulkCount">0</b>개 선택됨</span>
  <select id="bulkCat"><option value="">카테고리 선택...</option></select>
  <button onclick="applyBulkCategory()" class="blue">✓ 카테고리 지정</button>
  <button onclick="clearBulkSel()">선택 해제</button>
</div>
<dialog id="failed-dialog" style="max-width: 600px; width: 90vw;">
  <button class="dlg-close" onclick="document.getElementById('failed-dialog').close()">✕</button>
  <h3>⚠️ 일부 파일 이동 실패</h3>
  <div id="failed-summary" style="color: #666; font-size: 13px; margin-bottom: 8px;"></div>
  <div style="max-height: 300px; overflow-y: auto; margin: 8px 0; border: 1px solid #eee; border-radius: 6px; padding: 8px; background: #fafafa;">
    <div id="failed-list"></div>
  </div>
  <div style="display: flex; gap: 8px; justify-content: flex-end;">
    <button onclick="clearFailedMarks()">실패한 것 표시 해제</button>
  </div>
</dialog>
<dialog id="trash-dialog" style="max-width: 720px; width: 90vw;">
  <button class="dlg-close" onclick="closeTrash()">✕</button>
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
let hideTrashed = false;
let hidePerma = true;
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
  let maxMtime = 0, minMtime = Infinity;
  for (const it of c.items) {
    if (it.mtime) {
      if (it.mtime > maxMtime) maxMtime = it.mtime;
      if (it.mtime < minMtime) minMtime = it.mtime;
    }
  }
  return {
    count: c.items.length,
    size: c.items.reduce((s, it) => s + it.size, 0),
    maxMtime: maxMtime || 0,
    minMtime: minMtime === Infinity ? 0 : minMtime,
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

// 날짜 버킷 정의: [순서, 라벨, cutoff(초 단위 이내면 이 버킷)]
const DATE_BUCKETS = [
  { key: 'today',    label: '오늘',        maxAgeS: 86400 },
  { key: 'week',     label: '이번 주',     maxAgeS: 86400 * 7 },
  { key: 'month',    label: '이번 달',     maxAgeS: 86400 * 30 },
  { key: '3month',   label: '3개월 이내',   maxAgeS: 86400 * 90 },
  { key: '6month',   label: '6개월 이내',   maxAgeS: 86400 * 180 },
  { key: 'year',     label: '1년 이내',     maxAgeS: 86400 * 365 },
  { key: '2year',    label: '1~2년 전',     maxAgeS: 86400 * 365 * 2 },
  { key: '3year',    label: '2~3년 전',     maxAgeS: 86400 * 365 * 3 },
  { key: 'old',      label: '3년 이상 전',   maxAgeS: Infinity },
  { key: 'unknown',  label: '(날짜 정보 없음)', maxAgeS: Infinity },
];

function dateBucket(mtime) {
  if (!mtime) return DATE_BUCKETS[DATE_BUCKETS.length - 1];  // unknown
  const nowS = Date.now() / 1000;
  const ageS = nowS - mtime;
  for (const b of DATE_BUCKETS) {
    if (b.key === 'unknown') continue;
    if (ageS <= b.maxAgeS) return b;
  }
  return DATE_BUCKETS[DATE_BUCKETS.length - 2];  // old
}

function buildGroups() {
  const folders = MANIFEST.folders || MANIFEST.creators || [];
  if (groupBy === 'category') {
    const groups = {};
    for (const c of folders) {
      for (const it of c.items) {
        const cat = it.primary_cat || (it.cats && it.cats[0]) || '기타';
        if (!groups[cat]) groups[cat] = { name: cat, items: [] };
        groups[cat].items.push({...it, _creator: c.name});
      }
    }
    return Object.values(groups);
  }
  if (groupBy === 'date') {
    const groups = {};
    for (const c of folders) {
      for (const it of c.items) {
        const b = dateBucket(it.mtime);
        if (!groups[b.key]) groups[b.key] = { name: b.label, _bucketOrder: DATE_BUCKETS.indexOf(b), items: [] };
        groups[b.key].items.push({...it, _creator: c.name});
      }
    }
    // 최신 → 옛날 순 고정
    return Object.values(groups).sort((a, b) => a._bucketOrder - b._bucketOrder);
  }
  return folders.map(c => ({...c}));
}

function render() {
  const main = document.getElementById('main');
  main.innerHTML = '';
  if (!MANIFEST || !MANIFEST.creators.length) {
    main.innerHTML = `<div class="progress" style="padding:40px;">
      <div style="font-size:48px;margin-bottom:12px;">👋</div>
      <h2 style="margin:0 0 8px 0;">환영합니다!</h2>
      <div style="color:#666;margin-bottom:20px;">Mods 폴더에서 아직 CC를 스캔하지 않았어요.<br>아래 버튼을 눌러 시작해 보세요.</div>
      <button class="blue" style="font-size:14px;padding:10px 20px;" onclick="rescan()">🔄 스캔 시작</button>
    </div>`;
    updateFooter();
    updateStats(0, 0);
    return;
  }
  populateCatFilter();
  let totalItems = 0, visibleItems = 0;
  const creators = buildGroups();
  creators.forEach(c => c._stats = creatorStats(c));
  // 날짜별 그룹은 최신→옛 고정 순서 (buildGroups에서 이미 정렬됨)
  if (groupBy === 'date') {
    // no re-sort
  } else if (sortBy === 'size') creators.sort((a,b) => b._stats.size - a._stats.size);
  else if (sortBy === 'count') creators.sort((a,b) => b._stats.count - a._stats.count);
  else if (sortBy === 'recent') creators.sort((a,b) => b._stats.maxMtime - a._stats.maxMtime);
  else if (sortBy === 'oldest') creators.sort((a,b) => a._stats.minMtime - b._stats.minMtime);
  else creators.sort((a,b) => a.name.localeCompare(b.name));

  for (const c of creators) {
    let items = c.items.filter(it => {
      totalItems++;
      if (hidePerma && it.perma_deleted) return false;
      if (hideTrashed && it.trashed) return false;
      if (showMarkedOnly && !state[it.path]) return false;
      if (showOverrideOnly && !it.override) return false;
      if (!matchesCatFilter(it.cats)) return false;
      if (currentFilter && !it.file.toLowerCase().includes(currentFilter) && !c.name.toLowerCase().includes(currentFilter)) return false;
      return true;
    });
    if (itemSortBy === 'size') items = items.slice().sort((a,b) => b.size - a.size);
    else if (itemSortBy === 'category') items = items.slice().sort((a,b) => ((a.cats||[])[0]||'기타').localeCompare((b.cats||[])[0]||'기타') || a.file.localeCompare(b.file));
    else if (itemSortBy === 'recent') items = items.slice().sort((a,b) => (b.mtime||0) - (a.mtime||0));
    else if (itemSortBy === 'oldest') items = items.slice().sort((a,b) => (a.mtime||0) - (b.mtime||0));
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
          <button onclick="markCreator('${escapeAttr(c.name)}', true)" title="이 폴더 전체를 삭제 대상으로 표시">🗑️ 폴더 전체 삭제표시</button>
          <button onclick="markCreator('${escapeAttr(c.name)}', false)" title="이 폴더의 삭제 표시 전체 해제">✕ 폴더 전체 표시 해제</button>
        </div>
      </div>
      <div class="grid"></div>
    `;
    const grid = section.querySelector('.grid');
    for (const it of items) {
      const div = document.createElement('div');
      const classes = ['item'];
      if (it.perma_deleted) classes.push('perma-deleted');
      else if (it.trashed) classes.push('trashed');
      else if (state[it.path]) classes.push('marked');
      if (bulkSel.has(it.path)) classes.push('selected');
      div.className = classes.join(' ');
      div.draggable = !it.trashed && !it.perma_deleted;
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
      const trashBadge = it.perma_deleted ? '<div class="trash-badge" style="background:rgba(60,0,0,.85);">✖ 완전삭제</div>'
                        : it.trashed ? '<div class="trash-badge">🗑️ 휴지통</div>'
                        : '';
      const overrideClass = it.override ? ' override' : '';
      const catTitle = it.override ? '수동 지정' : (it.casp ? 'CASP 파싱' : '파일명 추측') + ' (우클릭으로 변경)';
      const catIcon = it.cat_icon ? `<div class="cat-icon${overrideClass}" title="${escapeHtml(catTitle + ': ' + (it.cats||[]).join(', '))}" data-item-path="${escapeHtml(it.path)}" data-item-cat="${escapeHtml(it.primary_cat||'')}">${it.cat_icon}</div>` : '';
      const creatorSubtitle = it._creator ? `<div class="creator-sub">${escapeHtml(it._creator)}</div>` : '';
      div.dataset.thumbs = JSON.stringify(it.thumbs);
      // 44자 초과 시 hover 팝오버로 전체 이름 보여줌 (짧으면 name-full 안 만듦)
      const needsPopover = it.file.length > 44;
      const nameFull = needsPopover ? `<div class="name-full">${escapeHtml(it.file)}</div>` : '';
      div.innerHTML = thumbHtml + trashBadge + catIcon + `<div class="sz">${human(it.size)}</div>${creatorSubtitle}<div class="name">${escapeHtml(it.file)}</div>${nameFull}`;
      const clickHandler = it.perma_deleted
        ? () => toast('❌ 완전삭제된 파일입니다 (되돌릴 수 없음)')
        : it.trashed
        ? async () => {
            if (!confirm(`휴지통에서 복원할까요?\\n${it.file}`)) return;
            const res = await fetch('/api/restore', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({paths: [it.trash_path || it.path]}),
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
      div.querySelector('.sz').addEventListener('click', clickHandler);
      // 이름 클릭 = 파일명 복사 (확장자 제외) - 검색용
      const nameEl = div.querySelector('.name');
      const nameFullEl = div.querySelector('.name-full');
      const copyName = async (e) => {
        e.stopPropagation();
        const clean = it.file.replace(/\\.package$/i, '');
        try {
          await navigator.clipboard.writeText(clean);
          toast(`📋 복사됨: ${clean.slice(0, 40)}${clean.length > 40 ? '…' : ''}`);
        } catch { toast('복사 실패'); }
      };
      nameEl.style.cursor = 'copy';
      nameEl.title = '클릭하면 파일명 복사 (확장자 제외)';
      nameEl.addEventListener('click', copyName);
      if (nameFullEl) {
        nameFullEl.style.cursor = 'copy';
        nameFullEl.title = '클릭하면 파일명 복사 (확장자 제외)';
        nameFullEl.style.pointerEvents = 'auto';
        nameFullEl.addEventListener('click', copyName);
      }
      // 우클릭 or cat-icon 클릭 → 카테고리 변경 메뉴
      const catEl = div.querySelector('.cat-icon');
      if (catEl) {
        catEl.addEventListener('click', (e) => {
          e.stopPropagation();
          // 우클릭/카테고리 아이콘 클릭은 항상 그 아이템 하나만
          // (다중 적용은 하단 bulk 바나 드래그 앤 드롭 사용)
          showCategoryMenu(e, [it.path], it.primary_cat);
        });
      }
      div.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        showCategoryMenu(e, [it.path], it.primary_cat);
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
  if (totalItems > 0 && visibleItems === 0) {
    const empty = document.createElement('div');
    empty.className = 'progress';
    empty.style.cssText = 'padding:40px;';
    empty.innerHTML = `<div style="font-size:36px;margin-bottom:8px;">🔍</div>
      <h3 style="margin:0 0 6px 0;">일치하는 아이템 없음</h3>
      <div style="color:#666;margin-bottom:16px;">현재 필터 조건에 맞는 항목이 없어요.</div>
      <button class="blue" onclick="resetFilters()">필터 초기화</button>`;
    main.appendChild(empty);
  }
  updateStats(totalItems, visibleItems);
  updateFooter();
}

function resetFilters() {
  currentFilter = '';
  showMarkedOnly = false;
  showOverrideOnly = false;
  hideTrashed = false;
  hidePerma = false;
  metaFilter = '';
  subFilter = '';
  const s = document.getElementById('search'); if (s) s.value = '';
  const t1 = document.getElementById('tglMarked'); if (t1) t1.checked = false;
  const t2 = document.getElementById('tglOverride'); if (t2) t2.checked = false;
  const t3 = document.getElementById('tglHideTrashed'); if (t3) t3.checked = false;
  const t4 = document.getElementById('tglHidePerma'); if (t4) t4.checked = false;
  render();
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

function openHelp() {
  document.getElementById('help-dialog').showModal();
}

function showCategoryMenu(event, pathOrPaths, currentCat) {
  // 배열 or 단일 경로 둘 다 받음
  const paths = Array.isArray(pathOrPaths) ? pathOrPaths : [pathOrPaths];
  const isBulk = paths.length > 1;
  const menu = document.getElementById('ctxMenu');
  const cats = MANIFEST.all_categories || [];
  const label = isBulk
    ? `<b style="color:#4a90e2;">📌 다중 ${paths.length}개</b> 카테고리 변경`
    : `카테고리 변경 <span style="color:#999">(${paths[0].split('/').pop()})</span>`;
  let html = `<div class="ctx-header">${label}</div>`;
  for (const c of cats) {
    const cls = !isBulk && c.name === currentCat ? ' current' : '';
    html += `<div class="ctx-item${cls}" data-cat="${escapeHtml(c.name)}">${c.icon} ${c.name}</div>`;
  }
  html += `<div class="ctx-item reset" data-cat="">↺ 수동 지정 해제 (자동 재분류)</div>`;
  menu.innerHTML = html;
  const x = Math.min(event.clientX, window.innerWidth - 200);
  const y = Math.min(event.clientY, window.innerHeight - 300);
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
  menu.style.display = 'block';
  menu.querySelectorAll('.ctx-item').forEach(el => {
    el.onclick = async () => {
      menu.style.display = 'none';
      const cat = el.dataset.cat;
      await applyToPaths(paths, cat || '__reset__');
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
      ? `<img class="thumb-img" src="/thumbs/${it.thumbs[0]}" style="width:100%; display:block;">`
      : `<div class="no-thumb" style="padding:40px 8px; text-align:center; color:#999; font-size:11px; background:#eee;">썸네일 없음</div>`;
    const needsPopover = it.file.length > 44;
    const nameFull = needsPopover ? `<div class="name-full">${escapeHtml(it.file)}</div>` : '';
    return `<div class="item marked-tile" data-path="${escapeHtml(it.path)}" style="border:2px solid #d9534f; background:white;">
      ${thumbHtml}
      <button style="position:absolute; top:4px; right:4px; background:rgba(255,255,255,.9); border:1px solid #ccc; border-radius:50%; width:22px; height:22px; padding:0; cursor:pointer; font-size:12px; z-index:3;" title="표시 해제" onclick="unmarkFromDialog('${escapeAttr(it.path)}')">✕</button>
      <div class="sz">${human(it.size)}</div>
      <div class="name">${escapeHtml(it.file)}</div>
      ${nameFull}
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
      const needsPopover = fileName.length > 44;
      const nameFull = needsPopover ? `<div class="name-full">${escapeHtml(fileName)}</div>` : '';
      return `
        <label class="trash-tile item" data-thumbs='${JSON.stringify(thumbs).replace(/'/g, "&#39;")}' data-thumb-idx="0" style="background:white; display:block;">
          <input type="checkbox" value="${escapeHtml(it.path)}" onchange="updateTrashSelectedCount()" style="position:absolute; top:6px; left:6px; z-index:3; transform:scale(1.3);">
          ${thumbHtml}
          <div class="sz">${human(it.size)}</div>
          <div class="name">${escapeHtml(fileName)}</div>
          ${nameFull}
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
  // 매니페스트가 자동 업데이트되므로 재스캔 불필요.
  // 필요 시 사용자가 상단 '🔄 재스캔' 버튼 수동 클릭.
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
document.getElementById('tglHideTrashed').addEventListener('change', e => {
  hideTrashed = e.target.checked;
  render();
});
document.getElementById('tglHidePerma').addEventListener('change', e => {
  hidePerma = e.target.checked;
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
        # 캐시 방지: HTML/API는 항상 최신, 썸네일은 캐시 가능
        if ctype.startswith("text/html") or "json" in ctype:
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
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
                mime = "image/png" if name.lower().endswith(".png") else "image/jpeg"
                self._send(200, fp.read_bytes(), mime)
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
