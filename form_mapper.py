import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf import PdfWriter
from pypdf.generic import BooleanObject, NameObject, TextStringObject

try:
    import fitz  # PyMuPDF
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
except Exception:  # pragma: no cover - optional runtime dependency
    fitz = None


@dataclass
class Widget:
    uid: str
    name: str
    page: int
    x: float
    y: float
    rect: list[float]
    field_type: str = ""
    tooltip: str = ""
    value: str = ""
    options: list[str] | None = None


@dataclass
class TextSpan:
    page: int
    x: float
    y: float
    text: str


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def widget_uid(page: int, name: str, rect: list[float]) -> str:
    coords = ",".join(f"{float(v):.2f}" for v in rect)
    return f"p{page}:{name}:{coords}"


def widget_rect_uid(page: int, rect: list[float]) -> str:
    coords = ",".join(f"{float(v):.2f}" for v in rect)
    return f"p{page}:{coords}"


def rect_uid_from_widget_uid(uid: str) -> str | None:
    match = re.match(r"^p(\d+):.*:([-0-9.,]+)$", uid)
    if not match:
        return None
    return f"p{match.group(1)}:{match.group(2)}"


def parse_date(value: Any) -> datetime | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%m/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def format_date_for_field(value: Any, field_name: str, default_format: str = "DD/MM/YYYY") -> str:
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""

    dt = parse_date(raw)
    if dt is None:
        return raw

    n = normalize(field_name)
    if "ddmmyyyy" in n:
        return dt.strftime("%d%m%Y")
    if "dateofbirthddmmyyyy" in n:
        return dt.strftime("%d%m%Y")
    if "ddmmyy" in n:
        return dt.strftime("%d%m%y")
    if "mmyy" in n:
        return dt.strftime("%m/%y")

    if default_format == "YYYY-MM-DD":
        return dt.strftime("%Y-%m-%d")
    if default_format == "MM/YYYY":
        return dt.strftime("%m/%Y")
    return dt.strftime("%d/%m/%Y")


def field_name_from_annot(annot_obj) -> str | None:
    parts: list[str] = []
    current = annot_obj
    while current:
        t = current.get("/T")
        if t:
            parts.append(str(t))
        parent = current.get("/Parent")
        current = parent.get_object() if parent else None
    if not parts:
        return None
    return ".".join(reversed(parts))


def inherited_value(annot_obj, key: str) -> Any:
    current = annot_obj
    while current:
        value = current.get(key)
        if value is not None:
            return value
        parent = current.get("/Parent")
        current = parent.get_object() if parent else None
    return None


def pdf_value_to_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(pdf_value_to_string(v) for v in value)
    return str(value)


def pdf_options_to_strings(value: Any) -> list[str]:
    if not value:
        return []
    out: list[str] = []
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, (list, tuple)) and item:
            out.append(pdf_value_to_string(item[0]))
        else:
            out.append(pdf_value_to_string(item))
    return [x for x in out if x]


def extract_widgets(pdf_path: str) -> list[Widget]:
    if fitz is not None:
        try:
            return extract_widgets_with_pymupdf(pdf_path)
        except Exception:
            pass

    reader = PdfReader(pdf_path)
    widgets: list[Widget] = []

    for page_index, page in enumerate(reader.pages, start=1):
        annots = page.get("/Annots") or []
        for a in annots:
            obj = a.get_object()
            rect = obj.get("/Rect")
            if not rect or len(rect) != 4:
                continue

            name = field_name_from_annot(obj)
            if not name:
                continue

            x0, y0, x1, y1 = [float(v) for v in rect]
            rect_values = [x0, y0, x1, y1]
            widgets.append(
                Widget(
                    uid=widget_uid(page_index, name, rect_values),
                    name=name,
                    page=page_index,
                    x=(x0 + x1) / 2,
                    y=(y0 + y1) / 2,
                    rect=rect_values,
                    field_type=pdf_value_to_string(inherited_value(obj, "/FT")),
                    tooltip=pdf_value_to_string(inherited_value(obj, "/TU")),
                    value=pdf_value_to_string(inherited_value(obj, "/V")),
                    options=pdf_options_to_strings(inherited_value(obj, "/Opt")),
                )
            )

    return widgets


def extract_widgets_with_pymupdf(pdf_path: str) -> list[Widget]:
    if fitz is None:
        return []

    doc = fitz.open(pdf_path)
    widgets: list[Widget] = []
    for page_index, page in enumerate(doc, start=1):
        page_height = float(page.rect.height)
        for widget in page.widgets() or []:
            name = str(widget.field_name or "").strip()
            if not name:
                continue

            rect = [
                float(widget.rect.x0),
                float(widget.rect.y0),
                float(widget.rect.x1),
                float(widget.rect.y1),
            ]
            label = str(getattr(widget, "field_label", "") or "").strip()
            if label.lower() == "undefined":
                label = ""
            options = getattr(widget, "choice_values", None) or []
            widgets.append(
                Widget(
                    uid=widget_uid(page_index, name, rect),
                    name=name,
                    page=page_index,
                    x=(rect[0] + rect[2]) / 2,
                    # Store y in PDF-style bottom-up coordinates so existing row
                    # ordering stays stable, while rect remains PyMuPDF top-down.
                    y=page_height - ((rect[1] + rect[3]) / 2),
                    rect=rect,
                    field_type=str(getattr(widget, "field_type_string", "") or ""),
                    tooltip=label,
                    value=pdf_value_to_string(getattr(widget, "field_value", "")),
                    options=[str(x) for x in options],
                )
            )

    doc.close()
    return widgets


def extract_text_spans(pdf_path: str) -> list[TextSpan]:
    reader = PdfReader(pdf_path)
    spans: list[TextSpan] = []

    for page_index, page in enumerate(reader.pages, start=1):
        def visitor_text(text, _cm, tm, _font_dict, _font_size):
            clean = re.sub(r"\s+", " ", text or "").strip()
            if not clean:
                return
            try:
                x = float(tm[4])
                y = float(tm[5])
            except Exception:
                return
            spans.append(TextSpan(page=page_index, x=x, y=y, text=clean))

        try:
            page.extract_text(visitor_text=visitor_text)
        except Exception:
            continue

    return spans


