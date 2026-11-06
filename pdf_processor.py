"""PDF naming, merging, manifest management, local OCR / partner matching, and Moodle feedback ZIP packaging."""

from __future__ import annotations

import csv
import difflib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"
DEFAULT_SETTINGS: Dict[str, Any] = {
    "workdir": "./sheets",
    "course_id": 48957,
    "username": "",
    "marker": "",
    "last_sheet": 1,
    "show_pdf_preview": True,
    "known_partners": {},
    "known_matriculations": {},
    "excluded_trainers": [],
}


UMLAUT_MAP = str.maketrans(
    {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "Ä": "Ae",
        "Ö": "Oe",
        "Ü": "Ue",
        "ß": "ss",
    }
)

OCR_DIGIT_REPAIR_MAP = str.maketrans(
    {
        "O": "0",
        "o": "0",
        "D": "0",
        "Q": "0",
        "I": "1",
        "l": "1",
        "|": "1",
        "!": "1",
        "Z": "2",
        "z": "2",
        "S": "5",
        "s": "5",
        "G": "6",
        "B": "8",
    }
)

HEADER_BOILERPLATE_WORDS = {
    "ubungsblatt",
    "uebungsblatt",
    "blatt",
    "sheet",
    "analysis",
    "ana",
    "matrikelnummer",
    "matrikelnr",
    "matrikel",
    "matr",
    "nr",
    "nummer",
    "tutorium",
    "tutor",
    "tutorin",
    "gruppe",
    "abgabe",
    "abgegeben",
    "von",
    "und",
    "and",
    "name",
    "namen",
    "student",
    "studenten",
    "studierende",
    "universitat",
    "universitaet",
    "lmu",
    "muenchen",
    "munchen",
    "fakultaet",
    "mathematik",
    "wintersemester",
    "sommersemester",
    "ws",
    "ss",
    "aufgabe",
    "aufgaben",
    "loesung",
    "losung",
    "loesungen",
    "beweis",
    "seite",
    "page",
    "datum",
    "prof",
    "dr",
    "dozent",
    "dozentin",
    "assistent",
    "assistentin",
    "assistenz",
    "uebungsleitung",
    "ubungsleitung",
    "vorlesung",
}

_MASK_WORDS = HEADER_BOILERPLATE_WORDS

_EXCLUDED_TRAINER_NAMES: Set[str] = set()


def register_excluded_partner_names(names: Sequence[str]) -> None:
    """Register course lecturers, assistants, or trainers to exclude from partner matching."""
    for name in names:
        if name and isinstance(name, str):
            clean = strip_moodle_userinitials(name).strip()
            if clean:
                _EXCLUDED_TRAINER_NAMES.add(clean)


def get_excluded_partner_names() -> Set[str]:
    """Return the current set of excluded trainer / lecturer / assistant names."""
    return set(_EXCLUDED_TRAINER_NAMES)


def clear_excluded_partner_names() -> None:
    """Clear the registered excluded trainer names."""
    _EXCLUDED_TRAINER_NAMES.clear()


EXCLUDED_PARTNER_NAMES: Tuple[str, ...] = ()
EXCLUDED_PARTNER_TOKEN_PAIRS: Tuple[Tuple[str, str], ...] = ()


@dataclass
class CourseStudent:
    """A student in the Moodle course roster (used for partner matching and dual grading)."""

    userid: str
    fullname: str
    firstname: str = ""
    surname: str = ""
    participant_id: Optional[str] = None
    moodle_folder: Optional[str] = None
    matriculation_number: Optional[str] = None

    def __post_init__(self) -> None:
        if self.fullname:
            self.fullname = strip_moodle_userinitials(self.fullname)
        if not self.firstname and not self.surname and self.fullname:
            self.firstname, self.surname = split_fullname(self.fullname)


@dataclass
class StudentSubmissionRecord:
    """Metadata mapping a local PDF filename back to a specific Moodle student submission."""

    userid: str
    fullname: str
    firstname: str
    surname: str
    filename: str
    sheet_number: int
    participant_id: Optional[str] = None
    moodle_folder: Optional[str] = None
    original_filenames: List[str] = field(default_factory=list)
    current_grade: str = ""
    partner_userid: Optional[str] = None
    partner_fullname: Optional[str] = None
    partner_match_source: Optional[str] = None
    partner_candidates: List[str] = field(default_factory=list)
    detected_matriculations: List[str] = field(default_factory=list)
    header_excerpt: str = ""


@dataclass
class MarkedSubmissionItem:
    """A marked student PDF paired with its Moodle target metadata and numerical points."""

    record: StudentSubmissionRecord
    pdf_path: Path
    points: Optional[str] = None


def strip_moodle_userinitials(fullname: str) -> str:
    """Remove a leading 2-letter uppercase Moodle avatar initial prefix (e.g. 'EM Erika Musterfrau' -> 'Erika Musterfrau')."""
    cleaned = " ".join((fullname or "").strip().split())
    if not cleaned:
        return ""
    tokens = cleaned.split(" ")
    if (
        len(tokens) >= 3
        and len(tokens[0]) == 2
        and tokens[0].isupper()
        and tokens[0].isalpha()
        and tokens[1]
        and tokens[-1]
    ):
        expected_initials = (tokens[1][0] + tokens[-1][0]).upper()
        if tokens[0] == expected_initials:
            return " ".join(tokens[1:])
    return cleaned


def _clean_name_token(text: str) -> str:
    """Convert a name part into PascalCase ASCII letters for clean filenames."""
    translated = text.translate(UMLAUT_MAP)
    normalized = unicodedata.normalize("NFKD", translated)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    parts = re.findall(r"[A-Za-z0-9]+", ascii_text)
    return "".join(part[0].upper() + part[1:] for part in parts if part)


def split_fullname(fullname: str) -> Tuple[str, str]:
    """Split a Moodle full name into (firstname, surname)."""
    cleaned = strip_moodle_userinitials(fullname)
    if not cleaned:
        return ("Student", "Unknown")
    if "," in cleaned:
        surname_part, firstname_part = [p.strip() for p in cleaned.split(",", 1)]
        return (firstname_part or "Student", surname_part or "Unknown")
    tokens = cleaned.split(" ")
    if len(tokens) == 1:
        return (tokens[0], "")
    return (" ".join(tokens[:-1]), tokens[-1])


def format_student_pdf_name(
    firstname: str,
    surname: str,
    sheet_number: int,
    used_filenames: Optional[set] = None,
    userid: Optional[str] = None,
) -> str:
    """Generate a unified filename of the form [FirstInitial][Lastname]_sheet[N].pdf (e.g. JDoe_sheet2.pdf)."""
    first_clean = _clean_name_token(firstname)
    first_initial = first_clean[0].upper() if first_clean else ""
    last_clean = _clean_name_token(surname)
    base_person = f"{first_initial}{last_clean}" or first_clean or "Student"
    stem = f"{base_person}_sheet{sheet_number}"
    candidate = f"{stem}.pdf"

    if used_filenames is not None and candidate in used_filenames:
        suffix = userid if userid else "2"
        candidate = f"{base_person}_{suffix}_sheet{sheet_number}.pdf"
        counter = 2
        while candidate in used_filenames:
            counter += 1
            candidate = f"{base_person}_{counter}_sheet{sheet_number}.pdf"

    if used_filenames is not None:
        used_filenames.add(candidate)

    return candidate


