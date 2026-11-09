"""중고차 기록부 데모 서버 (로컬 전용). API 키는 서버에서만 사용한다."""
import io
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from openai import (APIConnectionError, APIStatusError, AuthenticationError,
                    OpenAI, RateLimitError)

BASE_DIR = Path(__file__).resolve().parent
SAMPLES_DIR = BASE_DIR / "samples"
RESULTS_DIR = BASE_DIR / "results"
STATIC_DIR = BASE_DIR / "static"
MAX_BYTES = 24 * 1024 * 1024

load_dotenv(BASE_DIR / ".env")

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25MB
RESULTS_DIR.mkdir(exist_ok=True)

CITE_RE = re.compile(r"【†\d+】")
SUMMARY_KEYS = ["vehicle_no", "vin", "decision", "reason_codes", "reason"]
INCLUDE = ["all"]

JOBS = {}
JOBS_LOCK = threading.Lock()


class UserError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


@app.errorhandler(UserError)
def _user_error(e):
    return jsonify({"ok": False, "error": e.message}), e.status


@app.errorhandler(413)
def _too_big(e):
    return jsonify({"ok": False, "error": "파일이 너무 큼. 24MB 이하 PDF만 가능함."}), 413


def get_client():
    key = os.getenv("UPSTAGE_API_KEY", "").strip()
    if not key:
        raise UserError("UPSTAGE_API_KEY가 설정되지 않음. .env 파일에 키를 넣고 서버를 다시 시작할 것.", 500)
    return OpenAI(api_key=key, base_url="https://api.upstage.ai/v2")


def get_ids():
    agent_id = os.getenv("AGENT_ID", "").strip()
    config_id = os.getenv("CONFIG_ID", "2").strip() or "2"
    if not agent_id:
        raise UserError("AGENT_ID가 설정되지 않음. .env 파일을 확인할 것.", 500)
    return agent_id, config_id


def api_error_message(e):
    if isinstance(e, AuthenticationError):
        return "API 키가 올바르지 않거나 권한이 없음. .env의 UPSTAGE_API_KEY를 확인할 것."
    if isinstance(e, RateLimitError):
        return "요청 한도를 넘음. 잠시 후 다시 시도할 것."
    if isinstance(e, APIConnectionError):
        return "Upstage API에 연결하지 못함. 네트워크를 확인할 것."
    if isinstance(e, APIStatusError):
        return f"Upstage API 오류(상태 코드 {e.status_code}). 잠시 후 다시 시도할 것."
    return f"처리 중 오류가 발생함: {type(e).__name__}"


# ---------- parsing ----------

def clean(text):
    return CITE_RE.sub("", text or "")


def try_json(text):
    t = (text or "").strip()
    if not t:
        return None
    if t[0] not in "{[":
        m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
        if not m:
            return None
        t = m.group(1)
    try:
        return json.loads(t)
    except (ValueError, TypeError):
        return None


def to_dict(obj):
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return dict(getattr(obj, "__dict__", {}))


def item_text(item):
    parts = []
    for c in item.get("content") or []:
        c = to_dict(c)
        if c.get("text"):
            parts.append(c["text"])
    return unwrap("\n".join(parts))


