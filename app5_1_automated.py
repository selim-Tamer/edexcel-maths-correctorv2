import json
import os
import re
import io
import tempfile
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

import pandas as pd
import requests
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


APP_VERSION = "3.0.1"
PRIMARY_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    # NOTE: gemini-3.1-pro-preview was tried here as a last-resort fallback
    # on separate (Pro-tier) capacity, but this account's plan has quota
    # limit: 0 for it — it can never succeed, only ever fail, so it was
    # removed rather than left as dead weight. Re-add it if/when billing is
    # enabled on the Google Cloud project behind this API key.
]
AUDIT_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
]
GROUP_SIZE = 5

# Max time to wait for any single Gemini request before giving up on it.
GEMINI_REQUEST_TIMEOUT_MS = 60_000

# Hard ceiling on one submission's TOTAL time (across every model/attempt
# combined). Without this, a submission that stalls on every fallback model
# in turn could run for 15-20+ minutes before finally giving up. This forces
# a decisive move-on instead, and the abandoned submission is retried on the
# next check (it's never marked "done").
PER_SUBMISSION_TIMEOUT_SECONDS = 240

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

# ============================================================
# MODEL HEALTH: remember which models are failing so later submissions
# skip them instead of re-discovering the failure every time.
# ============================================================

MODEL_COOLDOWN_TEMPORARY_SECONDS = 2 * 60      # overloaded / slow: retry later
MODEL_COOLDOWN_PERMANENT_SECONDS = 12 * 3600   # retired / not found


@st.cache_resource
def _get_model_health_store():
    # cache_resource keeps ONE shared object alive across Streamlit reruns
    # (a plain module-level dict would be reset on every rerun). The lock
    # makes it safe to update from worker threads.
    return {"lock": threading.Lock(), "bad": {}}


MODEL_HEALTH = _get_model_health_store()


def is_permanent_model_error(exc: Exception) -> bool:
    message = str(exc).lower()
    if "no longer available" in message:
        return True
    if ("404" in message or "not_found" in message) and "model" in message:
        return True
    # A quota LIMIT of 0 means this API key/plan has no access to the model
    # at all (e.g. a Pro-tier model on the free tier) — retrying in a few
    # minutes will hit the exact same wall, unlike an ordinary "used up
    # today's quota" 429, so this is treated as effectively permanent.
    if "limit: 0" in message or "limit:0" in message:
        return True
    return False


def record_model_failure(model_name: str, exc: Exception):
    """Bench a model if the error means the MODEL is the problem. Errors caused
    by one particular submission (bad JSON, empty reply, etc.) are ignored."""
    if is_permanent_model_error(exc):
        cooldown, kind = MODEL_COOLDOWN_PERMANENT_SECONDS, "unavailable"
    elif is_timeout_error(exc) or is_temporary_model_error(exc):
        cooldown, kind = MODEL_COOLDOWN_TEMPORARY_SECONDS, "busy/slow"
    else:
        return

    with MODEL_HEALTH["lock"]:
        MODEL_HEALTH["bad"][model_name] = {
            "until": time.time() + cooldown,
            "kind": kind,
        }


def record_model_success(model_name: str):
    with MODEL_HEALTH["lock"]:
        MODEL_HEALTH["bad"].pop(model_name, None)


def usable_models(model_names):
    """Return the models worth trying now, in their original order."""
    now = time.time()
    with MODEL_HEALTH["lock"]:
        bad = MODEL_HEALTH["bad"]
        for name in [n for n, v in bad.items() if v["until"] <= now]:
            del bad[name]  # cooldown finished: give it another chance

        healthy = [m for m in model_names if m not in bad]
        if healthy:
            return healthy

        # Everything is benched. Rather than fail instantly, still try the
        # ones that are only temporarily struggling.
        fallback = [m for m in model_names if bad[m]["kind"] != "unavailable"]
        return fallback or list(model_names)


def benched_models() -> dict:
    """{model: (kind, seconds_left)} for models currently being skipped."""
    now = time.time()
    with MODEL_HEALTH["lock"]:
        return {
            m: (v["kind"], int(v["until"] - now))
            for m, v in MODEL_HEALTH["bad"].items()
            if v["until"] > now
        }


def reset_model_health():
    with MODEL_HEALTH["lock"]:
        MODEL_HEALTH["bad"].clear()


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


def make_gemini_client(key: str):
    """
    Create a Gemini client with a per-request time limit, so one stalled call
    fails (and the app moves on to the next model) instead of hanging for
    minutes. The limit is in milliseconds.
    """
    return genai.Client(
        api_key=key,
        http_options=types.HttpOptions(timeout=GEMINI_REQUEST_TIMEOUT_MS),
    )


def upload_pdf(client, uploaded_file, status_callback=None, max_wait_seconds=120):
    """Upload a Streamlit PDF to Gemini and wait until processing finishes."""
    if uploaded_file is None:
        raise ValueError(
            "No file was provided to upload (the upload may have been "
            "cleared, e.g. by a page reload)."
        )

    suffix = ".pdf"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded_file.getvalue())
        tmp_path = tmp.name

    try:
        if status_callback:
            status_callback(f"⬆️ Uploading {uploaded_file.name} to Gemini...")

        remote_file = client.files.upload(file=tmp_path)

        waited = 0.0
        while getattr(remote_file.state, "name", "") == "PROCESSING":
            if waited >= max_wait_seconds:
                raise RuntimeError(
                    f"Gemini has been stuck processing {uploaded_file.name} for "
                    f"over {max_wait_seconds}s without finishing. This usually "
                    f"means a transient issue on Gemini's side — try again."
                )
            time.sleep(0.5)
            waited += 0.5
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


# ============================================================
# REPORT RENDERING HELPERS
# (defined here, above both the Classroom and manual-upload
# flows, so either one can render/export a full student report)
# ============================================================

