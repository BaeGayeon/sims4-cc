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


def find_mods_root():
    """Mods 폴더 자동 감지. 캐시(mods_root) → 캐시(구버전 sims_root) → 환경변수 → 표준 경로 순."""
    # 캐시된 경로
    try:
        if _CONFIG_PATH.exists():
            cfg = json.loads(_CONFIG_PATH.read_text())
            mods = cfg.get("mods_root")
            if mods:
                p = Path(mods)
                if p.exists():
                    return p
            legacy_root = cfg.get("sims_root")  # 구버전: Sims 4 게임 폴더를 저장했었음
            if legacy_root:
                p = Path(legacy_root) / "Mods"
                if p.exists():
                    return p
    except Exception:
        pass
    candidates = []
    env = os.environ.get("SIMS4_MODS_PATH") or os.environ.get("SIMS4_PATH")
    if env:
        env_p = Path(env)
        candidates.append(env_p if env_p.name == "Mods" else env_p / "Mods")
    home = Path.home()
    candidates.append(home / "Documents" / "Electronic Arts" / "The Sims 4" / "Mods")
    candidates.append(home / "Games" / "Electronic Arts" / "The Sims 4" / "Mods")
    # 심볼릭 링크 따라가기
    ea = home / "Documents" / "Electronic Arts"
    if ea.exists():
        try:
            for child in ea.iterdir():
                if child.name == "The Sims 4" or child.name.startswith("The Sims 4"):
                    try:
                        resolved = child.resolve()
                    except Exception:
                        resolved = child
                    candidates.append(resolved / "Mods")
        except Exception:
            pass
    for p in candidates:
        try:
            if p and p.exists():
                # 캐시 저장
                try:
                    APP_STATE.mkdir(parents=True, exist_ok=True)
                    _CONFIG_PATH.write_text(json.dumps({"mods_root": str(p)}, indent=2))
                except Exception:
                    pass
                return p
        except Exception:
            continue
    print("\n" + "=" * 60)
    print("  Mods 폴더를 찾을 수 없습니다.")
    print("=" * 60)
    print("  다음 중 하나를 시도해 주세요:")
    print("   1) 환경변수로 지정:  export SIMS4_MODS_PATH=\"/경로/Mods\"")
    print("   2) 표준 위치에 설치: ~/Documents/Electronic Arts/The Sims 4/Mods")
    print("   3) 설정 파일 편집:   " + str(_CONFIG_PATH))
    print("      예: {\"mods_root\": \"/경로/Mods\"}")
    print("=" * 60 + "\n")
    raise SystemExit(1)


MODS = find_mods_root()
CC_ROOT = MODS / "CC FeaturedCreators"
THUMBS_DIR = APP_STATE / "thumbs"
MANIFEST_PATH = APP_STATE / "manifest.json"
TRASH_DIR = APP_STATE / "trash"

# 옛 위치들 → 새 위치 마이그레이션 (Sims 4 게임 폴더 바로 아래 있던 구버전 상태 파일)
_OLD_STATE = MODS.parent / ".cc_manager"
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

def _dbpf_header_info(path):
    """DBPF 헤더만 읽어서 유효성 확인. (ok, reason, index_count, index_offset)
    reason은 ok=False일 때만 채워짐 — 손상되거나 읽을 수 없는 .package를 UI에 표시하는 데 씀."""
    try:
        with open(path, "rb") as f:
            hdr = f.read(96)
    except OSError as e:
        return False, f"파일을 열 수 없음: {e}", 0, 0
    if len(hdr) < 96:
        return False, "헤더가 손상됨 (파일이 너무 작음)", 0, 0
    if hdr[:4] != b"DBPF":
        return False, "DBPF 형식이 아님", 0, 0
    version = struct.unpack("<I", hdr[4:8])[0]
    if version != 2:
        return False, f"지원하지 않는 DBPF 버전 ({version})", 0, 0
    index_count = struct.unpack("<I", hdr[36:40])[0]
    index_offset = struct.unpack("<I", hdr[64:68])[0]
    return True, None, index_count, index_offset


def parse_dbpf(path):
    ok, _reason, index_count, index_offset = _dbpf_header_info(path)
    if not ok or index_offset == 0 or index_count == 0:
        return
    try:
        with open(path, "rb") as f:
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


def parse_package_casp_and_thumbs(pkg_path, need_casp=True):
    """CASP 카테고리와 썸네일을 DBPF 인덱스 한 번만 순회해서 함께 추출.
    (스캔 시 카테고리용/썸네일용으로 따로 parse_dbpf를 두 번 부르면 인덱스를 매번 다시 읽게 됨)
    반환값에 readable/reason도 포함 — 헤더가 깨졌거나 열 수 없는 .package를 스캔 시 UI에 표시하는 데 씀
    (인덱스가 비어있을 뿐인 정상 파일은 손상으로 보지 않음)."""
    ok, reason, _index_count, _index_offset = _dbpf_header_info(pkg_path)
    if not ok:
        return None, [], False, reason
    casp_category = None
    thumbs = []
    seen = set()
    for entry in parse_dbpf(pkg_path):
        et = entry["type"]
        want_casp = need_casp and et == CASP_TYPE and casp_category is None
        want_thumb = et in THUMB_TYPES
        if not want_casp and not want_thumb:
            continue
        try:
            data = read_resource(pkg_path, entry)
        except Exception:
            continue
        if want_casp:
            name = parse_casp_name(data)
            if name:
                casp_type = extract_casp_type(name)
                if casp_type and casp_type.lower() in CASP_TYPE_MAP:
                    casp_category = CASP_TYPE_MAP[casp_type.lower()]
        if want_thumb:
            if data.startswith(b"\xff\xd8\xff"):
                ext = ".jpg"
            elif data.startswith(b"\x89PNG\r\n\x1a\n"):
                ext = ".png"
            else:
                continue
            h = hashlib.md5(data).hexdigest()
            if h not in seen:
                seen.add(h)
                thumbs.append((data, h, ext))
    return casp_category, thumbs, True, None


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


SCAN_PROGRESS = {"active": False, "current": 0, "total": 0, "name": ""}


def scan_cc(progress_cb=None):
    """전체 Mods 폴더 스캔 → 폴더 경로별 그룹."""
    # 스캔 도중 설정에서 Mods 경로가 바뀌어도(이 스캔 자체는) 시작 시점 경로 기준으로
    # 끝까지 일관되게 진행 — 전역 MODS를 매 파일마다 다시 읽지 않음
    mods_root = MODS
    if not mods_root.exists():
        return {"folders": [], "error": f"Mods 폴더 없음: {mods_root}"}
    SCAN_PROGRESS.update({"active": True, "current": 0, "total": 0, "name": "준비 중..."})

    try:
        # 폴더별 파일 그룹핑 - key는 Mods 기준 상대 폴더 경로
        from collections import defaultdict
        groups = defaultdict(list)
        all_pkgs = list(_iter_packages())
        total_pkgs = len(all_pkgs)
        total_thumbs = 0
        overrides = _load_overrides()

        for i, pkg in enumerate(all_pkgs, 1):
            rel_pkg = pkg.relative_to(mods_root)
            folder = str(rel_pkg.parent) if rel_pkg.parent != Path(".") else "(최상위)"
            SCAN_PROGRESS["current"] = i
            SCAN_PROGRESS["total"] = total_pkgs
            SCAN_PROGRESS["name"] = folder
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
                _, thumb_list, readable, unreadable_reason = parse_package_casp_and_thumbs(pkg, need_casp=False)
            else:
                is_override = False
                casp_cat, thumb_list, readable, unreadable_reason = parse_package_casp_and_thumbs(pkg)
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
            if not readable:
                item["unreadable"] = True
                item["unreadable_reason"] = unreadable_reason
            for img_bytes, h, ext in thumb_list:
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
        SCAN_PROGRESS.update({"current": total_pkgs, "total": total_pkgs, "name": "완료"})
        return manifest
    finally:
        # 도중에 예외가 나거나(예: 파싱 오류) Mods 경로가 바뀌어도 진행률 상태가
        # active=True로 영원히 멈춰서 프론트 폴링이 끝없이 도는 일이 없도록 보장
        SCAN_PROGRESS["active"] = False


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


def _compute_stats():
    m = load_manifest() or {"folders": [], "creators": []}
    folders = m.get("folders") or m.get("creators") or []
    trash_manifest = _load_trash_manifest()
    total_files = 0; total_size = 0
    creators = {}   # name -> {size, count}
    cats = {}       # cat -> {count, size}
    newest = 0; oldest = float("inf")
    unreadable_count = 0
    for f in folders:
        for it in f.get("items", []):
            if it.get("trashed") or it.get("perma_deleted"): continue
            total_files += 1
            if it.get("unreadable"): unreadable_count += 1
            sz = it.get("size", 0); total_size += sz
            cname = f.get("name", "?")
            c = creators.setdefault(cname, {"size": 0, "count": 0})
            c["size"] += sz; c["count"] += 1
            for cat in (it.get("cats") or [it.get("primary_cat") or "기타"]):
                cc = cats.setdefault(cat, {"count": 0, "size": 0})
                cc["count"] += 1; cc["size"] += sz
            mt = it.get("mtime", 0)
            if mt: newest = max(newest, mt); oldest = min(oldest, mt)
    trash_items = list(trash_manifest.values()) if isinstance(trash_manifest, dict) else []
    trash_count = sum(1 for x in trash_items if not x.get("perma_deleted"))
    trash_size = sum(x.get("size", 0) for x in trash_items if not x.get("perma_deleted"))
    top_creators = sorted(
        ({"name": n, "size": v["size"], "count": v["count"]} for n, v in creators.items()),
        key=lambda x: -x["size"]
    )[:10]
    cat_breakdown = sorted(
        ({"name": n, "count": v["count"], "size": v["size"]} for n, v in cats.items()),
        key=lambda x: -x["count"]
    )
    total_thumbs = sum(len(it.get("thumbs", [])) for f in folders for it in f.get("items", []))
    avg_size = (total_size / total_files) if total_files else 0
    return {
        "total_files": total_files,
        "total_size": total_size,
        "avg_size": avg_size,
        "total_thumbs": total_thumbs,
        "creator_count": len(creators),
        "top_creators": top_creators,
        "categories": cat_breakdown,
        "trash_count": trash_count,
        "trash_size": trash_size,
        "overrides_count": len(_load_overrides()),
        "unreadable_count": unreadable_count,
        "newest_mtime": newest,
        "oldest_mtime": oldest if oldest != float("inf") else 0,
    }


def _safe_path(root, rel):
    """rel이 root 밖으로 벗어나지 않도록 강제. 벗어나면(경로 순회 시도 등) None 반환."""
    if not rel:
        return None
    try:
        root_resolved = root.resolve()
        candidate = (root / rel).resolve()
        candidate.relative_to(root_resolved)
    except (OSError, ValueError):
        return None
    return candidate


def set_category_override(rel_path, category):
    """수동으로 카테고리 지정 (또는 해제)"""
    overrides = _load_overrides()
    if category is None or category == "":
        overrides.pop(rel_path, None)
    else:
        overrides[rel_path] = category
    _save_overrides(overrides)
    return {"ok": True}


def set_category_overrides_batch(rel_paths, category):
    """여러 파일의 카테고리를 한 번에 지정 (또는 해제).
    오버라이드 파일을 항목마다 따로 읽고 쓰지 않고 한 번만 읽고/병합하고/써서
    n개 일괄 지정 시 디스크 I/O를 O(n)에서 O(1)로 줄임."""
    overrides = _load_overrides()
    for rel_path in rel_paths:
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


def _update_manifest_items(paths, updates, per_item_updates=None):
    """매니페스트의 특정 파일들에 필드 업데이트 (재스캔 없이 즉시 반영).
    per_item_updates: {원본 path: {필드: 값}} — 항목별로 다른 값을 줘야 할 때 (예: trash_path)."""
    m = load_manifest()
    if not m: return
    path_set = set(paths)
    per_item_updates = per_item_updates or {}
    for folder in m.get("folders", []) + m.get("creators", []):
        for it in folder.get("items", []):
            if it["path"] in path_set or it.get("trash_path") in path_set:
                for k, v in updates.items():
                    it[k] = v
                for k, v in per_item_updates.get(it["path"], {}).items():
                    it[k] = v
    MANIFEST_PATH.write_text(json.dumps(m, ensure_ascii=False))