def unwrap(t):
    """instruct 출력은 JSON 문자열(따옴표로 감싸고 \\n 이스케이프)로 옴. 풀어서 돌려줌."""
    s = (t or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            v = json.loads(s)
            if isinstance(v, str):
                return v
        except ValueError:
            pass
    return t


def decode_extra(item):
    av = item.get("additional_values")
    if av is None:
        return None
    if isinstance(av, str):
        try:
            return json.loads(av)
        except ValueError:
            return av
    return av


def walk(obj):
    """dict 안의 모든 (key, value)를 재귀로 돌려줌."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


KV_RE = re.compile(r"^\s*(?:[-*]\s*)?\**\s*([A-Za-z_][A-Za-z0-9_]*)\s*\**\s*[:：]\s*(.*?)\s*$")


def kv_lines(text):
    out = {}
    for line in (text or "").splitlines():
        m = KV_RE.match(line)
        if m and m.group(2) != "":
            out.setdefault(m.group(1), m.group(2).strip().strip("*").strip())
    return out


def norm_codes(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        s = v.strip().strip("[]")
        parts = [p.strip().strip("'\"") for p in re.split(r"[,\s]+", s)]
        return [p for p in parts if p]
    return []


def collect_pairs(step):
    """한 단계에서 key-value 쌍을 모음 (JSON과 'key: value' 줄 모두)."""
    pairs = {}
    pj = step.get("parsed_json")
    if pj is not None:
        for k, v in walk(pj):
            if not isinstance(v, (dict, list)) or k == "reason_codes":
                pairs.setdefault(k, v)
    for k, v in kv_lines(step["text"]).items():
        pairs.setdefault(k, v)
    return pairs


def build_summary(steps):
    summary = {}
    for st in steps:
        pairs = collect_pairs(st)
        for k in SUMMARY_KEYS:
            v = pairs.get(k)
            if v in (None, "", []):
                continue
            if k == "reason_codes":
                v = norm_codes(v)
            if k in ("vehicle_no", "vin"):
                summary.setdefault(k, v)
            else:
                summary[k] = v
    return summary


def stage_of(step_type, name):
    st = (step_type or "").lower()
    n = (name or "").lower()
    if "parse" in st:
        return "parse"
    if "classify" in st:
        return "classify"
    if "extract" in st:
        return "extract"
    if "merge" in st:
        return "merge"
    if "instruct" in st or "instruct" in n:
        return "judge" if "판정" in n else "doc"
    if "decision" in st or "choice" in st or "decision" in n or "choice" in n:
        return "decision"
    return ""


def find_decision_choice(steps, summary):
    """실제 출력에는 Decision 노드 항목이 없음. additional_values -> 판정 Instruct 마지막 줄 -> 'decision:' 줄 순."""
    for st in steps:
        for src in (st.get("additional_values"), st.get("parsed_json")):
            if src is not None:
                for k, v in walk(src):
                    if k in ("choice", "decision_choice") and isinstance(v, str) and v.strip():
                        return v.strip()
    for st in steps:
        if st["stage"] in ("judge", "decision"):
            lines = [l.strip().strip("\"'*") for l in st["text"].splitlines() if l.strip()]
            if lines and len(lines[-1]) <= 12 and ":" not in lines[-1] and "：" not in lines[-1]:
                return lines[-1]
    d = summary.get("decision")
    return d.strip() if isinstance(d, str) and d.strip() else None


def parse_response(resp):
    d = resp.model_dump() if hasattr(resp, "model_dump") else to_dict(resp)
    steps = []
    for it in (to_dict(i) for i in (d.get("output") or [])):
        text = clean(item_text(it))
        av = decode_extra(it)
        if isinstance(av, str):
            av = clean(av)
        name = it.get("model") or it.get("type") or ""
        pj = try_json(text)
        parse_pages = None
        if "parse" in (it.get("step_type") or "").lower() and isinstance(pj, dict):
            pages = (pj.get("usage") or {}).get("pages")
            parse_pages = pages
            display = (f"{pages}쪽 · " if pages else "") + str(pj.get("model") or "document-parse")
            pj = None
        elif pj is not None:
            display = json.dumps(pj, ensure_ascii=False, indent=2)
        else:
            display = text
        classes = []   # 분류 노드: 콘텐츠 항목마다 (카테고리, 쪽 범위)
        if "classify" in (it.get("step_type") or "").lower():
            for c in (to_dict(c) for c in it.get("content") or []):
                cav = decode_extra(c)
                if c.get("text") and isinstance(cav, dict):
                    classes.append((c["text"].strip(), cav.get("page_ranges") or []))
        steps.append({
            "classes": classes,
            "display": display,
            "step": name,
            "step_type": it.get("step_type") or "",
            "stage": stage_of(it.get("step_type"), name),
            "text": "" if "parse" in (it.get("step_type") or "").lower() else text,
            "parsed_json": pj,
            "additional_values": av,
            "pages": parse_pages,
        })
    doc = None
    for st in steps:
        if st["stage"] == "doc" and st["text"].strip():
            doc = {"step": st["step"], "text": st["text"]}
            break
    fields = {}
    for st in steps:
        if st["stage"] == "extract":
            for k, v in collect_pairs(st).items():
                if v not in (None, ""):
                    fields.setdefault(k, v)
    summary = build_summary(steps)
    choice = find_decision_choice(steps, summary)
    out = {"steps": steps, "summary": summary,
           "decision_choice": choice, "doc": doc, "fields": fields}
    try:
        out["case"] = to_case(steps, summary, choice, doc)
    except Exception:  # noqa: BLE001
        out["case"] = None
    return out


# ---------- live result -> demo case shape ----------

TYPE_LABEL = {"inspection_record": "성능·상태점검기록부", "remarks_attachment": "특기사항 별지",
              "estimate": "견적서", "warranty_notice": "보증범위 안내"}
CASE_FIELDS = ["vehicle_no", "vin", "model_year", "first_registration", "mileage_km", "inspection_date",
               "inspection_valid_until", "accident_history", "simple_repair", "damaged_parts",
               "device_issues", "usage_change", "special_history", "remarks"]


def split_label(a, b):
    return f"{a}p" if a == b else f"{a}-{b}p"


def flat(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(flat(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def to_case(steps, summary, choice, doc):
    """라이브 결과를 데모의 case 객체(set, doc, pages, page_types, extracted, predicted, branch)로 바꿈. 정답·일치 필드는 없음."""
    types = []          # (start, end, label)
    step_ranges = {}    # 추출 단계 이름 -> [(a, b)]
    for st in steps:
        if st["stage"] != "merge":
            continue
        mj = st.get("parsed_json")
        if not isinstance(mj, dict):
            continue
        key_step = {x.get("branch_key"): x.get("step_name") for x in mj.get("source_steps") or []}
        for pr in mj.get("page_ranges") or []:
            key = pr.get("branch_key") or ""
            label = TYPE_LABEL.get(key.split(":", 1)[-1], key.split(":", 1)[-1] or "미분류")
            for rg in pr.get("ranges") or []:
                if len(rg) == 2:
                    types.append((int(rg[0]), int(rg[1]), label))
                    step_ranges.setdefault(key_step.get(key), []).append((int(rg[0]), int(rg[1])))
    cls = [(int(rg[0]), int(rg[1]), TYPE_LABEL.get(cat, cat or "미분류"))
           for st in steps if st["stage"] == "classify"
           for cat, rgs in st.get("classes") or [] for rg in rgs if len(rg) == 2]
    if cls:   # 쪽 종류는 분류 노드 결과 기준(병합에 안 이어진 갈래도 포함). 없으면 병합 기준 유지
        types = cls
    types.sort()
    pages = next((st["pages"] for st in steps if st.get("pages")), None) or max([t[1] for t in types] or [0])
    page_types = [{"split": split_label(a, b), "type": t} for a, b, t in types]

    extracted = []
    extra = {}
    for st in steps:
        if st["stage"] != "extract":
            continue
        pairs = {k: v for k, v in collect_pairs(st).items()}
        for k, v in pairs.items():
            if re.match(r"(attach_|estimate_)", k) and v not in (None, ""):
                extra[k] = flat(v)
        if "vehicle_no" in pairs or "car_name" in pairs:
            e = {"car_name": flat(pairs.get("car_name"))}
            for k in CASE_FIELDS:
                e[k] = flat(pairs.get(k))
            rgs = step_ranges.get(st["step"])
            if rgs:
                e["split"] = split_label(rgs[0][0], rgs[-1][1])
            extracted.append(e)
    if not extracted:
        extracted.append({"car_name": ""})
    extracted[0].update(extra)

    judge = next((st for st in steps if st["stage"] == "judge"), None)
    p = {"decision_choice": choice or "", "vehicle_no": summary.get("vehicle_no", ""),
         "vin": summary.get("vin", ""), "reason_codes": summary.get("reason_codes", []),
         "reason": summary.get("reason", "")}
    if judge:
        jp = collect_pairs(judge)
        for k in ("vehicle_no", "vin", "reason"):
            if jp.get(k):
                p[k] = flat(jp[k])
        if jp.get("reason_codes") is not None:
            p["reason_codes"] = norm_codes(jp["reason_codes"])
    branch = {"name": (doc or {}).get("step", ""), "text": (doc or {}).get("text", "")}
    branch["name"] = re.sub(r"^Instruct\s*-\s*", "", branch["name"])
    return {"set": "live", "doc": "", "pages": pages, "page_types": page_types,
            "extracted": extracted, "predicted": p, "branch": branch}


def save_raw(resp, name):
    RESULTS_DIR.mkdir(exist_ok=True)
    stem = re.sub(r"[^\w\-.]", "_", Path(name).stem)[:60] or "file"
    path = RESULTS_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{stem}.json"
    data = resp.model_dump() if hasattr(resp, "model_dump") else to_dict(resp)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path.name


# ---------- routes ----------

@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


def sample_names():
    return sorted(p.name for p in SAMPLES_DIR.glob("*.pdf")) if SAMPLES_DIR.exists() else []


@app.get("/img/<path:name>")
def img(name):
    return send_from_directory(STATIC_DIR / "img", name)


@app.get("/samples")
def samples():
    return jsonify({"samples": sample_names()})


@app.get("/samples/<name>")
def sample_file(name):
    if name not in sample_names():
        raise UserError("샘플을 찾을 수 없음.", 404)
    return send_from_directory(SAMPLES_DIR, name, mimetype="application/pdf")


@app.post("/api/run")
def run():
    client = get_client()
    agent_id, config_id = get_ids()

    upload = request.files.get("file")
    if upload and upload.filename:
        name = upload.filename
        data = upload.read()
    else:
        body = request.get_json(silent=True) or {}
        sample = request.form.get("sample") or body.get("sample")
        if not sample:
            raise UserError("PDF 파일을 올리거나 샘플을 고를 것.")
        if sample not in sample_names():
            raise UserError("없는 샘플임.")
        name = sample
        data = (SAMPLES_DIR / sample).read_bytes()

    if not name.lower().endswith(".pdf") or not data.startswith(b"%PDF"):
        raise UserError("PDF 파일만 가능함.")
    if len(data) > MAX_BYTES:
        raise UserError("파일이 너무 큼. 24MB 이하 PDF만 가능함.")

    try:
        uploaded = client.files.create(
            file=(Path(name).name, io.BytesIO(data), "application/pdf"),
            purpose="user_data",
        )
        input_data = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "file_id": uploaded.id,
                    }
                ],
            }
        ]
        response = client.responses.create(
            model=agent_id,
            include=INCLUDE,
            input=input_data,
            extra_body={"config_id": config_id},
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": api_error_message(e)}), 502

    with JOBS_LOCK:
        JOBS[response.id] = {"start": time.time(), "name": name, "saved": None}
    return jsonify({"ok": True, "job_id": response.id, "status": response.status})


@app.get("/api/job/<job_id>")
def job(job_id):
    client = get_client()
    try:
        resp = client.responses.retrieve(job_id, include=INCLUDE)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": api_error_message(e)}), 502

    with JOBS_LOCK:
        info = JOBS.setdefault(job_id, {"start": time.time(), "name": "file", "saved": None})
    status = resp.status
    out = {"ok": True, "job_id": job_id, "status": status,
           "elapsed": round(time.time() - info["start"], 1), "file": info["name"]}

    if status == "failed":
        out["ok"] = False
        out["error"] = "Studio 워크플로우 실행이 실패함. 파일을 바꾸거나 잠시 후 다시 시도할 것."
    elif status == "completed":
        with JOBS_LOCK:
            if info["saved"] is None:
                try:
                    info["saved"] = save_raw(resp, info["name"])
                except OSError:
                    info["saved"] = ""
        try:
            out.update(parse_response(resp))
        except Exception as e:  # noqa: BLE001
            out["ok"] = False
            out["error"] = f"결과를 해석하지 못함: {type(e).__name__}"
        out["saved_as"] = info["saved"]
    elif status not in ("queued", "in_progress"):
        out["ok"] = False
        out["error"] = f"알 수 없는 상태: {status}"
    return jsonify(out)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