def _nearby_text_for_widget(widget: Widget, spans: list[TextSpan]) -> str:
    same_page = [s for s in spans if s.page == widget.page]
    scored: list[tuple[float, TextSpan]] = []

    for span in same_page:
        dx = abs(span.x - widget.x)
        dy = abs(span.y - widget.y)
        same_row_left = dy <= 18 and span.x <= widget.x + 12 and dx <= 360
        above_or_below = dx <= 260 and dy <= 46
        if not same_row_left and not above_or_below:
            continue
        # Prefer labels on the same row and directly above the field.
        penalty = dy * 2 + max(0, span.x - widget.x) * 1.5 + max(0, widget.x - span.x) * 0.25
        scored.append((penalty, span))

    chosen = [s for _, s in sorted(scored, key=lambda item: item[0])[:8]]
    chosen.sort(key=lambda s: (-s.y, s.x))
    text = " ".join(s.text for s in chosen)
    return re.sub(r"\s+", " ", text).strip()[:500]


def build_field_contexts(pdf_path: str, widgets: list[Widget]) -> dict[str, dict[str, Any]]:
    if fitz is not None:
        try:
            return build_field_contexts_with_pymupdf(pdf_path, widgets)
        except Exception:
            pass

    spans = extract_text_spans(pdf_path)
    contexts: dict[str, dict[str, Any]] = {}

    for widget in widgets:
        contexts[widget.name] = {
            "name": widget.name,
            "page": widget.page,
            "rect": widget.rect,
            "field_type": widget.field_type,
            "tooltip": widget.tooltip,
            "value": widget.value,
            "options": widget.options or [],
            "nearby_text": _nearby_text_for_widget(widget, spans),
        }

    return contexts


def build_field_contexts_with_pymupdf(pdf_path: str, widgets: list[Widget]) -> dict[str, dict[str, Any]]:
    if fitz is None:
        return {}

    by_uid = {w.uid: w for w in widgets}
    contexts: dict[str, dict[str, Any]] = {}
    doc = fitz.open(pdf_path)

    for page_index, page in enumerate(doc, start=1):
        words = page.get_text("words")
        page_widgets = [w for w in by_uid.values() if w.page == page_index]
        for widget in page_widgets:
            x0, y0, x1, y1 = widget.rect
            scored: list[tuple[float, str]] = []
            for word in words:
                wx0, wy0, wx1, wy1, text = word[:5]
                clean = re.sub(r"\s+", " ", str(text or "")).strip()
                if not clean:
                    continue

                wcy = (float(wy0) + float(wy1)) / 2
                wcx = (float(wx0) + float(wx1)) / 2
                same_row_left = abs(wcy - ((y0 + y1) / 2)) <= 18 and wcx <= x1 and abs(wcx - x0) <= 360
                above = y0 - 70 <= wcy <= y0 + 8 and abs(wcx - ((x0 + x1) / 2)) <= 280
                if not same_row_left and not above:
                    continue

                penalty = abs(wcy - ((y0 + y1) / 2)) * 2 + max(0, wcx - x0) * 0.8 + max(0, x0 - wcx) * 0.2
                scored.append((penalty, clean))

            nearby = " ".join(text for _, text in sorted(scored, key=lambda item: item[0])[:20])
            contexts[widget.uid] = {
                "uid": widget.uid,
                "name": widget.name,
                "page": widget.page,
                "rect": widget.rect,
                "field_type": widget.field_type,
                "tooltip": widget.tooltip,
                "value": widget.value,
                "options": widget.options or [],
                "nearby_text": re.sub(r"\s+", " ", nearby).strip()[:700],
            }
            contexts.setdefault(widget.name, contexts[widget.uid])

    doc.close()
    return contexts


def extract_field_names(pdf_path: str) -> list[str]:
    reader = PdfReader(pdf_path)
    fields = reader.get_fields() or {}
    return sorted(fields.keys())


def fingerprint_fields(field_names: list[str]) -> str:
    joined = "\n".join(sorted(field_names))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def _cluster_rows(widgets: list[Widget], y_threshold: float = 14.0) -> list[list[Widget]]:
    rows: list[list[Widget]] = []
    widgets_sorted = sorted(widgets, key=lambda w: (w.page, -w.y, w.x))

    for w in widgets_sorted:
        if not rows:
            rows.append([w])
            continue

        last_row = rows[-1]
        pivot = last_row[0]
        same_page = pivot.page == w.page
        near_y = abs(pivot.y - w.y) <= y_threshold

        if same_page and near_y:
            last_row.append(w)
        else:
            rows.append([w])

    return rows


