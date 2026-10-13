# Frozen grade_with_model from main 0fd4aedeeec0dec9b4039e8a478f0df2aa9a9573 (merged PR9).
def grade_with_model(
    client,
    model_names,
    student_file,
    mark_scheme_file,
    question_file=None,
    status_callback=None,
    selected_questions=None,
    scheme_maxima=None,
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
- include an M/A/B breakdown for EVERY awarded mark with visible handwriting evidence,
- student_answer must transcribe what the student wrote, not the mark scheme,
- working_summary must identify actual visible working, or say unreadable/not shown,
- do not write your own self-corrections or guessed alternate steps in working_summary; if unreadable report uncertainty instead,
- cross-check each part of the full-mark solution with the official mark scheme,
- if the answer cannot be read from the student PDF, mark Low confidence; never invent evidence,
- explain lost marks,
- provide a full-mark solution,
- flag any unreadable or genuinely ambiguous evidence,
- inspect handwriting, diagrams, graphs, annotations, and page orientation carefully,
- if image quality prevents reliable marking, mark the question confidence as Low rather than guessing.

Do not calculate the final total as a free-form narrative. The application will calculate it from the individual question marks.
"""

    if selected_questions is not None:
        selected = [str(q).strip() for q in selected_questions]
        prompt += (
            "\\nSTRICT SELECTION: Mark ONLY these main questions: "
            + ", ".join(selected)
            + ". Include all parts of each selected question. "
            "Do not mark, return, or assign scores to unselected questions. "
            "Return one question entry per selected main question if identifiable. "
            "Treat the listed question identifiers as authoritative.\\n"
        )

    # AI-extracted maxima are a guide; the original scheme is authoritative.
    if scheme_maxima:
        allowed = (
            {str(n).strip() for n in selected_questions}
            if selected_questions is not None else None
        )
        reference = {
            str(n): int(m) for n, m in scheme_maxima.items()
            if allowed is None or str(n) in allowed
        }
        prompt += (
            "\\nQUESTION MAXIMUM MARK REFERENCE (AI-extracted from the official "
            "mark scheme, not independently certified): "
            + json.dumps(reference, sort_keys=True)
            + "\\nUse these totals where they agree with the original scheme. "
            "If the original document disagrees, use its value, lower confidence, "
            "and explain the discrepancy. Never award marks just because a "
            "full-mark solution exists; check the student's actual handwriting.\\n"
        )

    if isinstance(student_file, (list, tuple)):
        student_files = list(student_file)
    else:
        student_files = [student_file]

    contents_base = list(student_files) + [mark_scheme_file]
    if question_file is not None:
        contents_base.append(question_file)
    contents_base.append(prompt)

    errors = []

    for model_name in model_names:
        if model_on_quota_cooldown(model_name):
            continue
        for attempt in range(1):
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
                    return extract_json(response.text), model_name

                raise RuntimeError(f"{model_name} returned an empty response.")

            except Exception as exc:
                errors.append(f"{model_name}: {exc}")

                remember_model_failure(model_name, exc)

                # Daily free-tier quota exhaustion will not be fixed by waiting
                # a couple of seconds or retrying this same model. Skip straight
                # to the next candidate to avoid wasting time.
                if is_daily_quota_exhausted_error(exc):
                    if status_callback:
                        status_callback(
                            f"⏭️ Skipping {model_name}: daily quota exhausted"
                        )
                    break

                # A 429 caused by a short-window quota should not trigger a
                # retry against the same model; move to the next candidate.
                if is_quota_error(exc):
                    if status_callback:
                        status_callback(
                            f"⏭️ Skipping {model_name}: Gemini quota limit reached"
                        )
                    break

                # Do not repeat a costly generation after a timeout; try the next model.
                if is_model_unavailable_error(exc) and status_callback:
                    status_callback(f"⏭️ Skipping unavailable model {model_name}")

                break

    raise RuntimeError(
        "All configured Gemini models failed. "
        + " | ".join(errors)
    )
