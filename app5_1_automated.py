import json
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import streamlit as st
from google import genai
from google.genai import types


# ============================================================
# APP CONFIG
# ============================================================

st.set_page_config(
    page_title="Edexcel IGCSE Maths Auto-Marker",
    page_icon="📐",
    layout="wide",
)

st.markdown(
    """
    <style>
        .stButton > button {
            border-radius: 8px;
            font-weight: 700;
        }
        div[data-testid="stStatusWidget"] {
            border-radius: 8px;
        }
        .score-card {
            padding: 1rem;
            border: 1px solid rgba(128,128,128,.25);
            border-radius: 12px;
            margin-bottom: 1rem;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


APP_VERSION = "2.3"
PRIMARY_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash",
]
AUDIT_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash",
]
GROUP_SIZE = 5

# IMPORTANT:
# Never put a real API key in this source file.
# Recommended:
#   Windows PowerShell: $env:GEMINI_API_KEY="your-key"
#   Streamlit Cloud: app secrets -> GEMINI_API_KEY
#
# A sidebar field is also provided so the app can be used locally
# without editing this file.


# ============================================================
# HELPERS
# ============================================================

def get_saved_api_key() -> str:
    """Read the API key from Streamlit secrets, local .env, or the environment."""
    # Streamlit secrets
    try:
        secret_key = st.secrets.get("GEMINI_API_KEY", "")
        if secret_key:
            return str(secret_key).strip()
    except Exception:
        pass

    # Local .env file in the same folder as this app.
    # This is useful for a personal/local installation so the key
    # survives closing and reopening the app.
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    if key.strip() == "GEMINI_API_KEY":
                        value = value.strip().strip('"').strip("'")
                        if value:
                            return value
    except OSError:
        pass

    return os.getenv("GEMINI_API_KEY", "").strip()


def save_api_key_locally(api_key: str) -> bool:
    """Save the working key in a local .env file next to the app."""
    if not api_key:
        return False

    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("# Local Gemini API key for the IGCSE Maths Corrector\n")
            f.write("GEMINI_API_KEY=" + api_key.replace("\n", "").strip() + "\n")

        # Best-effort restriction on systems that support chmod.
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass

        return True
    except OSError:
        return False


def mask_key(key: str) -> str:
    if len(key) <= 8:
        return "********"
    return f"{key[:4]}{'*' * max(4, len(key) - 8)}{key[-4:]}"


def upload_pdf(client, uploaded_file):
    """Upload a Streamlit PDF to Gemini and wait until processing finishes."""
    suffix = ".pdf"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded_file.getvalue())
        tmp_path = tmp.name

    try:
        remote_file = client.files.upload(file=tmp_path)

        while getattr(remote_file.state, "name", "") == "PROCESSING":
            time.sleep(0.5)
            remote_file = client.files.get(name=remote_file.name)

        state_name = getattr(remote_file.state, "name", "")
        if state_name and state_name not in {"ACTIVE", "SUCCEEDED"}:
            raise RuntimeError(
                f"Gemini could not finish processing {uploaded_file.name}. "
                f"File state: {state_name}"
            )

        return remote_file
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def extract_json(text: str) -> dict:
    """Parse JSON even if a model wraps it in a markdown code fence."""
    if not text:
        raise ValueError("The model returned an empty response.")

    cleaned = text.strip()

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Last-resort extraction of the largest JSON object.
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])

    raise ValueError("The model response was not valid JSON.")


def clamp_mark(value, max_marks):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(value, max_marks))


def normalise_result(data: dict) -> dict:
    """Clean and validate model output before anything is displayed."""
    questions = data.get("questions", [])
    if not isinstance(questions, list):
        questions = []

    cleaned = []

    for index, q in enumerate(questions, start=1):
        if not isinstance(q, dict):
            continue

        number = str(q.get("question_number", index)).strip()
        try:
            max_marks = int(q.get("max_marks", 0))
        except (TypeError, ValueError):
            max_marks = 0

        max_marks = max(0, max_marks)
        awarded = clamp_mark(q.get("awarded_marks", 0), max_marks)

        breakdown = q.get("mark_breakdown", [])
        if not isinstance(breakdown, list):
            breakdown = []

        cleaned_breakdown = []
        for item in breakdown:
            if isinstance(item, dict):
                cleaned_breakdown.append(
                    {
                        "code": str(item.get("code", "")).strip(),
                        "awarded": bool(item.get("awarded", False)),
                        "reason": str(item.get("reason", "")).strip(),
                    }
                )

        cleaned.append(
            {
                "question_number": number,
                "max_marks": max_marks,
                "awarded_marks": awarded,
                "topic": str(q.get("topic", "Unclassified")).strip(),
                "student_answer": str(q.get("student_answer", "")).strip(),
                "working_summary": str(q.get("working_summary", "")).strip(),
                "loss_reason": str(q.get("loss_reason", "")).strip(),
                "mark_breakdown": cleaned_breakdown,
                "correct_method": str(q.get("correct_method", "")).strip(),
                "full_mark_solution": str(q.get("full_mark_solution", "")).strip(),
            }
        )

    cleaned.sort(key=lambda q: q["question_number"])

    info = data.get("paper_info", {})
    if not isinstance(info, dict):
        info = {}

    notes = data.get("overall_notes", {})
    if not isinstance(notes, dict):
        notes = {}

    uncertainties = data.get("uncertainties", [])
    if not isinstance(uncertainties, list):
        uncertainties = []

    return {
        "paper_info": {
            "qualification": str(info.get("qualification", "Pearson Edexcel International GCSE Mathematics")).strip(),
            "paper": str(info.get("paper", "Not identified")).strip(),
            "session": str(info.get("session", "Not identified")).strip(),
            "max_marks_declared": info.get("max_marks"),
        },
        "questions": cleaned,
        "overall_notes": {
            "summary": str(notes.get("summary", "")).strip(),
            "strengths": [str(x).strip() for x in notes.get("strengths", []) if str(x).strip()],
            "revision_areas": [
                str(x).strip() for x in notes.get("revision_areas", []) if str(x).strip()
            ],
        },
        "uncertainties": [str(x).strip() for x in uncertainties if str(x).strip()],
    }


def calculate_totals(result: dict):
    earned = sum(q["awarded_marks"] for q in result["questions"])
    available = sum(q["max_marks"] for q in result["questions"])
    percentage = (earned / available * 100) if available else 0.0

    return earned, available, percentage


def percentage_band_grade(percentage: float) -> str:
    """
    This is only a provisional percentage band.
    It is NOT an official Pearson Edexcel session-specific grade boundary.
    """
    if percentage >= 90:
        return "9"
    if percentage >= 80:
        return "8"
    if percentage >= 70:
        return "7"
    if percentage >= 60:
        return "6"
    if percentage >= 50:
        return "5"
    if percentage >= 40:
        return "4"
    if percentage >= 30:
        return "3"
    if percentage >= 20:
        return "2"
    return "1"


def make_group_rows(questions):
    groups = []
    for start in range(0, len(questions), GROUP_SIZE):
        chunk = questions[start:start + GROUP_SIZE]
        earned = sum(q["awarded_marks"] for q in chunk)
        available = sum(q["max_marks"] for q in chunk)
        pct = (earned / available * 100) if available else 0.0
        groups.append(
            {
                "group": f"Questions {chunk[0]['question_number']} to {chunk[-1]['question_number']}",
                "earned": earned,
                "available": available,
                "percentage": pct,
                "questions": chunk,
            }
        )
    return groups


def test_api_key(api_key: str):
    """
    Validate authentication before uploading PDFs.
    The models.list() call is deliberately lightweight and avoids spending
    generation tokens just to test the key.
    """
    if not api_key:
        return False, "No API key was entered."

    try:
        client = genai.Client(api_key=api_key)
        models = list(client.models.list())
        if not models:
            return True, "API key accepted, but no models were returned."
        return True, "API key accepted by Gemini."
    except Exception as exc:
        message = str(exc)

        if "reported as leaked" in message.lower():
            return False, (
                "This API key has been blocked because Google reports it as leaked. "
                "Create a new API key in Google AI Studio."
            )

        if "401" in message or "unauthenticated" in message.lower() or "invalid api key" in message.lower():
            return False, (
                "Gemini rejected the API key. Make a new key in Google AI Studio "
                "and make sure you are using the newly created key."
            )

        if "403" in message or "permission" in message.lower() or "forbidden" in message.lower():
            return False, (
                "Gemini received the key but refused access. The key/project may "
                "have a permission, API, or restriction problem."
            )

        if "429" in message or "quota" in message.lower():
            return False, (
                "The key was recognised, but the project appears to have hit a "
                "rate or usage limit."
            )

        return False, f"Gemini returned an authentication/setup error: {message}"


# ============================================================
# MARKING PROMPT
# ============================================================

MARKING_SYSTEM = r"""
You are an automated Pearson Edexcel International GCSE Mathematics
marking assistant. You are NOT an official Pearson employee or examiner.

Your job is to mark the student's submitted work against the supplied
official mark scheme as strictly and faithfully as possible.

CORE RULES
1. Read the entire student submission and the entire mark scheme before
   assigning marks.
2. Match each student question to the corresponding mark-scheme question.
3. Award marks using the supplied mark scheme, not generic intuition.
4. Respect method (M), accuracy (A), independent/basis (B), error-carried-forward
   (ECF), follow-through, implied answers, and "ignore subsequent working" (ISW)
   where the mark scheme supports them.
5. Do not award a mark simply because a final answer looks plausible.
6. Do not remove an earned earlier mark because of a later contradiction when
   the mark scheme says the earlier work stands (ISW).
7. When a later answer follows correctly from an earlier student error and the
   mark scheme permits ECF/follow-through, award the appropriate later marks.
8. Do not invent missing working. If the image/PDF is unclear, flag uncertainty.
9. Do not penalise a student for a formatting difference when the mathematics
   is equivalent.
10. Use the mark scheme's exact maximum marks for each question where possible.
11. Award only integer marks unless the supplied mark scheme explicitly uses
    another convention.
12. Never exceed a question's maximum mark.
13. Return one entry for every identifiable question in the student submission.
14. For each lost mark, explain the precise mathematical/mark-scheme reason.
15. Give a correct full-mark solution that a student can study.
16. Keep the output factual and concise. Do not give a fake "official" grade.

VERY IMPORTANT
The application's Python code will calculate the total score and percentage.
Your job is to provide the individual question marks and marking evidence.
Do not attempt to make the arithmetic sound consistent by changing marks.
"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "paper_info": {
            "type": "object",
            "properties": {
                "qualification": {"type": "string"},
                "paper": {"type": "string"},
                "session": {"type": "string"},
                "max_marks": {"type": "integer"},
            },
            "required": ["qualification", "paper", "session"],
        },
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question_number": {"type": "string"},
                    "max_marks": {"type": "integer"},
                    "awarded_marks": {"type": "integer"},
                    "topic": {"type": "string"},
                    "student_answer": {"type": "string"},
                    "working_summary": {"type": "string"},
                    "loss_reason": {"type": "string"},
                    "mark_breakdown": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string"},
                                "awarded": {"type": "boolean"},
                                "reason": {"type": "string"},
                            },
                            "required": ["code", "awarded", "reason"],
                        },
                    },
                    "correct_method": {"type": "string"},
                    "full_mark_solution": {"type": "string"},
                },
                "required": [
                    "question_number",
                    "max_marks",
                    "awarded_marks",
                    "topic",
                    "student_answer",
                    "working_summary",
                    "loss_reason",
                    "mark_breakdown",
                    "correct_method",
                    "full_mark_solution",
                ],
            },
        },
        "overall_notes": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "strengths": {"type": "array", "items": {"type": "string"}},
                "revision_areas": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "strengths", "revision_areas"],
        },
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["paper_info", "questions", "overall_notes", "uncertainties"],
}