def discover_employment_rows(widgets: list[Widget]) -> list[dict[str, Any]]:
    def is_candidate(name: str) -> bool:
        n = normalize(name)
        return (
            n.startswith("employer")
            or n.startswith("jobt")
            or n.startswith("salary")
            or n.startswith("leave")
            or n.startswith("empdate")
            or n.startswith("empdater")
            or n.startswith("datefrom")
            or n.startswith("dateto")
        )

    cands = [w for w in widgets if is_candidate(w.name)]
    rows = _cluster_rows(cands)

    discovered: list[dict[str, Any]] = []
    for row in rows:
        employer_fields: list[Widget] = []
        job_fields: list[Widget] = []
        salary_fields: list[Widget] = []
        leave_fields: list[Widget] = []
        date_fields: list[Widget] = []

        for w in sorted(row, key=lambda x: x.x):
            n = normalize(w.name)
            if n.startswith("employer"):
                employer_fields.append(w)
            elif n.startswith("job") or "jobtitle" in n or "position" in n:
                job_fields.append(w)
            elif n.startswith("salary") or "pay" in n:
                salary_fields.append(w)
            elif n.startswith("leave") or "reasonforleaving" in n:
                leave_fields.append(w)
            elif (
                n.startswith("empdate")
                or n.startswith("empdater")
                or n.startswith("datefrom")
                or n.startswith("dateto")
            ):
                date_fields.append(w)

        # Common broken-form case: a start-date widget mislabeled as EmployerN
        if len(employer_fields) > 1:
            employer_fields = sorted(employer_fields, key=lambda x: x.x)
            true_employer = employer_fields[0]
            maybe_dates = [e for e in employer_fields[1:] if e.x - true_employer.x > 60]
            date_fields.extend(maybe_dates)
            employer_fields = [true_employer]

        employer_widget = sorted(employer_fields, key=lambda x: x.x)[0] if employer_fields else None
        job_widget = sorted(job_fields, key=lambda x: x.x)[0] if job_fields else None
        salary_widget = sorted(salary_fields, key=lambda x: x.x)[0] if salary_fields else None
        leave_widget = sorted(leave_fields, key=lambda x: x.x)[0] if leave_fields else None

        employer = employer_widget.name if employer_widget else None
        job = job_widget.name if job_widget else None
        salary = salary_widget.name if salary_widget else None
        leave = leave_widget.name if leave_widget else None

        start_date = None
        end_date = None
        start_date_widget = None
        end_date_widget = None
        if date_fields:
            d_sorted = sorted(date_fields, key=lambda x: x.x)
            if len(d_sorted) == 1:
                start_date_widget = d_sorted[0]
                start_date = start_date_widget.name
            else:
                from_named = [d for d in d_sorted if "from" in normalize(d.name) or "start" in normalize(d.name)]
                to_named = [d for d in d_sorted if "to" in normalize(d.name) or "end" in normalize(d.name)]
                if from_named and to_named:
                    start_date_widget = from_named[0]
                    end_date_widget = to_named[0]
                    start_date = start_date_widget.name
                    end_date = end_date_widget.name
                else:
                    start_date_widget = d_sorted[0]
                    end_date_widget = d_sorted[1]
                    start_date = start_date_widget.name
                    end_date = end_date_widget.name

        if not employer and not job and not salary and not leave:
            continue

        discovered.append(
            {
                "page": row[0].page,
                "y": row[0].y,
                "employer_field": employer,
                "employer_widget_id": employer_widget.uid if employer_widget else None,
                "start_date_field": start_date,
                "start_date_widget_id": start_date_widget.uid if start_date_widget else None,
                "end_date_field": end_date,
                "end_date_widget_id": end_date_widget.uid if end_date_widget else None,
                "job_title_field": job,
                "job_title_widget_id": job_widget.uid if job_widget else None,
                "salary_field": salary,
                "salary_widget_id": salary_widget.uid if salary_widget else None,
                "leave_reason_field": leave,
                "leave_reason_widget_id": leave_widget.uid if leave_widget else None,
            }
        )

    discovered.sort(key=lambda r: (r["page"], -r["y"]))

    # Resolve duplicate field-name conflicts across rows.
    role_priority = {
        "employer_field": 50,
        "job_title_field": 40,
        "salary_field": 35,
        "leave_reason_field": 30,
        "start_date_field": 10,
        "end_date_field": 10,
    }

    field_refs: dict[str, list[tuple[int, str, int]]] = {}
    for idx, row in enumerate(discovered):
        for role, priority in role_priority.items():
            fname = row.get(role)
            if fname:
                widget_key = row.get(role.replace("_field", "_widget_id"))
                key = str(widget_key or fname)
                field_refs.setdefault(key, []).append((idx, role, priority))

    for fname, refs in field_refs.items():
        if len(refs) <= 1:
            continue

        refs_sorted = sorted(refs, key=lambda x: (-x[2], x[0]))
        keep_idx, _, _ = refs_sorted[0]
        for idx, role, _ in refs_sorted[1:]:
            discovered[idx][role] = None
            discovered[idx][role.replace("_field", "_widget_id")] = None

        if (
            discovered[keep_idx].get("start_date_field")
            and discovered[keep_idx].get("end_date_field")
            and discovered[keep_idx]["start_date_field"] == discovered[keep_idx]["end_date_field"]
        ):
            discovered[keep_idx]["end_date_field"] = None

    return discovered


def discover_education_rows(widgets: list[Widget]) -> list[dict[str, Any]]:
    def is_candidate(name: str) -> bool:
        n = normalize(name)
        return (
            n.startswith("nameandaddress")
            or n.startswith("courseandtitle")
            or n.startswith("standard")
            or n.startswith("grade")
            or n.startswith("qualification")
        )

    cands = [w for w in widgets if is_candidate(w.name)]
    rows = _cluster_rows(cands)

    discovered: list[dict[str, Any]] = []
    for row in rows:
        row_sorted = sorted(row, key=lambda x: x.x)
        institution = None
        course = None
        standard = None
        grade = None

        for w in row_sorted:
            n = normalize(w.name)
            if n.startswith("nameandaddress"):
                if institution is None:
                    institution = w
            elif n.startswith("courseandtitle") or n.startswith("qualification"):
                if course is None:
                    course = w
            elif n.startswith("standard") or "alevel" in n:
                if standard is None:
                    standard = w
            elif n.startswith("grade"):
                if grade is None:
                    grade = w

        if not institution and not course and not standard and not grade:
            continue

        discovered.append(
            {
                "page": row[0].page,
                "y": row[0].y,
                "institution_field": institution.name if institution else None,
                "institution_widget_id": institution.uid if institution else None,
                "course_field": course.name if course else None,
                "course_widget_id": course.uid if course else None,
                "standard_field": standard.name if standard else None,
                "standard_widget_id": standard.uid if standard else None,
                "grade_field": grade.name if grade else None,
                "grade_widget_id": grade.uid if grade else None,
            }
        )

    discovered.sort(key=lambda r: (r["page"], -r["y"]))
    return discovered