def build_markdown_report(student_name: str, result: dict, totals: dict) -> str:
    info = result["paper_info"]
    lines = [
        f"# Pearson Edexcel IGCSE Maths Auto-Marker Report — {student_name}",
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
    lines.extend(f"- {x}" for x in result["overall_notes"]["strengths"])
    lines.append("")
    lines.append("## Revision areas")
    lines.extend(f"- {x}" for x in result["overall_notes"]["revision_areas"])
    lines.append("")
    lines.append("## Question breakdown")

    for q in result["questions"]:
        lines.extend(
            [
                "",
                f"### Question {q['question_number']} — {q['awarded_marks']} / {q['max_marks']}",
                f"Topic: {q['topic']}",
                f"Loss reason: {q['loss_reason'] or 'None'}",
                f"Correct method: {q['correct_method']}",
                f"Full-mark solution: {q['full_mark_solution']}",
            ]
        )

    return "\n".join(lines)


def build_csv_rows(student_name: str, result: dict) -> list:
    rows = []
    for q in result["questions"]:
        rows.append(
            {
                "Student": student_name,
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
    return rows


def render_student_report(student_name, result, totals, groups, marking_model, audit_model):
    """Render the full single-student report (metrics, notes, groups, per-question detail)."""
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Score", f"{totals['earned']} / {totals['available']}")
    c2.metric("Percentage", f"{totals['percentage']:.2f}%")
    c3.metric("Provisional band", f"Grade {totals['provisional_grade']}")
    c4.metric("Questions marked", len(result["questions"]))

    info = result["paper_info"]
    st.caption(
        f"Qualification: {info['qualification']}  |  "
        f"Paper: {info['paper']}  |  "
        f"Session: {info['session']}"
    )
    st.caption(f"Marker model: {marking_model}  |  Audit model: {audit_model}")

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

    st.subheader("📊 Question groups")

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

        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.subheader("📝 Question-by-question correction")

    for q in result["questions"]:
        with st.expander(
            f"Question {q['question_number']} — {q['awarded_marks']} / {q['max_marks']} — {q['topic']}"
        ):
            st.markdown("**Student answer / visible working**")
            st.write(q["student_answer"] or q["working_summary"] or "No readable answer extracted.")

            st.markdown("**Mark loss**")
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

                st.dataframe(pd.DataFrame(breakdown_rows), use_container_width=True, hide_index=True)

            st.markdown("**Correct method**")
            st.write(q["correct_method"] or "Not provided.")

            st.markdown("**Full-mark model solution**")
            st.write(q["full_mark_solution"] or "Not provided.")

    with st.expander("🔧 View machine-readable JSON"):
        st.code(json.dumps(result, indent=2, ensure_ascii=False), language="json")


def mark_one_submission(
    client,
    student_name: str,
    student_file,
    mark_scheme_file,
    question_file,
    audit_enabled: bool,
):
    """
    Mark one student's already-uploaded file(s) and return a result dict.

    Deliberately makes NO Streamlit calls (no st.write/st.warning/etc.) so it
    is safe to run inside a worker thread via ThreadPoolExecutor — Streamlit
    only supports UI calls from the main script thread. Callers should run
    several of these concurrently to mark multiple students in parallel,
    then report progress/results back in the main thread as futures complete.
    """
    raw_result, marking_model = grade_with_model(
        client=client,
        model_names=PRIMARY_MODELS,
        student_file=student_file,
        mark_scheme_file=mark_scheme_file,
        question_file=question_file,
        status_callback=None,
    )

    result = normalise_result(raw_result)
    audit_model = "Not run"
    audit_warning = None

    if audit_enabled:
        try:
            audit_candidates = [
                m for m in AUDIT_MODELS if m != marking_model
            ] + [
                m for m in PRIMARY_MODELS
                if m != marking_model and m not in AUDIT_MODELS
            ]
            # The audit is a bonus check, so fail fast: try at most two models.
            audit_candidates = usable_models(audit_candidates)[:2]

            audited, audit_model = audit_result(
                client=client,
                result=result,
                student_file=student_file,
                mark_scheme_file=mark_scheme_file,
                model_names=audit_candidates,
                status_callback=None,
            )
            result = normalise_result(audited)
        except Exception as audit_error:
            audit_warning = str(audit_error)

    earned, available, percentage = calculate_totals(result)
    provisional_grade = percentage_band_grade(percentage)
    groups = make_group_rows(result["questions"])

    return {
        "student_name": student_name,
        "status": "Marked",
        "error": None,
        "audit_warning": audit_warning,
        "result": result,
        "totals": {
            "earned": earned,
            "available": available,
            "percentage": percentage,
            "provisional_grade": provisional_grade,
        },
        "groups": groups,
        "marking_model": marking_model,
        "audit_model": audit_model,
    }


def test_api_key(api_key: str):
    """
    Validate authentication before uploading PDFs.
    The models.list() call is deliberately lightweight and avoids spending
    generation tokens just to test the key.
    """
    if not api_key:
        return False, "No API key was entered."

    try:
        client = make_gemini_client(api_key)
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
# GOOGLE CLASSROOM INTEGRATION
# ============================================================

CLASSROOM_BASE = "https://classroom.googleapis.com/v1"
DRIVE_BASE = "https://www.googleapis.com/drive/v3"

CLASSROOM_SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.students",
    "https://www.googleapis.com/auth/drive.readonly",
    # Needed for get_user_profile() / userProfiles.get, so student names can
    # be resolved instead of falling back to their raw numeric Google ID.
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
]


def classroom_auth_status():
    """
    Check the secrets.toml settings needed for Google Classroom access.

    Returns (configured, problems). `configured` is True only when login
    AND the access token needed to actually call the Classroom/Drive APIs
    are both set up correctly. `problems` lists everything missing or
    misconfigured, so the UI can show something more useful than a plain
    "not configured" message.
    """
    problems = []

    try:
        auth = st.secrets.get("auth")
    except Exception:
        return False, ["Could not read st.secrets['auth']."]

    if not auth:
        return False, ["No [auth] section found in secrets.toml."]

    # We call st.login("google"), so provider-specific settings live
    # under [auth.google]. Shared settings live under [auth].
    google_auth = auth.get("google")
    if not google_auth:
        problems.append("No [auth.google] section found in secrets.toml.")
    else:
        for key in ("client_id", "client_secret", "server_metadata_url"):
            if not google_auth.get(key):
                problems.append(f"[auth.google] is missing '{key}'.")

        client_kwargs = google_auth.get("client_kwargs") or {}
        try:
            scope = str(client_kwargs.get("scope", ""))
        except AttributeError:
            scope = ""

        for required_scope in (
            "classroom.courses.readonly",
            "classroom.coursework.students",
            "drive.readonly",
            "classroom.rosters.readonly",
        ):
            if required_scope not in scope:
                problems.append(
                    f"[auth.google].client_kwargs.scope is missing the "
                    f"'{required_scope}' scope, so Classroom/Drive calls will fail "
                    f"even after logging in successfully."
                )

    if not auth.get("redirect_uri"):
        problems.append("[auth] is missing 'redirect_uri'.")

    if not auth.get("cookie_secret"):
        problems.append("[auth] is missing 'cookie_secret'.")

    expose_tokens = auth.get("expose_tokens")
    if not expose_tokens or "access" not in list(expose_tokens):
        problems.append(
            "[auth] is missing 'expose_tokens = [\"access\"]'. Without this, "
            "st.user.tokens['access'] is always empty — login will appear to "
            "succeed but every Classroom/Drive call will fail with "
            "'no API access token is available'."
        )

    return (len(problems) == 0), problems


def classroom_auth_configured() -> bool:
    """Return True when Streamlit Google OAuth is fully configured for Classroom."""
    configured, _ = classroom_auth_status()
    return configured


def google_logged_in() -> bool:
    """Safely check Streamlit's Google OIDC session."""
    try:
        return bool(st.user.is_logged_in)
    except Exception:
        return False


def get_google_access_token() -> str:
    """Return the OAuth access token exposed by Streamlit, never display it."""
    try:
        return str(st.user.tokens["access"])
    except Exception:
        return ""


def classroom_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def classroom_request(method: str, url: str, token: str, **kwargs):
    """Call a Google API endpoint with a clear error message."""
    headers = kwargs.pop("headers", {})
    merged = classroom_headers(token)
    merged.update(headers)

    response = requests.request(
        method=method,
        url=url,
        headers=merged,
        timeout=60,
        **kwargs,
    )

    if response.status_code >= 400:
        detail = response.text[:2000]
        raise RuntimeError(
            f"Google API error {response.status_code}: {detail}"
        )

    if not response.content:
        return {}

    return response.json()


def classroom_list_all(url: str, token: str, params: dict, items_key: str):
    """
    Follow nextPageToken until Google says there are no more pages, so a
    class/course/assignment count above one page (100) is never silently
    truncated. params is not mutated.
    """
    items = []
    page_params = dict(params)

    while True:
        data = classroom_request("GET", url, token, params=page_params)
        items.extend(data.get(items_key, []))

        next_token = data.get("nextPageToken")
        if not next_token:
            return items

        page_params = dict(params)
        page_params["pageToken"] = next_token


def list_teacher_courses(token: str):
    return classroom_list_all(
        f"{CLASSROOM_BASE}/courses",
        token,
        params={
            "teacherId": "me",
            "courseStates": "ACTIVE",
            "pageSize": 100,
        },
        items_key="courses",
    )


def list_assignments(token: str, course_id: str):
    assignments = classroom_list_all(
        f"{CLASSROOM_BASE}/courses/{course_id}/courseWork",
        token,
        params={
            "courseWorkStates": "PUBLISHED",
            "pageSize": 100,
        },
        items_key="courseWork",
    )
    return [
        item for item in assignments
        if item.get("workType") == "ASSIGNMENT"
    ]


def list_turned_in_submissions(token: str, course_id: str, coursework_id: str):
    return classroom_list_all(
        f"{CLASSROOM_BASE}/courses/{course_id}/courseWork/{coursework_id}/studentSubmissions",
        token,
        params={
            "states": "TURNED_IN",
            "pageSize": 100,
        },
        items_key="studentSubmissions",
    )


def get_user_profile(token: str, user_id: str):
    return classroom_request(
        "GET",
        f"{CLASSROOM_BASE}/userProfiles/{user_id}",
        token,
    )


def download_drive_file(token: str, file_id: str):
    """
    Download a Drive attachment.
    PDFs/images are downloaded directly. Google Docs are exported to PDF.
    """
    meta = classroom_request(
        "GET",
        f"{DRIVE_BASE}/files/{file_id}",
        token,
        params={"fields": "id,name,mimeType,capabilities"},
    )

    name = meta.get("name", f"drive_file_{file_id}")
    mime = meta.get("mimeType", "application/octet-stream")

    capabilities = meta.get("capabilities", {})
    if capabilities and capabilities.get("canDownload") is False:
        raise RuntimeError(f"Google Drive does not allow downloading '{name}'.")

    if mime == "application/vnd.google-apps.document":
        response = requests.get(
            f"{DRIVE_BASE}/files/{file_id}/export",
            headers=classroom_headers(token),
            params={"mimeType": "application/pdf"},
            timeout=60,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Could not export Google Doc '{name}' to PDF: "
                f"{response.status_code} {response.text[:1000]}"
            )
        return name.rsplit(".", 1)[0] + ".pdf", response.content, "application/pdf"

    response = requests.get(
        f"{DRIVE_BASE}/files/{file_id}",
        headers=classroom_headers(token),
        params={"alt": "media"},
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Could not download '{name}': "
            f"{response.status_code} {response.text[:1000]}"
        )

    return name, response.content, mime


def upload_bytes_to_gemini(client, data: bytes, filename: str, max_wait_seconds=120):
    """Upload Classroom-downloaded content to Gemini as a temporary file."""
    suffix = os.path.splitext(filename)[1].lower() or ".bin"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        remote_file = client.files.upload(file=tmp_path)

        waited = 0.0
        while getattr(remote_file.state, "name", "") == "PROCESSING":
            if waited >= max_wait_seconds:
                raise RuntimeError(
                    f"Gemini has been stuck processing '{filename}' for over "
                    f"{max_wait_seconds}s without finishing. This usually means "
                    f"a transient issue on Gemini's side — try again."
                )
            time.sleep(0.5)
            waited += 0.5
            remote_file = client.files.get(name=remote_file.name)

        state_name = getattr(remote_file.state, "name", "")
        if state_name and state_name not in {"ACTIVE", "SUCCEEDED"}:
            raise RuntimeError(
                f"Gemini could not finish processing '{filename}'. "
                f"File state: {state_name}"
            )

        return remote_file
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def classroom_attachment_ids(submission: dict):
    attachments = (
        submission.get("assignmentSubmission", {})
        .get("attachments", [])
    )

    ids = []
    for attachment in attachments:
        drive_file = attachment.get("driveFile")
        if drive_file and drive_file.get("id"):
            ids.append(drive_file["id"])

    return ids


def mark_one_classroom_submission(
    token: str,
    client,
    submission: dict,
    coursework: dict,
    mark_scheme_remote,
    audit_enabled: bool,
    status_callback=None,
):
    """
    Mark one Classroom submission's attachment(s) and return a result dict.
    Guaranteed never to raise — ANY failure, expected or not, is captured in
    the returned dict's "status"/"reason" fields instead, via the outer
    try/except below. This matters because one submission's problem must
    never abort the whole batch (and silently lose everyone processed
    before it) in the caller's loop.

    status_callback, if given, is called with short progress strings (e.g.
    st.write) as each stage starts. Only pass one when this function is
    called sequentially from the main thread — Streamlit calls aren't safe
    from a worker thread, so leave this as None if this is ever run via
    ThreadPoolExecutor again.
    """
    # Grabbed defensively, outside the main try, so even a malformed
    # `submission` dict still gives the caller an identifiable failure row
    # instead of a bare exception.
    submission_id = submission.get("id", "unknown-submission")
    student_id = submission.get("userId", "Unknown student")
    student_name = student_id

    try:
        return _mark_one_classroom_submission_inner(
            token,
            client,
            submission,
            coursework,
            mark_scheme_remote,
            audit_enabled,
            status_callback,
            submission_id,
            student_id,
        )
    except Exception as unexpected_error:
        return {
            "submission_id": submission_id,
            "student": student_name,
            "status": "Failed",
            "reason": f"Unexpected error: {unexpected_error}",
            "audit_warning": None,
            "result": None,
            "totals": None,
            "groups": None,
            "marking_model": None,
            "audit_model": None,
            "suggested_classroom_grade": None,
        }


def _mark_one_classroom_submission_inner(
    token,
    client,
    submission,
    coursework,
    mark_scheme_remote,
    audit_enabled,
    status_callback,
    submission_id,
    student_id,
):
    started_at = time.time()

    def report(message):
        if status_callback:
            status_callback(message)

    try:
        profile = get_user_profile(token, student_id)
        student_name = profile.get("name", {}).get("fullName", student_id)
    except Exception:
        student_name = student_id

    attachment_ids = classroom_attachment_ids(submission)

    if not attachment_ids:
        return {
            "submission_id": submission_id,
            "student": student_name,
            "status": "Skipped",
            "reason": "No Google Drive attachment found.",
            "audit_warning": None,
            "result": None,
            "totals": None,
            "groups": None,
            "marking_model": None,
            "audit_model": None,
            "suggested_classroom_grade": None,
        }

    remote_students = []
    try:
        for file_id in attachment_ids:
            filename, data, mime = download_drive_file(token, file_id)

            # For now, safely accept PDFs and common images.
            # Unsupported Drive types are skipped with an explanation.
            supported = (
                mime == "application/pdf"
                or mime.startswith("image/")
                or filename.lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".webp"))
            )

            if not supported:
                raise RuntimeError(
                    f"Unsupported submitted file type: {mime} ({filename})"
                )

            report(f"⬆️ Uploading **{student_name}**'s {filename} to Gemini...")
            remote_students.append(upload_bytes_to_gemini(client, data, filename))

        report(f"🧠 Marking **{student_name}**'s submission against the mark scheme...")

        raw_result, marking_model = grade_with_model(
            client=client,
            model_names=PRIMARY_MODELS,
            student_file=remote_students,
            mark_scheme_file=mark_scheme_remote,
            question_file=None,
            status_callback=status_callback,
        )

        result = normalise_result(raw_result)
        audit_model = "Not run"
        audit_warning = None

        if audit_enabled:
            report(f"🔎 Second-pass audit for **{student_name}**...")
            audit_candidates = [
                m for m in AUDIT_MODELS if m != marking_model
            ] + [
                m for m in PRIMARY_MODELS
                if m != marking_model and m not in AUDIT_MODELS
            ]
            # The audit is a bonus check, so fail fast: try at most two models.
            audit_candidates = usable_models(audit_candidates)[:2]

            try:
                audited, audit_model = audit_result(
                    client=client,
                    result=result,
                    student_file=remote_students,
                    mark_scheme_file=mark_scheme_remote,
                    model_names=audit_candidates,
                    status_callback=status_callback,
                )
                result = normalise_result(audited)
            except Exception as audit_error:
                audit_warning = str(audit_error)

        earned, available, percentage = calculate_totals(result)
        provisional_grade = percentage_band_grade(percentage)
        groups = make_group_rows(result["questions"])

        max_points = coursework.get("maxPoints")
        suggested_classroom_grade = None
        if max_points and float(max_points) > 0 and available:
            suggested_classroom_grade = round(
                (earned / available) * float(max_points), 2
            )

        return {
            "submission_id": submission_id,
            "student": student_name,
            "status": "Marked",
            "reason": None,
            "audit_warning": audit_warning,
            "score": f"{earned}/{available}",
            "percentage": f"{percentage:.2f}%",
            "seconds": round(time.time() - started_at),
            "suggested_classroom_grade": suggested_classroom_grade,
            "marking_model": marking_model,
            "audit_model": audit_model,
            "questions": len(result["questions"]),
            "result": result,
            "totals": {
                "earned": earned,
                "available": available,
                "percentage": percentage,
                "provisional_grade": provisional_grade,
            },
            "groups": groups,
        }

    except Exception as mark_error:
        return {
            "submission_id": submission_id,
            "student": student_name,
            "status": "Failed",
            "reason": str(mark_error),
            "audit_warning": None,
            "result": None,
            "totals": None,
            "groups": None,
            "marking_model": None,
            "audit_model": None,
            "suggested_classroom_grade": None,
        }

    finally:
        for remote in remote_students:
            try:
                client.files.delete(name=remote.name)
            except Exception:
                pass