def is_temporary_model_error(exc: Exception) -> bool:
    """Return True for errors where trying another model is sensible."""
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "503",
            "unavailable",
            "high demand",
            "overloaded",
            "429",
            "resource exhausted",
            "temporarily",
            "deadline exceeded",
        )
    )


def grade_with_model(
    client,
    model_names,
    student_file,
    mark_scheme_file,
    question_file=None,
    status_callback=None,
):
    """
    Try several current Gemini models automatically.

    503/high-demand errors are temporary model availability problems, so
    the marker moves to the next compatible model rather than failing the
    entire homework run.
    """
    prompt = """
Mark the student's work in the uploaded PDF against the uploaded mark scheme.

The files are:
- Student submission: the actual work to mark.
- Official mark scheme: the source of marking rules and maximum marks.
- Optional question paper: use it only to improve question identification/context.

Return ONLY the requested JSON structure.

Before returning:
- identify every question you can,
- assign max marks from the mark scheme,
- assign earned marks question-by-question,
- record M/A/B-style evidence where available,
- explain lost marks,
- provide a full-mark solution,
- flag any unreadable or genuinely ambiguous evidence.

Do not calculate the final total as a free-form narrative. The application will calculate it from your individual question marks.
"""

    contents = [student_file, mark_scheme_file]
    if question_file is not None:
        contents.append(question_file)
    contents.append(prompt)

    errors = []

    for model_name in model_names:
        for attempt in range(2):
            try:
                if status_callback:
                    status_callback(
                        f"🧠 Marking with {model_name}"
                        + (f" (attempt {attempt + 1}/2)" if attempt else "")
                    )

                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=MARKING_SYSTEM,
                        response_mime_type="application/json",
                        response_schema=OUTPUT_SCHEMA,
                    ),
                )

                if response and response.text:
                    return extract_json(response.text), model_name

                raise RuntimeError(f"{model_name} returned an empty response.")

            except Exception as exc:
                errors.append(f"{model_name}: {exc}")

                # Retry temporary availability/quota errors once.
                if is_temporary_model_error(exc) and attempt == 0:
                    time.sleep(2)
                    continue

                # For model-specific availability errors, continue to the next model.
                break

    raise RuntimeError(
        "All configured Gemini models failed. "
        + " | ".join(errors[-8:])
    )