def discover_reference_slots(field_names: list[str]) -> list[dict[str, Any]]:
    names = [f for f in field_names if re.match(r"(?i)^Name_\d+$", f)]
    names_sorted = sorted(names, key=lambda f: int(re.search(r"(\d+)$", f).group(1)))

    slots: list[dict[str, Any]] = []
    for idx, name_field in enumerate(names_sorted):
        slots.append(
            {
                "index": idx,
                "name_field": name_field,
                "title_field": "OccupationTitle" if idx == 0 else f"OccupationTitle_{idx+1}",
                "address_field": "Occupation Address" if idx == 0 else f"Occupation Address_{idx+1}",
                "phone_field": "Tel No" if idx == 0 else f"Tel No_{idx+1}",
                "email_field": "Email address" if idx == 0 else f"Email address_{idx+1}",
            }
        )

    return slots


def discover_statement_fields(field_names: list[str]) -> list[str]:
    out: list[str] = []
    for f in field_names:
        n = normalize(f)
        if (
            n == "1000words"
            or "personalstatement" in n
            or "supportingstatement" in n
            or "supportinginformation" in n
            or ("statement" in n)
        ):
            out.append(f)
    return out


def discover_scalar_map(field_names: list[str], reserved_fields: set[str]) -> dict[str, str]:
    scalar_map: dict[str, str] = {}

    def map_if(field: str, target: str):
        if field not in reserved_fields and field not in scalar_map:
            scalar_map[field] = target

    for field in field_names:
        n = normalize(field)

        if n in {"prefixmrmrsmissmsetc", "prefix", "title"}:
            map_if(field, "profile.title")
        elif any(k in n for k in ["forename", "firstname", "givenname"]):
            map_if(field, "profile.first_name")
        elif any(k in n for k in ["surname", "lastname", "familyname"]):
            map_if(field, "profile.last_name")
        elif "preferredname" in n:
            map_if(field, "profile.preferred_name")
        elif "dateofbirth" in n or n == "dob" or "birthdate" in n:
            map_if(field, "profile.date_of_birth")
        elif "noticeperiod" in n or n == "notice":
            map_if(field, "profile.notice_period")
        elif "email" in n:
            map_if(field, "contact.email")
        elif any(k in n for k in ["phone", "mobile", "telno", "telephone", "contactnumber"]):
            map_if(field, "contact.phone")
        elif "postcode" in n:
            map_if(field, "address_current.postcode")
        elif "city" in n or "town" in n:
            map_if(field, "address_current.city")
        elif "country" in n:
            map_if(field, "address_current.country")
        elif "address" in n and "occupationaddress" not in n:
            map_if(field, "address_current.line1")
        elif "signature" in n:
            map_if(field, "profile.full_name")
        elif "signdate" in n or ("sign" in n and "date" in n):
            map_if(field, "$today")
        elif "membership" in n and "professional" in n:
            map_if(field, "$membership_summary")
        elif "criminal" in n or "unspentconvictions" in n or n == "yesno":
            map_if(field, "declarations.criminal_convictions")

    return scalar_map


def _iter_candidate_values(obj: Any, prefix: str = "", max_list_items: int = 3) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in {"aliases", "format_rules", "statement_selection_rules"}:
                continue
            p = f"{prefix}.{k}" if prefix else k
            out.extend(_iter_candidate_values(v, p, max_list_items=max_list_items))
        return out

    if isinstance(obj, list):
        for i, v in enumerate(obj[:max_list_items]):
            p = f"{prefix}[{i}]"
            out.extend(_iter_candidate_values(v, p, max_list_items=max_list_items))
        return out

    if obj is None:
        return out

    if isinstance(obj, (str, int, float, bool)):
        value = str(obj)
        if len(value) > 120:
            value = value[:117] + "..."
        out.append({"path": prefix, "sample": value})

    return out


def _extract_json_from_text(text: str) -> Any:
    ansi_re = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
    raw = ansi_re.sub("", text or "").strip()
    if not raw:
        raise ValueError("Empty model output")

    # Direct parse first.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Try extracting first JSON object/array from mixed output.
    for opener, closer in [("{", "}"), ("[", "]")]:
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start >= 0 and end > start:
            snippet = raw[start : end + 1]
            try:
                return json.loads(snippet)
            except json.JSONDecodeError:
                continue

    raise ValueError("Could not parse JSON from model output")


def reserved_fields_from_layout(layout: dict[str, Any]) -> set[str]:
    reserved: set[str] = set(layout.get("statement_fields", []))

    for row in layout.get("employment_rows", []):
        for k in [
            "employer_field",
            "start_date_field",
            "end_date_field",
            "job_title_field",
            "salary_field",
            "leave_reason_field",
        ]:
            v = row.get(k)
            if v:
                reserved.add(v)

    for row in layout.get("education_rows", []):
        for k in ["institution_field", "course_field", "standard_field", "grade_field"]:
            v = row.get(k)
            if v:
                reserved.add(v)

    for slot in layout.get("reference_slots", []):
        for k in ["name_field", "title_field", "address_field", "phone_field", "email_field"]:
            v = slot.get(k)
            if v:
                reserved.add(v)

    return reserved