def run_classroom_marking_job(
    token: str,
    client,
    course_id: str,
    coursework: dict,
    mark_scheme_upload,
    audit_enabled: bool,
    already_processed_ids: set,
):
    """
    Find TURNED_IN submissions not yet processed this session and mark their
    attachments. This does NOT write grades back to Classroom: the Classroom
    API only allows an app to modify (grade) courseWork that the SAME OAuth
    project created, so any attempt to write a draft/assigned grade to an
    assignment created in the Classroom web UI fails with a 403
    ProjectPermissionDenied error. Scores are surfaced in the UI/report
    instead, for the teacher to enter into the gradebook.

    Submissions are marked one at a time, in order — never two submissions'
    Gemini calls in flight simultaneously — so this is slower than running
    everyone in parallel, but it's predictable and keeps request volume low.

    `already_processed_ids` is a set of submission IDs already handled in
    this session; the caller is responsible for persisting/updating it
    (there is no Classroom-side signal we can use instead, since we never
    set a grade).
    """
    st.write("📋 Checking Classroom for turned-in submissions...")

    submissions = list_turned_in_submissions(
        token,
        course_id,
        coursework["id"],
    )

    pending = [
        s for s in submissions
        if s.get("state") == "TURNED_IN"
        and s["id"] not in already_processed_ids
    ]

    if not pending:
        return {
            "pending": 0,
            "processed": [],
            "message": "No new turned-in submissions found.",
        }

    st.write(f"📌 Found {len(pending)} new submission(s) to mark.")

    mark_scheme_remote = upload_pdf(client, mark_scheme_upload, status_callback=st.write)

    # Persisted incrementally below, not just once at the very end — so if
    # anything anywhere throws partway through, whatever was already marked
    # is still visible instead of vanishing along with the crashed run.
    live_result = {
        "pending": len(pending),
        "processed": [],
        "message": "In progress...",
    }
    st.session_state["classroom_last_result"] = live_result
    processed = live_result["processed"]

    try:
        completed_count = 0

        for submission in pending:
            completed_count += 1

            # A single-worker executor here isn't for concurrency (still
            # exactly one submission at a time) — it's what lets the main
            # loop stop WAITING on a stalled call after
            # PER_SUBMISSION_TIMEOUT_SECONDS, even though the call itself
            # has no way to be forcibly killed. Progress messages go through
            # a queue instead of calling st.write directly, since st.write
            # from a worker thread isn't safe — only this main thread reads
            # the queue and prints them.
            progress_queue = queue.Queue()

            try:
                submission_executor = ThreadPoolExecutor(max_workers=1)
                future = submission_executor.submit(
                    mark_one_classroom_submission,
                    token,
                    client,
                    submission,
                    coursework,
                    mark_scheme_remote,
                    audit_enabled,
                    progress_queue.put,
                )

                try:
                    row = future.result(timeout=PER_SUBMISSION_TIMEOUT_SECONDS)
                except FuturesTimeoutError:
                    row = {
                        "submission_id": submission.get("id", "unknown-submission"),
                        "student": submission.get("userId", "Unknown student"),
                        "status": "Failed",
                        "reason": (
                            f"Gave up after {PER_SUBMISSION_TIMEOUT_SECONDS}s "
                            f"across all model attempts — will retry next check."
                        ),
                        "audit_warning": None,
                        "result": None,
                        "totals": None,
                        "groups": None,
                        "marking_model": None,
                        "audit_model": None,
                        "suggested_classroom_grade": None,
                    }
                finally:
                    # wait=False is deliberate: on a timeout, the worker
                    # thread is still running and Python has no way to force
                    # a thread to stop. shutdown(wait=True) here would block
                    # this loop until that abandoned call finally finishes —
                    # exactly what the timeout exists to avoid. The thread
                    # is left to finish (or fail) quietly on its own.
                    submission_executor.shutdown(wait=False)

                    while True:
                        try:
                            st.write(progress_queue.get_nowait())
                        except queue.Empty:
                            break
            except Exception as unexpected_error:
                # Belt-and-suspenders: mark_one_classroom_submission is
                # designed to never raise, but if it somehow does anyway,
                # one bad submission still must not lose the whole batch.
                row = {
                    "submission_id": submission.get("id", "unknown-submission"),
                    "student": submission.get("userId", "Unknown student"),
                    "status": "Failed",
                    "reason": f"Unexpected error: {unexpected_error}",
                    "audit_warning": None,
                    "result": None,
                    "totals": None,
                    "groups": None,
                    "marking_model": None,
                    "audit_model": None,
                    "suggested_classroom_grade": None,
                }

            if row.get("audit_warning"):
                st.warning(
                    f"Audit failed for submission {row['submission_id']}; "
                    f"the first-pass result was kept. {row['audit_warning']}"
                )

            if row["status"] == "Marked":
                st.write(
                    f"✅ [{completed_count}/{len(pending)}] Finished marking "
                    f"**{row['student']}** — {row['score']} ({row['percentage']})"
                    f" · took {row.get('seconds', '?')}s"
                )
            elif row["status"] == "Skipped":
                st.write(
                    f"⏭️ [{completed_count}/{len(pending)}] Skipped "
                    f"**{row['student']}** — {row['reason']}"
                )
            else:
                st.write(
                    f"⚠️ [{completed_count}/{len(pending)}] Failed to mark "
                    f"**{row['student']}** — {row['reason']}"
                )

            processed.append(row)
            # processed IS live_result["processed"] (same list object), so
            # session_state already reflects this submission immediately —
            # no separate write-back needed.

            # Skipped/Marked are considered handled; a Failed submission
            # (e.g. a transient Gemini error) is retried on the next check.
            if row["status"] in ("Marked", "Skipped"):
                already_processed_ids.add(row["submission_id"])

        marked_n = sum(1 for r in processed if r["status"] == "Marked")
        skipped_n = sum(1 for r in processed if r["status"] == "Skipped")
        failed_n = sum(1 for r in processed if r["status"] == "Failed")

        st.write(
            f"🏁 Batch finished: {marked_n} marked, {skipped_n} skipped, "
            f"{failed_n} failed to mark — {len(processed)} of {len(pending)} "
            f"total attempted."
        )
        if failed_n:
            st.caption(
                f"Failed submissions aren't marked as done, so they'll be "
                f"retried automatically the next time you check."
            )

        live_result["message"] = f"Processed {len(processed)} submission(s)."
        return live_result

    finally:
        try:
            client.files.delete(name=mark_scheme_remote.name)
        except Exception:
            pass


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