def audit_result(
    client,
    result,
    student_file,
    mark_scheme_file,
    model_names,
    status_callback=None,
):
    """
    Independent second pass. It uses a different model where possible and
    falls back automatically if that model is overloaded.
    """
    audit_prompt = f"""
You are performing a second-pass audit of an automated mathematics marking report.

Compare the report below against the actual student submission and the official
mark scheme. Correct any question-level marks that are not supported by the
mark scheme. Pay particular attention to:
- M/A/B mark logic
- ECF/follow-through
- ISW
- arithmetic in the student's working
- omitted vs shown working
- maximum marks
- question matching
- diagrams and geometry
- algebraic equivalence

Do NOT change a mark merely because another valid method exists.
Do NOT invent evidence that is not visible.
Return the COMPLETE corrected JSON in exactly the requested schema.

FIRST-PASS REPORT:
{json.dumps(result, ensure_ascii=False)}
"""

    contents = [student_file, mark_scheme_file, audit_prompt]
    errors = []

    for model_name in model_names:
        for attempt in range(2):
            try:
                if status_callback:
                    status_callback(
                        f"🔎 Auditing with {model_name}"
                        + (f" (attempt {attempt + 1}/2)" if attempt else "")
                    )

                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=MARKING_SYSTEM,
                        response_mime_type="application/json",
                        response_schema=OUTPUT_SCHEMA,
                    ),
                )

                if response and response.text:
                    return extract_json(response.text), model_name

                raise RuntimeError(f"{model_name} returned an empty response.")

            except Exception as exc:
                errors.append(f"{model_name}: {exc}")

                if is_temporary_model_error(exc) and attempt == 0:
                    time.sleep(2)
                    continue

                break

    raise RuntimeError("Audit models failed. " + " | ".join(errors[-8:]))