def merge_pdf_bytes_list(pdf_payloads: List[Tuple[str, bytes]]) -> bytes:
    """Return a single PDF from one or more (filename, pdf_bytes) payloads."""
    if not pdf_payloads:
        raise ValueError("No PDF payloads provided.")
    if len(pdf_payloads) == 1:
        return pdf_payloads[0][1]

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for _, payload in sorted(pdf_payloads, key=lambda item: item[0].lower()):
        reader = PdfReader(io.BytesIO(payload))
        for page in reader.pages:
            writer.add_page(page)

    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _shared_roster_cache_path(manifest_path: Path, course_id: int) -> Path:
    """Return path to the course-wide roster cache shared across exercise sheets."""
    return manifest_path.resolve().parent.parent / f"course_roster_{course_id}.json"


def save_manifest(
    manifest_path: Path,
    course_id: int,
    cmid: int,
    sheet_number: int,
    records: List[StudentSubmissionRecord],
    course_roster: Optional[List[CourseStudent]] = None,
) -> None:
    """Save the mapping from local PDF filenames to Moodle student identifiers and course roster."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if course_roster is None and manifest_path.exists():
        try:
            existing_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if "course_roster" in existing_raw:
                valid_keys = set(CourseStudent.__dataclass_fields__.keys())
                course_roster = [
                    CourseStudent(**{k: v for k, v in item.items() if k in valid_keys})
                    for item in existing_raw.get("course_roster", [])
                    if isinstance(item, dict)
                ]
        except Exception:
            course_roster = None

    sorted_records = sorted(
        records, key=lambda r: ((r.fullname or "").lower(), (r.filename or "").lower())
    )
    payload: Dict[str, Any] = {
        "course_id": course_id,
        "cmid": cmid,
        "sheet_number": sheet_number,
        "students": {rec.filename: asdict(rec) for rec in sorted_records},
        "excluded_trainers": sorted(get_excluded_partner_names()),
    }
    if course_roster is not None:
        filtered_roster = [
            cs for cs in course_roster if not is_excluded_partner_name(cs.fullname)
        ]
        roster_dicts = [asdict(cs) for cs in filtered_roster]
        payload["course_roster"] = roster_dicts
        # Also update the shared course-wide roster cache if this roster is non-empty
        if len(filtered_roster) > 0:
            try:
                shared_path = _shared_roster_cache_path(manifest_path, course_id)
                shared_path.write_text(
                    json.dumps(roster_dicts, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass

    manifest_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_manifest_with_roster(
    manifest_path: Path,
) -> Tuple[int, int, int, Dict[str, StudentSubmissionRecord], List[CourseStudent]]:
    """Load the sheet manifest and full course roster from disk."""
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    course_id = int(raw["course_id"])
    cmid = int(raw["cmid"])
    sheet_number = int(raw["sheet_number"])

    if "excluded_trainers" in raw and isinstance(raw["excluded_trainers"], list):
        register_excluded_partner_names(raw["excluded_trainers"])

    unsorted_students: List[Tuple[str, StudentSubmissionRecord]] = []
    valid_rec_keys = set(StudentSubmissionRecord.__dataclass_fields__.keys())
    for fname, data in raw.get("students", {}).items():
        filtered = {k: v for k, v in data.items() if k in valid_rec_keys}
        unsorted_students.append((fname, StudentSubmissionRecord(**filtered)))
    unsorted_students.sort(
        key=lambda item: ((item[1].fullname or "").lower(), item[0].lower())
    )
    students: Dict[str, StudentSubmissionRecord] = dict(unsorted_students)

    roster_by_key: Dict[str, CourseStudent] = {}
    valid_roster_keys = set(CourseStudent.__dataclass_fields__.keys())

    def _add_roster_item(item_dict: Dict[str, Any]) -> None:
        filtered_r = {k: v for k, v in item_dict.items() if k in valid_roster_keys}
        fname_val = str(filtered_r.get("fullname") or "").strip()
        if not fname_val or is_excluded_partner_name(fname_val):
            return
        cs = CourseStudent(**filtered_r)
        key = str(cs.userid) if cs.userid else cs.fullname.lower()
        if key not in roster_by_key:
            roster_by_key[key] = cs
        else:
            ex = roster_by_key[key]
            if cs.participant_id and not ex.participant_id:
                ex.participant_id = cs.participant_id
            if cs.moodle_folder and not ex.moodle_folder:
                ex.moodle_folder = cs.moodle_folder
            if cs.matriculation_number and not ex.matriculation_number:
                ex.matriculation_number = cs.matriculation_number

    for item in raw.get("course_roster", []):
        if isinstance(item, dict):
            _add_roster_item(item)

    # Also merge shared course-wide roster cache if present
    shared_path = _shared_roster_cache_path(manifest_path, course_id)
    if shared_path.exists():
        try:
            shared_list = json.loads(shared_path.read_text(encoding="utf-8"))
            if isinstance(shared_list, list):
                for item in shared_list:
                    if isinstance(item, dict):
                        _add_roster_item(item)
        except Exception:
            pass

    for rec in students.values():
        _add_roster_item(
            {
                "userid": rec.userid,
                "fullname": rec.fullname,
                "firstname": rec.firstname,
                "surname": rec.surname,
                "participant_id": rec.participant_id,
                "moodle_folder": rec.moodle_folder,
            }
        )

    roster = sorted(roster_by_key.values(), key=lambda c: c.fullname.lower())
    return (course_id, cmid, sheet_number, students, roster)


def load_manifest(manifest_path: Path) -> Tuple[int, int, int, Dict[str, StudentSubmissionRecord]]:
    """Load the sheet manifest from disk."""
    course_id, cmid, sheet_number, students, _ = load_manifest_with_roster(manifest_path)
    return (course_id, cmid, sheet_number, students)


def sync_grades_csv(
    csv_path: Path,
    records: List[StudentSubmissionRecord],
    overwrite: bool = False,
) -> Dict[str, str]:
    """Create or update grades_sheet_[n].csv, sorted alphabetically by submitting student."""
    existing_points: Dict[str, str] = {}
    existing_partners: Dict[str, Tuple[str, str]] = {}
    if not overwrite and csv_path.exists():
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                key = (row.get("filename") or "").strip()
                pts = (row.get("points") or "").strip()
                p_name = (row.get("partner_name") or "").strip()
                p_uid = (row.get("partner_userid") or "").strip()
                if key and pts:
                    existing_points[key] = pts
                if key and (p_name or p_uid):
                    existing_partners[key] = (p_name, p_uid)

    sorted_records = sorted(records, key=lambda r: (r.fullname or "").lower())
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "filename",
                "student_name",
                "userid",
                "partner_name",
                "partner_userid",
                "points",
            ],
        )
        writer.writeheader()
        for rec in sorted_records:
            if overwrite:
                pts = rec.current_grade or ""
                p_name = rec.partner_fullname or ""
                p_uid = rec.partner_userid or ""
            else:
                pts = existing_points.get(rec.filename, rec.current_grade or "")
                p_name = rec.partner_fullname or existing_partners.get(rec.filename, ("", ""))[0]
                p_uid = rec.partner_userid or existing_partners.get(rec.filename, ("", ""))[1]
            writer.writerow(
                {
                    "filename": rec.filename,
                    "student_name": rec.fullname,
                    "userid": rec.userid,
                    "partner_name": p_name or "",
                    "partner_userid": p_uid or "",
                    "points": pts,
                }
            )
    return existing_points


def read_grades_csv(csv_path: Path) -> Dict[str, str]:
    """Read filename -> points mapping from grades_sheet_[n].csv."""
    points_by_file: Dict[str, str] = {}
    if not csv_path.exists():
        return points_by_file
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            fname = (row.get("filename") or "").strip()
            pts = (row.get("points") or "").strip()
            if fname and pts:
                points_by_file[fname] = pts
    return points_by_file


def extract_score_from_filename(pdf_filename: str, canonical_stem: str) -> Optional[str]:
    """Extract optional numerical score suffix from a marked PDF filename.

    Supports patterns such as:
    - MMustermann_sheet1_14.5p.pdf
    - MMustermann_sheet1_14,5pts.pdf
    - MMustermann_sheet1-18p.pdf
    """
    stem = Path(pdf_filename).stem
    if not stem.lower().startswith(canonical_stem.lower()):
        return None
    remainder = stem[len(canonical_stem) :]
    match = re.search(
        r"[_\s-]+(\d+(?:[.,]\d+)?)\s*(?:p|pt|pts|punkte)?$",
        remainder,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)
    return None


def match_marked_pdfs(
    marked_dir: Path,
    students_by_filename: Dict[str, StudentSubmissionRecord],
    csv_points: Optional[Dict[str, str]] = None,
) -> List[MarkedSubmissionItem]:
    """Match exported Notability PDFs in marked_dir back to their StudentSubmissionRecord."""
    csv_points = csv_points or {}
    if not marked_dir.exists():
        return []

    pdf_files = sorted(
        p for p in marked_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"
    )
    matched: List[MarkedSubmissionItem] = []
    used_paths = set()

    for canonical_name, record in students_by_filename.items():
        canonical_stem = Path(canonical_name).stem
        chosen_path: Optional[Path] = None
        extracted_score: Optional[str] = None

        for candidate in pdf_files:
            if candidate in used_paths:
                continue
            if candidate.name.lower() == canonical_name.lower():
                chosen_path = candidate
                break

        if chosen_path is None:
            for candidate in pdf_files:
                if candidate in used_paths:
                    continue
                if candidate.stem.lower().startswith(canonical_stem.lower()):
                    chosen_path = candidate
                    extracted_score = extract_score_from_filename(
                        candidate.name, canonical_stem
                    )
                    break

        if chosen_path is not None:
            used_paths.add(chosen_path)
            points = (
                csv_points.get(canonical_name)
                or (record.current_grade if record.current_grade else None)
                or extracted_score
                or None
            )
            matched.append(
                MarkedSubmissionItem(
                    record=record,
                    pdf_path=chosen_path,
                    points=points,
                )
            )

    return matched


def _normalize_match_str(text: str) -> str:
    """Normalize text for fuzzy name matching (lowercase, expand German umlauts, strip accents)."""
    translated = text.translate(UMLAUT_MAP).lower()
    normalized = unicodedata.normalize("NFKD", translated)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_only).strip()


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute exact character-level Levenshtein edit distance between s1 and s2."""
    if s1 == s2:
        return 0
    if not s1:
        return len(s2)
    if not s2:
        return len(s1)
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1, start=1):
        cur_row = [i]
        for j, c2 in enumerate(s2, start=1):
            insertions = prev_row[j] + 1
            deletions = cur_row[j - 1] + 1
            substitutions = prev_row[j - 1] + (0 if c1 == c2 else 1)
            cur_row.append(min(insertions, deletions, substitutions))
        prev_row = cur_row
    return prev_row[-1]


