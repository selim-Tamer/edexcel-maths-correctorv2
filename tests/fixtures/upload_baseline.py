# PR #4 commit 6169dd9: preserved solely for offline comparison.
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

def upload_pdf(client, uploaded_file):
    """Upload a manual PDF using the same lifecycle as Classroom attachments."""
    return upload_bytes_to_gemini(client, uploaded_file.getvalue(), uploaded_file.name)


def upload_pdf_batch(client, files_to_upload):
    """Own all uploads until the whole batch succeeds, including late completions."""
    uploaded = {}
    errors = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(files_to_upload)))) as executor:
        futures = {
            executor.submit(upload_pdf, client, upload): name
            for name, upload in files_to_upload.items()
        }
        for future in as_completed(futures):
            try:
                uploaded[futures[future]] = future.result()
            except Exception as exc:
                errors.append(exc)
    if errors:
        for remote_file in uploaded.values():
            try:
                client.files.delete(name=remote_file.name)
            except Exception:
                pass
        raise errors[0]
    return uploaded



def upload_bytes_to_gemini(client, data: bytes, filename: str):
    """Transfer ownership to the caller only after remote processing succeeds."""
    suffix = os.path.splitext(filename)[1].lower() or ".bin"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    remote_file = None
    try:
        remote_file = client.files.upload(file=tmp_path)
        upload_wait_started = time.monotonic()
        upload_timeout_seconds = 180
        while getattr(getattr(remote_file, "state", None), "name", "") == "PROCESSING":
            if time.monotonic() - upload_wait_started > upload_timeout_seconds:
                raise TimeoutError(
                    f"Gemini file processing timed out after "
                    f"{upload_timeout_seconds} seconds for '{filename}'."
                )
            time.sleep(0.5)
            remote_file = client.files.get(name=remote_file.name)

        state_name = getattr(getattr(remote_file, "state", None), "name", "")
        if state_name not in {"ACTIVE", "SUCCEEDED"}:
            raise RuntimeError(
                f"Gemini could not finish processing '{filename}'. "
                f"File state: {state_name or 'unknown'}"
            )
        return remote_file
    except Exception:
        if remote_file is not None:
            try:
                client.files.delete(name=remote_file.name)
            except Exception:
                pass
        raise
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass



def prepare_manual_baseline(client, student_pdfs, mark_scheme_pdf, question_pdf, random_question_count=None):
    uploaded_remote_files = []
    files_to_upload = {"mark_scheme": mark_scheme_pdf}
    if question_pdf:
        files_to_upload["question"] = question_pdf
    for i, sp in enumerate(student_pdfs):
        files_to_upload[f"student_{i}"] = sp

    upload_jobs = upload_pdf_batch(client, files_to_upload)
    uploaded_remote_files.extend(upload_jobs.values())

    mark_scheme_remote = upload_jobs["mark_scheme"]
    question_remote = upload_jobs.get("question")
    scheme_maxima, scheme_answers, scheme_warning = extract_scheme_maxima(client, mark_scheme_remote)
    if scheme_warning:
        st.warning(scheme_warning)
    selected_questions = None
    if random_question_count is not None:
        numbers = discover_question_numbers(client, mark_scheme_remote, question_remote)
        selected_questions = select_spread_questions(numbers, random_question_count)
        st.info("Random questions for all students: " + ", ".join(selected_questions))

    return upload_jobs