def move_to_trash(rel_paths):
    moved, failed = [], []
    trash_m = _load_trash_manifest()
    trash_rel_by_original = {}
    for rel in rel_paths:
        src = _safe_path(MODS, rel)
        if not src or not src.exists():
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
            trash_rel_by_original[rel] = trash_rel
        except Exception as e:
            failed.append({"path": rel, "reason": str(e)})
    _save_trash_manifest(trash_m)
    # 매니페스트에 상태 반영 (재스캔 안 해도 목록에 표시됨)
    if moved:
        # 이름 충돌로 리네임된 경우 실제 휴지통 경로(trash_path)를 항목별로 저장해야
        # 나중에 이 경로로 복원 요청을 보낼 수 있음
        per_item = {rel: {"trash_path": trash_rel_by_original[rel]} for rel in moved}
        _update_manifest_items(moved, {"trashed": True, "perma_deleted": False}, per_item_updates=per_item)
    return {"moved": moved, "failed": failed, "moved_trash_paths": trash_rel_by_original}


def restore_from_trash(rel_paths):
    restored, failed = [], []
    restored_originals = []
    trash_m = _load_trash_manifest()
    for rel in rel_paths:
        src = _safe_path(TRASH_DIR, rel)
        info = trash_m.get(rel, {})
        original = info.get("original_path", rel)
        dst = _safe_path(MODS, original)
        if not src or not src.exists() or not dst:
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
    # 복원 → trashed 플래그 제거, trash_path도 비워야 다음번 삭제 때 새 경로로 다시 채워짐
    if restored_originals:
        _update_manifest_items(restored_originals, {"trashed": False, "perma_deleted": False, "trash_path": None})
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
        fp = _safe_path(TRASH_DIR, rel)
        if fp and fp.exists() and fp.is_file():
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
        _update_manifest_items(perma_originals, {"trashed": False, "perma_deleted": True, "trash_path": None})
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
        _update_manifest_items(perma_originals, {"trashed": False, "perma_deleted": True, "trash_path": None})
    return {"count": count, "size_freed": total}


# ─────────── HTTP 서버 ───────────