def is_timeout_error(exc: Exception) -> bool:
    """True when a request ran out of time (retrying the same model would just
    burn another full timeout, so callers move straight to the next model)."""
    message = str(exc).lower()
    return "timed out" in message or "timeout" in message


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

    student_file may be one Gemini file or a list of Gemini files. Lists are
    useful for Google Classroom submissions containing multiple pages/files.
    """
    prompt = """
Mark the student's work in the uploaded file(s) against the uploaded mark scheme.

The student file(s) may contain multiple pages/files belonging to one submission.
Treat them as ONE student's complete submission.

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

Do not calculate the final total as a free-form narrative. The application will calculate it from the individual question marks.
"""

    if isinstance(student_file, (list, tuple)):
        student_files = list(student_file)
    else:
        student_files = [student_file]

    contents_base = list(student_files) + [mark_scheme_file]
    if question_file is not None:
        contents_base.append(question_file)
    contents_base.append(prompt)

    errors = []

    all_models = list(model_names)
    model_names = usable_models(all_models)
    skipped = [m for m in all_models if m not in model_names]
    if skipped and status_callback:
        status_callback(
            "⏭️ Skipping models that recently failed: " + ", ".join(skipped)
        )

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
                    contents=contents_base,
                    config=types.GenerateContentConfig(
                        system_instruction=MARKING_SYSTEM,
                        response_mime_type="application/json",
                        response_schema=OUTPUT_SCHEMA,
                    ),
                )

                if response and response.text:
                    parsed = extract_json(response.text)
                    record_model_success(model_name)
                    return parsed, model_name

                raise RuntimeError(f"{model_name} returned an empty response.")

            except Exception as exc:
                errors.append(f"{model_name}: {exc}")

                if is_timeout_error(exc):
                    record_model_failure(model_name, exc)
                    break  # don't spend another full timeout on the same model

                if is_temporary_model_error(exc) and attempt == 0:
                    time.sleep(2)
                    continue

                record_model_failure(model_name, exc)
                break

    raise RuntimeError(
        "All configured Gemini models failed. "
        + " | ".join(errors)
    )


def audit_result(
    client,
    result,
    student_file,
    mark_scheme_file,
    model_names,
    status_callback=None,
    attempts_per_model=1,
):
    """
    Independent second pass. Supports multiple student files/pages.
    """
    audit_prompt = f"""
