# Frozen production functions from main 78230becbdfb8dcd3a9141c974823a083e623286 (PR8).
def prepare_manual_batch(client, students, scheme, question, audit, random_count, session, run, report=None):
    """Resolve successful result hits BEFORE uploading student/reference documents."""
    started = time.monotonic()
    def event(label, stage, elapsed, done=True):
        if report:
            report(label, {"stage": stage, "elapsed": elapsed, "done": done})
    scope = session["files"].scope
    scheme_data = scheme.getvalue()
    question_data = question.getvalue() if question else None
    scheme_hash = documents.digest(scheme_data)
    question_hash = documents.digest(question_data) if question_data is not None else "none"
    config = (APP_VERSION, tuple(PRIMARY_MODELS), tuple(AUDIT_MODELS))
    reference_key = (scope, "manual", scheme_hash, question_hash, config)
    metadata = session["metadata"]
    results = session["results"]
    remotes = {}
    event("References", "Content hashing", time.monotonic() - started)

    def acquire(label, data, filename, owner, reference=False):
        callback = (lambda update: report(label, update)) if report else None
        return run.acquire(data, filename, owner, reference, callback)

    def references():
        if "mark_scheme" not in remotes:
            remotes["mark_scheme"] = acquire("Mark scheme", scheme_data, scheme.name, "manual:scheme", True)
            if question:
                remotes["question"] = acquire("Question paper", question_data, question.name, "manual:question", True)

    selected = None
    if random_count is not None:
        numbers = metadata.get((reference_key, "question_numbers"))
        if numbers is None:
            references()
            tick = time.monotonic()
            event("References", "Identify question numbers", 0, False)
            numbers = discover_question_numbers(client, remotes["mark_scheme"], remotes.get("question"))
            metadata.put((reference_key, "question_numbers"), numbers)
            event("References", "Identify question numbers", time.monotonic() - tick)
        selected = select_spread_questions(numbers, random_count)

    started = time.monotonic()
    keys, cached, pending = {}, {}, []
    for index, student in enumerate(students):
        key = (reference_key, index, student.name, documents.digest(student.getvalue()),
               bool(audit), tuple(selected) if selected is not None else None)
        keys[index] = key
        row = results.get(key)
        if row is not None and row.get("status") == "Marked" and not row.get("audit_warning"):
            cached[index] = row
        else:
            pending.append(index)
    event("Batch", "Hash and result-cache lookup", time.monotonic() - started)
    scheme_info = (None, None, None)
    if pending:
        # Resolve cache hits first, then overlap at most two required uploads.
        # Wait for every worker before handing cleanup back to DocumentRun.close().
        jobs = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            if "mark_scheme" not in remotes:
                jobs[executor.submit(acquire, "Mark scheme", scheme_data, scheme.name, "manual:scheme", True)] = "mark_scheme"
                if question:
                    jobs[executor.submit(acquire, "Question paper", question_data, question.name, "manual:question", True)] = "question"
            for index in pending:
                student = students[index]
                jobs[executor.submit(acquire, f"Student PDF {index + 1}", student.getvalue(), student.name,
                                     f"manual:student:{index}:{student.name}")] = f"student_{index}"
            for future in as_completed(jobs):
                remotes[jobs[future]] = future.result()
        scheme_info = metadata.get((reference_key, "scheme"))
        if scheme_info is None:
            tick = time.monotonic()
            event("Mark scheme", "Prepare marking evidence", 0, False)
            scheme_info = extract_scheme_maxima(client, remotes["mark_scheme"])
            if scheme_info[0] and not scheme_info[2]:
                metadata.put((reference_key, "scheme"), scheme_info)
            event("Mark scheme", "Prepare marking evidence", time.monotonic() - tick)
        else:
            event("Mark scheme", "Reused extracted evidence", 0)
    else:
        event("Batch", "All student results reused" if remotes else "All results reused; no Gemini requests", 0)
    return {"remotes": remotes, "scheme_info": scheme_info, "selected": selected,
            "keys": keys, "cached": cached, "pending": pending}