# ============================================================
# UI
# ============================================================

st.title("📐 Pearson Edexcel IGCSE Maths Auto-Marker")
st.caption(
    "Upload the student's work and official mark scheme. The app marks question-by-question, "
    "calculates the score in Python, diagnoses errors, and produces a study report."
)

with st.sidebar:
    st.header("⚙️ Settings")

    saved_key = get_saved_api_key()
    api_key = st.text_input(
        "Gemini API key",
        value=saved_key,
        type="password",
        help="Use a current Gemini API key from Google AI Studio. "
             "The app can remember a working key locally in a .env file.",
    ).strip()

    remember_key = st.checkbox(
        "Remember this API key on this computer",
        value=True,
        help="After a successful test, save the key locally so you do not "
             "need to enter it every time.",
    )

    if st.button("🔑 Test API key", use_container_width=True):
        ok, message = test_api_key(api_key)
        if ok:
            st.success(message)

            if remember_key:
                if save_api_key_locally(api_key):
                    st.success("✅ API key saved. You will not need to enter it next time.")
                else:
                    st.warning(
                        "The API key works, but I could not save it locally. "
                        "You may need to enter it again next time."
                    )
        else:
            st.error(message)

    if api_key:
        st.caption(f"Key loaded: {mask_key(api_key)}")
    else:
        st.caption("No API key loaded.")

    st.caption(f"App version: {APP_VERSION}")
    st.caption("Automatic model fallback: ON")
    st.caption("Models tried from newest to fallback")

    audit_enabled = st.checkbox(
        "Run second-pass marking audit",
        value=True,
        help="Uses a second model pass to catch unsupported marks or missed ECF/ISW.",
    )

    if st.button("🗑️ Forget saved API key", use_container_width=True):
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        try:
            if os.path.exists(env_path):
                os.remove(env_path)
                st.success("Saved local API key removed.")
                st.rerun()
            else:
                st.info("No locally saved API key was found.")
        except OSError as exc:
            st.error(f"Could not remove the saved key: {exc}")

    st.divider()
    st.subheader("Grade handling")
    st.write(
        "Raw score and percentage are calculated exactly. "
        "Official Edexcel 1–9 grades depend on the relevant exam session and paper."
    )
    st.info(
        "Without official session-specific grade boundaries, the app shows a "
        "PROVISIONAL percentage-band grade rather than pretending it is an official Pearson grade."
    )