def _norm_edit_similarity(s1: str, s2: str) -> float:
    """Return normalized Levenshtein similarity in [0.0, 1.0] (1.0 = identical, 0.0 = completely different)."""
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0
    max_len = max(len(s1), len(s2))
    dist = _levenshtein_distance(s1, s2)
    return max(0.0, 1.0 - (dist / max_len))


def _crop_image_top_bytes(image_bytes: bytes, top_fraction: float = 0.42) -> bytes:
    """If Pillow is available and the image is tall (full page scan), crop to the top header region."""
    try:
        from PIL import Image, ImageOps  # type: ignore

        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        if h > 600 and h > w * 0.9:
            img = img.crop((0, 0, w, max(250, int(h * top_fraction))))
        img = ImageOps.grayscale(img)
        img = ImageOps.autocontrast(img, cutoff=2)
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return image_bytes


def _run_ocr_on_image_bytes(image_bytes: bytes) -> str:
    """Run local OCR on image bytes if a local OCR engine (tesseract, pytesseract, rapidocr) is available."""
    if not image_bytes:
        return ""

    cropped_bytes = _crop_image_top_bytes(image_bytes, top_fraction=0.42)

    # 1. Try rapidocr_onnxruntime if installed
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        engine = RapidOCR()
        result, _ = engine(cropped_bytes)
        if result:
            return "\n".join(str(line[1]) for line in result if len(line) > 1)
    except Exception:
        pass

    # 2. Try pytesseract if installed
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore

        img = Image.open(io.BytesIO(cropped_bytes))
        return str(pytesseract.image_to_string(img))
    except Exception:
        pass

    # 3. Try tesseract CLI if installed on PATH
    if os.sys.platform != "ios" and shutil.which("tesseract"):
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp_img:
                tmp_img.write(cropped_bytes)
                tmp_img.flush()
                proc = subprocess.run(
                    ["tesseract", tmp_img.name, "stdout", "-l", "deu+eng"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    return proc.stdout
                proc_en = subprocess.run(
                    ["tesseract", tmp_img.name, "stdout"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                if proc_en.returncode == 0:
                    return proc_en.stdout
        except Exception:
            pass

    return ""


def _extract_top_prioritized_page_text(page: Any) -> Tuple[str, str]:
    """Extract text from a pypdf Page object, separating the top 42% header from the rest of the page."""
    fragments: List[Tuple[float, float, str]] = []

    def _visitor(text: str, cm: Any, tm: Any, font_dict: Any, font_size: Any) -> None:
        if text and text.strip():
            try:
                x = float(tm[4])
                y = float(tm[5])
            except Exception:
                x, y = 0.0, 0.0
            fragments.append((y, x, text))

    try:
        page.extract_text(visitor_text=_visitor)
    except Exception:
        fragments = []

    if fragments:
        ys = [f[0] for f in fragments]
        min_y, max_y = min(ys), max(ys)
        try:
            box_h = float(page.mediabox.height)
            box_top = float(page.mediabox.top)
            if box_h > 100:
                max_y = max(max_y, box_top)
                min_y = min(min_y, box_top - box_h)
        except Exception:
            pass

        span_y = max(max_y - min_y, 1.0)
        # In PDF user coordinates, larger y is closer to the top of the page
        top_cutoff_y = max_y - 0.42 * span_y
        # Sort top-to-bottom (descending y in steps of 4pt), then left-to-right (ascending x)
        fragments.sort(key=lambda item: (-round(item[0] / 4.0), round(item[1], 1)))

        top_parts = [f[2] for f in fragments if f[0] >= top_cutoff_y]
        body_parts = [f[2] for f in fragments if f[0] < top_cutoff_y]
        top_text = " ".join(top_parts).strip()
        body_text = " ".join(body_parts).strip()
        return (top_text, body_text)

    try:
        raw_text = (page.extract_text() or "").strip()
        return (raw_text[:800], raw_text[800:2000])
    except Exception:
        return ("", "")


def is_excluded_partner_name(fullname: str) -> bool:
    """Return True if fullname belongs to the course professor or assistants who cannot be partners."""
    norm = _normalize_match_str(fullname or "")
    if not norm:
        return False
    tokens = set(norm.split())
    cand_name_tokens = [tok for tok in norm.split() if tok not in _MASK_WORDS and len(tok) >= 2]
    cand_core = " ".join(cand_name_tokens)

    for trainer_name in get_excluded_partner_names():
        t_norm = _normalize_match_str(trainer_name)
        if not t_norm:
            continue
        if _norm_edit_similarity(norm, t_norm) >= 0.85:
            return True
        t_name_tokens = [tok for tok in t_norm.split() if tok not in _MASK_WORDS and len(tok) >= 2]
        t_core = " ".join(t_name_tokens)
        if cand_core and t_core and _norm_edit_similarity(cand_core, t_core) >= 0.85:
            return True
        if len(t_name_tokens) >= 2:
            if all(
                any(_norm_edit_similarity(t_tok, c_tok) >= 0.85 for c_tok in tokens)
                for t_tok in t_name_tokens
            ):
                return True
        elif len(t_name_tokens) == 1:
            if t_name_tokens[0] in cand_name_tokens and len(cand_name_tokens) == 1:
                return True
    return False


def is_totally_typed_page(page: Any) -> bool:
    """Return True if a PDF page consists solely of typed text with no images, ink annotations, or handwritten vector strokes."""
    # 1. Check for embedded raster images (scans, photos, rasterized handwriting)
    try:
        resources = page.get("/Resources")
        if hasattr(resources, "get_object"):
            resources = resources.get_object()
        if isinstance(resources, dict):
            xobjs = resources.get("/XObject")
            if hasattr(xobjs, "get_object"):
                xobjs = xobjs.get_object()
            if isinstance(xobjs, dict):
                for xobj_ref in xobjs.values():
                    try:
                        xobj = (
                            xobj_ref.get_object()
                            if hasattr(xobj_ref, "get_object")
                            else xobj_ref
                        )
                        if (
                            isinstance(xobj, dict)
                            and str(xobj.get("/Subtype", "")) == "/Image"
                        ):
                            return False
                    except Exception:
                        pass
    except Exception:
        pass

    try:
        if getattr(page, "images", None) and len(page.images) > 0:
            return False
    except Exception:
        pass

    # 2. Check for non-Link PDF annotations (/Ink, /FreeText, /Popup, /Stamp, etc.)
    try:
        annots = page.get("/Annots")
        if hasattr(annots, "get_object"):
            annots = annots.get_object()
        if annots:
            for annot_ref in annots:
                annot_obj = (
                    annot_ref.get_object()
                    if hasattr(annot_ref, "get_object")
                    else annot_ref
                )
                if isinstance(annot_obj, dict):
                    subtype = str(annot_obj.get("/Subtype", ""))
                    if subtype and subtype != "/Link":
                        return False
    except Exception:
        pass

    # 3. Check content stream for handwritten vector paths (Notability / GoodNotes / Apple Pencil ink strokes)
    try:
        contents = page.get_contents()
        if contents is not None:
            raw_bytes = contents.get_data()
            stream_str = raw_bytes.decode("latin-1", errors="ignore")
            non_text_stream = re.sub(
                r"\bBT\b.*?\bET\b", " ", stream_str, flags=re.DOTALL
            )
            stroke_ops = re.findall(
                r"(?<![A-Za-z0-9_/])(?:c|v|y|l)(?![A-Za-z0-9_])", non_text_stream
            )
            if len(stroke_ops) > 25:
                return False
    except Exception:
        pass

    # 4. If the page has embedded typed text and no images/annotations/handwritten strokes, it is totally typed
    try:
        raw_txt = (page.extract_text() or "").strip()
        if len(raw_txt) > 0:
            return True
    except Exception:
        pass

    return False


def _is_typed_assignment_text(top_txt: str, body_txt: str) -> bool:
    """Return True if embedded PDF text on a page is the typed course assignment sheet."""
    combined_norm = _normalize_match_str(f"{top_txt} {body_txt}")
    if not combined_norm:
        return False
    tokens = set(combined_norm.split())
    for trainer_name in get_excluded_partner_names():
        t_norm = _normalize_match_str(trainer_name)
        t_name_tokens = [tok for tok in t_norm.split() if tok not in _MASK_WORDS and len(tok) >= 2]
        if len(t_name_tokens) >= 2:
            if all(
                any(_norm_edit_similarity(t_tok, c_tok) >= 0.85 for c_tok in tokens)
                for t_tok in t_name_tokens
            ):
                return True
    return False


def extract_text_and_ocr_from_pdf(pdf_path: Path, max_pages: int = 4) -> Tuple[str, str]:
    """Extract top-of-page text, annotations, and local OCR text from the first non-typed page of a PDF.

    Automatically skips totally typed pages (such as an included assignment sheet) so that
    only pages containing student handwriting, annotations, or scanned images are searched.
    """
    if not pdf_path.exists():
        return ("", "none")

    from pypdf import PdfReader

    top_text_chunks: List[str] = []
    body_text_chunks: List[str] = []
    ocr_chunks: List[str] = []

    try:
        reader = PdfReader(str(pdf_path))
        pages_to_scan = reader.pages[: max(1, max_pages)]
        active_page_idx: Optional[int] = None
        active_page: Any = None

        for page_idx, page in enumerate(pages_to_scan):
            if is_totally_typed_page(page):
                continue
            active_page_idx = page_idx
            active_page = page
            break

        if active_page is None:
            return ("", "typed_excluded")

        if reader.metadata and active_page_idx == 0:
            for meta_key in ("/Author", "/Title", "/Subject", "/Keywords"):
                val = reader.metadata.get(meta_key)
                if val and isinstance(val, str) and val.strip():
                    top_text_chunks.append(val.strip())

        top_txt, body_txt = _extract_top_prioritized_page_text(active_page)
        if top_txt:
            top_text_chunks.append(top_txt)
        if body_txt:
            body_text_chunks.append(body_txt[:600])

        # Extract any PDF free-text / note annotations on the active page
        try:
            annots = active_page.get("/Annots")
            if annots:
                for annot_ref in annots:
                    annot_obj = annot_ref.get_object()
                    for key in ("/Contents", "/T", "/Subj"):
                        val = annot_obj.get(key)
                        if val and isinstance(val, str) and val.strip():
                            top_text_chunks.append(val.strip())
        except Exception:
            pass

        # Check active page images / OCR even when template text exists
        try:
            for img_file in active_page.images[:4]:
                ocr_txt = _run_ocr_on_image_bytes(img_file.data)
                if ocr_txt.strip():
                    ocr_chunks.append(ocr_txt.strip())
        except Exception:
            pass

        page_num_1based = str((active_page_idx or 0) + 1)
        if not ocr_chunks and os.sys.platform != "ios" and shutil.which("pdftoppm") and shutil.which("tesseract"):
            try:
                with tempfile.TemporaryDirectory() as tmp_dir:
                    prefix = str(Path(tmp_dir) / "page")
                    subprocess.run(
                        [
                            "pdftoppm",
                            "-png",
                            "-f",
                            page_num_1based,
                            "-l",
                            page_num_1based,
                            "-r",
                            "180",
                            str(pdf_path),
                            prefix,
                        ],
                        capture_output=True,
                        timeout=15,
                        check=False,
                    )
                    for png_path in sorted(Path(tmp_dir).glob("page*.png")):
                        ocr_txt = _run_ocr_on_image_bytes(png_path.read_bytes())
                        if ocr_txt.strip():
                            ocr_chunks.append(ocr_txt.strip())
            except Exception:
                pass

        combined_top = "\n".join(ocr_chunks + top_text_chunks).strip()
        combined_all = "\n".join([combined_top] + body_text_chunks).strip()
        if ocr_chunks and top_text_chunks:
            return (combined_all, "pdf_text+ocr")
        if ocr_chunks:
            return (combined_all, "ocr")
        if combined_all:
            return (combined_all, "pdf_text")
    except Exception:
        pass

    return ("", "none")


OCR_LETTER_REPAIR_IN_WORD = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "8": "b",
    }
)


def find_matriculation_anchors(text: str) -> List[Tuple[str, str]]:
    """Find 8-digit LMU matriculation numbers (including 1-2 char OCR digit lookalikes) and their local text windows.

    Returns a list of (eight_digit_number, surrounding_window_text) tuples.
    """
    if not text:
        return []

    anchors: List[Tuple[str, str]] = []
    seen_spans: List[Tuple[int, int]] = []

    def _overlaps(s: int, e: int) -> bool:
        return any(not (e <= os or s >= oe) for os, oe in seen_spans)

    def _window_around(start: int, end: int) -> str:
        # Prefer the same line (and immediately preceding line if short), stopping before any "Aufgabe" body line
        line_start = text.rfind("\n", 0, start)
        line_start = 0 if line_start == -1 else line_start + 1
        line_end = text.find("\n", end)
        line_end = len(text) if line_end == -1 else line_end

        same_line = text[line_start:line_end].strip()
        prev_line = ""
        if line_start > 1:
            prev_nl = text.rfind("\n", 0, line_start - 1)
            prev_start = 0 if prev_nl == -1 else prev_nl + 1
            cand_prev = text[prev_start : line_start - 1].strip()
            if cand_prev and not re.search(r"\b\d{8}\b", cand_prev):
                prev_line = cand_prev

        combined = f"{prev_line} {same_line}".strip() if len(same_line) < 28 else same_line
        return combined[:160]

    # 1. Exact 8-digit numbers or 4+4 digit numbers split by a single space/hyphen
    for m in re.finditer(r"(?<!\d)(\d{8}|\d{4}[ \t-]\d{4})(?!\d)", text):
        raw_digits = re.sub(r"\D", "", m.group(1))
        if len(raw_digits) == 8:
            seen_spans.append((m.start(), m.end()))
            anchors.append((raw_digits, _window_around(m.start(), m.end())))

    # 2. OCR-noisy 8-digit tokens (at least 6 real digits and 1-2 lookalike letters O/I/l/S/B/G/Z)
    for m in re.finditer(r"(?<![A-Za-z0-9])([0-9OoDQIl|!ZzSsGB]{8})(?![A-Za-z0-9])", text):
        if _overlaps(m.start(), m.end()):
            continue
        token = m.group(1)
        real_digit_count = sum(1 for ch in token if ch.isdigit())
        if real_digit_count >= 6:
            repaired = token.translate(OCR_DIGIT_REPAIR_MAP)
            if len(repaired) == 8 and repaired.isdigit():
                seen_spans.append((m.start(), m.end()))
                anchors.append((repaired, _window_around(m.start(), m.end())))

    # 3. Explicit "Matrikelnummer" / "Matr.-Nr." keyword windows even if digits were unreadable
    for m in re.finditer(r"matr(?:ikel(?:nummer|nr)?)?\.?\s*(?:-\s*nr\.?)?", text, re.IGNORECASE):
        if not _overlaps(m.start(), m.end()):
            anchors.append(("matr_kw", _window_around(m.start(), m.end())))

    return anchors


def _mask_submitter_and_boilerplate(
    raw_text: str,
    submitter_fullname: str,
    marker_name: str = "",
) -> str:
    """Remove the submitting student's name, tutor's name, numbers, and boilerplate header words."""
    norm = _normalize_match_str(raw_text)
    if not norm:
        return ""

    sub_first, sub_last = split_fullname(submitter_fullname)
    sub_first_norm = _normalize_match_str(sub_first)
    sub_last_norm = _normalize_match_str(sub_last)
    sub_primary_first = sub_first_norm.split()[0] if sub_first_norm else ""
    sub_initial = sub_primary_first[0] if sub_primary_first else ""

    # Remove full submitter phrases and excluded professor/assistant names first
    phrases_to_strip = [
        f"{sub_first_norm} {sub_last_norm}".strip(),
        f"{sub_last_norm} {sub_first_norm}".strip(),
        f"{sub_primary_first} {sub_last_norm}".strip(),
        f"{sub_last_norm} {sub_primary_first}".strip(),
        f"{sub_initial} {sub_last_norm}".strip(),
        f"{sub_initial}{sub_last_norm}".strip(),
    ]
    for trainer_name in get_excluded_partner_names():
        t_norm = _normalize_match_str(trainer_name)
        if not t_norm:
            continue
        phrases_to_strip.append(t_norm)
        t_name_tokens = [tok for tok in t_norm.split() if tok not in _MASK_WORDS and len(tok) >= 2]
        if len(t_name_tokens) >= 2:
            first = t_name_tokens[0]
            last = t_name_tokens[-1]
            first_init = first[0] if first else ""
            phrases_to_strip.extend(
                [
                    f"{first} {last}",
                    f"{last} {first}",
                ]
            )
            if first_init and last:
                phrases_to_strip.append(f"{first_init} {last}")
                phrases_to_strip.append(f"{first_init}{last}")
    if marker_name:
        m_first, m_last = split_fullname(marker_name)
        mf_norm = _normalize_match_str(m_first)
        ml_norm = _normalize_match_str(m_last)
        phrases_to_strip.extend(
            [
                _normalize_match_str(marker_name),
                f"{mf_norm} {ml_norm}".strip(),
                f"{ml_norm} {mf_norm}".strip(),
                ml_norm,
            ]
        )

    for phrase in phrases_to_strip:
        if phrase and len(phrase) >= 2:
            norm = re.sub(rf"\b{re.escape(phrase)}\b", " ", norm)

    # Also remove standalone submitter surname (and standalone submitter first name tokens if >= 3 chars)
    if sub_last_norm and len(sub_last_norm) >= 2:
        norm = re.sub(rf"\b{re.escape(sub_last_norm)}\b", " ", norm)
    for ft in sub_first_norm.split():
        if len(ft) >= 3:
            norm = re.sub(rf"\b{re.escape(ft)}\b", " ", norm)

    # Filter out boilerplate tokens and pure numbers, repairing OCR digits inside name tokens (e.g. er1ka -> erika)
    kept_tokens: List[str] = []
    for tok in norm.split():
        if tok.isdigit():
            continue
        # Check if token is an 8-char OCR-noisy matriculation number
        if len(tok) == 8 and sum(1 for c in tok if c.isdigit()) >= 5:
            continue
        # Strip trailing digits on boilerplate like blatt1, sheet2, aufgabe3
        stem_no_trailing_num = re.sub(r"\d+$", "", tok)
        if stem_no_trailing_num in HEADER_BOILERPLATE_WORDS or tok in HEADER_BOILERPLATE_WORDS:
            continue
        # Repair OCR digit substitutions inside alphabetic words (e.g. er1ka -> erika, j0hn -> john)
        repaired_word = re.sub(r"\d+", "", tok.translate(OCR_LETTER_REPAIR_IN_WORD))
        if not repaired_word or repaired_word in HEADER_BOILERPLATE_WORDS:
            continue
        kept_tokens.append(repaired_word)

    return " ".join(kept_tokens).strip()


def _score_student_against_zone(
    student: CourseStudent,
    zone_norm_text: str,
    unique_surname: bool = False,
) -> float:
    """Compute a continuous similarity score in [0.0, 1.0] (1 - min_distance) between a student and a cleaned text zone."""
    if not zone_norm_text:
        return 0.0

    tokens = zone_norm_text.split()
    if not tokens:
        return 0.0

    first_norm = _normalize_match_str(student.firstname)
    last_norm = _normalize_match_str(student.surname)
    if not last_norm:
        return 0.0

    first_parts = first_norm.split()
    primary_first = first_parts[0] if first_parts else ""
    initial = primary_first[0] if primary_first else ""

    short_fwd = f"{primary_first} {last_norm}".strip()
    short_rev = f"{last_norm} {primary_first}".strip()
    full_fwd = f"{first_norm} {last_norm}".strip()
    full_rev = f"{last_norm} {first_norm}".strip()
    init_space = f"{initial} {last_norm}".strip() if initial else ""
    init_concat = f"{initial}{last_norm}".strip() if initial else ""
    concat_full = f"{primary_first}{last_norm}".strip()

    best_sim = 0.0

    # 1. Exact substring / word-boundary check
    for phrase in (full_fwd, full_rev, short_fwd, short_rev):
        if len(phrase) >= 5 and re.search(rf"\b{re.escape(phrase)}\b", zone_norm_text):
            return 0.99

    # 2. Sliding n-gram word windows (1, 2, 3 words) minimum Levenshtein distance
    target_two_word = [p for p in (short_fwd, short_rev, full_fwd, full_rev) if p]
    for win_size in (1, 2, 3):
        if len(tokens) < win_size:
            continue
        for i in range(len(tokens) - win_size + 1):
            w_str = " ".join(tokens[i : i + win_size])
            for target in target_two_word:
                sim = _norm_edit_similarity(target, w_str)
                if sim > best_sim:
                    best_sim = sim
            if win_size in (1, 2) and init_space:
                # Initial + surname (e.g. "e musterfrau" or "emusterfrau")
                sim_init = max(
                    _norm_edit_similarity(init_space, w_str),
                    _norm_edit_similarity(init_concat, w_str),
                ) * 0.93
                if sim_init > best_sim:
                    best_sim = sim_init
            if win_size == 1 and concat_full:
                sim_c = _norm_edit_similarity(concat_full, w_str) * 0.96
                if sim_c > best_sim:
                    best_sim = sim_c

    # 3. Separate token-pair minimum distance (First Name token + Surname token)
    if primary_first and len(last_norm) >= 2:
        last_sims = [(_norm_edit_similarity(last_norm, tok), idx) for idx, tok in enumerate(tokens)]
        best_last_sim, best_last_idx = max(last_sims, key=lambda x: x[0])

        first_sims = []
        for idx, tok in enumerate(tokens):
            if idx == best_last_idx and len(tokens) > 1:
                continue
            f_sim = _norm_edit_similarity(primary_first, tok)
            if len(tok) == 1 and tok == initial:
                f_sim = max(f_sim, 0.82)
            first_sims.append((f_sim, abs(idx - best_last_idx)))

        if first_sims:
            best_first_sim, gap = max(first_sims, key=lambda x: (x[0], -x[1]))
            gap_penalty = 0.0 if gap <= 2 else (0.04 if gap <= 4 else 0.12)
            pair_sim = max(0.0, 0.38 * best_first_sim + 0.62 * best_last_sim - gap_penalty)
            # Synergy boost when BOTH first name and surname match near each other
            if best_first_sim >= 0.58 and best_last_sim >= 0.62 and gap <= 3:
                pair_sim = min(0.98, pair_sim + 0.08)
            if pair_sim > best_sim:
                best_sim = pair_sim

        # 4. Surname-only fallback when first name is absent (capped so a 2-token first+last match always wins)
        if len(last_norm) >= 4:
            weight = 0.68 if unique_surname else 0.60
            surname_only_sim = best_last_sim * weight
            if surname_only_sim > best_sim:
                best_sim = surname_only_sim

    return round(min(1.0, best_sim), 4)


def match_partner_in_course_roster(
    extracted_text: str,
    original_filenames: List[str],
    submitter_userid: str,
    submitter_fullname: str,
    course_roster: List[CourseStudent],
    marker_name: str = "",
    known_partners: Optional[Dict[str, str]] = None,
    known_matriculations: Optional[Dict[str, str]] = None,
) -> List[Tuple[CourseStudent, float, str]]:
    """Rank students in the Ana 1 course roster by minimum edit distance to names in the PDF header.

    Uses 8-digit LMU matriculation numbers as high-priority spatial anchors, masks out the
    submitting student and boilerplate words, and ranks the entire roster so the closest
    match is always suggested for tutor review.
    """
    if not course_roster:
        return []

    known_partners = known_partners or {}
    known_matriculations = known_matriculations or {}
    sub_norm_full = _normalize_match_str(submitter_fullname)

    # Extract 8-digit matriculation number anchors and their surrounding text windows
    anchors = find_matriculation_anchors(extracted_text or "")
    anchor_windows_cleaned: List[Tuple[str, str]] = []
    detected_eight_digits: List[str] = []

    for matr_num, raw_win in anchors:
        if len(matr_num) == 8 and matr_num.isdigit() and matr_num not in detected_eight_digits:
            detected_eight_digits.append(matr_num)
        cleaned_win = _mask_submitter_and_boilerplate(raw_win, submitter_fullname, marker_name)
        if cleaned_win:
            anchor_windows_cleaned.append((matr_num, cleaned_win))

    # Clean the top header text (first 1200 chars of extracted text, which is already top-prioritized)
    header_slice = (extracted_text or "")[:1200]
    cleaned_header = _mask_submitter_and_boilerplate(header_slice, submitter_fullname, marker_name)

    # Clean the original uploaded filenames
    fname_raw = " ".join(Path(fn).stem.replace("_", " ").replace("-", " ") for fn in original_filenames)
    cleaned_fname = _mask_submitter_and_boilerplate(fname_raw, submitter_fullname, marker_name)

    has_any_signal = bool(
        anchor_windows_cleaned
        or cleaned_header
        or cleaned_fname
        or str(submitter_userid) in known_partners
    )
    if not has_any_signal:
        return []

    # Count surname frequencies in roster to weight unique surnames appropriately
    surname_counts: Dict[str, int] = {}
    for student in course_roster:
        s_norm = _normalize_match_str(student.surname)
        if s_norm:
            surname_counts[s_norm] = surname_counts.get(s_norm, 0) + 1

    ranked_candidates: List[Tuple[CourseStudent, float, str]] = []
    remembered_partner_uid = str(known_partners.get(str(submitter_userid), ""))

    for student in course_roster:
        if str(student.userid) == str(submitter_userid):
            continue
        if _normalize_match_str(student.fullname) == sub_norm_full:
            continue
        if is_excluded_partner_name(student.fullname):
            continue

        last_norm = _normalize_match_str(student.surname)
        if not last_norm:
            continue

        is_unique_surname = surname_counts.get(last_norm, 0) == 1
        best_score = 0.0
        best_reason = "min_distance"

        # 1. Check direct 8-digit matriculation number match (exact or 1-digit edit distance)
        student_matr = student.matriculation_number
        if not student_matr:
            for k_matr, k_uid in known_matriculations.items():
                if str(k_uid) == str(student.userid):
                    student_matr = k_matr
                    break

        if student_matr and len(student_matr) == 8:
            for det_m in detected_eight_digits:
                dist_m = _levenshtein_distance(student_matr, det_m)
                if dist_m == 0:
                    best_score = 0.995
                    best_reason = f"matr_exact:{det_m}"
                    break
                if dist_m == 1 and best_score < 0.94:
                    best_score = 0.94
                    best_reason = f"matr_near:{det_m}"

        # 2. Score against 8-digit matriculation anchor windows (highest priority text zone)
        for matr_tag, win_text in anchor_windows_cleaned:
            sim_win = _score_student_against_zone(student, win_text, unique_surname=is_unique_surname)
            # Boost matches found directly next to an 8-digit matriculation number
            boosted_sim = min(0.99, sim_win * 1.08) if matr_tag != "matr_kw" else min(0.98, sim_win * 1.04)
            if boosted_sim > best_score:
                best_score = round(boosted_sim, 4)
                label = f"near Matr. {matr_tag}" if len(matr_tag) == 8 else "near Matr.-Nr."
                best_reason = label

        # 3. Score against general top-of-page header text
        if cleaned_header:
            sim_hdr = _score_student_against_zone(
                student, cleaned_header, unique_surname=is_unique_surname
            )
            if sim_hdr > best_score:
                best_score = sim_hdr
                best_reason = "top_header"

        # 4. Score against original uploaded filename
        if cleaned_fname:
            sim_fn = _score_student_against_zone(
                student, cleaned_fname, unique_surname=is_unique_surname
            )
            if sim_fn > best_score:
                best_score = sim_fn
                best_reason = "filename"

        # 5. Cross-sheet partner memory (if these two students worked together on another sheet)
        if remembered_partner_uid and str(student.userid) == remembered_partner_uid:
            if best_score >= 0.45:
                best_score = min(0.99, best_score + 0.20)
                best_reason = f"previous_partner+{best_reason}"
            else:
                best_score = max(best_score, 0.88)
                best_reason = "previous_sheet_partner"

        if best_score > 0.0:
            ranked_candidates.append((student, round(best_score, 4), best_reason))

    ranked_candidates.sort(key=lambda item: (item[1], len(item[0].fullname)), reverse=True)
    return ranked_candidates


def detect_partner_for_record(
    record: StudentSubmissionRecord,
    pdf_path: Optional[Path],
    course_roster: List[CourseStudent],
    extra_top_text: str = "",
    marker_name: str = "",
) -> Optional[CourseStudent]:
    """Extract top-of-page text/OCR, find 8-digit matriculation numbers, and suggest the minimum-distance partner."""
    settings = load_user_settings()
    if not marker_name:
        marker_name = str(settings.get("marker") or "")
    known_partners: Dict[str, str] = dict(settings.get("known_partners") or {})
    known_matriculations: Dict[str, str] = dict(settings.get("known_matriculations") or {})

    extracted_text = ""
    method = "filename"
    if pdf_path and pdf_path.exists():
        extracted_text, method = extract_text_and_ocr_from_pdf(pdf_path)

    if extra_top_text and extra_top_text.strip():
        extracted_text = f"{extra_top_text.strip()}\n{extracted_text}".strip()
        method = "webkit_ocr" if method == "none" else f"webkit_ocr+{method}"

    anchors = find_matriculation_anchors(extracted_text)
    record.detected_matriculations = []
    for m_num, _ in anchors:
        if len(m_num) == 8 and m_num.isdigit() and m_num not in record.detected_matriculations:
            record.detected_matriculations.append(m_num)

    cleaned_preview = _mask_submitter_and_boilerplate(
        (extracted_text or "")[:350], record.fullname, marker_name
    )
    record.header_excerpt = cleaned_preview[:120]

    matches = match_partner_in_course_roster(
        extracted_text=extracted_text,
        original_filenames=record.original_filenames,
        submitter_userid=record.userid,
        submitter_fullname=record.fullname,
        course_roster=course_roster,
        marker_name=marker_name,
        known_partners=known_partners,
        known_matriculations=known_matriculations,
    )

    # Always store top 5 closest minimum-distance candidates from the Ana 1 course roster
    record.partner_candidates = [
        f"{m[0].fullname} ({m[0].userid})" if m[0].userid else m[0].fullname
        for m in matches[:5]
    ]

    if matches:
        best_student, score, reason = matches[0]
        # Suggest the minimum-distance student whenever there is any plausible name signal (>= 0.42)
        if score >= 0.42:
            record.partner_userid = best_student.userid
            record.partner_fullname = best_student.fullname
            pct = int(round(score * 100))
            if score >= 0.80:
                record.partner_match_source = f"{reason} ({pct}%)"
            else:
                record.partner_match_source = f"min-distance suggestion: {reason} ({pct}% • review)"
            return best_student

    return None


def remember_confirmed_partner(
    record: StudentSubmissionRecord,
    settings_path: Path = SETTINGS_FILE,
) -> None:
    """Persist confirmed student pairs and 8-digit matriculation associations across sheets."""
    if not record.userid or not record.partner_userid:
        return
    current = load_user_settings(settings_path)
    known_partners = dict(current.get("known_partners") or {})
    known_partners[str(record.userid)] = str(record.partner_userid)
    known_partners[str(record.partner_userid)] = str(record.userid)
    save_user_settings({"known_partners": known_partners}, settings_path=settings_path)


def forget_confirmed_partner(
    userid: str,
    settings_path: Path = SETTINGS_FILE,
) -> None:
    """Remove any stored cross-sheet partner association for userid."""
    if not userid:
        return
    current = load_user_settings(settings_path)
    known_partners = dict(current.get("known_partners") or {})
    uid_str = str(userid)
    old_partner = known_partners.pop(uid_str, None)
    if old_partner and known_partners.get(str(old_partner)) == uid_str:
        known_partners.pop(str(old_partner), None)
    save_user_settings({"known_partners": known_partners}, settings_path=settings_path)


def resolve_partner_by_query(
    query: str,
    course_roster: List[CourseStudent],
    exclude_userid: Optional[str] = None,
) -> Optional[CourseStudent]:
    """Resolve a tutor-entered partner name or userid against the Ana 1 course roster."""
    cleaned = (query or "").strip()
    if not cleaned:
        return None

    name_part_check = re.sub(r"\s*\([^()]+\)$", "", cleaned).strip()
    if is_excluded_partner_name(name_part_check):
        return None

    # Check if userid is in parentheses, e.g. "Erika Musterfrau (102)" or "(part_55502)" or raw digits
    uid_match = re.search(r"\(([^()]+)\)$", cleaned)
    target_uid = uid_match.group(1).strip() if uid_match else (cleaned if cleaned.isdigit() else None)
    name_part = strip_moodle_userinitials(re.sub(r"\s*\([^()]+\)$", "", cleaned).strip())
    if target_uid:
        for st in course_roster:
            if str(st.userid) == str(target_uid) and not is_excluded_partner_name(st.fullname):
                return st
        if name_part and not is_excluded_partner_name(name_part):
            p_first, p_last = split_fullname(name_part)
            p_pid = (
                target_uid[5:]
                if str(target_uid).startswith("part_") and str(target_uid)[5:].isdigit()
                else None
            )
            return CourseStudent(
                userid=str(target_uid),
                fullname=name_part,
                firstname=p_first,
                surname=p_last,
                participant_id=p_pid,
            )

    norm_q = _normalize_match_str(name_part)
    if not norm_q:
        return None

    # Exact normalized full name match
    for st in course_roster:
        if exclude_userid and str(st.userid) == str(exclude_userid):
            continue
        if is_excluded_partner_name(st.fullname):
            continue
        first_n = _normalize_match_str(st.firstname)
        last_n = _normalize_match_str(st.surname)
        if norm_q in (
            _normalize_match_str(st.fullname),
            f"{first_n} {last_n}".strip(),
            f"{last_n} {first_n}".strip(),
        ):
            return st

    # Minimum edit distance / token subset match across the roster
    q_tokens = set(norm_q.split())
    best_st: Optional[CourseStudent] = None
    best_sim = 0.0
    for st in course_roster:
        if exclude_userid and str(st.userid) == str(exclude_userid):
            continue
        if is_excluded_partner_name(st.fullname):
            continue
        st_norm = _normalize_match_str(st.fullname)
        st_tokens = set(st_norm.split())
        if len(q_tokens) >= 2 and q_tokens.issubset(st_tokens):
            return st
        sim = _norm_edit_similarity(norm_q, st_norm)
        if sim > best_sim:
            best_sim = sim
            best_st = st

    if best_st and best_sim >= 0.80:
        return best_st
    return None


def expand_marked_items_with_partners(
    items: List[MarkedSubmissionItem],
    course_roster: List[CourseStudent],
) -> List[MarkedSubmissionItem]:
    """Expand marked submission items so that each student's partner also receives the grade and grader."""
    roster_by_uid = {str(st.userid): st for st in course_roster}
    expanded: List[MarkedSubmissionItem] = list(items)
    seen_userids = {str(it.record.userid) for it in items}

    for item in items:
        rec = item.record
        partner_st: Optional[CourseStudent] = None
        if rec.partner_userid and str(rec.partner_userid) in roster_by_uid:
            partner_st = roster_by_uid[str(rec.partner_userid)]
        if partner_st is None and rec.partner_fullname:
            partner_st = resolve_partner_by_query(
                rec.partner_fullname, course_roster, exclude_userid=rec.userid
            )
            if partner_st is None:
                p_first, p_last = split_fullname(rec.partner_fullname)
                fallback_uid = (
                    str(rec.partner_userid)
                    if rec.partner_userid
                    else f"partner_of_{rec.userid}"
                )
                fallback_pid = (
                    fallback_uid[5:]
                    if fallback_uid.startswith("part_") and fallback_uid[5:].isdigit()
                    else None
                )
                partner_st = CourseStudent(
                    userid=fallback_uid,
                    fullname=rec.partner_fullname,
                    firstname=p_first,
                    surname=p_last,
                    participant_id=fallback_pid,
                )

        if partner_st and str(partner_st.userid) not in seen_userids:
            seen_userids.add(str(partner_st.userid))
            p_first, p_last = split_fullname(partner_st.fullname)
            p_pid = partner_st.participant_id
            if not p_pid and str(partner_st.userid).startswith("part_"):
                cand_pid = str(partner_st.userid)[5:]
                if cand_pid.isdigit():
                    p_pid = cand_pid
            partner_rec = StudentSubmissionRecord(
                userid=str(partner_st.userid),
                fullname=partner_st.fullname,
                firstname=p_first,
                surname=p_last,
                filename=rec.filename,
                sheet_number=rec.sheet_number,
                participant_id=p_pid,
                moodle_folder=partner_st.moodle_folder,
                original_filenames=list(rec.original_filenames),
                current_grade=str(item.points or rec.current_grade or ""),
                partner_match_source="partner_expansion",
            )
            expanded.append(
                MarkedSubmissionItem(
                    record=partner_rec,
                    pdf_path=item.pdf_path,
                    points=item.points,
                )
            )

    return expanded


def build_moodle_feedback_zip(items: List[MarkedSubmissionItem]) -> bytes:
    """Build an in-memory Moodle feedback ZIP where each PDF sits in its exact student folder."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            rec = item.record
            if rec.moodle_folder:
                folder = rec.moodle_folder.rstrip("/")
            elif rec.participant_id:
                folder = f"{rec.fullname}_{rec.participant_id}_assignsubmission_file_"
            else:
                raise ValueError(
                    f"Missing moodle_folder and participant_id for {rec.fullname} (userid={rec.userid})."
                )
            stem = Path(rec.filename).stem
            target_entry = f"{folder}/{stem}_annotated.pdf"
            zf.writestr(target_entry, item.pdf_path.read_bytes())
    return buffer.getvalue()


def load_user_settings(settings_path: Path = SETTINGS_FILE) -> Dict[str, Any]:
    """Load non-sensitive user preferences (workdir, course_id, username, last_sheet)."""
    merged = dict(DEFAULT_SETTINGS)
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                loaded.pop("password", None)
                merged.update(loaded)
        except Exception:
            pass
    if "excluded_trainers" in merged and isinstance(merged["excluded_trainers"], list):
        register_excluded_partner_names(merged["excluded_trainers"])
    return merged


def save_user_settings(
    updates: Dict[str, Any], settings_path: Path = SETTINGS_FILE
) -> Dict[str, Any]:
    """Persist non-sensitive user preferences to settings.json (never stores passwords)."""
    current = load_user_settings(settings_path)
    safe_updates = {k: v for k, v in updates.items() if k.lower() != "password"}
    current.update(safe_updates)
    settings_path.write_text(
        json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return current


def resolve_storage_path(path_str: str) -> Path:
    """Resolve a storage folder path, including iPadOS a-Shell bookmarks such as ~icloud."""
    cleaned = (path_str or "./sheets").strip()
    expanded = os.path.expanduser(cleaned)
    script_dir = Path(__file__).resolve().parent
    if expanded.startswith("~") and not expanded.startswith("~/"):
        # Handle a-Shell bookmark syntax (~bookmark or ~bookmark/subdir) without spawning subprocesses on iOS
        parts = expanded.split("/", 1)
        subpath = parts[1].strip("/") if len(parts) > 1 else ""
        return (script_dir / subpath).resolve() if subpath else script_dir
    candidate = Path(expanded)
    if not candidate.is_absolute():
        return (script_dir / candidate).resolve()
    return candidate.resolve()


def update_grades_csv_entries(
    csv_path: Path,
    records: List[StudentSubmissionRecord],
    points_updates: Dict[str, str],
) -> None:
    """Overwrite grades_sheet_[n].csv with the current partners and points without preserving older values."""
    sorted_records = sorted(records, key=lambda r: (r.fullname or "").lower())
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "filename",
                "student_name",
                "userid",
                "partner_name",
                "partner_userid",
                "points",
            ],
        )
        writer.writeheader()
        for rec in sorted_records:
            if rec.filename in points_updates:
                pts = str(points_updates[rec.filename] or "").strip()
            else:
                pts = str(rec.current_grade or "").strip()
            rec.current_grade = pts
            writer.writerow(
                {
                    "filename": rec.filename,
                    "student_name": rec.fullname,
                    "userid": rec.userid,
                    "partner_name": rec.partner_fullname or "",
                    "partner_userid": rec.partner_userid or "",
                    "points": pts,
                }
            )