def extract_scheme_maxima(client, mark_scheme_file):
    """One shared extraction of main homework-question totals, for review only."""
    schema = {
        "type": "object",
        "properties": {"questions": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "question_number": {"type": "string"},
                "max_marks": {"type": "integer"},
                "answer_summary": {"type": "string"},
            },
            "required": ["question_number", "max_marks", "answer_summary"],
        }}},
        "required": ["questions"],
    }
    prompt = (
        "From this official homework mark scheme, extract the TOTAL maximum marks "
        "for EACH MAIN HOMEWORK question (sum all parts). Use the worksheet's "
        "outer numbered items, NOT the embedded past-paper question identifiers. "
        "For each main question also transcribe a SHORT official answer for "
        "each subpart (e.g. (a) 10-19; (b) 42/59; (c) 1376) into answer_summary. "
        "Use empty answer_summary for unclear answers; never guess. "
        "Only report clear, supported maximum marks. JSON only."
    )
    errors = []
    for model in PRIMARY_MODELS:
        if model_on_quota_cooldown(model):
            continue
        try:
            response = client.models.generate_content(
                model=model, contents=[mark_scheme_file, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", response_schema=schema
                ),
            )
            maxima, answers, conflicts = {}, {}, set()
            for q in extract_json(response.text).get("questions", []):
                number = str(q.get("question_number", "")).strip()
                try:
                    maximum = int(q["max_marks"])
                except (ValueError, TypeError, KeyError):
                    continue
                if not number or maximum <= 0:
                    continue
                if number in maxima and maximum != maxima[number]:
                    conflicts.add(number)
                maxima[number] = maximum
                summary = str(q.get("answer_summary", "")).strip()
                if summary:
                    answers[number] = summary
            for number in conflicts:
                maxima.pop(number, None)
                answers.pop(number, None)
            if not maxima:
                raise ValueError("No readable question maximum marks.")
            return maxima, answers, None
        except Exception as exc:
            remember_model_failure(model, exc)
            errors.append(f"{model}: {exc}")
    return {}, {}, "Unable to extract mark-scheme totals; teacher verification required."

def discover_question_numbers(client, mark_scheme_file, question_file=None):
    """One small metadata call per batch; never guess how many questions exist."""
    contents = [mark_scheme_file]
    if question_file is not None:
        contents.append(question_file)
    contents.append(
        "Identify all distinct MAIN question numbers, in their original order, "
        "in this mathematics homework. Return a JSON object with one key "
        "'question_numbers' containing an array of strings. "
        "For parts (a), (b), etc., use the parent question number only. "
        "Do not include page numbers, mark codes, or example question numbers. "
        "Do not invent questions."
    )
    errors = []
    for model in PRIMARY_MODELS:
        if model_on_quota_cooldown(model):
            continue
        try:
            response = client.models.generate_content(
                model=model, contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema={
                        "type": "object",
                        "properties": {
                            "question_numbers": {
                                "type": "array", "items": {"type": "string"}
                            }
                        },
                        "required": ["question_numbers"],
                    },
                ),
            )
            numbers = extract_json(response.text).get("question_numbers", [])
            numbers = list(dict.fromkeys(str(n).strip() for n in numbers if str(n).strip()))
            if not numbers:
                raise ValueError("No homework question numbers found.")
            return numbers
        except Exception as exc:
            errors.append(f"{model}: {exc}")
            remember_model_failure(model, exc)
    raise RuntimeError("Could not identify questions for random marking. " + " | ".join(errors))