st.subheader("📁 Upload files")

col1, col2 = st.columns(2)

with col1:
    st.markdown("### 1. Student submission")
    student_pdf = st.file_uploader(
        "Student PDF",
        type=["pdf"],
        key="student_pdf_uploader",
        help="Include all student pages, including workings and diagrams.",
    )

with col2:
    st.markdown("### 2. Official mark scheme")
    mark_scheme_pdf = st.file_uploader(
        "Mark scheme PDF",
        type=["pdf"],
        key="ms_pdf_uploader",
        help="Use the official scheme for the exact paper.",
    )

col3, col4 = st.columns(2)

with col3:
    st.markdown("### 3. Question paper (optional)")
    question_pdf = st.file_uploader(
        "Question paper PDF",
        type=["pdf"],
        key="question_pdf_uploader",
        help="Optional, but helpful when the student PDF does not contain the questions.",
    )

with col4:
    st.markdown("### 4. Grade boundaries (optional)")
    grade_boundary_pdf = st.file_uploader(
        "Official grade-boundary PDF",
        type=["pdf"],
        key="grade_boundary_uploader",
        help="Optional. This is needed for a genuine session-specific 1–9 boundary result.",
    )

ready = bool(student_pdf and mark_scheme_pdf and api_key)

if not ready:
    st.warning("Upload the student PDF, mark scheme PDF, and enter/define a Gemini API key to start.")