You are performing a second-pass audit of an automated mathematics marking report.

Compare the report below against the actual student submission file(s) and the
official mark scheme. Correct any question-level marks that are not supported
by the mark scheme. Pay particular attention to:
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

    if isinstance(student_file, (list, tuple)):
        student_files = list(student_file)
    else:
        student_files = [student_file]

    contents = student_files + [mark_scheme_file, audit_prompt]
    errors = []

    model_names = usable_models(list(model_names))

    for model_name in model_names:
        for attempt in range(attempts_per_model):
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
                    parsed = extract_json(response.text)
                    record_model_success(model_name)
                    return parsed, model_name

                raise RuntimeError(f"{model_name} returned an empty response.")

            except Exception as exc:
                errors.append(f"{model_name}: {exc}")

                if is_timeout_error(exc):
                    record_model_failure(model_name, exc)
                    break

                if is_temporary_model_error(exc) and attempt < attempts_per_model - 1:
                    time.sleep(2)
                    continue

                record_model_failure(model_name, exc)
                break

    raise RuntimeError("Audit models failed. " + " | ".join(errors))


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

    cloud_key_configured = False
    try:
        cloud_key_configured = bool(st.secrets.get("GEMINI_API_KEY", ""))
    except Exception:
        cloud_key_configured = False

    if cloud_key_configured:
        api_key = str(st.secrets["GEMINI_API_KEY"]).strip()
        st.success("🔐 Gemini API key loaded from Streamlit Secrets")
        st.caption("The API key is hidden from app users.")
    else:
        api_key = st.text_input(
            "Gemini API key",
            value=saved_key,
            type="password",
            help="For local use, enter a Gemini API key. For Community Cloud, "
                 "put GEMINI_API_KEY in Streamlit Secrets instead.",
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

            if remember_key and not cloud_key_configured:
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
    st.caption("Automatic Gemini model fallback: ON")

    _benched = benched_models()
    if _benched:
        st.caption(
            "⏸️ Skipping for now (recently failed): "
            + ", ".join(
                f"{m} ({'retired' if kind == 'unavailable' else 'busy'}"
                + ("" if kind == "unavailable" else f", retry in ~{max(1, secs // 60)} min")
                + ")"
                for m, (kind, secs) in _benched.items()
            )
        )
        if st.button("Retry all models now", use_container_width=True):
            reset_model_health()
            st.rerun()
    st.caption("Google Classroom integration: available when configured")

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


# ============================================================
# GOOGLE CLASSROOM
# ============================================================

with st.expander("🎓 Google Classroom — automatic marking & grade return", expanded=True):
    st.markdown(
        """
        **Workflow:** connect your Google account → choose your class →
        choose an assignment → upload its mark scheme → find turned-in work →
        mark automatically → save a Classroom draft grade or return it to the student.
        """
    )

    classroom_ok, classroom_problems = classroom_auth_status()

    if not classroom_ok:
        st.warning(
            "Google Classroom is not fully configured yet. Fix the following "
            "in your Streamlit secrets, then restart the app:"
        )
        for problem in classroom_problems:
            st.caption(f"• {problem}")
        st.caption(
            "The normal PDF marker below will still work with GEMINI_API_KEY."
        )
    else:
        auth_col1, auth_col2 = st.columns([3, 1])

        with auth_col1:
            if google_logged_in():
                email = getattr(st.user, "email", "")
                name = getattr(st.user, "name", "")
                st.success(
                    f"✅ Google Classroom connected"
                    + (f" — {name}" if name else "")
                    + (f" ({email})" if email else "")
                )
            else:
                st.info(
                    "Connect your Google account to let MathsMark AI read your "
                    "teacher Classroom submissions and write grades."
                )

        with auth_col2:
            if google_logged_in():
                if st.button("Log out", use_container_width=True, key="classroom_logout"):
                    st.logout()
            else:
                if st.button("🔗 Connect Google Classroom", type="primary", use_container_width=True):
                    st.login("google")

        if google_logged_in():
            token = get_google_access_token()

            if not token:
                st.error(
                    "Google login succeeded, but no API access token is available. "
                    "Make sure expose_tokens includes 'access' and reconnect."
                )
            else:
                try:
                    courses = list_teacher_courses(token)

                    if not courses:
                        st.warning(
                            "No active Google Classroom courses where this account "
                            "is a teacher were returned."
                        )
                    else:
                        course_labels = {
                            c["id"]: f"{c.get('name', 'Unnamed course')} "
                                      f"({c.get('section', '').strip()})".strip()
                            for c in courses
                        }

                        selected_course_id = st.selectbox(
                            "1. Choose your class",
                            options=list(course_labels.keys()),
                            format_func=lambda x: course_labels[x],
                            key="classroom_course_id",
                        )

                        assignments = list_assignments(
                            token,
                            selected_course_id,
                        )

                        if not assignments:
                            st.warning(
                                "No published assignments were found in this class."
                            )
                            selected_course = None
                        else:
                            assignment_labels = {
                                a["id"]: (
                                    f"{a.get('title', 'Untitled assignment')} "
                                    f"— {a.get('maxPoints', 0)} points"
                                )
                                for a in assignments
                            }

                            selected_assignment_id = st.selectbox(
                                "2. Choose the assignment",
                                options=list(assignment_labels.keys()),
                                format_func=lambda x: assignment_labels[x],
                                key="classroom_assignment_id",
                            )
                            selected_course = next(
                                c for c in courses if c["id"] == selected_course_id
                            )
                            selected_assignment = next(
                                a for a in assignments if a["id"] == selected_assignment_id
                            )

                        if selected_course is not None:
                            ms_upload = st.file_uploader(
                                "3. Upload the official mark scheme PDF for this assignment",
                                type=["pdf"],
                                key="classroom_mark_scheme",
                            )

                            st.caption(
                                "Note: this tool cannot write grades into Classroom's gradebook — "
                                "Google's Classroom API only lets an app grade coursework that the "
                                "same app created, and these assignments were created in the "
                                "Classroom website. Marks are shown below for you to enter manually."
                            )

                            auto_col1, auto_col2 = st.columns(2)

                            with auto_col1:
                                classroom_audit = st.checkbox(
                                    "Second-pass audit",
                                    value=True,
                                    key="classroom_audit",
                                )

                            with auto_col2:
                                auto_poll = st.checkbox(
                                    "Auto-check every 60 seconds while this page is open",
                                    value=False,
                                    key="classroom_auto_poll",
                                )

                            process_now = st.button(
                                "🚀 Check and mark new submissions",
                                type="primary",
                                use_container_width=True,
                                disabled=not bool(ms_upload),
                                key="classroom_process_now",
                            )

                            job_request = process_now

                            # Submissions this app has already marked this session. There's no
                            # Classroom-side "already graded" signal to rely on any more (we
                            # never write a grade), so this session-only set is what prevents
                            # re-marking the same submission on every check.
                            processed_ids = st.session_state.setdefault(
                                "classroom_processed_ids", set()
                            )

                            # Automatic polling while the active Streamlit session remains open.
                            if auto_poll and hasattr(st, "fragment"):
                                @st.fragment(run_every="60s")
                                def classroom_auto_runner():
                                    st.caption("🔄 Auto-check is active — checking for new turned-in work.")

                                    if not ms_upload:
                                        st.warning(
                                            "⚠️ Auto-check is paused: the mark scheme PDF "
                                            "upload above is empty (it may have been cleared "
                                            "by a page reload). Re-upload it to resume."
                                        )
                                        return

                                    try:
                                        poll_client = make_gemini_client(api_key)

                                        poll_result = run_classroom_marking_job(
                                            token=token,
                                            client=poll_client,
                                            course_id=selected_course_id,
                                            coursework=selected_assignment,
                                            mark_scheme_upload=ms_upload,
                                            audit_enabled=classroom_audit,
                                            already_processed_ids=processed_ids,
                                        )

                                        if poll_result["processed"]:
                                            st.success(
                                                f"✅ Auto-check processed "
                                                f"{len(poll_result['processed'])} new submission(s)."
                                            )
                                            st.session_state["classroom_last_result"] = poll_result
                                        else:
                                            st.caption(poll_result["message"])
                                    except Exception as auto_error:
                                        st.error(
                                            "Automatic Classroom check failed: "
                                            f"{auto_error}"
                                        )

                                classroom_auto_runner()

                            if job_request:
                                if not api_key:
                                    st.error(
                                        "GEMINI_API_KEY is not configured. Add it to "
                                        "Streamlit Secrets before running Classroom marking."
                                    )
                                else:
                                    try:
                                        classroom_client = make_gemini_client(api_key)

                                        with st.status(
                                            "Processing new Google Classroom submissions...",
                                            expanded=False,
                                        ) as classroom_status:
                                            result = run_classroom_marking_job(
                                                token=token,
                                                client=classroom_client,
                                                course_id=selected_course_id,
                                                coursework=selected_assignment,
                                                mark_scheme_upload=ms_upload,
                                                audit_enabled=classroom_audit,
                                                already_processed_ids=processed_ids,
                                            )

                                            classroom_status.update(
                                                label="✅ Classroom processing complete",
                                                state="complete",
                                                expanded=False,
                                            )

                                        st.session_state["classroom_last_result"] = result

                                    except Exception as classroom_error:
                                        message = str(classroom_error)
                                        if "401" in message:
                                            st.error(
                                                "Google authorization expired or was revoked. "
                                                "Log out and reconnect Google Classroom."
                                            )
                                        elif "403" in message:
                                            st.error(
                                                "Google denied Classroom/Drive access. "
                                                "Check the OAuth scopes, Google Cloud APIs, "
                                                "and that this account is a teacher in the selected class."
                                            )
                                        else:
                                            st.error(
                                                f"Classroom marking failed: {message}"
                                            )

                                        with st.expander("Technical error details"):
                                            st.code(message)

                            # Render the most recent batch (persists across reruns, e.g. from
                            # expanding a report below, without re-hitting the Classroom API).
                            last_result = st.session_state.get("classroom_last_result")

                            if last_result and last_result["processed"]:
                                processed_rows = last_result["processed"]

                                # A run that never reached its normal completion (e.g. the
                                # script was interrupted mid-loop by a Streamlit-internal
                                # rerun signal, which bypasses ordinary exception handling)
                                # leaves this message stuck at "In progress..." forever,
                                # with fewer processed rows than the pending count found.
                                if (
                                    last_result.get("message") == "In progress..."
                                    and len(processed_rows) < last_result.get("pending", 0)
                                ):
                                    st.warning(
                                        f"This batch stopped after {len(processed_rows)} of "
                                        f"{last_result['pending']} submission(s) — it looks like "
                                        f"it was interrupted rather than finished. The results "
                                        f"below are genuine and saved, but the remaining "
                                        f"submission(s) haven't been attempted yet. Click "
                                        f"'Check and mark new submissions' again to pick up "
                                        f"where it left off."
                                    )

                                st.subheader("📬 Classroom processing result")

                                summary_rows = []
                                for row in processed_rows:
                                    summary_rows.append(
                                        {
                                            "Student": row["student"],
                                            "Status": row["status"],
                                            "Score": row.get("score", "—"),
                                            "Percentage": row.get("percentage", "—"),
                                            "Time (s)": row.get("seconds", "—"),
                                            "Suggested Classroom grade": (
                                                row["suggested_classroom_grade"]
                                                if row.get("suggested_classroom_grade") is not None
                                                else "—"
                                            ),
                                            "Note": row.get("reason") or "",
                                        }
                                    )

                                st.dataframe(
                                    pd.DataFrame(summary_rows),
                                    use_container_width=True,
                                    hide_index=True,
                                )

                                st.info(
                                    "Scores are not written to Classroom. Enter the score or "
                                    "'Suggested Classroom grade' shown above into the gradebook "
                                    "yourself for each student."
                                )

                                marked_rows = [r for r in processed_rows if r["status"] == "Marked"]

                                if marked_rows:
                                    st.subheader("🧑‍🎓 Individual student reports")

                                    for row in marked_rows:
                                        with st.expander(
                                            f"{row['student']} — {row['score']} ({row['percentage']})"
                                        ):
                                            render_student_report(
                                                row["student"],
                                                row["result"],
                                                row["totals"],
                                                row["groups"],
                                                row["marking_model"],
                                                row["audit_model"],
                                            )
                            elif last_result:
                                st.info(last_result["message"])

                except Exception as classroom_load_error:
                    st.error(
                        f"Could not load Google Classroom data: {classroom_load_error}"
                    )

st.subheader("📁 Upload files")

col1, col2 = st.columns(2)

with col1:
    st.markdown("### 1. Student submission(s)")
    student_pdfs = st.file_uploader(
        "Student PDF(s)",
        type=["pdf"],
        accept_multiple_files=True,
        key="student_pdf_uploader",
        help="Upload one PDF per student. Include all pages, workings and diagrams. "
             "Every student here is marked against the same mark scheme below.",
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

ready = bool(student_pdfs and mark_scheme_pdf and api_key)

if not ready:
    st.warning(
        "Upload at least one student PDF, the mark scheme PDF, and enter/define "
        "a Gemini API key to start."
    )

if st.button(
    "🚀 Mark Homework Automatically",
    type="primary",
    use_container_width=True,
    disabled=not ready,
):
    client = None
    uploaded_remote_files = []
    batch_results = []

    try:
        client = make_gemini_client(api_key)

        with st.status("Preparing automatic marking...", expanded=False) as status:
            st.write(
                f"⚡ Uploading mark scheme, question paper (if any), and "
                f"{len(student_pdfs)} student PDF(s) to Gemini..."
            )

            files_to_upload = {"mark_scheme": mark_scheme_pdf}
            if question_pdf:
                files_to_upload["question"] = question_pdf
            for i, sp in enumerate(student_pdfs):
                files_to_upload[f"student_{i}"] = sp

            upload_jobs = {}
            with ThreadPoolExecutor(max_workers=min(8, len(files_to_upload))) as executor:
                futures = {
                    name: executor.submit(upload_pdf, client, upload)
                    for name, upload in files_to_upload.items()
                }

                for name, future in futures.items():
                    upload_jobs[name] = future.result()
                    uploaded_remote_files.append(upload_jobs[name])

            mark_scheme_remote = upload_jobs["mark_scheme"]
            question_remote = upload_jobs.get("question")

            # Mark every student CONCURRENTLY instead of one at a time — the
            # Gemini calls are independent per student, so this is the single
            # biggest lever for wall-clock speed on a batch. Worker threads
            # never touch Streamlit directly (see mark_one_submission); this
            # main thread reports progress as each one finishes.
            max_workers = min(4, len(student_pdfs))
            st.write(
                f"🧠 Marking {len(student_pdfs)} student(s) with up to "
                f"{max_workers} running at once..."
            )

            with ThreadPoolExecutor(max_workers=max_workers) as marking_executor:
                marking_futures = {
                    marking_executor.submit(
                        mark_one_submission,
                        client,
                        sp.name,
                        upload_jobs[f"student_{i}"],
                        mark_scheme_remote,
                        question_remote,
                        audit_enabled,
                    ): sp.name
                    for i, sp in enumerate(student_pdfs)
                }

                completed = 0
                for future in as_completed(marking_futures):
                    sp_name = marking_futures[future]
                    completed += 1

                    try:
                        row = future.result()

                        if row.get("audit_warning"):
                            st.warning(
                                f"Second-pass audit could not be completed for "
                                f"{sp_name}, so the first-pass marking was kept. "
                                f"Reason: {row['audit_warning']}"
                            )

                        batch_results.append(row)
                        st.write(
                            f"✅ [{completed}/{len(student_pdfs)}] Marked {sp_name}"
                        )

                    except Exception as student_error:
                        st.warning(
                            f"⚠️ [{completed}/{len(student_pdfs)}] Marking failed "
                            f"for {sp_name}: {student_error}"
                        )
                        batch_results.append(
                            {
                                "student_name": sp_name,
                                "status": "Failed",
                                "error": str(student_error),
                                "result": None,
                                "totals": None,
                                "groups": None,
                                "marking_model": None,
                                "audit_model": None,
                            }
                        )

            # Store for download/re-render.
            st.session_state["last_batch_results"] = batch_results
            st.session_state["last_mark_scheme_name"] = mark_scheme_pdf.name

            marked_count = sum(1 for r in batch_results if r["status"] == "Marked")
            status.update(
                label=f"✅ Marking complete — {marked_count}/{len(student_pdfs)} student(s) marked",
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
                "The Gemini project is refusing the key. Check the key's "
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



if "last_batch_results" in st.session_state:
    batch_results = st.session_state["last_batch_results"]
    marked_results = [r for r in batch_results if r["status"] == "Marked"]

    st.divider()
    st.header("🏫 Class summary")

    summary_rows = []
    for r in batch_results:
        if r["status"] == "Marked":
            summary_rows.append(
                {
                    "Student": r["student_name"],
                    "Score": f"{r['totals']['earned']} / {r['totals']['available']}",
                    "Percentage": f"{r['totals']['percentage']:.2f}%",
                    "Provisional grade": r["totals"]["provisional_grade"],
                    "Status": "✅ Marked",
                }
            )
        else:
            summary_rows.append(
                {
                    "Student": r["student_name"],
                    "Score": "—",
                    "Percentage": "—",
                    "Provisional grade": "—",
                    "Status": f"❌ Failed: {r['error']}",
                }
            )

    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

    if len(batch_results) > len(marked_results):
        st.warning(
            f"{len(batch_results) - len(marked_results)} of {len(batch_results)} "
            f"student PDF(s) could not be marked. See the Status column above and "
            f"the individual report below for details."
        )

    # ------------------------------------------------------------
    # Combined downloads across the whole class
    # ------------------------------------------------------------
    if marked_results:
        st.header("⬇️ Export (whole class)")

        csv_rows = []
        for r in marked_results:
            csv_rows.extend(build_csv_rows(r["student_name"], r["result"]))
        csv_data = pd.DataFrame(csv_rows).to_csv(index=False)

        combined_report = "\n\n---\n\n".join(
            build_markdown_report(r["student_name"], r["result"], r["totals"])
            for r in marked_results
        )

        col_a, col_b = st.columns(2)

        with col_a:
            st.download_button(
                "📄 Download combined Markdown report",
                data=combined_report,
                file_name="igcse_maths_marking_report_class.md",
                mime="text/markdown",
                use_container_width=True,
            )

        with col_b:
            st.download_button(
                "📊 Download combined question marks CSV",
                data=csv_data,
                file_name="igcse_maths_question_marks_class.csv",
                mime="text/csv",
                use_container_width=True,
            )

    # ------------------------------------------------------------
    # Individual per-student reports
    # ------------------------------------------------------------
    st.header("🧑‍🎓 Individual student reports")

    for idx, r in enumerate(batch_results):
        label = r["student_name"]
        if r["status"] == "Marked":
            label += (
                f" — {r['totals']['earned']}/{r['totals']['available']} "
                f"({r['totals']['percentage']:.2f}%)"
            )
        else:
            label += " — ❌ Failed"

        with st.expander(label, expanded=(len(batch_results) == 1)):
            if r["status"] == "Marked":
                render_student_report(
                    r["student_name"],
                    r["result"],
                    r["totals"],
                    r["groups"],
                    r["marking_model"],
                    r["audit_model"],
                )

                st.download_button(
                    "📄 Download this student's Markdown report",
                    data=build_markdown_report(r["student_name"], r["result"], r["totals"]),
                    file_name=f"igcse_maths_report_{r['student_name']}.md",
                    mime="text/markdown",
                    use_container_width=True,
                    key=f"dl_md_{idx}_{r['student_name']}",
                )
            else:
                st.error(f"Marking failed for this student: {r['error']}")

st.divider()
st.caption(
    "Important: this is an AI-assisted marking tool. It can make mistakes, especially "
    "with blurry handwriting, ambiguous diagrams, or unusual student methods. "
    "The official mark scheme remains the authority."
)