def _arithmetic_equals_issues(explanation):
    """Check numeric chains without confusing truncation with completed rounding.

    Ellipses specify a decimal prefix, not a rounded endpoint. Keep the original
    numeric value across a chain so an intermediate approximation cannot hide an
    incorrect final answer (or introduce double rounding). Unsupported symbolic
    expressions and prose are deliberately left for source-based review.
    """
    from fractions import Fraction
    operand = r"-?\d+(?:\.\d+)?"
    expr = operand + r"(?:\s*[+*/×÷xX-]\s*" + operand + r")*"
    # Bounded grouping support; never insert missing parentheses on the student's behalf.
    for _ in range(3):
        atom = r"(?:" + operand + r"|\(\s*" + expr + r"\s*\))"
        expr = atom + r"(?:\s*[+*/×÷xX-]\s*" + atom + r")*"
    prefix_decimal = r"-?\d+\.\d+(?:\.\.\.|…)"
    recurring = r"-?\d+\.\d*\(\d+\)"
    token = r"(?:" + prefix_decimal + "|" + recurring + "|" + expr + ")"
    trailing = re.compile(r"(" + token + r")\s*$")
    leading = re.compile(r"^\s*(" + token + r")(?!\d|\.\d|\s*[+*/×÷-])(?=$|[\s,;.)?!])")

    def exact_value(text):
        if _numeric_expression_value(text) is None:
            return None
        source = re.sub(r"(?<=\d)\s*[xX]\s*(?=\d)", "*", text.replace("×", "*").replace("÷", "/"))

        def evaluate(node):
            if isinstance(node, ast.Constant):
                return Fraction(ast.get_source_segment(source, node))
            if isinstance(node, ast.UnaryOp):
                value = evaluate(node.operand)
                return -value if isinstance(node.op, ast.USub) else value
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            return left / right

        return evaluate(ast.parse(source, mode="eval").body)

    def number(text):
        if len(text) > 120:
            return None, None
        repeat = re.fullmatch(r"(-?)(\d+)\.(\d*)\((\d+)\)", text)
        if repeat:
            sign, whole, initial, cycle = repeat.groups()
            # Bound conversion/work just like the numeric expression evaluator.
            value = (int(whole) + Fraction(int(initial) if initial else 0, 10 ** len(initial))
                     + Fraction(int(cycle), (10 ** len(cycle) - 1) * 10 ** len(initial)))
            if value > 10 ** 12:
                return None, None
            return (-value if sign else value), None
        partial = re.fullmatch(r"(-?\d+\.\d+)(?:\.\.\.|…)", text)
        if partial:
            value = exact_value(partial.group(1))
            if value is None:
                return None, None
            step = Fraction(1, 10 ** len(partial.group(1).split(".")[1]))
            # Truncation is towards zero, including a written negative zero.
            interval = (value - step, value) if text.startswith("-") else (value, value + step)
            return value, interval
        return exact_value(text), None

    pieces = re.split(r"(=|≈|≃)", explanation.replace("~=", "≈"))
    issues = []
    anchor = None
    anchor_interval = None
    approximate_chain = False
    for i in range(0, len(pieces) - 2, 2):
        left_piece, operator, right_piece = pieces[i:i + 3]
        if (left_piece.rstrip().endswith(("<", ">", "!", "="))
                or right_piece.lstrip().startswith("=")):
            anchor = None
            continue
        left_match = trailing.search(left_piece)
        right_match = leading.match(right_piece)
        if not left_match or not right_match:
            anchor = None
            continue
        prefix = left_piece[:left_match.start()]
        fragment = re.split(r"[;:\n]", prefix)[-1].strip()
        if (prefix and (prefix[-1].isalnum() or prefix[-1] in "_.(")
                or re.search(r"[A-Za-z]", fragment)
                or re.search(r"[A-Za-z)]\s*[+*/×÷-]\s*$", prefix)):
            anchor = None
            continue
        left_text, right_text = left_match.group(1), right_match.group(1)
        lhs, lhs_interval = number(left_text)
        rhs, rhs_interval = number(right_text)
        if lhs is None or rhs is None:
            anchor = None
            continue
        # Continue only through a whole numeric middle piece, never across prose.
        if anchor is None or left_piece.strip() != left_text:
            anchor, anchor_interval = lhs, lhs_interval
            approximate_chain = lhs_interval is not None
        approximate_chain = approximate_chain or operator != "=" or rhs_interval is not None
        if rhs_interval is not None:
            lo, hi = rhs_interval
            if anchor_interval is None:
                # Include the endpoint: an infinite tail of nines can reach it
                # (0.4999... = 0.5). Ellipses do not reveal the hidden digits.
                valid = lo <= anchor <= hi
            else:
                valid = max(lo, anchor_interval[0]) <= min(hi, anchor_interval[1])
        else:
            decimal = re.fullmatch(r"-?\d+\.(\d+)", right_text)
            tolerance = (Fraction(1, 2 * 10 ** len(decimal.group(1))) if decimal else
                         Fraction(1, 2) if approximate_chain and re.fullmatch(r"-?\d+", right_text) else 0)
            if anchor_interval is None:
                valid = abs(anchor - rhs) <= tolerance
            else:
                # Without an exact origin, only flag disjoint possible values.
                valid = (anchor_interval[0] <= rhs + tolerance
                         and anchor_interval[1] >= rhs - tolerance)
        if not valid:
            issues.append(f"{left_text.strip()} {operator} {right_text.strip()}")
        if right_piece.strip() != right_text:
            anchor = None
    return list(dict.fromkeys(issues))