def suggest_scalar_map_with_ollama(
    unmapped_fields: list[str],
    data: dict[str, Any],
    field_contexts: dict[str, dict[str, Any]] | None = None,
    model: str = "gemma4:e4b-it-q8_0",
    min_confidence: float = 0.86,
    timeout_sec: int = 120,
) -> dict[str, Any]:
    if not unmapped_fields:
        return {
            "enabled": True,
            "model": model,
            "status": "skipped",
            "reason": "no_unmapped_fields",
            "applied": {},
            "candidate_count": 0,
            "review": [],
        }

    candidates = _iter_candidate_values(data)
    candidate_paths = sorted({c["path"] for c in candidates if c.get("path")})
    candidate_paths.extend(["$today", "$membership_summary"])
    candidate_paths = sorted(set(candidate_paths))
    candidate_samples = {
        c["path"]: c.get("sample", "")
        for c in candidates
        if c.get("path")
    }
    candidate_samples["$today"] = datetime.now().strftime("%d/%m/%Y")
    candidate_samples["$membership_summary"] = membership_summary(data)

    field_contexts = field_contexts or {}
    fields_to_map = []
    for field in unmapped_fields:
        ctx = field_contexts.get(field, {})
        fields_to_map.append(
            {
                "field": field,
                "type": ctx.get("field_type", ""),
                "tooltip": ctx.get("tooltip", ""),
                "nearby_text": ctx.get("nearby_text", ""),
                "options": ctx.get("options", []),
                "page": ctx.get("page"),
            }
        )

    prompt = {
        "task": "Map PDF form fields to profile data paths for an autofill tool.",
        "rules": [
            "Use ONLY provided candidate paths.",
            "Use nearby_text, tooltip, options, and field name together; nearby_text is often more reliable than the raw PDF field name.",
            "For yes/no fields, choose a declaration or profile path only when the meaning is clear.",
            "If uncertain, set source to null and confidence to 0.",
            "Do not invent fields or paths.",
            "Return JSON only.",
        ],
        "fields_to_map": fields_to_map,
        "candidate_paths": candidate_paths,
        "candidate_samples": candidate_samples,
        "output_schema": {
            "mappings": [
                {
                    "field": "<one field label>",
                    "source": "<candidate path or null>",
                    "confidence": "<0.0-1.0>",
                    "reason": "<short reason>",
                }
            ]
        },
    }

    body = {
        "model": model,
        "prompt": json.dumps(prompt, ensure_ascii=False),
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0,
        },
    }

    req = urllib.request.Request(
        url="http://127.0.0.1:11434/api/generate",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return {
            "enabled": True,
            "model": model,
            "status": "error",
            "error": f"ollama_api_error: {exc}",
            "applied": {},
            "candidate_count": len(candidate_paths),
            "review": [],
        }

    try:
        api_payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "enabled": True,
            "model": model,
            "status": "error",
            "error": f"ollama_api_json_error: {exc}",
            "applied": {},
            "candidate_count": len(candidate_paths),
            "review": [],
            "raw_output": raw[:2000],
        }

    model_text = str(api_payload.get("response", "") or "")
    if not model_text.strip():
        return {
            "enabled": True,
            "model": model,
            "status": "error",
            "error": "empty_model_response",
            "applied": {},
            "candidate_count": len(candidate_paths),
            "review": [],
            "raw_output": raw[:2000],
        }

    try:
        parsed = _extract_json_from_text(model_text)
    except Exception as exc:
        return {
            "enabled": True,
            "model": model,
            "status": "error",
            "error": f"parse_error: {exc}",
            "applied": {},
            "candidate_count": len(candidate_paths),
            "review": [],
            "raw_output": model_text[:2000],
        }

    mappings = parsed.get("mappings", []) if isinstance(parsed, dict) else []
    allowed_sources = set(candidate_paths)
    wanted_fields = set(unmapped_fields)

    applied: dict[str, str] = {}
    review: list[dict[str, Any]] = []

    for item in mappings:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field", "")).strip()
        source_raw = item.get("source", None)
        source = str(source_raw).strip() if source_raw is not None else ""
        try:
            confidence = float(item.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(item.get("reason", "")).strip()

        if field not in wanted_fields:
            continue

        approved = bool(source and source in allowed_sources and confidence >= min_confidence)
        review.append(
            {
                "field": field,
                "source": source if source else None,
                "confidence": confidence,
                "approved": approved,
                "reason": reason,
            }
        )

        if approved:
            applied[field] = source

    return {
        "enabled": True,
        "model": model,
        "status": "ok",
        "applied": applied,
        "candidate_count": len(candidate_paths),
        "review": review,
    }


def apply_ollama_scalar_fallback(
    layout: dict[str, Any],
    data: dict[str, Any],
    model: str = "gemma4:e4b-it-q8_0",
    min_confidence: float = 0.86,
    timeout_sec: int = 120,
) -> dict[str, Any]:
    field_names = layout.get("field_names", [])
    scalar_map = dict(layout.get("scalar_map", {}))

    reserved = reserved_fields_from_layout(layout)
    unmapped = [f for f in field_names if f not in reserved and f not in scalar_map]

    batch_size = 12
    batch_results: list[dict[str, Any]] = []
    applied: dict[str, str] = {}
    review: list[dict[str, Any]] = []
    errors: list[str] = []

    for start in range(0, len(unmapped), batch_size):
        batch = unmapped[start : start + batch_size]
        result = suggest_scalar_map_with_ollama(
            unmapped_fields=batch,
            data=data,
            field_contexts=layout.get("field_contexts", {}),
            model=model,
            min_confidence=min_confidence,
            timeout_sec=timeout_sec,
        )
        batch_results.append(result)
        if result.get("status") == "error":
            errors.append(str(result.get("error", "unknown_error")))
            continue
        applied.update(result.get("applied", {}) if isinstance(result, dict) else {})
        review.extend(result.get("review", []) if isinstance(result, dict) else [])

    for field, source in applied.items():
        scalar_map[field] = source

    layout["scalar_map"] = scalar_map
    layout["reserved_fields"] = sorted(reserved)
    layout["unmapped_scalar_fields"] = [f for f in unmapped if f not in applied]

    return {
        "enabled": True,
        "model": model,
        "status": "partial_error" if errors and applied else ("error" if errors else "ok"),
        "errors": errors,
        "applied": applied,
        "applied_count": len(applied),
        "unmapped_before": len(unmapped),
        "unmapped_after": len(layout["unmapped_scalar_fields"]),
        "candidate_count": max([int(r.get("candidate_count", 0) or 0) for r in batch_results] or [0]),
        "review": review,
        "batches": batch_results,
    }


def _get_path(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for token in re.finditer(r"([^.\[\]]+)|\[(\d+)\]", path):
        key = token.group(1)
        idx = token.group(2)

        if key is not None:
            if isinstance(cur, dict):
                cur = cur.get(key)
            else:
                return None
        elif idx is not None:
            if isinstance(cur, list):
                i = int(idx)
                if i < 0 or i >= len(cur):
                    return None
                cur = cur[i]
            else:
                return None

    return cur


def choose_statement(data: dict[str, Any], form_context: str, sector_hint: str | None) -> str:
    statements = data.get("personal_statements", {})
    rules = data.get("statement_selection_rules", {})
    default_key = rules.get("default", "general")

    if sector_hint in {"public", "public_sector"}:
        key = "public_sector"
    elif sector_hint in {"private", "private_sector"}:
        key = "private_sector"
    else:
        context = (form_context or "").lower()
        pub_terms = [t.lower() for t in rules.get("use_public_sector_when", [])]
        pri_terms = [t.lower() for t in rules.get("use_private_sector_when", [])]

        if any(t in context for t in pub_terms):
            key = "public_sector"
        elif any(t in context for t in pri_terms):
            key = "private_sector"
        else:
            key = default_key

    return (
        statements.get(key)
        or statements.get("general")
        or ""
    )


def membership_summary(data: dict[str, Any], default_date_format: str = "DD/MM/YYYY") -> str:
    memberships = data.get("memberships", [])
    if not memberships:
        return ""

    items: list[str] = []
    for m in memberships:
        body = str(m.get("body", "")).strip()
        since = format_date_for_field(m.get("member_since", ""), "member_since", default_date_format)
        if body and since:
            items.append(f"{body} - Member since {since}")
        elif body:
            items.append(body)

    return "; ".join(items)


def build_layout(pdf_path: str) -> dict[str, Any]:
    field_names = extract_field_names(pdf_path)
    widgets = extract_widgets(pdf_path)
    field_contexts = build_field_contexts(pdf_path, widgets)
    field_instances = [
        {
            "uid": w.uid,
            "name": w.name,
            "page": w.page,
            "rect": w.rect,
            "field_type": w.field_type,
            "tooltip": w.tooltip,
            "value": w.value,
            "options": w.options or [],
        }
        for w in widgets
    ]

    employment_rows = discover_employment_rows(widgets)
    education_rows = discover_education_rows(widgets)
    reference_slots = discover_reference_slots(field_names)
    statement_fields = discover_statement_fields(field_names)

    reserved_fields: set[str] = set(statement_fields)

    for row in employment_rows:
        reserved_fields.update(
            [
                f
                for f in [
                    row.get("employer_field"),
                    row.get("start_date_field"),
                    row.get("end_date_field"),
                    row.get("job_title_field"),
                    row.get("salary_field"),
                    row.get("leave_reason_field"),
                ]
                if f
            ]
        )

    for row in education_rows:
        reserved_fields.update(
            [
                f
                for f in [
                    row.get("institution_field"),
                    row.get("course_field"),
                    row.get("standard_field"),
                    row.get("grade_field"),
                ]
                if f
            ]
        )

    for slot in reference_slots:
        reserved_fields.update(
            [
                f
                for f in [
                    slot.get("name_field"),
                    slot.get("title_field"),
                    slot.get("address_field"),
                    slot.get("phone_field"),
                    slot.get("email_field"),
                ]
                if f
            ]
        )

    scalar_map = discover_scalar_map(field_names, reserved_fields)
    unmapped_scalar_fields = [f for f in field_names if f not in reserved_fields and f not in scalar_map]

    return {
        "field_names": field_names,
        "field_instances": field_instances,
        "fingerprint": fingerprint_fields(field_names + [w.uid for w in widgets]),
        "employment_rows": employment_rows,
        "education_rows": education_rows,
        "reference_slots": reference_slots,
        "statement_fields": statement_fields,
        "field_contexts": field_contexts,
        "scalar_map": scalar_map,
        "reserved_fields": sorted(reserved_fields),
        "unmapped_scalar_fields": unmapped_scalar_fields,
    }


def build_fill_payload(layout: dict[str, Any], data: dict[str, Any], sector_hint: str | None = None) -> tuple[dict[str, str], dict[str, Any]]:
    field_names: list[str] = layout["field_names"]
    fill: dict[str, str] = {f: "" for f in field_names}
    widget_values: dict[str, str] = {}

    name_to_uids: dict[str, list[str]] = {}
    for item in layout.get("field_instances", []):
        name = str(item.get("name", "") or "")
        uid = str(item.get("uid", "") or "")
        if name and uid:
            name_to_uids.setdefault(name, []).append(uid)

    def set_field(field: str | None, value: Any, widget_id: str | None = None) -> None:
        if not field:
            return
        text = "" if value is None else str(value)
        fill[field] = text
        if widget_id:
            widget_values[widget_id] = text
        elif len(name_to_uids.get(field, [])) == 1:
            widget_values[name_to_uids[field][0]] = text

    default_date_format = data.get("format_rules", {}).get("default_date_format", "DD/MM/YYYY")
    current_text = data.get("format_rules", {}).get("current_job_end_date_text", "Present")

    report = {
        "filled_fields": [],
        "left_blank_fields": [],
    }

    # Scalars
    for field, source in layout.get("scalar_map", {}).items():
        if source == "$today":
            value = datetime.now().strftime("%d/%m/%Y")
        elif source == "$membership_summary":
            value = membership_summary(data, default_date_format)
        else:
            value = _get_path(data, source)
            if "date" in normalize(field):
                value = format_date_for_field(value, field, default_date_format)

        value = "" if value is None else str(value)
        set_field(field, value)

    # Statement
    context = " ".join(field_names)
    statement_text = choose_statement(data, context, sector_hint)
    for field in layout.get("statement_fields", []):
        set_field(field, statement_text)

    # Employment
    jobs = data.get("employment_history", [])
    for i, row in enumerate(layout.get("employment_rows", [])):
        job = jobs[i] if i < len(jobs) else {}

        employer = str(job.get("employer", "") or "")
        title = str(job.get("job_title", "") or "")
        salary = str(job.get("salary", "") or "")
        leave = str(job.get("reason_for_leaving", "") or "")
        start = format_date_for_field(job.get("start_date", ""), row.get("start_date_field", ""), default_date_format)

        end_raw = job.get("end_date", None)
        end = current_text if (end_raw is None and job) else format_date_for_field(end_raw, row.get("end_date_field", ""), default_date_format)

        set_field(row.get("employer_field"), employer, row.get("employer_widget_id"))
        set_field(row.get("job_title_field"), title, row.get("job_title_widget_id"))
        set_field(row.get("salary_field"), salary, row.get("salary_widget_id"))
        set_field(row.get("leave_reason_field"), leave, row.get("leave_reason_widget_id"))

        # If start and end point to same PDF field name, prefer start date.
        start_field = row.get("start_date_field")
        end_field = row.get("end_date_field")
        start_widget_id = row.get("start_date_widget_id")
        end_widget_id = row.get("end_date_widget_id")
        if start_field:
            set_field(start_field, start, start_widget_id)
        if end_field and (end_field != start_field or end_widget_id != start_widget_id):
            set_field(end_field, end, end_widget_id)

    # Education
    education = data.get("education_history", [])
    for i, row in enumerate(layout.get("education_rows", [])):
        edu = education[i] if i < len(education) else {}
        institution = str(edu.get("institution", "") or "")
        course = str(edu.get("course", "") or "")
        standard = str(edu.get("standard", "") or "")
        grade = str(edu.get("grade", "") or "")

        set_field(row.get("institution_field"), institution, row.get("institution_widget_id"))
        set_field(row.get("course_field"), course, row.get("course_widget_id"))
        set_field(row.get("standard_field"), standard, row.get("standard_widget_id"))
        set_field(row.get("grade_field"), grade, row.get("grade_widget_id"))

    # Form-specific rescue mapping for common legacy UK application layouts.
    # This keeps output stable when field naming is inconsistent.
    if education:
        e1 = education[0] if len(education) > 0 else {}
        e2 = education[1] if len(education) > 1 else {}
        e3 = education[2] if len(education) > 2 else {}

        overrides = {
            "Name and Address": str(e1.get("institution", "") or ""),
            "Name and Address_2": str(e2.get("institution", "") or ""),
            "Name and Address1": str(e3.get("institution", "") or ""),
            "Course and Title eg BA Hons English1": str(e1.get("course", "") or ""),
            "Course and Title eg BA Hons English2": str(e2.get("course", "") or ""),
            "Course and Title eg BA Hons English3": str(e3.get("course", "") or ""),
            "Standard24": str(e1.get("standard", "") or ""),
            "Standard25": str(e2.get("standard", "") or ""),
            "Standard23": str(e3.get("standard", "") or ""),
            "Grade1": str(e1.get("grade", "") or ""),
            "Grade2": str(e2.get("grade", "") or ""),
            "Grade3": str(e3.get("grade", "") or ""),
        }

        for field, value in overrides.items():
            if field in fill and value:
                set_field(field, value)

    # References
    refs = data.get("references", [])
    for slot in layout.get("reference_slots", []):
        idx = slot.get("index", 0)
        ref = refs[idx] if idx < len(refs) else {}
        if slot.get("name_field") in fill:
            set_field(slot["name_field"], ref.get("name", "") or "")
        if slot.get("title_field") in fill:
            set_field(slot["title_field"], ref.get("relationship_or_title", "") or "")
        if slot.get("address_field") in fill:
            set_field(slot["address_field"], ref.get("address", "") or "")
        if slot.get("phone_field") in fill:
            set_field(slot["phone_field"], ref.get("phone", "") or "")
        if slot.get("email_field") in fill:
            set_field(slot["email_field"], ref.get("email", "") or "")

    for k, v in fill.items():
        if str(v).strip():
            report["filled_fields"].append(k)
        else:
            report["left_blank_fields"].append(k)

    report["widget_values"] = widget_values

    return fill, report


def write_filled_pdf(
    pdf_path: str | Path,
    out_path: str | Path,
    fill_payload: dict[str, str],
    widget_payload: dict[str, str] | None = None,
) -> None:
    if fitz is not None:
        try:
            write_filled_pdf_with_pymupdf(pdf_path, out_path, fill_payload, widget_payload or {})
            return
        except Exception:
            pass

    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)

    acroform = reader.trailer.get("/Root", {}).get("/AcroForm")
    if acroform is not None:
        writer._root_object.update({NameObject("/AcroForm"): acroform})
        try:
            writer._root_object["/AcroForm"].update({NameObject("/NeedAppearances"): BooleanObject(True)})
        except Exception:
            pass

    if hasattr(writer, "set_need_appearances_writer"):
        try:
            writer.set_need_appearances_writer(True)
        except Exception:
            pass

    for page in writer.pages:
        annots = page.get("/Annots") or []
        for annot in annots:
            obj = annot.get_object()
            name = field_name_from_annot(obj)
            if not name or name not in fill_payload:
                continue
            value = "" if fill_payload[name] is None else str(fill_payload[name])
            pdf_value = TextStringObject(value)
            obj.update({NameObject("/V"): pdf_value})
            parent = obj.get("/Parent")
            if parent:
                try:
                    parent.get_object().update({NameObject("/V"): pdf_value})
                except Exception:
                    pass
            # Drop stale widget appearance streams; /NeedAppearances asks viewers to regenerate.
            if "/AP" in obj:
                try:
                    del obj["/AP"]
                except Exception:
                    pass

    with Path(out_path).open("wb") as f:
        writer.write(f)


def write_filled_pdf_with_pymupdf(
    pdf_path: str | Path,
    out_path: str | Path,
    fill_payload: dict[str, str],
    widget_payload: dict[str, str],
) -> None:
    doc = fitz.open(str(pdf_path))
    widget_items: list[dict[str, Any]] = []
    for page_index, page in enumerate(doc, start=1):
        for widget in page.widgets() or []:
            name = str(widget.field_name or "").strip()
            if not name:
                continue
            rect = [
                float(widget.rect.x0),
                float(widget.rect.y0),
                float(widget.rect.x1),
                float(widget.rect.y1),
            ]
            uid = widget_uid(page_index, name, rect)
            if uid in widget_payload:
                value = widget_payload[uid]
            elif name in fill_payload:
                value = fill_payload[name]
            else:
                continue
            if value is None or not str(value).strip():
                continue

            widget_items.append(
                {
                    "widget": widget,
                    "page": page,
                    "name": name,
                    "uid": uid,
                    "rect": rect,
                    "value": "" if value is None else str(value),
                }
            )

    values_by_name: dict[str, set[str]] = {}
    for item in widget_items:
        value = str(item["value"])
        if value.strip():
            values_by_name.setdefault(str(item["name"]), set()).add(value)

    conflicting_names = {name for name, values in values_by_name.items() if len(values) > 1}

    for item in widget_items:
        widget = item["widget"]
        name = str(item["name"])
        if name in conflicting_names:
            suffix = hashlib.sha1(str(item["uid"]).encode("utf-8")).hexdigest()[:10]
            widget.field_name = f"{name}__pi_{suffix}"
        widget.field_value = item["value"]
        widget.update()

    for item in widget_items:
        draw_visual_value(item["page"], item["rect"], item["value"])

    # Flatten the output. Several real-world forms reuse one AcroForm field name
    # for different visible cells; PDF field semantics cannot represent different
    # values for those widgets. A visual text layer is the reliable deliverable.
    for page in doc:
        for widget in list(page.widgets() or []):
            page.delete_widget(widget)

    doc.save(str(out_path), garbage=3, deflate=True)
    doc.close()


def draw_visual_value(page: Any, rect_values: list[float], value: str) -> None:
    text = str(value or "").strip()
    if not text:
        return

    rect = fitz.Rect(*rect_values)
    fill_rect = fitz.Rect(rect.x0 + 1, rect.y0 + 1, rect.x1 - 1, rect.y1 - 1)
    text_rect = fitz.Rect(rect.x0 + 5, rect.y0 + 4, rect.x1 - 4, rect.y1 - 3)
    if fill_rect.width <= 2 or fill_rect.height <= 2:
        return

    page.draw_rect(fill_rect, color=None, fill=(1, 1, 1), overlay=True)

    width = max(1.0, text_rect.width)
    height = max(1.0, text_rect.height)
    longest_word = max((len(part) for part in re.split(r"\s+", text) if part), default=len(text))
    compact_len = max(longest_word, min(len(text), 24))

    if "\n" not in text and height <= 30:
        font_size = min(11.5, max(8.5, width / max(1, len(text) * 0.55)))
        page.insert_text(
            fitz.Point(text_rect.x0, min(text_rect.y1 - 1, text_rect.y0 + (height * 0.74))),
            text[:120],
            fontsize=font_size,
            fontname="helv",
            color=(0, 0, 0),
            overlay=True,
        )
        return

    if height > 36 and len(text) > 28:
        font_size = min(10.5, max(6.0, width / max(1, compact_len * 0.52)))
    else:
        font_size = min(12.0, max(5.5, width / max(1, compact_len * 0.55)))

    for size in [font_size, 9.5, 8.5, 7.5, 6.5, 5.5]:
        rc = page.insert_textbox(
            text_rect,
            text,
            fontsize=min(size, font_size),
            fontname="helv",
            color=(0, 0, 0),
            align=0,
            overlay=True,
        )
        if rc >= 0:
            return

    page.insert_text(
        fitz.Point(text_rect.x0, min(text_rect.y1 - 1, text_rect.y0 + 6)),
        text[:80],
        fontsize=5.5,
        fontname="helv",
        color=(0, 0, 0),
        overlay=True,
    )


def verify_filled_pdf(
    out_path: str | Path,
    fill_payload: dict[str, str],
    widget_payload: dict[str, str] | None = None,
) -> dict[str, Any]:
    widget_payload = widget_payload or {}
    widget_payload_by_rect = {
        rect_uid: value
        for uid, value in widget_payload.items()
        for rect_uid in [rect_uid_from_widget_uid(uid)]
        if rect_uid
    }
    mismatches: list[dict[str, Any]] = []
    checked = 0
    matched = 0

    if fitz is not None:
        doc = fitz.open(str(out_path))
        had_widgets = False
        for page_index, page in enumerate(doc, start=1):
            page_widgets = list(page.widgets() or [])
            if page_widgets:
                had_widgets = True
            for widget in page_widgets:
                name = str(widget.field_name or "").strip()
                if not name:
                    continue
                rect = [
                    float(widget.rect.x0),
                    float(widget.rect.y0),
                    float(widget.rect.x1),
                    float(widget.rect.y1),
                ]
                uid = widget_uid(page_index, name, rect)
                rect_uid = widget_rect_uid(page_index, rect)
                expected = widget_payload.get(uid, widget_payload_by_rect.get(rect_uid, fill_payload.get(name)))
                if expected is None or not str(expected).strip():
                    continue

                actual = "" if widget.field_value is None else str(widget.field_value)
                checked += 1
                if actual == str(expected):
                    matched += 1
                else:
                    mismatches.append(
                        {
                            "uid": uid,
                            "field": name,
                            "page": page_index,
                            "expected": str(expected),
                            "actual": actual,
                        }
                    )
        if checked == 0 and widget_payload:
            expected_by_page: dict[int, set[str]] = {}
            for uid, expected in widget_payload.items():
                if expected is None or not str(expected).strip():
                    continue
                match = re.match(r"^p(\d+):", uid)
                if not match:
                    continue
                expected_by_page.setdefault(int(match.group(1)), set()).add(str(expected).strip())

            for page_index, page in enumerate(doc, start=1):
                page_text = re.sub(r"\s+", " ", page.get_text("text") or "")
                for expected in expected_by_page.get(page_index, set()):
                    checked += 1
                    expected_text = re.sub(r"\s+", " ", expected)
                    if expected_text in page_text:
                        matched += 1
                    else:
                        mismatches.append(
                            {
                                "field": None,
                                "page": page_index,
                                "expected": expected,
                                "actual": "not found in flattened text layer",
                            }
                        )
        engine = "pymupdf-widget" if had_widgets else "pymupdf-flattened-text"
        doc.close()
        return {
            "engine": engine,
            "checked": checked,
            "matched": matched,
            "mismatched": len(mismatches),
            "ok": len(mismatches) == 0,
            "mismatches": mismatches[:50],
        }

    return {
        "engine": "none",
        "checked": 0,
        "matched": 0,
        "mismatched": 0,
        "ok": False,
        "error": "PyMuPDF is not available for verification",
        "mismatches": [],
    }


def save_json(path: str | Path, payload: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