HTML_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Sims 4 CC Manager</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; }
  /* ─── 디자인 토큰 (CC Manager 정리안 기준) ─── */
  :root {
    --c-blue: #2f6fd0;
    --c-blue-hover: #2560bb;
    --c-blue-bg: #eaf1fc;
    --c-red: #b23c2b;
    --c-red-hover: #9c3324;
    --c-red-bg: #fdeeeb;
    --c-bg: #f4f4f5;
    --c-surface: #ffffff;
    --c-surface-2: #f6f6f7;
    --c-seg-bg: #eeeeef;
    --c-border: rgba(0,0,0,.08);
    --c-border-strong: rgba(0,0,0,.13);
    --c-text: #19191b;
    --c-text-muted: #6b6b70;
    --c-text-subtle: #a3a3a8;
    --c-text-label: #86868c;
    --c-overlay: rgba(255,255,255,.92);
    --radius-sm: 7px;
    --radius-md: 10px;
    --radius-lg: 12px;
    --h-input: 32px;
    --h-thumb: 130px;
    --shadow-sm: 0 1px 3px rgba(0,0,0,.06);
    --shadow-md: 0 6px 20px rgba(0,0,0,.1);
    --shadow-lg: 0 12px 32px rgba(0,0,0,.18);
  }
  /* Noto Sans KR 모든 요소 */
  body, button, input, select, textarea, optgroup, option, kbd { font-family: 'Noto Sans KR', -apple-system, 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif; }
  input::placeholder { font-family: inherit; color: var(--c-text-subtle); }
  body { margin: 0; background: var(--c-bg); color: var(--c-text); font-size: 13px; -webkit-font-smoothing: antialiased; }
  header { position: sticky; top: 0; background: var(--c-surface); border-bottom: 1px solid var(--c-border); padding: 0; z-index: 100; }
  .title-row { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; padding: 11px 16px; border-bottom: 1px solid var(--c-border); }
  .title-row h1 { margin: 0; font-size: 15px; font-weight: 700; flex-shrink: 0; white-space: nowrap; letter-spacing: -.2px; }
  .stats { color: var(--c-text-label); font-size: 11.5px; display: flex; gap: 12px; flex-shrink: 0; white-space: nowrap; }
  .stats b { color: var(--c-text-muted); font-weight: 600; }
  /* 접기 대상 영역 */
  /* title-row와 좌우 여백을 통일 (16px) - 이전엔 .row가 여백 없이 붙어 있었음 */
  .collapsible-body { padding: 0 16px; }
  body.header-collapsed .collapsible-body { display: none; }
  body.header-collapsed #collapseBtn { transform: rotate(180deg); }
  #collapseBtn { transition: transform .15s; }
  /* 반응형: 좁을수록 접기 */
  @media (max-width: 1200px) {
    .stats { flex-basis: 100%; order: 10; padding: 2px 0 0; }
  }
  @media (max-width: 900px) {
    #modeSeg button { padding: 0 10px !important; font-size: 11px !important; }
    button { padding: 0 10px; font-size: 11px; }
    /* 라벨은 유지 - 사용자가 뭔지 알아야 함 */
  }
  @media (max-width: 640px) {
    .title-row, .collapsible-body { padding-left: 10px; padding-right: 10px; }
    .row { padding: 4px 0; }
    #search { width: 100% !important; }
    #footer { padding: 6px 10px; }
  }
  .row { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; padding: 5px 0; border-top: 1px solid var(--c-border); }
  .row:first-of-type { border-top: none; padding-top: 3px; }
  /* 필터 팝오버 행: 배경이 채워지는 카드라서 자체 여백을 따로 줘야 내용이 테두리에 안 붙음 */
  #filterPanel { border-radius: var(--radius-md); padding: 10px 14px; margin: 4px 0; border-top: none; }
  .group { display: inline-flex; align-items: center; gap: 6px; background: var(--c-bg); padding: 4px 10px; border-radius: var(--radius-sm); height: var(--h-input); }
  .group .label { color: var(--c-text-muted); font-size: 11px; margin: 0; }
  .divider { width: 1px; height: 20px; background: var(--c-border); margin: 0 4px; }
  .toggle-group { display: inline-flex; align-items: center; gap: 12px; }

  /* ─── 통일된 폼 컨트롤 (모두 32px 높이) ─── */
  input[type=text], input[type=search] {
    height: var(--h-input); padding: 0 12px; border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm); font-size: 13px; background: var(--c-surface);
    color: var(--c-text); transition: border-color .12s, box-shadow .12s;
  }
  input[type=text]:focus, input[type=search]:focus {
    outline: none; border-color: var(--c-blue); box-shadow: 0 0 0 3px rgba(59,130,246,.15);
  }
  select {
    height: var(--h-input); padding: 0 28px 0 10px; border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm); font-size: 12px; background: var(--c-surface);
    color: var(--c-text); cursor: pointer;
    appearance: none; -webkit-appearance: none;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 10 10'><path d='M2 4 L5 7 L8 4' stroke='%236b7280' stroke-width='1.4' fill='none'/></svg>");
    background-repeat: no-repeat; background-position: right 10px center;
  }
  select:focus { outline: none; border-color: var(--c-blue); box-shadow: 0 0 0 3px rgba(59,130,246,.15); }

  /* ─── 통일된 버튼 (모두 32px 높이) ─── */
  button {
    height: var(--h-input); padding: 0 14px; font-size: 12px; font-weight: 500;
    background: var(--c-surface); border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm); cursor: pointer; color: var(--c-text);
    transition: background-color .12s, border-color .12s, transform .06s;
    display: inline-flex; align-items: center; gap: 5px; white-space: nowrap;
  }
  button:hover { background: var(--c-bg); border-color: var(--c-text-muted); }
  button:active { transform: translateY(1px); }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  button.primary { background: var(--c-red); color: white; border-color: var(--c-red-hover); }
  button.primary:hover { background: var(--c-red-hover); border-color: var(--c-red-hover); }
  button.blue { background: var(--c-blue); color: white; border-color: var(--c-blue-hover); }
  button.blue:hover { background: var(--c-blue-hover); border-color: var(--c-blue-hover); }
  button.icon-only { width: var(--h-input); padding: 0; justify-content: center; font-size: 15px; border-radius: 50%; }
  /* 오버플로우 (⋯) 메뉴 */
  .menu-wrap { display: inline-block; }
  /* position:fixed + JS 계산 좌표 사용 (flex-wrap 환경에서도 항상 정확) */
  .overflow-menu { display: none; position: fixed; background: var(--c-surface); border: 1px solid var(--c-border); border-radius: var(--radius-md); box-shadow: var(--shadow-lg); padding: 4px; z-index: 300; width: 170px; }
  .overflow-menu.open { display: block; }
  .overflow-menu button { display: flex; width: 100%; justify-content: flex-start; border: none; background: transparent; padding: 8px 12px; border-radius: 4px; text-align: left; height: auto; }
  .overflow-menu button:hover { background: var(--c-bg); }
  .overflow-menu hr { border: 0; border-top: 1px solid var(--c-border); margin: 4px 0; }
  /* 헤더 접기 상태: 첫 title-row + stats 만 보임 */
  body.header-collapsed header > .row { display: none; }

  /* Segmented toggle (묶음/모드 공용 — 색코딩 없음, 흰 알약이 선택 표시) */
  .seg { display: inline-flex; align-items: center; height: var(--h-input); border: none; border-radius: 9px; overflow: hidden; background: var(--c-seg-bg); padding: 3px; gap: 2px; box-sizing: border-box; }
  .seg button { border: none !important; border-radius: 6px !important; padding: 0 12px !important; background: transparent; font-size: 12px; font-weight: 500; height: 26px !important; color: var(--c-text-muted); box-shadow: none; }
  .seg button.on { background: var(--c-surface) !important; color: var(--c-text) !important; font-weight: 600; box-shadow: 0 1px 2px rgba(0,0,0,.12); }
  .seg button:not(.on):hover { color: var(--c-text-muted); }
  #modeSeg button { padding: 0 14px !important; }
  /* Switch toggle */
  .switch { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--c-text-muted); cursor: pointer; user-select: none; }
  .switch input { appearance: none; -webkit-appearance: none; width: 32px; height: 18px; background: var(--c-border-strong); border-radius: 10px; position: relative; cursor: pointer; transition: background .2s; margin: 0; }
  .switch input:checked { background: var(--c-blue); }
  .switch input::before { content: ''; position: absolute; top: 2px; left: 2px; width: 14px; height: 14px; background: var(--c-surface); border-radius: 50%; transition: transform .2s; }
  .switch input:checked::before { transform: translateX(14px); }
  /* 필터 팝오버 안 토글 칩 (스위치 대신 파란 알약) */
  .chip-toggle { height: 28px; padding: 0 12px; border-radius: 14px; background: var(--c-surface); border: 1px solid var(--c-border-strong); color: var(--c-text-muted); font-size: 11.5px; font-weight: 500; }
  .chip-toggle:hover { background: var(--c-bg); }
  .chip-toggle.on { background: var(--c-blue-bg); color: var(--c-blue); border-color: var(--c-blue); font-weight: 600; }
  .pill-btn { height: 28px; padding: 0 11px; border-radius: 14px; background: var(--c-surface); border: 1px solid var(--c-border-strong); color: var(--c-text-muted); font-size: 11.5px; font-weight: 500; }
  .pill-btn:hover { background: var(--c-bg); }
  .pill-btn.danger { color: var(--c-red); }
  .pill-btn.danger:hover { background: var(--c-red-bg); }
  #filterToggleBtn.active { border-color: var(--c-blue) !important; background: var(--c-blue-bg) !important; color: var(--c-blue) !important; font-weight: 600; }
  /* 카테고리 알약: 미선택 흰 배경, 선택 시 진한 배경(색 코딩 없이 무채색 강조) */
  .chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .chip { height: 30px; padding: 0 12px; border-radius: 15px; background: var(--c-surface); border: 1px solid var(--c-border-strong); cursor: pointer; font-size: 12px; font-weight: 500; display: inline-flex; align-items: center; gap: 7px; transition: all .12s; color: var(--c-text-muted); }
  .chip:hover { background: var(--c-bg); }
  .chip.on { background: var(--c-text); color: var(--c-bg); border-color: var(--c-text); font-weight: 600; }
  .chip .count { font: 600 10.5px ui-monospace, Menlo, monospace; color: var(--c-text-subtle); }
  .chip.on .count { color: var(--c-bg); opacity: .7; }
  .subchips { padding-left: 16px; }
  #subRow { padding-top: 0 !important; }
  .subchip { height: 28px; padding: 0 11px; border-radius: 14px; background: var(--c-surface); border: 1px solid var(--c-border-strong); cursor: pointer; font-size: 11.5px; font-weight: 500; color: var(--c-text-muted); }
  .subchip:hover { background: var(--c-bg); }
  .subchip.on { background: var(--c-text); color: var(--c-bg); border-color: var(--c-text); font-weight: 600; }
  .subchip .count { font: 600 10px ui-monospace, Menlo, monospace; color: var(--c-text-subtle); margin-left: 3px; }
  .subchip.on .count { color: var(--c-bg); opacity: .7; }
  .label { font-size: 11.5px; color: var(--c-text-label); margin-right: 4px; font-weight: 500; white-space: nowrap; flex-shrink: 0; }
  main { padding: 16px; }
  .creator { background: var(--c-surface); margin-bottom: 14px; border-radius: var(--radius-md); overflow: visible; box-shadow: var(--shadow-sm); border: 1px solid var(--c-border); }
  .creator-header { padding: 12px 14px; background: var(--c-surface); border-bottom: 1px solid var(--c-border); display: flex; align-items: center; gap: 10px; cursor: pointer; user-select: none; }
  .creator-header .crumb-parent { font-size: 11.5px; color: var(--c-text-subtle); }
  .creator-header h2 { margin: 0; font-size: 13px; font-weight: 600; }
  .creator-count { color: var(--c-text-label); font-size: 11.5px; }
  .creator-marked-badge { font-size: 10.5px; font-weight: 600; color: var(--c-red); background: var(--c-red-bg); border-radius: 11px; padding: 3px 8px; }
  .creator-actions { display: flex; gap: 6px; margin-left: auto; }
  .creator-actions button { height: 28px !important; padding: 0 10px !important; font-size: 11.5px !important; font-weight: 500 !important; color: var(--c-text-muted); }
  .creator-actions button.danger-hover:hover { background: var(--c-red-bg) !important; color: var(--c-red) !important; border-color: rgba(178,60,43,.3) !important; }
  .grid { padding: 14px; display: flex; flex-wrap: wrap; gap: 12px; position: relative; }
  .item { position: relative; width: var(--h-thumb); cursor: pointer; }
  /* CAS 썸네일 원본 비율(104x148, 세로형)을 유지 - 정사각형으로 강제하지 않음 */
  .item .thumb-frame { position: relative; width: var(--h-thumb); aspect-ratio: 104 / 148; border-radius: 9px; overflow: hidden; border: 2px solid var(--c-border-strong); box-sizing: border-box; background: var(--c-surface-2); }
  .item .thumb-img { display: block; width: 100%; height: 100%; object-fit: cover; background: var(--c-surface-2); }
  .item:hover .thumb-frame { border-color: var(--c-blue); }
  body.mode-category .item:not(.trashed):hover { cursor: cell; }
  body.mode-category .item:not(.trashed):hover .thumb-frame { border-color: var(--c-blue); }
  /* 삭제 표시: 빨간 테두리 + 반투명 빨강 오버레이 (텍스트도 취소선) */
  .item.marked .thumb-frame { border-color: #d6705f; }
  .item.marked .thumb-frame::after {
    content: ''; position: absolute; inset: 0; background: rgba(178,60,43,.24); pointer-events: none;
  }
  .item.marked .name { color: var(--c-red); text-decoration: line-through; }
  body.mode-category .item:not(.trashed).marked .thumb-frame { opacity: 0.5; }
  /* 다중 선택: 파란 테두리 + 좌상단 체크 뱃지 */
  .item.selected .thumb-frame { border-color: var(--c-blue) !important; box-shadow: 0 0 0 3px rgba(47,111,208,.18); }
  .item.selected .sel-badge { display: flex; }
  .sel-badge { display: none; position: absolute; left: 5px; top: 5px; width: 17px; height: 17px; border-radius: 5px; background: var(--c-blue); color: #fff; font-size: 10px; font-weight: 700; align-items: center; justify-content: center; z-index: 3; }
  .item.kb-focus .thumb-frame { outline: 2px dashed var(--c-blue); outline-offset: 2px; }
  .chip.drop-target { background: #ffe066 !important; color: #333 !important; border-color: #f0a500 !important; transform: scale(1.1); }
  .item.dragging { opacity: 0.4; }
  body { padding-bottom: 66px; }
  .item.trashed .thumb-frame { opacity: 0.55; border-color: var(--c-border-strong); filter: grayscale(0.6); }
  .item.perma-deleted .thumb-frame { opacity: 0.35; filter: grayscale(1) brightness(0.7); }
  .item.perma-deleted .name, .item.perma-deleted .name-full { text-decoration: line-through; color: var(--c-text-subtle); }
  .trash-badge { position: absolute; top: 5px; left: 5px; background: rgba(20,19,18,.72); color: white; padding: 2px 6px; border-radius: 4px; font-size: 9.5px; z-index: 3; }
  .unreadable-badge { position: absolute; right: 5px; bottom: 5px; background: rgba(178,60,43,.88); color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 9.5px; font-weight: 600; z-index: 3; cursor: help; }
  /* 카테고리 텍스트 뱃지: 썸네일 우상단 흰 알약 */
  .cat-pill { position: absolute; right: 5px; top: 5px; font-size: 9px; font-weight: 600; color: var(--c-text-muted); background: var(--c-overlay); border-radius: 9px; padding: 2px 6px; box-shadow: 0 1px 2px rgba(0,0,0,.12); z-index: 2; cursor: context-menu; max-width: 78%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cat-pill.override { background: #fff3c4; box-shadow: 0 0 0 1px #d4a017; }
  /* 카테고리 변경 메뉴: 클릭한 아이템에 앵커되는 팝업 (전역 고정 아님) */
  #ctxMenu { position: absolute; background: var(--c-surface); border: 1px solid var(--c-border-strong); border-radius: var(--radius-md); box-shadow: var(--shadow-lg); z-index: 20; width: 300px; display: none; overflow: hidden; }
  #ctxMenu .ctx-header { padding: 11px 13px 9px; border-bottom: 1px solid var(--c-border); }
  #ctxMenu .ctx-title { font-size: 11.5px; font-weight: 600; color: var(--c-text); }
  #ctxMenu .ctx-file { font: 400 10.5px ui-monospace, Menlo, monospace; color: var(--c-text-subtle); margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #ctxMenu .ctx-search-wrap { padding: 9px 11px; }
  #ctxMenu .ctx-search { width: 100%; height: 29px; padding: 0 10px; border: 1px solid var(--c-border-strong); border-radius: 6px; background: var(--c-surface-2); font-size: 11.5px; outline: none; }
  #ctxMenu .ctx-search:focus { border-color: var(--c-blue); background: var(--c-surface); }
  #ctxMenu .ctx-list { max-height: 250px; overflow-y: auto; padding: 0 7px 8px; }
  #ctxMenu .ctx-section { font-size: 10px; font-weight: 600; color: var(--c-text-subtle); letter-spacing: .06em; padding: 9px 6px 5px; }
  #ctxMenu .ctx-item { display: flex; align-items: center; justify-content: space-between; padding: 7px 9px; border-radius: 6px; cursor: pointer; font-size: 12px; color: var(--c-text-muted); }
  #ctxMenu .ctx-item:hover { background: var(--c-bg); }
  #ctxMenu .ctx-item.current { color: var(--c-blue); background: var(--c-blue-bg); font-weight: 600; }
  #ctxMenu .ctx-check { color: var(--c-blue); font-weight: 700; font-size: 11px; opacity: 0; }
  #ctxMenu .ctx-item.current .ctx-check { opacity: 1; }
  #ctxMenu .ctx-reset { border-top: 1px solid var(--c-border); margin: 4px 7px 8px; padding: 9px 9px 0; color: var(--c-red); cursor: pointer; font-size: 11.5px; }
  .creator-sub { padding: 0 6px 2px; font-size: 9px; color: var(--c-text-subtle); font-style: italic; }
  .item .no-thumb { display: flex; width: 100%; height: 100%; align-items: center; justify-content: center; text-align: center; color: var(--c-text-subtle); font-size: 11px; background: repeating-linear-gradient(135deg, var(--c-surface-2) 0 6px, var(--c-seg-bg) 6px 12px); }
  /* 이름: 썸네일 아래 일반 흐름, 2줄까지 잘리지 않고 완전히 보임 */
  .item .name { margin-top: 6px; padding: 0 2px; font-size: 10.5px; line-height: 1.4; color: var(--c-text-muted); word-break: break-all; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; text-overflow: ellipsis; }
  .item .name-full { display: none; position: absolute; left: 0; right: 0; top: 100%; margin-top: -2px; padding: 4px 6px; font-size: 10.5px; color: var(--c-text); word-break: break-all; line-height: 1.4; background: var(--c-surface); z-index: 100; box-shadow: 0 4px 10px rgba(0,0,0,.15); border-radius: 4px; }
  .item:hover .name-full { display: block; }
  .item .sz { position: absolute; left: 5px; bottom: 5px; background: rgba(20,19,18,.62); color: white; padding: 2px 5px; font-size: 9.5px; font-weight: 600; font-family: ui-monospace, Menlo, monospace; border-radius: 4px; pointer-events: none; z-index: 2; }
  .thumb-img, .no-thumb { cursor: pointer; }
  /* Thumb nav: 썸네일 하단 중앙 (사이즈뱃지·카테고리필 피해서) */
  .thumb-nav { position: absolute; bottom: 5px; left: 50%; transform: translateX(-50%); display: flex; align-items: center; gap: 4px; opacity: 0; transition: opacity .15s; pointer-events: none; background: rgba(20,19,18,.72); border-radius: 14px; padding: 2px 4px; z-index: 3; }
  .item:hover .thumb-nav { opacity: 1; pointer-events: auto; }
  .thumb-btn { background: rgba(255,255,255,.2); color: white; border: none; border-radius: 50%; width: 18px; height: 18px; font-size: 9px; padding: 0; cursor: pointer; display: flex; align-items: center; justify-content: center; }
  .thumb-btn:hover { background: rgba(255,255,255,.4); }
  .thumb-counter { color: white; padding: 0 4px; font-size: 9px; font-variant-numeric: tabular-nums; }
  .collapsed .grid, .collapsed .creator-actions { display: none; }
  /* 병합된 하단 바: 선택됨 + 삭제표시 + 카테고리 지정 + 휴지통 이동 전부 한 줄 */
  /* flex-wrap: 좁은 화면에서는 줄바꿈으로 처리 - 글자 단위로 깨지는 것 방지 */
  #footer { position: fixed; bottom: 0; left: 0; right: 0; min-height: 58px; height: auto; padding: 8px 16px; background: var(--c-surface); border-top: 1px solid var(--c-border-strong); box-shadow: 0 -2px 10px rgba(0,0,0,.04); z-index: 100; display: flex; flex-wrap: wrap; align-items: center; gap: 10px 14px; }
  .footer-info { display: flex; flex-direction: column; gap: 2px; min-width: 190px; flex-shrink: 0; cursor: pointer; user-select: none; }
  .footer-sel-count { font-size: 12.5px; font-weight: 600; color: var(--c-text); }
  .footer-sub { font-size: 11px; color: var(--c-text-label); }
  .footer-sub b { font-weight: 600; }
  .footer-sub .red { color: var(--c-red); }
  /* 관련 컨트롤을 한 덩어리로 묶어서 줄바꿈 시에도 서로 떨어지지 않게 함 (라벨+셀렉트+버튼, 구분선+버튼 등) */
  .footer-group { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
  #footer select { height: 34px; flex-shrink: 0; }
  #footer button.blue, #footer button.danger-outline { height: 34px; padding: 0 16px; font-weight: 600; font-size: 12.5px; flex-shrink: 0; }
  #footer .divider { flex-shrink: 0; }
  #footer > div[style*="flex:1"] { flex-shrink: 1; min-width: 0; }
  .danger-outline { background: var(--c-surface); color: var(--c-red); border: 1px solid rgba(178,60,43,.35) !important; }
  .danger-outline:hover:not(:disabled) { background: var(--c-red) !important; color: #fff !important; border-color: var(--c-red) !important; }
  .toast { position: fixed; bottom: 90px; left: 50%; transform: translateX(-50%); background: #333; color: white; padding: 10px 20px; border-radius: 6px; opacity: 0; transition: opacity .3s; z-index: 200; }
  .toast.show { opacity: 1; }
  .badge { display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 10px; margin-left: 4px; }
  .badge.warn { background: #fff3cd; color: #856404; }
  .progress { background: var(--c-surface-2); color: var(--c-text); padding: 20px; border-radius: 8px; margin: 40px auto; max-width: 500px; text-align: center; }
  /* 빈 상태: 배경 박스 없이 심플하게 */
  .empty-state { text-align: center; padding: 60px 20px; color: var(--c-text-muted); }
  .empty-state-icon { font-size: 32px; opacity: .5; margin-bottom: 10px; }
  .empty-state-title { font-size: 14px; font-weight: 600; color: var(--c-text); margin-bottom: 4px; }
  .empty-state-desc { font-size: 12px; margin-bottom: 14px; }
  .progress-bar { height: 8px; background: var(--c-border-strong); border-radius: 4px; overflow: hidden; margin-top: 12px; }
  .progress-bar > div { height: 100%; background: var(--c-blue); transition: width .3s; }
  dialog { border: none; border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,.2); padding: 20px; max-width: 500px; background: var(--c-surface); color: var(--c-text); }
  dialog::backdrop { background: rgba(0,0,0,.5); }
  /* 모달 우측 상단 X 닫기 버튼: 카드 안쪽에 온전히 위치, 다른 아이콘 버튼과 동일한 톤 */
  .dlg-close { position: absolute; top: 14px; right: 14px; width: 26px; height: 26px; padding: 0; font-size: 13px; border-radius: 50%; background: transparent; border: none; color: var(--c-text-muted); cursor: pointer; z-index: 10; display: flex; align-items: center; justify-content: center; }
  kbd { background: var(--c-surface-2); color: var(--c-text); border: 1px solid var(--c-border-strong); border-bottom-width: 2px; border-radius: 3px; padding: 1px 6px; font-size: 11px; font-family: -apple-system, monospace; }
  .dlg-close:hover { background: var(--c-bg); color: var(--c-text); }
  dialog { position: relative; }
  /* 삭제표시 목록 다이얼로그의 개별 아이템 "표시 해제" 버튼 - 카테고리 펄과 동일한 코너 배치 */
  .marked-remove-btn { position: absolute; top: 5px; right: 5px; width: 20px; height: 20px; border-radius: 50%; border: none; background: var(--c-overlay); color: var(--c-text-muted); font-size: 11px; padding: 0; cursor: pointer; z-index: 4; box-shadow: 0 1px 2px rgba(0,0,0,.12); display: flex; align-items: center; justify-content: center; }
  .marked-remove-btn:hover { background: var(--c-red); color: #fff; }
  /* 휴지통 목록: 그리드 셀 폭에 맞춰 썸네일 프레임을 채움 (고정 --h-thumb 대신) */
  .trash-tile.item, .marked-tile.item { width: 100%; }
  .trash-tile .thumb-frame, .marked-tile .thumb-frame { width: 100%; }
  .trash-check { position: absolute; top: 6px; left: 6px; z-index: 4; transform: scale(1.2); }
  .trash-item { padding: 6px 8px; border-bottom: 1px solid var(--c-border); display: flex; gap: 8px; font-size: 12px; align-items: center; }
  .trash-item input { margin: 0; }
  /* ── 다크 모드: 디자인 토큰(색상 변수)만 재정의 → 모든 컴포넌트가 자동으로 대응됨 ── */
  html[data-theme="light"] { color-scheme: light; }
  html[data-theme="dark"] {
    color-scheme: dark;
    --c-blue: #6fa4ec;
    --c-blue-hover: #86b3ef;
    --c-blue-bg: rgba(111,164,236,.16);
    --c-red: #e2695c;
    --c-red-hover: #e87e72;
    --c-red-bg: rgba(226,105,92,.16);
    --c-bg: #19191a;
    --c-surface: #232324;
    --c-surface-2: #2a2a2c;
    --c-seg-bg: #2a2a2c;
    --c-border: rgba(255,255,255,.08);
    --c-border-strong: rgba(255,255,255,.14);
    --c-text: #f2f2f3;
    --c-text-muted: #a8a8ac;
    --c-text-subtle: #6e6e72;
    --c-text-label: #8a8a8e;
    --c-overlay: rgba(42,42,44,.88);
  }
  @media (prefers-color-scheme: dark) {
    html:not([data-theme="light"]) {
      color-scheme: dark;
      --c-blue: #6fa4ec;
      --c-blue-hover: #86b3ef;
      --c-blue-bg: rgba(111,164,236,.16);
      --c-red: #e2695c;
      --c-red-hover: #e87e72;
      --c-red-bg: rgba(226,105,92,.16);
      --c-bg: #19191a;
      --c-surface: #232324;
      --c-surface-2: #2a2a2c;
      --c-seg-bg: #2a2a2c;
      --c-border: rgba(255,255,255,.08);
      --c-border-strong: rgba(255,255,255,.14);
      --c-text: #f2f2f3;
      --c-text-muted: #a8a8ac;
      --c-text-subtle: #6e6e72;
      --c-text-label: #8a8a8e;
      --c-overlay: rgba(42,42,44,.88);
    }
  }
</style>
</head><body>
<header>
  <!-- 브랜드 바: 로고 + 통계 + 모드 + 상시 액션 (전부 한 줄, 색 코딩 없이 무채색) -->
  <div class="title-row">
    <div style="display:flex; align-items:center; gap:9px; flex-shrink:0;">
      <div style="width:22px; height:22px; border-radius:6px; background:var(--c-text);"></div>
      <h1>CC Manager</h1>
    </div>
    <div class="stats" id="stats">로딩 중...</div>
    <div style="flex:1;"></div>
    <div class="seg" id="modeSeg" title="선택 모드">
      <button data-v="delete" class="on">삭제 선택</button>
      <button data-v="category">카테고리 편집</button>
    </div>
    <div class="divider"></div>
    <button onclick="undo()" id="undoBtn" title="되돌리기 (Cmd+Z)" class="icon-only" disabled>↶</button>
    <button onclick="rescan()" title="Mods 폴더 다시 스캔">재스캔</button>
    <button onclick="openTrash()" title="휴지통">휴지통</button>
    <button onclick="openStats()" title="통계">통계</button>
    <div class="menu-wrap">
      <button onclick="toggleOverflowMenu(event)" class="icon-only" title="더보기">···</button>
      <div id="overflowMenu" class="overflow-menu">
        <button onclick="openSettings(); closeOverflow();">설정</button>
        <button onclick="openHelp(); closeOverflow();">도움말</button>
      </div>
    </div>
    <button onclick="toggleHeaderCollapse()" id="collapseBtn" class="icon-only" title="헤더 접기/펼치기">▲</button>
    <input type="file" id="importOvFile" accept=".json,application/json" style="display:none;">
  </div>

  <div class="collapsible-body">
  <div class="row">
    <div style="position:relative; width:280px;">
      <div style="position:absolute; left:11px; top:0; bottom:0; display:flex; align-items:center; font-size:12px; color:var(--c-text-subtle); pointer-events:none;">⌕</div>
      <input type="text" id="search" placeholder="파일 · 폴더 · 제작자 검색" style="width:100%; padding-left:28px; padding-right:32px;" autocomplete="off">
      <button id="searchClear" type="button" title="지우기" style="position:absolute; right:6px; top:50%; transform:translateY(-50%); width:20px; height:20px; padding:0; min-width:0; border-radius:50%; border:none; background:var(--c-border-strong); color:var(--c-text-muted); cursor:pointer; font-size:10px; display:none; line-height:1; align-items:center; justify-content:center;">✕</button>
      <div id="searchHistory" style="display:none; position:absolute; top:calc(100% + 4px); left:0; right:0; background:var(--c-surface); border:1px solid var(--c-border); border-radius:var(--radius-md); z-index:200; box-shadow:var(--shadow-md); overflow:hidden;"></div>
    </div>
    <div class="divider"></div>
    <span class="label">그룹</span>
    <div class="seg" id="groupSeg">
      <button data-v="creator" class="on">폴더별</button>
      <button data-v="category">카테고리별</button>
      <button data-v="date">날짜별</button>
    </div>
    <select id="groupSortBy" title="그룹(폴더) 정렬" style="margin-left:4px;">
      <option value="name">이름순</option>
      <option value="size">용량순</option>
      <option value="recent">최신순</option>
    </select>
    <span class="label" style="margin-left:6px;">아이템 정렬</span>
    <select id="itemSortBy">
      <option value="name">이름순</option>
      <option value="size">용량순</option>
      <option value="recent">날짜순</option>
    </select>
    <div style="flex:1;"></div>
    <button id="filterToggleBtn" onclick="toggleFilterPanel()" title="자주 안 쓰는 필터·선택 도구">필터</button>
    <div class="divider"></div>
    <span class="label">썸네일</span>
    <input type="range" id="zoomSlider" min="80" max="240" value="130" style="width: 96px; height: 20px; accent-color: var(--c-blue);" title="아이템 썸네일 크기">
  </div>

  <div class="row" id="filterPanel" style="background:var(--c-surface-2); display:none;">
    <button class="chip-toggle" data-filter="tglMarked" onclick="toggleChipFilter('tglMarked')">지울 것만</button>
    <button class="chip-toggle" data-filter="tglOverride" onclick="toggleChipFilter('tglOverride')">수동 지정만</button>
    <button class="chip-toggle" data-filter="tglUnreadable" onclick="toggleChipFilter('tglUnreadable')">읽기 실패만</button>
    <button class="chip-toggle" data-filter="tglHideTrashed" onclick="toggleChipFilter('tglHideTrashed')">휴지통 항목 숨기기</button>
    <button class="chip-toggle on" data-filter="tglHidePerma" onclick="toggleChipFilter('tglHidePerma')">완전삭제 숨기기</button>
    <button class="chip-toggle" data-filter="tglCollapsed" onclick="toggleChipFilter('tglCollapsed')">모두 접기</button>
    <div class="divider"></div>
    <span class="label">일괄 작업</span>
    <button onclick="selectAllFiltered()" title="지금 화면에 보이는 아이템 모두를 다중선택에 추가" class="pill-btn">전체 선택</button>
    <button onclick="clearBulkSel()" title="다중선택 전체 해제" class="pill-btn">전체 선택 해제</button>
    <button onclick="clearMarks()" title="삭제 표시 전체 해제" class="pill-btn danger">삭제표시 전체 해제</button>
  </div>

  <div class="row">
    <span class="label">카테고리</span>
    <div class="chips" id="metaChips" style="flex:1;"></div>
  </div>
  <div class="row" id="subRow" style="display:none;">
    <div class="chips subchips" id="subChips"></div>
  </div>
  </div><!-- /collapsible-body -->
</header>
<main id="main"><div class="progress">로딩 중...</div></main>
<div id="footer">
  <div class="footer-info" onclick="openMarkedDialog()" title="삭제 표시된 아이템 목록 보기">
    <div class="footer-sel-count"><b id="bulkCount">0</b>개 선택됨</div>
    <div class="footer-sub">삭제 표시 <b id="marked-count">0</b>개 · 절약 <b id="marked-size" class="red">0 B</b></div>
  </div>
  <button class="pill-btn" onclick="clearBulkSel()" title="다중선택 전체 해제">전체 선택 해제</button>
  <div style="flex:1;"></div>
  <div class="footer-group">
    <span class="label">선택 항목을</span>
    <select id="bulkCat"><option value="">카테고리 선택…</option></select>
    <button class="blue" onclick="applyBulkCategory()" id="applyCatBtn" disabled>카테고리 지정</button>
  </div>
  <div class="footer-group">
    <div class="divider"></div>
    <button class="danger-outline" onclick="performDelete()" id="toTrashBtn" disabled>휴지통으로 이동</button>
  </div>
</div>
<dialog id="settings-dialog" style="max-width: 560px; width: 90vw;">
  <button class="dlg-close" onclick="document.getElementById('settings-dialog').close()">✕</button>
  <h3 style="margin: 0 0 16px;">⚙️ 설정</h3>
  <div style="display: flex; flex-direction: column; gap: 16px;">
    <div>
      <label style="font-weight: 600; font-size: 13px;">🌓 테마</label>
      <div class="seg" id="themeSeg" style="display:inline-flex; margin-left:8px;">
        <button data-v="auto" class="on">시스템</button>
        <button data-v="light">라이트</button>
        <button data-v="dark">다크</button>
      </div>
    </div>
    <div>
      <label style="font-weight: 600; font-size: 13px;">📂 Mods 폴더 경로</label>
      <div style="font-size: 12px; color: var(--c-text-muted); margin-top: 4px;" id="currentPath"></div>
      <div style="display:flex; gap:6px; margin-top:6px; align-items:center;">
        <input type="text" id="pathInput" placeholder="예: ~/Documents/Electronic Arts/The Sims 4/Mods" style="flex:1; padding: 5px 8px; font-size: 12px;">
        <button onclick="savePath()" class="blue">저장</button>
      </div>
      <div style="font-size: 11px; color: #888; margin-top: 4px;">Sims 4 게임 폴더를 넣어도 안의 Mods를 자동으로 찾아요 · 경로 변경 후 재스캔 필요</div>
    </div>
    <div>
      <label style="font-weight: 600; font-size: 13px;">🏷️ 카테고리 지정 백업</label>
      <div style="display:flex; gap:6px; margin-top:6px;">
        <button onclick="exportOverrides()">📤 내보내기</button>
        <button onclick="importOverrides()">📥 불러오기</button>
      </div>
      <div style="font-size: 11px; color: #888; margin-top: 4px;">수동으로 지정한 카테고리 목록 백업/공유</div>
    </div>
  </div>
</dialog>
<dialog id="marked-dialog" style="max-width:720px; width:90vw;">
  <button class="dlg-close" onclick="document.getElementById('marked-dialog').close()">✕</button>
  <h3>🗑️ 삭제 표시된 아이템 <span id="marked-summary" style="font-weight:normal; color:var(--c-text-muted); font-size:13px;"></span></h3>
  <div style="max-height:60vh; overflow-y:auto; margin:8px 0; border:1px solid var(--c-border); border-radius:6px; padding:8px; background:var(--c-surface-2);">
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
  <div style="padding: 16px 22px; border-bottom: 1px solid var(--c-border);">
    <h2 style="margin: 0; font-size: 17px;">🎮 사용법</h2>
  </div>
  <div style="padding: 16px 22px; max-height: 72vh; overflow-y: auto; font-size: 13px; line-height: 1.55;">

    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 18px;">
      <div style="border: 2px solid #d9534f; background: #fbecec; color: #402020; border-radius: 8px; padding: 10px 12px;">
        <div style="font-weight: 700; color: #d9534f; margin-bottom: 4px;">🗑️ 삭제 모드</div>
        <div style="font-size: 12px;">클릭해서 지울 것 <b>표시</b> → 하단 "🗑️ 휴지통으로 이동" 버튼</div>
      </div>
      <div style="border: 2px solid #4a90e2; background: #eaf3fc; color: #1a2e42; border-radius: 8px; padding: 10px 12px;">
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
      <tr><td style="padding:4px 8px; color:var(--c-text-muted); width:120px;"><kbd>Cmd+Z</kbd></td><td>되돌리기</td></tr>
      <tr><td style="padding:4px 8px; color:var(--c-text-muted);"><kbd>Cmd+클릭</kbd></td><td>모드 상관없이 다중 선택</td></tr>
      <tr><td style="padding:4px 8px; color:var(--c-text-muted);">빈 공간 드래그</td><td>박스 다중 선택</td></tr>
      <tr><td style="padding:4px 8px; color:var(--c-text-muted);">우클릭</td><td>단일 카테고리 변경 메뉴</td></tr>
      <tr><td style="padding:4px 8px; color:var(--c-text-muted);">이름 클릭</td><td>파일명 복사 (검색용)</td></tr>
      <tr><td style="padding:4px 8px; color:var(--c-text-muted);">이름 hover</td><td>긴 파일명 전체 표시</td></tr>
    </table>

    <h3 style="margin: 16px 0 6px; font-size: 14px;">💡 팁</h3>
    <ul style="margin: 0; padding-left: 20px; font-size: 12px;">
      <li>대량 카테고리 지정: Cmd+클릭 다중선택 → 하단 드롭다운, 또는 카테고리 chip으로 드래그</li>
      <li>휴지통 이동은 안전. 언제든 복원 가능. <b>완전 삭제만 되돌릴 수 없음</b></li>
      <li>날짜별 그룹 → "3년 이상 전" 오래된 것 대량 정리</li>
      <li>수동 지정한 카테고리는 재스캔 시에도 유지</li>
    </ul>

    <p style="margin: 14px 0 0; padding: 8px; background: var(--c-surface-2); border-radius: 4px; font-size: 11px; color: var(--c-text-muted);">
      📂 앱 데이터: <code>~/Library/Application Support/Sims4CCManager/</code> (Sims 4 폴더 안 건드림)
    </p>

  </div>
</dialog>
<dialog id="failed-dialog" style="max-width: 600px; width: 90vw;">
  <button class="dlg-close" onclick="document.getElementById('failed-dialog').close()">✕</button>
  <h3>⚠️ 일부 파일 이동 실패</h3>
  <div id="failed-summary" style="color: var(--c-text-muted); font-size: 13px; margin-bottom: 8px;"></div>
  <div style="max-height: 300px; overflow-y: auto; margin: 8px 0; border: 1px solid var(--c-border); border-radius: 6px; padding: 8px; background: var(--c-surface-2);">
    <div id="failed-list"></div>
  </div>
  <div style="display: flex; gap: 8px; justify-content: flex-end;">
    <button onclick="clearFailedMarks()">실패한 것 표시 해제</button>
  </div>
</dialog>
<dialog id="trash-dialog" style="max-width: 720px; width: 90vw;">
  <button class="dlg-close" onclick="closeTrash()">✕</button>
  <h3>휴지통 <span id="trash-summary" style="font-weight: normal; color: var(--c-text-muted); font-size: 13px;"></span></h3>
  <div style="display: flex; gap: 8px; margin: 8px 0; align-items: center;">
    <button onclick="trashSelectAll(true)">전체 선택</button>
    <button onclick="trashSelectAll(false)">전체 해제</button>
    <span style="color:var(--c-text-muted); font-size:12px;">선택: <b id="trash-selected-count">0</b>개</span>
  </div>
  <div style="max-height: 60vh; overflow-y: auto; margin: 8px 0; border: 1px solid var(--c-border); border-radius: 6px; padding: 8px; background: var(--c-surface-2);">
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
let showUnreadableOnly = false;
let hideTrashed = false;
let hidePerma = true;
let editMode = 'delete';  // 'delete' | 'category'
const FILTER_KEYS = ['tglMarked', 'tglOverride', 'tglUnreadable', 'tglHideTrashed', 'tglHidePerma', 'tglCollapsed'];

function applyFilterState(key, val) {
  if (key === 'tglMarked') showMarkedOnly = val;
  else if (key === 'tglOverride') showOverrideOnly = val;
  else if (key === 'tglUnreadable') showUnreadableOnly = val;
  else if (key === 'tglHideTrashed') hideTrashed = val;
  else if (key === 'tglHidePerma') hidePerma = val;
  else if (key === 'tglCollapsed') collapsedMode = val;
}
function setChipFilterUI(key, val) {
  const btn = document.querySelector(`.chip-toggle[data-filter="${key}"]`);
  if (btn) btn.classList.toggle('on', val);
  applyFilterState(key, val);
}
function toggleChipFilter(key) {
  const btn = document.querySelector(`.chip-toggle[data-filter="${key}"]`);
  const isOn = btn.classList.toggle('on');
  applyFilterState(key, isOn);
  updateFilterBadge();
  render();
}
function updateFilterBadge() {
  const onCount = FILTER_KEYS.filter(k => {
    const btn = document.querySelector(`.chip-toggle[data-filter="${k}"]`);
    return btn && btn.classList.contains('on');
  }).length;
  const btn = document.getElementById('filterToggleBtn');
  if (!btn) return;
  btn.textContent = onCount ? `필터 · ${onCount}` : '필터';
  btn.classList.toggle('active', onCount > 0);
}
function toggleFilterPanel() {
  const panel = document.getElementById('filterPanel');
  const open = panel.style.display === 'none';
  panel.style.display = open ? 'flex' : 'none';
  localStorage.setItem('ccm_filter_panel_open', open ? '1' : '0');
}

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
    metaHtml.push(`<div class="chip ${metaFilter === mname ? 'on' : ''}" data-meta="${mname}">${mname} <span class="count">${metaCounts[mname]}</span></div>`);
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
      subHtml.push(`<div class="subchip ${subFilter === sub ? 'on' : ''}" data-sub="${sub}">${sub} <span class="count">${n}</span></div>`);
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
  // ctxMenu는 카테고리 메뉴가 열릴 때 특정 .grid 안으로 이동됨 - main을 비우기 전에 구출
  const _ctxMenu = document.getElementById('ctxMenu');
  if (_ctxMenu && main.contains(_ctxMenu)) { document.body.appendChild(_ctxMenu); _ctxMenu.style.display = 'none'; }
  main.innerHTML = '';
  if (!MANIFEST || !MANIFEST.creators.length) {
    main.innerHTML = `<div class="progress" style="padding:40px;">
      <div style="font-size:48px;margin-bottom:12px;">👋</div>
      <h2 style="margin:0 0 8px 0;">환영합니다!</h2>
      <div style="color:var(--c-text-muted);margin-bottom:20px;">Mods 폴더에서 아직 CC를 스캔하지 않았어요.<br>아래 버튼을 눌러 시작해 보세요.</div>
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
      if (showUnreadableOnly && !it.unreadable) return false;
      if (!matchesCatFilter(it.cats)) return false;
      if (currentFilter) {
        const hay = (it.file + ' ' + c.name).toLowerCase();
        const terms = currentFilter.split(/\\s+/).filter(Boolean);
        for (const t of terms) { if (!hay.includes(t)) return false; }
      }
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
    const slashIdx = c.name.lastIndexOf('/');
    const crumbParent = slashIdx > -1 ? c.name.slice(0, slashIdx) : '';
    const crumbLeaf = slashIdx > -1 ? c.name.slice(slashIdx + 1) : c.name;
    // 선택/삭제표시 가능한 항목 기준으로 "이미 전체 적용됐는지" 판단 → 버튼이 선택·해제를 토글
    const selectable = items.filter(it => !it.trashed && !it.perma_deleted);
    const allSelected = selectable.length > 0 && selectable.every(it => bulkSel.has(it.path));
    const allMarked = selectable.length > 0 && selectable.every(it => state[it.path]);
    section.innerHTML = `
      <div class="creator-header" onclick="if(!event.target.closest('button'))this.parentElement.classList.toggle('collapsed')">
        ${crumbParent ? `<span class="crumb-parent">${escapeHtml(crumbParent)} /</span>` : ''}
        <h2>${escapeHtml(crumbLeaf)}</h2>
        <span class="creator-count">${items.length}개 · ${human(c._stats.size)}</span>
        ${markedInCreator ? `<span class="creator-marked-badge">삭제 표시 ${markedInCreator}</span>` : ''}
        <div class="creator-actions">
          <button class="folder-select-btn" title="${allSelected ? '이 폴더의 다중선택을 모두 해제' : '이 폴더의 모든 아이템을 다중선택에 추가'}">${allSelected ? '폴더 선택 해제' : '폴더 전체 선택'}</button>
          <button class="folder-mark-btn danger-hover" title="${allMarked ? '이 폴더 전체의 삭제 표시를 해제' : '이 폴더 전체를 삭제 대상으로 표시'}">${allMarked ? '표시 해제' : '전체 삭제표시'}</button>
        </div>
      </div>
      <div class="grid"></div>
    `;
    section.querySelector('.folder-select-btn').onclick = () => {
      let changed = 0;
      for (const it of selectable) {
        if (allSelected) { if (bulkSel.delete(it.path)) changed++; }
        else if (!bulkSel.has(it.path)) { bulkSel.add(it.path); changed++; }
      }
      updateBulkBar();
      render();
      if (changed) toast(allSelected ? `✅ ${changed}개 다중선택 해제됨` : `✅ ${changed}개 다중선택에 추가됨`);
    };
    section.querySelector('.folder-mark-btn').onclick = () => markCreator(c.name, !allMarked);
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
      div.title = (it.trashed ? '휴지통에 있음 (클릭 → 복원)\\n' : '')
        + (it.unreadable ? `⚠️ 읽기 실패: ${it.unreadable_reason || '알 수 없는 오류'}\\n` : '')
        + it.file + '\\n' + human(it.size);
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
      const trashBadge = it.perma_deleted ? '<div class="trash-badge" style="background:rgba(60,0,0,.85);">완전삭제</div>'
                        : it.trashed ? '<div class="trash-badge">휴지통</div>'
                        : '';
      const unreadableBadge = it.unreadable
        ? `<div class="unreadable-badge" title="${escapeHtml('읽기 실패: ' + (it.unreadable_reason || '알 수 없는 오류'))}">⚠️ 읽기 실패</div>`
        : '';
      const overrideClass = it.override ? ' override' : '';
      const catLabel = it.primary_cat || (it.cats && it.cats[0]) || '기타';
      const catTitle = it.override ? '수동 지정' : (it.casp ? 'CASP 파싱' : '파일명 추측') + ' (클릭으로 변경)';
      const catPill = `<div class="cat-pill${overrideClass}" title="${escapeHtml(catTitle + ': ' + (it.cats||[]).join(', '))}" data-item-path="${escapeHtml(it.path)}" data-item-cat="${escapeHtml(it.primary_cat||'')}">${escapeHtml(catLabel)}</div>`;
      const creatorSubtitle = it._creator ? `<div class="creator-sub">${escapeHtml(it._creator)}</div>` : '';
      div.dataset.thumbs = JSON.stringify(it.thumbs);
      // 44자 초과 시 hover 팝오버로 전체 이름 보여줌 (짧으면 name-full 안 만듦)
      const needsPopover = it.file.length > 44;
      const nameFull = needsPopover ? `<div class="name-full">${escapeHtml(it.file)}</div>` : '';
      div.innerHTML = `<div class="thumb-frame">${thumbHtml}${trashBadge}${catPill}<div class="sel-badge">✓</div><div class="sz">${human(it.size)}</div>${unreadableBadge}</div>${creatorSubtitle}<div class="name">${escapeHtml(it.file)}</div>${nameFull}`;
      const clickHandler = it.perma_deleted
        ? () => toast('❌ 완전삭제된 파일입니다 (되돌릴 수 없음)')
        : it.trashed
        ? async () => {
            if (!(await customConfirm(`휴지통에서 복원할까요?\\n${it.file}`, {okText:'복원'}))) return;
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
      nameEl.title = '클릭=파일명 복사 · 우클릭=다른 복사 옵션';
      nameEl.addEventListener('click', copyName);
      const nameRightClick = (e) => {
        e.preventDefault(); e.stopPropagation();
        showNameCopyMenu(e, it, c.name);
      };
      nameEl.addEventListener('contextmenu', nameRightClick);
      if (nameFullEl) {
        nameFullEl.style.cursor = 'copy';
        nameFullEl.title = '클릭=파일명 복사 · 우클릭=다른 복사 옵션';
        nameFullEl.style.pointerEvents = 'auto';
        nameFullEl.addEventListener('click', copyName);
        nameFullEl.addEventListener('contextmenu', nameRightClick);
      }
      // 우클릭 or 카테고리 필 클릭 → 카테고리 변경 메뉴 (해당 아이템에 앵커됨)
      const catEl = div.querySelector('.cat-pill');
      if (catEl) {
        catEl.addEventListener('click', (e) => {
          e.stopPropagation();
          // 카테고리 필 클릭은 항상 그 아이템 하나만
          // (다중 적용은 하단 bulk 바나 chip 드래그 앤 드롭 사용)
          showCategoryMenu(div, [it.path], it.primary_cat);
        });
      }
      div.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        showCategoryMenu(div, [it.path], it.primary_cat);
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
    empty.className = 'empty-state';
    empty.innerHTML = `<div class="empty-state-icon">🔍</div>
      <div class="empty-state-title">일치하는 아이템 없음</div>
      <div class="empty-state-desc">현재 필터 조건에 맞는 항목이 없어요.</div>
      <button class="blue" onclick="resetFilters()">필터 초기화</button>`;
    main.appendChild(empty);
  }
  updateStats(totalItems, visibleItems);
  updateFooter();
}

function resetFilters() {
  currentFilter = '';
  metaFilter = '';
  subFilter = '';
  const s = document.getElementById('search'); if (s) s.value = '';
  setChipFilterUI('tglMarked', false);
  setChipFilterUI('tglOverride', false);
  setChipFilterUI('tglHideTrashed', false);
  setChipFilterUI('tglHidePerma', false);
  updateFilterBadge();
  render();
}

function escapeHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function escapeAttr(s) { return s.replace(/'/g, "\\\\'"); }

function updateStats(total, visible) {
  const folderCount = (MANIFEST.folders || MANIFEST.creators || []).length;
  document.getElementById('stats').textContent =
    `폴더 ${folderCount}개 · 파일 ${total.toLocaleString()}개 · 표시 중 ${visible.toLocaleString()}개 · 썸네일 ${MANIFEST.total_thumbs.toLocaleString()}개`;
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
  const count = bulkSel.size;
  document.getElementById('bulkCount').textContent = count;
  const applyBtn = document.getElementById('applyCatBtn');
  if (applyBtn) applyBtn.disabled = count === 0;
  // 드롭다운 채우기
  const sel = document.getElementById('bulkCat');
  if (sel && sel.options.length <= 1 && MANIFEST) {
    const cats = MANIFEST.all_categories || [];
    sel.innerHTML = '<option value="">카테고리 선택…</option>' +
      cats.map(c => `<option value="${escapeHtml(c.name)}">${c.name}</option>`).join('') +
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

// ─────── 오버플로우 메뉴 ───────
// CSS position:absolute + right:0 은 header가 flex-wrap 되는 좁은 화면에서
// containing block 계산이 꼬여 엉뚱한 위치에 뜨는 버그가 있었음.
// → 버튼의 실제 화면 좌표(getBoundingClientRect)를 기준으로 JS가 직접
//   position:fixed 좌표를 계산해서 항상 버튼 바로 아래에 뜨도록 함.
function toggleOverflowMenu(e) {
  e.stopPropagation();
  const menu = document.getElementById('overflowMenu');
  const isOpen = menu.classList.contains('open');
  if (isOpen) { closeOverflow(); return; }
  const btn = e.currentTarget || e.target.closest('button');
  const r = btn.getBoundingClientRect();
  const menuWidth = 170;
  let left = r.right - menuWidth;   // 버튼 오른쪽 끝에 메뉴 오른쪽 맞춤
  left = Math.max(8, Math.min(left, window.innerWidth - menuWidth - 8));  // 화면 밖으로 안 나가게
  menu.style.left = left + 'px';
  menu.style.top = (r.bottom + 4) + 'px';
  menu.classList.add('open');
}
function closeOverflow() {
  document.getElementById('overflowMenu').classList.remove('open');
}
window.addEventListener('resize', closeOverflow);
document.addEventListener('click', (e) => {
  const m = document.getElementById('overflowMenu');
  if (m && !m.contains(e.target) && !e.target.closest('.menu-wrap button')) closeOverflow();
});

// ─────── 헤더 접기 (검색/필터/카테고리 영역만 접힘, 로고·모드·액션 버튼은 항상 유지) ───────
function toggleHeaderCollapse() {
  const collapsed = document.body.classList.toggle('header-collapsed');
  localStorage.setItem('ccm_header_collapsed', collapsed ? '1' : '0');
  document.getElementById('collapseBtn').title = collapsed ? '헤더 펼치기' : '헤더 접기';
}
(function initHeaderCollapse() {
  if (localStorage.getItem('ccm_header_collapsed') === '1') {
    document.body.classList.add('header-collapsed');
  }
})();

// ─────── 설정 다이얼로그 ───────
function openSettings() {
  // 현재 경로 표시
  fetch('/api/config').then(r => r.json()).then(cfg => {
    document.getElementById('currentPath').textContent = '현재: ' + (cfg.mods_root || '(미지정)');
    document.getElementById('pathInput').value = cfg.mods_root || '';
  }).catch(() => {
    document.getElementById('currentPath').textContent = '(설정 조회 실패)';
  });
  // 현재 테마 하이라이트
  const theme = localStorage.getItem('theme') || 'auto';
  document.querySelectorAll('#themeSeg button').forEach(b => {
    b.classList.toggle('on', b.dataset.v === theme);
  });
  document.getElementById('settings-dialog').showModal();
}

async function savePath() {
  const path = document.getElementById('pathInput').value.trim();
  if (!path) { toast('경로를 입력해주세요'); return; }
  const res = await fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mods_root: path}),
  });
  const data = await res.json();
  if (data.ok) {
    toast('✅ 저장됨 · 재스캔 실행 중...');
    await rescan();
    document.getElementById('settings-dialog').close();
  } else {
    toast('❌ ' + (data.error || '저장 실패'));
  }
}

// 테마 적용
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('theme', theme);
}
(function initTheme() {
  const saved = localStorage.getItem('theme') || 'auto';
  applyTheme(saved);
})();
document.addEventListener('DOMContentLoaded', () => {
  const seg = document.getElementById('themeSeg');
  if (seg) {
    seg.querySelectorAll('button').forEach(b => {
      b.onclick = () => {
        seg.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
        applyTheme(b.dataset.v);
      };
    });
  }
});

async function _copyText(text, label) {
  try { await navigator.clipboard.writeText(text); toast(`📋 복사: ${label}`); }
  catch { toast('복사 실패'); }
}

function showNameCopyMenu(event, item, creatorName) {
  const menu = document.getElementById('ctxMenu');
  document.body.appendChild(menu);  // 카테고리 메뉴가 grid 안으로 옮겨놨을 수 있으니 body로 복귀
  menu.style.position = 'fixed';
  menu.style.width = '260px';
  const clean = item.file.replace(/\\.package$/i, '');
  const opts = [
    {label: '파일명 복사 (확장자 제외)', val: clean, hint: '기본'},
    {label: '전체 파일명 복사 (.package 포함)', val: item.file},
    {label: '창작자 복사', val: creatorName || '(폴더 없음)'},
    {label: '카테고리 복사', val: (item.cats || [item.primary_cat || '기타']).join(', ')},
    {label: '경로 복사', val: item.path},
  ];
  let html = `<div class="ctx-header"><div class="ctx-title">복사 옵션</div></div><div class="ctx-list" style="padding:6px 7px;">`;
  html += opts.map((o, i) => `<div class="ctx-item" data-idx="${i}"><span>${escapeHtml(o.label)}</span>${o.hint?`<span style="color:var(--c-text-subtle);font-size:10px;">${o.hint}</span>`:'<span></span>'}</div>`).join('');
  html += `</div>`;
  menu.innerHTML = html;
  const x = Math.min(event.clientX, window.innerWidth - 270);
  const y = Math.min(event.clientY, window.innerHeight - 250);
  menu.style.left = x + 'px'; menu.style.top = y + 'px'; menu.style.display = 'block';
  menu.querySelectorAll('.ctx-item').forEach(el => {
    el.onclick = () => {
      menu.style.display = 'none';
      const o = opts[+el.dataset.idx];
      const preview = String(o.val).slice(0, 40) + (String(o.val).length > 40 ? '…' : '');
      _copyText(o.val, preview);
    };
  });
}

// 카테고리 메뉴를 3개 섹션으로 묶어 보여줌 (진단안 스펙: 얼굴·피부 / 의상 / 기타)
const CATEGORY_MENU_SECTIONS = [
  ['얼굴 · 피부', ['눈썹', '눈속눈썹', '입술', '메이크업', '렌즈', '스킨', '스킨디테일']],
  ['의상', ['상의', '하의', '전신', '수영복', '잠옷', '속옷', '신발']],
  ['기타', ['헤어', '모자', '액세서리', '유틸', '기타']],
];

function showCategoryMenu(anchorEl, pathOrPaths, currentCat) {
  // anchorEl: item div element(단일 대상 클릭) 이거나, 배치 액션이면 event 객체(폴백)
  const paths = Array.isArray(pathOrPaths) ? pathOrPaths : [pathOrPaths];
  const isBulk = paths.length > 1;
  const menu = document.getElementById('ctxMenu');
  menu.style.width = '300px';
  const catNameSet = new Set((MANIFEST.all_categories || []).map(c => c.name));

  const fileLabel = isBulk ? `다중 ${paths.length}개` : paths[0].split('/').pop();
  const titleLabel = isBulk ? '카테고리 변경' : '카테고리 변경';
  menu.innerHTML = `
    <div class="ctx-header">
      <div class="ctx-title">${titleLabel}</div>
      <div class="ctx-file">${escapeHtml(fileLabel)}</div>
    </div>
    <div class="ctx-search-wrap"><input type="text" class="ctx-search" placeholder="카테고리 검색" autocomplete="off"></div>
    <div class="ctx-list"></div>
    <div class="ctx-reset">↺ 수동 지정 해제 (자동 재분류)</div>
  `;

  const listEl = menu.querySelector('.ctx-list');
  const searchEl = menu.querySelector('.ctx-search');

  function renderList(query) {
    const q = (query || '').trim().toLowerCase();
    let html = '';
    for (const [section, labels] of CATEGORY_MENU_SECTIONS) {
      const visible = labels.filter(l => catNameSet.has(l) && (!q || l.toLowerCase().includes(q)));
      if (!visible.length) continue;
      html += `<div class="ctx-section">${escapeHtml(section)}</div>`;
      for (const label of visible) {
        const on = !isBulk && label === currentCat;
        html += `<div class="ctx-item${on ? ' current' : ''}" data-cat="${escapeHtml(label)}"><span>${escapeHtml(label)}</span><span class="ctx-check">✓</span></div>`;
      }
    }
    listEl.innerHTML = html || `<div style="padding:14px 9px; color:var(--c-text-subtle); font-size:12px;">일치하는 카테고리 없음</div>`;
    listEl.querySelectorAll('.ctx-item').forEach(el => {
      el.onclick = async () => {
        menu.style.display = 'none';
        await applyToPaths(paths, el.dataset.cat || '__reset__');
      };
    });
  }
  renderList('');
  searchEl.addEventListener('input', () => renderList(searchEl.value));
  menu.querySelector('.ctx-reset').onclick = async () => {
    menu.style.display = 'none';
    await applyToPaths(paths, '__reset__');
  };

  // 위치 계산: anchorEl이 아이템이면 그 옆(오른쪽, 공간 없으면 왼쪽)에 앵커. 아니면 커서 위치(fixed).
  const isItemAnchor = anchorEl instanceof HTMLElement;
  if (isItemAnchor) {
    const grid = anchorEl.closest('.grid');
    grid.appendChild(menu);
    menu.style.position = 'absolute';
    const menuW = 300;
    const gridW = grid.clientWidth;
    const left = anchorEl.offsetLeft;
    const top = anchorEl.offsetTop;
    const itemW = anchorEl.offsetWidth;
    const spaceRight = gridW - (left + itemW);
    if (spaceRight >= menuW + 12) {
      menu.style.left = (left + itemW + 12) + 'px';
    } else if (left >= menuW + 12) {
      menu.style.left = (left - menuW - 12) + 'px';
    } else {
      menu.style.left = Math.max(0, gridW - menuW) + 'px';
    }
    menu.style.top = top + 'px';
  } else {
    // 폴백: 이벤트 객체가 넘어온 경우 (구버전 호출 대비) — 커서 위치에 고정
    document.body.appendChild(menu);
    menu.style.position = 'fixed';
    const ev = anchorEl;
    menu.style.left = Math.min(ev.clientX, window.innerWidth - 320) + 'px';
    menu.style.top = Math.min(ev.clientY, window.innerHeight - 300) + 'px';
  }
  menu.style.display = 'block';
  searchEl.focus();
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
  if (MANIFEST) for (const c of (MANIFEST.folders || MANIFEST.creators))
    for (const it of c.items) if (state[it.path]) sz += it.size;
  document.getElementById('marked-size').textContent = human(sz);
  const toTrashBtn = document.getElementById('toTrashBtn');
  if (toTrashBtn) toTrashBtn.disabled = marked.length === 0;
}

function showOnlyMarked() { showMarkedOnly = !showMarkedOnly; render(); }
async function clearMarks() {
  if (!(await customConfirm('모든 표시를 초기화할까요?', {okText:'초기화', danger:true}))) return;
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
      ? `<img class="thumb-img" src="/thumbs/${it.thumbs[0]}">`
      : `<div class="no-thumb">썸네일 없음</div>`;
    const needsPopover = it.file.length > 44;
    const nameFull = needsPopover ? `<div class="name-full">${escapeHtml(it.file)}</div>` : '';
    return `<div class="item marked-tile marked" data-path="${escapeHtml(it.path)}">
      <div class="thumb-frame">
        ${thumbHtml}
        <button class="marked-remove-btn" title="표시 해제" onclick="unmarkFromDialog('${escapeAttr(it.path)}')">✕</button>
        <div class="sz">${human(it.size)}</div>
      </div>
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

async function clearMarksFromDialog() {
  if (!(await customConfirm('삭제 표시 전체를 해제할까요?', {okText:'해제', danger:true}))) return;
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
  if (!(await customConfirm(`${paths.length}개 파일을 휴지통으로 이동할까요?\\n(휴지통에서 복원 가능해요)`, {okText:'이동', danger:true}))) return;
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
  return data;
}

async function rescan() {
  showScanOverlay(true);
  await fetch('/api/scan', {method: 'POST'});
  await pollScanProgress();
  showScanOverlay(false);
  await loadManifest();
  toast('✅ 재스캔 완료');
}

function showScanOverlay(show) {
  let ov = document.getElementById('scanOverlay');
  if (!ov && show) {
    ov = document.createElement('div');
    ov.id = 'scanOverlay';
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:9999;display:flex;align-items:center;justify-content:center;';
    ov.innerHTML = `<div style="background:white;padding:32px;border-radius:10px;min-width:360px;text-align:center;box-shadow:0 10px 40px rgba(0,0,0,.3);">
      <div style="font-size:32px;margin-bottom:8px;">🔄</div>
      <h3 style="margin:0 0 12px 0;">CC 스캔 중...</h3>
      <div id="scanOvName" style="color:var(--c-text-muted);font-size:12px;margin-bottom:10px;word-break:break-all;min-height:16px;">준비 중...</div>
      <div class="progress-bar" style="margin-top:6px;"><div id="scanOvBar" style="width:0%"></div></div>
      <div id="scanOvCount" style="margin-top:8px;font-size:12px;color:#888;">0 / 0</div>
    </div>`;
    document.body.appendChild(ov);
  }
  if (ov && !show) ov.remove();
}

async function pollScanProgress() {
  return new Promise((resolve) => {
    const tick = async () => {
      try {
        const r = await fetch('/api/scan-progress');
        const p = await r.json();
        const bar = document.getElementById('scanOvBar');
        const nameEl = document.getElementById('scanOvName');
        const cntEl = document.getElementById('scanOvCount');
        if (bar) {
          const pct = p.total ? Math.round((p.current / p.total) * 100) : 0;
          bar.style.width = pct + '%';
          if (nameEl) nameEl.textContent = p.name || '';
          if (cntEl) cntEl.textContent = `${p.current} / ${p.total}`;
        }
        if (!p.active) { resolve(); return; }
      } catch {}
      setTimeout(tick, 300);
    };
    tick();
  });
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
        thumbHtml = `<img class="thumb-img" src="/thumbs/${thumbs[0]}">`;
        if (thumbs.length > 1) {
          thumbHtml += `<div class="thumb-nav">
            <button class="thumb-btn thumb-prev" onclick="cycleThumb(event, this, -1)">◀</button>
            <span class="thumb-counter">1/${thumbs.length}</span>
            <button class="thumb-btn thumb-next" onclick="cycleThumb(event, this, 1)">▶</button>
          </div>`;
        }
      } else {
        thumbHtml = `<div class="no-thumb">썸네일 없음</div>`;
      }
      const needsPopover = fileName.length > 44;
      const nameFull = needsPopover ? `<div class="name-full">${escapeHtml(fileName)}</div>` : '';
      return `
        <label class="trash-tile item" data-thumbs='${JSON.stringify(thumbs).replace(/'/g, "&#39;")}' data-thumb-idx="0">
          <div class="thumb-frame">
            <input type="checkbox" class="trash-check" value="${escapeHtml(it.path)}" onchange="updateTrashSelectedCount()">
            ${thumbHtml}
            <div class="sz">${human(it.size)}</div>
          </div>
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
    <div style="padding: 6px; border-bottom: 1px solid var(--c-border); font-size: 12px;">
      <div style="color: #d9534f; font-weight: 600;">${escapeHtml(e.reason)}</div>
      <div style="color: var(--c-text-muted); margin-top: 2px; word-break: break-all;">${escapeHtml(e.path)}</div>
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
  if (!(await customConfirm('휴지통을 완전히 비울까요? 되돌릴 수 없어요.', {okText:'비우기', danger:true}))) return;
  const res = await fetch('/api/empty-trash', {method: 'POST'});
  const data = await res.json();
  toast(`✅ ${data.count}개 완전 삭제 (${human(data.size_freed)} 확보)`);
  await postBulkAction();
}

async function restoreAll() {
  const paths = [...document.querySelectorAll('#trash-list input')].map(el => el.value);
  if (!paths.length) { toast('휴지통이 비어있어요.'); return; }
  if (!(await customConfirm(`${paths.length}개 파일 전체를 원위치로 복원할까요?`, {okText:'복원'}))) return;
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
  if (!(await customConfirm(`선택한 ${paths.length}개를 완전히 삭제할까요? 되돌릴 수 없어요.`, {okText:'완전 삭제', danger:true}))) return;
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

(function initSearch(){
  const s = document.getElementById('search');
  const clearBtn = document.getElementById('searchClear');
  const hist = document.getElementById('searchHistory');
  const HIST_KEY = 'ccm_search_history';
  // 타이핑마다 전체 목록을 다시 그리면 라이브러리가 클수록 버벅이므로
  // 실제 렌더는 살짝 디바운스하고, 즉시 반응해야 하는 UI(지우기 버튼, 최근검색 숨김)만 바로 반영
  let searchDebounceTimer = null;
  const renderDebounced = () => {
    clearTimeout(searchDebounceTimer);
    searchDebounceTimer = setTimeout(render, 150);
  };
  const renderNow = () => { clearTimeout(searchDebounceTimer); render(); };
  const loadHist = () => { try { return JSON.parse(localStorage.getItem(HIST_KEY) || '[]'); } catch { return []; } };
  const saveHist = (q) => {
    if (!q) return;
    const arr = loadHist().filter(x => x !== q);
    arr.unshift(q);
    localStorage.setItem(HIST_KEY, JSON.stringify(arr.slice(0, 5)));
  };
  const removeHistItem = (q) => {
    const arr = loadHist().filter(x => x !== q);
    localStorage.setItem(HIST_KEY, JSON.stringify(arr));
    showHist();
  };
  const clearAllHist = () => {
    localStorage.setItem(HIST_KEY, '[]');
    hist.style.display = 'none';
  };
  window._ccmRemoveHist = removeHistItem;
  window._ccmClearHist = clearAllHist;
  const showHist = () => {
    const arr = loadHist();
    if (!arr.length || s.value) { hist.style.display = 'none'; return; }
    let html = '<div style="padding:6px 10px; font-size:11px; color:var(--c-text-label); display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--c-border);"><span>🕒 최근 검색</span><span onmousedown="event.preventDefault(); window._ccmClearHist()" style="cursor:pointer; color:var(--c-text-subtle); font-size:11px;">전체 지우기</span></div>';
    html += arr.map(q => `<div class="hist-item" data-q="${escapeHtml(q)}" style="padding:6px 10px; cursor:pointer; font-size:12px; display:flex; justify-content:space-between; align-items:center;"><span>${escapeHtml(q)}</span><span onmousedown="event.preventDefault(); event.stopPropagation(); window._ccmRemoveHist('${escapeAttr(q)}')" style="opacity:.5; cursor:pointer; padding:0 4px;">✕</span></div>`).join('');
    hist.innerHTML = html;
    hist.style.display = 'block';
    hist.querySelectorAll('.hist-item').forEach(el => {
      el.onmousedown = (ev) => {
        if (ev.target.tagName === 'SPAN' && ev.target.textContent === '✕') return;
        ev.preventDefault();
        s.value = el.dataset.q;
        currentFilter = s.value.toLowerCase();
        clearBtn.style.display = 'block';
        hist.style.display = 'none';
        renderNow();
      };
    });
  };
  s.addEventListener('input', e => {
    currentFilter = e.target.value.toLowerCase().trim();
    clearBtn.style.display = e.target.value ? 'block' : 'none';
    hist.style.display = 'none';
    renderDebounced();
  });
  s.addEventListener('focus', showHist);
  s.addEventListener('blur', () => { setTimeout(() => { hist.style.display = 'none'; }, 150); });
  s.addEventListener('change', () => saveHist(s.value.trim()));
  s.addEventListener('keydown', (e) => { if (e.key === 'Enter') saveHist(s.value.trim()); });
  clearBtn.addEventListener('click', () => {
    s.value = ''; currentFilter = ''; clearBtn.style.display = 'none'; renderNow(); s.focus();
  });
})();
document.getElementById('itemSortBy').addEventListener('change', e => {
  itemSortBy = e.target.value;
  render();
});
document.getElementById('groupSortBy').addEventListener('change', e => {
  sortBy = e.target.value;
  render();
});
// 필터 패널 초기 상태 복원 (기본 열림)
(function initFilterPanel() {
  const saved = localStorage.getItem('ccm_filter_panel_open');
  const open = saved === null ? true : saved === '1';
  document.getElementById('filterPanel').style.display = open ? 'flex' : 'none';
  updateFilterBadge();
})();
// 아이템 크기 슬라이더
(function initZoom() {
  const slider = document.getElementById('zoomSlider');
  const saved = localStorage.getItem('ccm_zoom');
  if (saved) { slider.value = saved; document.documentElement.style.setProperty('--h-thumb', saved + 'px'); }
  slider.addEventListener('input', e => {
    const v = e.target.value;
    document.documentElement.style.setProperty('--h-thumb', v + 'px');
    localStorage.setItem('ccm_zoom', v);
  });
})();
document.getElementById('groupSeg').querySelectorAll('button').forEach(b => {
  b.onclick = () => {
    groupBy = b.dataset.v;
    document.querySelectorAll('#groupSeg button').forEach(x => x.classList.toggle('on', x === b));
    // 날짜별 그룹은 최신→옛 고정 순서라 그룹 정렬 컨트롤이 의미 없음
    document.getElementById('groupSortBy').disabled = (groupBy === 'date');
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
  const data = await _origPerformDelete();
  // 취소되었거나 아무것도 못 옮겼으면 undo에 남기지 않음 (실제로 옮겨진 것만 기록)
  if (data && data.moved && data.moved.length) {
    const movedTrashPaths = data.moved.map(p => (data.moved_trash_paths || {})[p] || p);
    pushUndo({type: 'delete', originalPaths: data.moved, movedTrashPaths});
  }
};

// ─────── 통계 & 오버라이드 import/export ───────
function exportOverrides() {
  window.location = '/api/overrides/export';
}
function importOverrides() {
  const inp = document.getElementById('importOvFile');
  inp.onchange = async () => {
    const f = inp.files && inp.files[0];
    if (!f) return;
    try {
      const text = await f.text();
      const parsed = JSON.parse(text);
      const payload = parsed.overrides ? parsed : {overrides: parsed};
      const res = await fetch('/api/overrides/import', {
        method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload),
      });
      const data = await res.json();
      toast(`📥 ${data.imported}개 추가/변경됨 (총 ${data.total}개)`);
      await loadManifest();
    } catch (e) {
      toast('❌ 파일 형식 오류');
    }
    inp.value = '';
  };
  inp.click();
}

async function openStats() {
  const res = await fetch('/api/stats');
  const s = await res.json();
  let dlg = document.getElementById('stats-dialog');
  if (!dlg) {
    dlg = document.createElement('dialog');
    dlg.id = 'stats-dialog';
    dlg.style.cssText = 'max-width:640px;width:90vw;';
    document.body.appendChild(dlg);
  }
  const maxSz = Math.max(1, ...(s.top_creators || []).map(c => c.size));
  const fmtDate = t => t ? new Date(t * 1000).toLocaleDateString('ko-KR') : '-';
  dlg.innerHTML = `
    <button class="dlg-close" onclick="this.parentElement.close()">✕</button>
    <h2 style="margin:0 0 12px 0;">📊 통계</h2>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px;">
      <div style="padding:10px;background:var(--c-surface-2);border-radius:6px;"><div style="font-size:11px;color:var(--c-text-muted);">전체 파일</div><div style="font-size:20px;font-weight:600;">${s.total_files.toLocaleString()}</div></div>
      <div style="padding:10px;background:var(--c-surface-2);border-radius:6px;"><div style="font-size:11px;color:var(--c-text-muted);">전체 용량</div><div style="font-size:20px;font-weight:600;">${human(s.total_size)}</div></div>
      <div style="padding:10px;background:var(--c-surface-2);border-radius:6px;"><div style="font-size:11px;color:var(--c-text-muted);">폴더/창작자</div><div style="font-size:20px;font-weight:600;">${s.creator_count}</div></div>
      <div style="padding:10px;background:var(--c-surface-2);border-radius:6px;"><div style="font-size:11px;color:var(--c-text-muted);">수동 카테고리</div><div style="font-size:20px;font-weight:600;">${s.overrides_count}</div></div>
      <div style="padding:10px;background:var(--c-surface-2);border-radius:6px;"><div style="font-size:11px;color:var(--c-text-muted);">휴지통</div><div style="font-size:14px;">${s.trash_count}개 · ${human(s.trash_size)}</div></div>
      <div style="padding:10px;background:var(--c-surface-2);border-radius:6px;"><div style="font-size:11px;color:var(--c-text-muted);">최신 / 최고령</div><div style="font-size:12px;">${fmtDate(s.newest_mtime)}<br>${fmtDate(s.oldest_mtime)}</div></div>
      <div style="padding:10px;background:var(--c-surface-2);border-radius:6px;"><div style="font-size:11px;color:var(--c-text-muted);">평균 크기 / 썸네일</div><div style="font-size:12px;">${human(s.avg_size||0)}<br>${(s.total_thumbs||0).toLocaleString()}개</div></div>
      <div style="padding:10px;background:var(--c-surface-2);border-radius:6px;"><div style="font-size:11px;color:var(--c-text-muted);">읽기 실패</div><div style="font-size:20px;font-weight:600;${s.unreadable_count ? 'color:var(--c-red);' : ''}">${s.unreadable_count || 0}</div></div>
    </div>
    <h3 style="margin:8px 0;font-size:14px;">🏆 창작자 Top 10 (용량 기준)</h3>
    <div style="margin-bottom:16px;">
      ${(s.top_creators || []).map(c => `
        <div style="margin:4px 0;">
          <div style="display:flex;justify-content:space-between;font-size:12px;"><span>${escapeHtml(c.name)}</span><span style="color:var(--c-text-muted);">${c.count}개 · ${human(c.size)}</span></div>
          <div style="height:6px;background:var(--c-border-strong);border-radius:3px;overflow:hidden;"><div style="height:100%;background:#4a90e2;width:${(c.size/maxSz*100).toFixed(1)}%;"></div></div>
        </div>`).join('')}
    </div>
    <h3 style="margin:8px 0;font-size:14px;">🏷️ 카테고리 분포</h3>
    <div style="display:flex;flex-wrap:wrap;gap:6px;">
      ${(s.categories || []).map(c => `<span style="padding:4px 8px;background:var(--c-surface-2);color:var(--c-text);border-radius:12px;font-size:12px;">${escapeHtml(c.name)} <b>${c.count}</b> <span style="color:var(--c-text-subtle);">(${human(c.size)})</span></span>`).join('')}
    </div>`;
  dlg.showModal();
}

// ─────── 키보드 네비게이션 ───────
(function initKbNav(){
  let focusIdx = -1;
  const getItems = () => Array.from(document.querySelectorAll('#main .item')).filter(el => !el.classList.contains('trashed') && !el.classList.contains('perma-deleted'));
  const setFocus = (idx) => {
    const items = getItems();
    if (!items.length) return;
    idx = Math.max(0, Math.min(items.length - 1, idx));
    document.querySelectorAll('.item.kb-focus').forEach(el => el.classList.remove('kb-focus'));
    focusIdx = idx;
    items[idx].classList.add('kb-focus');
    items[idx].scrollIntoView({block: 'nearest', behavior: 'smooth'});
  };
  const columns = () => {
    const items = getItems();
    if (items.length < 2) return 1;
    const y0 = items[0].getBoundingClientRect().top;
    let n = 1;
    for (let i = 1; i < items.length; i++) {
      if (Math.abs(items[i].getBoundingClientRect().top - y0) < 5) n++;
      else break;
    }
    return n;
  };
  window.addEventListener('keydown', (e) => {
    if (e.target.matches('input, textarea, select')) return;
    if (document.querySelector('dialog[open]')) return;
    const items = getItems();
    if (!items.length) return;
    if (e.key === 'ArrowRight') { e.preventDefault(); setFocus(focusIdx < 0 ? 0 : focusIdx + 1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); setFocus(focusIdx < 0 ? 0 : focusIdx - 1); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); setFocus(focusIdx < 0 ? 0 : focusIdx + columns()); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setFocus(focusIdx < 0 ? 0 : focusIdx - columns()); }
    else if (e.key === 'Enter' || e.key === ' ') {
      if (focusIdx < 0) return;
      e.preventDefault();
      const el = items[focusIdx];
      const p = el.dataset.path;
      if (p) toggle(p);
    } else if (e.key === 'Escape') {
      if (bulkSel.size > 0) { e.preventDefault(); clearBulkSel(); }
      document.querySelectorAll('.item.kb-focus').forEach(el => el.classList.remove('kb-focus'));
      focusIdx = -1;
    }
  });
})();

// ─────── customConfirm ───────
function customConfirm(message, opts) {
  opts = opts || {};
  const okText = opts.okText || '확인';
  const cancelText = opts.cancelText || '취소';
  const danger = !!opts.danger;
  return new Promise((resolve) => {
    let dlg = document.getElementById('customConfirmDlg');
    if (!dlg) {
      dlg = document.createElement('dialog');
      dlg.id = 'customConfirmDlg';
      dlg.style.cssText = 'min-width:300px;max-width:480px;';
      document.body.appendChild(dlg);
    }
    dlg.innerHTML = `
      <div id="ccMsg" style="white-space:pre-wrap;margin-bottom:16px;font-size:14px;line-height:1.5;"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;">
        <button id="ccCancel" type="button">${escapeHtml(cancelText)}</button>
        <button id="ccOk" type="button" class="${danger ? 'primary' : 'blue'}">${escapeHtml(okText)}</button>
      </div>`;
    dlg.querySelector('#ccMsg').textContent = message;
    const close = (v) => {
      dlg.removeEventListener('keydown', onKey);
      try { dlg.close(); } catch {}
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); close(false); }
      else if (e.key === 'Enter') { e.preventDefault(); close(true); }
    };
    dlg.addEventListener('keydown', onKey);
    dlg.querySelector('#ccOk').onclick = () => close(true);
    dlg.querySelector('#ccCancel').onclick = () => close(false);
    dlg.showModal();
    setTimeout(() => dlg.querySelector('#ccOk').focus(), 20);
  });
}

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
        if path == "/api/scan-progress":
            self._json(dict(SCAN_PROGRESS))
            return
        if path == "/api/config":
            self._json({"mods_root": str(MODS)})
            return
        if path == "/api/overrides/export":
            from datetime import datetime
            data = _load_overrides()
            body = json.dumps({"exported_at": datetime.now().isoformat(), "count": len(data), "overrides": data}, ensure_ascii=False, indent=2).encode()
            fn = f"overrides-{datetime.now().strftime('%Y%m%d')}.json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{fn}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/stats":
            self._json(_compute_stats())
            return
        if path.startswith("/thumbs/"):
            name = path[len("/thumbs/"):]
            fp = _safe_path(THUMBS_DIR, name)
            if fp and fp.exists() and fp.is_file():
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
        elif path == "/api/config":
            if SCAN_PROGRESS["active"]:
                self._json({"ok": False, "error": "스캔 중에는 경로를 바꿀 수 없어요. 스캔이 끝난 뒤 다시 시도해 주세요."})
                return
            new_path = data.get("mods_root", "").strip()
            if not new_path:
                self._json({"ok": False, "error": "경로가 비어있음"})
                return
            expanded = Path(new_path).expanduser().resolve()
            if not expanded.exists():
                self._json({"ok": False, "error": f"경로가 존재하지 않음: {expanded}"})
                return
            # Sims 4 게임 폴더를 줬으면 그 안의 Mods를 자동으로 찾아 사용
            if expanded.name != "Mods" and (expanded / "Mods").exists():
                expanded = expanded / "Mods"
            if expanded.name != "Mods":
                self._json({"ok": False, "error": f"Mods 폴더가 아님: {expanded}"})
                return
            _CONFIG_PATH.write_text(json.dumps({"mods_root": str(expanded)}, indent=2))
            # 전역 경로 업데이트
            global MODS, CC_ROOT
            MODS = expanded
            CC_ROOT = MODS / "CC FeaturedCreators"
            self._json({"ok": True, "mods_root": str(expanded)})
        elif path == "/api/scan":
            # 백그라운드 스레드에서 스캔, 프론트는 /api/scan-progress로 폴링
            if not SCAN_PROGRESS["active"]:
                SCAN_PROGRESS.update({"active": True, "current": 0, "total": 0, "name": "시작 중..."})
                t = threading.Thread(target=scan_cc, daemon=True)
                t.start()
            self._json({"started": True})
        elif path == "/api/empty-trash":
            self._json(empty_trash())
        elif path == "/api/delete-from-trash":
            self._json(delete_from_trash(data.get("paths", [])))
        elif path == "/api/overrides/import":
            merged = _load_overrides()
            incoming = data.get("overrides") if isinstance(data.get("overrides"), dict) else (data if all(isinstance(v, str) for v in data.values()) else {})
            added = 0
            for k, v in (incoming or {}).items():
                if isinstance(k, str) and isinstance(v, str) and v:
                    if merged.get(k) != v:
                        merged[k] = v; added += 1
            _save_overrides(merged)
            self._json({"imported": added, "total": len(merged)})
        elif path == "/api/set-category-batch":
            # {paths: [...], category: "..."}
            paths = data.get("paths", [])
            cat = data.get("category")
            set_category_overrides_batch(paths, cat)
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