if st.button(
    "🚀 Mark Homework Automatically",
    type="primary",
    use_container_width=True,
    disabled=not ready,
):
    client = None
    uploaded_remote_files = []

    try:
        client = genai.Client(api_key=api_key)

        with st.status("Preparing automatic marking...", expanded=True) as status:
            st.write("⚡ Uploading PDFs to Gemini...")

            upload_jobs = {}
            files_to_upload = {
                "student": student_pdf,
                "mark_scheme": mark_scheme_pdf,
            }

            if question_pdf:
                files_to_upload["question"] = question_pdf

            with ThreadPoolExecutor(max_workers=len(files_to_upload)) as executor:
                futures = {
                    name: executor.submit(upload_pdf, client, upload)
                    for name, upload in files_to_upload.items()
                }

                for name, future in futures.items():
                    upload_jobs[name] = future.result()
                    uploaded_remote_files.append(upload_jobs[name])

            st.write("🧠 First-pass marking against the official mark scheme...")

            raw_result, marking_model = grade_with_model(
                client=client,
                model_names=PRIMARY_MODELS,
                student_file=upload_jobs["student"],
                mark_scheme_file=upload_jobs["mark_scheme"],
                question_file=upload_jobs.get("question"),
                status_callback=st.write,
            )

            result = normalise_result(raw_result)
            st.session_state["last_marking_model"] = marking_model

            if audit_enabled:
                st.write("🔎 Running an independent second-pass marking audit...")
                try:
                    audit_candidates = [
                        m for m in AUDIT_MODELS if m != marking_model
                    ] + [m for m in PRIMARY_MODELS if m != marking_model and m not in AUDIT_MODELS]

                    audited, audit_model = audit_result(
                        client=client,
                        result=result,
                        student_file=upload_jobs["student"],
                        mark_scheme_file=upload_jobs["mark_scheme"],
                        model_names=audit_candidates,
                        status_callback=st.write,
                    )
                    result = normalise_result(audited)
                    st.session_state["last_audit_model"] = audit_model
                except Exception as audit_error:
                    st.warning(
                        f"Second-pass audit could not be completed, so the first-pass "
                        f"marking was kept. Reason: {audit_error}"
                    )

            earned, available, percentage = calculate_totals(result)
            provisional_grade = percentage_band_grade(percentage)
            groups = make_group_rows(result["questions"])

            # Store for download/re-render.
            st.session_state["last_result"] = result
            st.session_state["last_totals"] = {
                "earned": earned,
                "available": available,
                "percentage": percentage,
                "provisional_grade": provisional_grade,
            }
            st.session_state["last_groups"] = groups
            st.session_state["last_student_name"] = student_pdf.name
            st.session_state["last_mark_scheme_name"] = mark_scheme_pdf.name

            status.update(
                label="✅ Marking complete",
                state="complete",
                expanded=False,
            )

    except Exception as exc:
        message = str(exc)

        if "reported as leaked" in message.lower():
            st.error(
                "This Gemini API key has been blocked as a leaked key. "
                "Create a NEW key in Google AI Studio and replace the old one."
            )
        elif "401" in message or "unauthenticated" in message.lower() or "invalid api key" in message.lower():
            st.error(
                "Gemini rejected this API key. Click 'Test API key' in the sidebar "
                "to verify the replacement key before grading."
            )
        elif "403" in message or "permission" in message.lower() or "forbidden" in message.lower():
            st.error(
                "The key is being refused by the Gemini project. Check the key's "
                "project/restrictions and that the Gemini API is enabled."
            )
        elif "429" in message or "quota" in message.lower():
            st.error(
                "The Gemini project has hit a rate/usage limit. Check the project's "
                "Gemini API usage and billing/quota."
            )
        else:
            st.error(f"The automatic marking run failed: {message}")

        with st.expander("Technical error details"):
            st.code(message)

    finally:
        if client is not None:
            for remote_file in uploaded_remote_files:
                try:
                    client.files.delete(name=remote_file.name)
                except Exception:
                    pass


# ============================================================
# REPORT RENDERING
# ============================================================

if "last_result" in st.session_state:
    result = st.session_state["last_result"]
    totals = st.session_state["last_totals"]
    groups = st.session_state["last_groups"]

    st.divider()
    st.header("🏆 Performance Summary")

    c1, c2, c3, c4 = st.columns(4)

    c1.metric(
        "Score",
        f"{totals['earned']} / {totals['available']}",
    )
    c2.metric(
        "Percentage",
        f"{totals['percentage']:.2f}%",
    )
    c3.metric(
        "Provisional band",
        f"Grade {totals['provisional_grade']}",
    )
    c4.metric(
        "Questions marked",
        len(result["questions"]),
    )

    info = result["paper_info"]
    st.caption(
        f"Qualification: {info['qualification']}  |  "
        f"Paper: {info['paper']}  |  "
        f"Session: {info['session']}"
    )

    marking_model = st.session_state.get("last_marking_model", "Unknown")
    audit_model = st.session_state.get("last_audit_model", "Not run")
    st.caption(
        f"Marker model: {marking_model}  |  Audit model: {audit_model}"
    )

    st.info(
        "The score above is calculated directly from the question-level marks returned "
        "by the marker. The Grade shown is only a provisional percentage band unless "
        "official session-specific grade boundaries are supplied and applied."
    )

    if result["overall_notes"]["summary"]:
        st.subheader("Chapter / topic mastery overview")
        st.write(result["overall_notes"]["summary"])

    strengths_col, revision_col = st.columns(2)

    with strengths_col:
        st.subheader("✅ Key strengths")
        if result["overall_notes"]["strengths"]:
            for item in result["overall_notes"]["strengths"]:
                st.write(f"• {item}")
        else:
            st.write("No specific strengths were identified.")

    with revision_col:
        st.subheader("📚 Primary revision areas")
        if result["overall_notes"]["revision_areas"]:
            for item in result["overall_notes"]["revision_areas"]:
                st.write(f"• {item}")
        else:
            st.write("No major revision areas were identified.")

    if result["uncertainties"]:
        st.subheader("⚠️ Marking uncertainties")
        for item in result["uncertainties"]:
            st.write(f"• {item}")

    # Group summary
    st.header("📊 Question groups")

    for group in groups:
        st.markdown(
            f"**{group['group']}** — "
            f"{group['earned']} / {group['available']} "
            f"({group['percentage']:.2f}%)"
        )

        rows = []
        for q in group["questions"]:
            rows.append(
                {
                    "Question": q["question_number"],
                    "Marks": f"{q['awarded_marks']} / {q['max_marks']}",
                    "Topic": q["topic"],
                    "Loss reason": q["loss_reason"] or "None",
                }
            )

        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
        )

    # Full question-by-question report
    st.header("📝 Question-by-question correction")

    for q in result["questions"]:
        with st.expander(
            f"Question {q['question_number']} — {q['awarded_marks']} / {q['max_marks']} — {q['topic']}"
        ):
            st.markdown(f"**Student answer / visible working**")
            st.write(q["student_answer"] or q["working_summary"] or "No readable answer extracted.")

            st.markdown(f"**Mark loss**")
            st.write(q["loss_reason"] or "No marks lost.")

            if q["mark_breakdown"]:
                breakdown_rows = []
                for item in q["mark_breakdown"]:
                    breakdown_rows.append(
                        {
                            "Mark": item["code"],
                            "Awarded": "Yes" if item["awarded"] else "No",
                            "Reason": item["reason"],
                        }
                    )

                st.dataframe(
                    pd.DataFrame(breakdown_rows),
                    use_container_width=True,
                    hide_index=True,
                )

            st.markdown("**Correct method**")
            st.write(q["correct_method"] or "Not provided.")

            st.markdown("**Full-mark model solution**")
            st.write(q["full_mark_solution"] or "Not provided.")

    # Downloads
    st.header("⬇️ Export")

    report_lines = [
        "# Pearson Edexcel IGCSE Maths Auto-Marker Report",
        "",
        f"Score: {totals['earned']} / {totals['available']}",
        f"Percentage: {totals['percentage']:.2f}%",
        f"Provisional percentage-band grade: {totals['provisional_grade']}",
        f"Paper: {info['paper']}",
        f"Session: {info['session']}",
        "",
        "## Overview",
        result["overall_notes"]["summary"],
        "",
        "## Strengths",
    ]

    report_lines.extend(f"- {x}" for x in result["overall_notes"]["strengths"])
    report_lines.append("")
    report_lines.append("## Revision areas")
    report_lines.extend(f"- {x}" for x in result["overall_notes"]["revision_areas"])
    report_lines.append("")
    report_lines.append("## Question breakdown")

    for q in result["questions"]:
        report_lines.extend(
            [
                "",
                f"### Question {q['question_number']} — {q['awarded_marks']} / {q['max_marks']}",
                f"Topic: {q['topic']}",
                f"Loss reason: {q['loss_reason'] or 'None'}",
                f"Correct method: {q['correct_method']}",
                f"Full-mark solution: {q['full_mark_solution']}",
            ]
        )

    report_text = "\n".join(report_lines)

    csv_rows = []
    for q in result["questions"]:
        csv_rows.append(
            {
                "Question": q["question_number"],
                "Awarded": q["awarded_marks"],
                "Max": q["max_marks"],
                "Percentage": (
                    round(q["awarded_marks"] / q["max_marks"] * 100, 2)
                    if q["max_marks"]
                    else 0
                ),
                "Topic": q["topic"],
                "Loss Reason": q["loss_reason"],
                "Correct Method": q["correct_method"],
            }
        )

    csv_data = pd.DataFrame(csv_rows).to_csv(index=False)

    col_a, col_b = st.columns(2)

    with col_a:
        st.download_button(
            "📄 Download Markdown report",
            data=report_text,
            file_name="igcse_maths_marking_report.md",
            mime="text/markdown",
            use_container_width=True,
        )

    with col_b:
        st.download_button(
            "📊 Download question marks CSV",
            data=csv_data,
            file_name="igcse_maths_question_marks.csv",
            mime="text/csv",
            use_container_width=True,
        )

    with st.expander("🔧 View machine-readable JSON"):
        st.code(json.dumps(result, indent=2, ensure_ascii=False), language="json")

st.divider()
st.caption(
    "Important: this is an AI-assisted marking tool. It can make mistakes, especially "
    "with blurry handwriting, ambiguous diagrams, or unusual student methods. "
    "The official mark scheme remains the authority."
)
