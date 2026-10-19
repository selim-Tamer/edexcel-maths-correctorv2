"""Structured mark-scheme model for the offline verification engine.

Phase C Stage 2. Investigation-only: this module is NOT imported by the
production app and imports nothing from it (no app5_1_automated.py,
marking_requests.py, Gemini SDK, Streamlit or classroom code). No network,
no AI API calls, no eval/exec of scheme content anywhere.

Purpose: give the verification engine a STRICT, validated, in-memory
representation of a mark scheme. The model layer validates only *shapes and
container types* (fail-closed, content-free error messages) and deliberately
NEVER invents values: an absent optional field stays None, unknown JSON keys
are retained verbatim in ``extras`` and never interpreted. Structural
inconsistencies (duplicates, bad codes, weight/total mismatches, missing
required fields, dependency faults) are FINDINGS reported by
``investigation.verification_engine``, not exceptions here, so that one bad
record cannot crash a whole batch and every reportable fact can be audited.

Two information channels are kept strictly separated:

- Explicit structured declarations (dependencies, follow_through flags,
  alternative group names, question totals) are the ONLY facts that
  deterministic checks may rely on.
- Natural-language prose (``note``, ``claimed_solution``) is carried verbatim
  for humans; the engine must never read dependency relationships, validity
  rules or official decisions out of prose.

Supported mark-code notation (grammar ``^(dM|M|A|B)[1-9][0-9]*ft?$``):
M, A, B, dM bases with a positive numerical level (for example M1, A1,
B2, dM1) plus an optional literal ``ft`` suffix (follow-through notation,
for example A1ft). Anything else - including unknown bases such as "X1",
level 0 like "M0", ambiguous "dA1" or lowercase "m1" - is structurally
invalid and reported as a finding rather than silently accepted.

Malformed input (wrong types, wrong containers, empty identifiers, or
oversized documents) raises :class:`SchemeModelError`, a ValueError subclass,
with a message that names the offending document position but never echoes
scheme prose, so errors can be logged safely. Optional prose and extra
fields are BOUNDED so no document content can drive unbounded processing:
``note``/``claimed_solution`` are character-limited, ``source_ref`` strings
and dicts are length/key/value-scalar limited, and every extra field must be
JSON-serializable data within a total serialized-size cap (deep nesting is
rejected rather than recursed).

Supported input contract (all keys absent/unavailable unless stated):

{
  "provenance": {"author": "...", "verified": false, ...},   # optional dict
  "questions": [                                             # required
    {
      "id": "Q1",                       # required non-empty string
      "total": 5,                       # optional int|null (=100 max)
      "diagram_dependent": false,       # optional strict bool
      "claimed_solution": null,         # optional string|null, verbatim prose
      "source_ref": null,               # optional dict|string|null, verbatim
      "criteria": [                     # required list of criterion objects
        {
          "id": "Q1-c1",                # required non-empty string
          "code": "M1",                 # optional string|null (grammar above)
          "weight": 1,                  # optional int|null (findings if <=0)
          "dependencies": [],           # optional list of criterion-id strings
          "follow_through": false,      # optional strict bool
          "alternative_group": null,    # optional string|null group name
          "note": "evidence note",      # optional string|null, verbatim prose
          "source_ref": null,           # optional dict|string|null, verbatim
        }, ...
    }, ...
  ]
}

Constraints enforced here (malformed -> SchemeModelError):
- questions/criteria are lists of dicts; ids are non-empty strings
- ``weight``/``total`` are strictly int|null (bool is not an int here);
  correctness (positive/sums) is a verification-engine finding, not here
- ``follow_through``/``diagram_dependent`` are strict bools when present
- ``dependencies`` is a list of non-empty unique-by-validation strings
- ``alternative_group`` is a non-empty string when present
- ``claimed_solution``/``note`` are strings at most NOTE_CHAR_LIMIT chars
- every *extra* key is retained verbatim in ``extras`` and otherwise ignored
- size caps: MAX_QUESTIONS/CRITERIA/DEPENDENCIES/NOTE_CHAR_LIMIT
"""
import json
import re

MAX_QUESTIONS = 100
MAX_CRITERIA_PER_QUESTION = 50
MAX_DEPENDENCIES_PER_CRITERION = 20
MAX_NOTE_CHAR_LIMIT = 4000
MAX_SOURCE_REF_STRING_CHARS = 1000
MAX_SOURCE_REF_KEYS = 20
MAX_EXTRAS_SERIALIZED_CHARS = 50_000
MAX_EXTRA_DEPTH = 32

_MARK_CODE_PATTERN = re.compile(r"^(?:(?:dM|M|A|B)[1-9][0-9]*)(?:ft)?$")
_SUPPORTED_BASES = ("M", "A", "B", "dM")


class SchemeModelError(ValueError):
    """Malformed scheme record; message names position, never scheme prose."""


def code_is_supported(code):
    """True only for codes matching the supported M/A/B/dM(+level)(+ft) grammar.

    This is the single shared definition of mark-code syntax; the engine
    reports unsupported codes as findings instead of assuming them valid.
    """
    return isinstance(code, str) and bool(_MARK_CODE_PATTERN.fullmatch(code))


def code_has_follow_through_suffix(code):
    """True for codes such as "A1ft" (follow-through declared inside the code)."""
    return code_is_supported(code) and code.endswith("ft")


def _strict_bool(value, position, field):
    if value is None:
        return None
    if type(value) is not bool:
        raise SchemeModelError(
            f"{position}: field '{field}' must be a strict bool or omitted")
    return value


def _optional_nonnegative_int(value, position, field):
    if value is None:
        return None
    if type(value) is not bool and type(value) is int:
        return value
    raise SchemeModelError(
        f"{position}: field '{field}' must be an integer or omitted")


def _verbatim_string(value, position, field, limit=MAX_NOTE_CHAR_LIMIT):
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemeModelError(
            f"{position}: field '{field}' must be a string or omitted")
    if len(value) > limit:
        raise SchemeModelError(
            f"{position}: field '{field}' exceeds the {limit}-character limit")
    return value


def _source_ref(value, position):
    """Verbatim source reference; dict (e.g. pdf page) or short string or null.

    Kept exactly as supplied: the engine never parses pages or sessions from
    it in Stage C2. Bounds: strings <= MAX_SOURCE_REF_STRING_CHARS, dicts
    <= MAX_SOURCE_REF_KEYS with scalar values only, so no source-reference
    payload can drive unbounded processing.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip() or len(value) > MAX_NOTE_CHAR_LIMIT:
            raise SchemeModelError(f"{position}: source_ref is empty or too long")
        return value
    if isinstance(value, dict):
        if len(value) > MAX_SOURCE_REF_KEYS:
            raise SchemeModelError(
                f"{position}: source_ref dict has more than "
                f"{MAX_SOURCE_REF_KEYS} keys")
        for key, item in value.items():
            if not isinstance(key, str):
                raise SchemeModelError(
                    f"{position}: source_ref keys must be strings")
            if isinstance(item, (dict, list)):
                raise SchemeModelError(
                    f"{position}: source_ref values must be scalars")
            if isinstance(item, str) and len(item) > MAX_SOURCE_REF_STRING_CHARS:
                raise SchemeModelError(
                    f"{position}: source_ref string values exceed "
                    f"{MAX_SOURCE_REF_STRING_CHARS} characters")
        return dict(value)
    raise SchemeModelError(
        f"{position}: source_ref must be a string, a dict or omitted")


def _bounded_payload(value, position, what):
    """Bound any verbatim-retained payload (extras and similar).

    The payload must be JSON data (it is never code) and fit within
    MAX_EXTRAS_SERIALIZED_CHARS when serialized; container nesting deeper
    than MAX_EXTRA_DEPTH is rejected by an EXPLICIT STACK WALK (never
    recursive), so no document content can drive unbounded or deep
    processing. Nothing else is enforced: unknown fields remain unknown and
    are never interpreted.
    """
    if not value:
        return
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_EXTRA_DEPTH:
            raise SchemeModelError(
                f"{position}: {what} are nested deeper than "
                f"{MAX_EXTRA_DEPTH} levels")
        if isinstance(node, dict):
            for nested in node.values():
                stack.append((nested, depth + 1))
        elif isinstance(node, list):
            for nested in node:
                stack.append((nested, depth + 1))
    try:
        serialized = json.dumps(value, sort_keys=True)
    except (TypeError, ValueError, RecursionError) as exc:
        raise SchemeModelError(
            f"{position}: {what} must be JSON-serializable data") from exc
    if len(serialized) > MAX_EXTRAS_SERIALIZED_CHARS:
        raise SchemeModelError(
            f"{position}: {what} exceed the {MAX_EXTRAS_SERIALIZED_CHARS}-"
            "character serialized limit")


def _require_nonempty_id(value, position, field):
    if not isinstance(value, str) or not value.strip():
        raise SchemeModelError(f"{position}: a non-empty string '{field}' is required")
    if len(value) > 200:
        raise SchemeModelError(f"{position}: '{field}' exceeds 200 characters")
    return value


class Criterion:
    """One mark criterion; every field is either explicit data or None."""

    __slots__ = ("document_index", "question_id", "id", "code", "weight",
                 "dependencies", "follow_through", "alternative_group",
                 "note", "source_ref", "extras")

    def __init__(self, document_index, question_id, id, code, weight,
                 dependencies, follow_through, alternative_group, note,
                 source_ref, extras):
        self.document_index = document_index
        self.question_id = question_id
        self.id = id
        self.code = code
        self.weight = weight
        self.dependencies = dependencies
        self.follow_through = follow_through
        self.alternative_group = alternative_group
        self.note = note
        self.source_ref = source_ref
        self.extras = extras

    def as_dict(self):
        payload = {}
        for slot in self.__slots__:
            if slot == "document_index":
                continue
            value = getattr(self, slot)
            payload[slot] = [dep for dep in value] if slot == "dependencies" else value
        return payload


class Question:
    """One question with its explicit total (None when unavailable)."""

    __slots__ = ("document_index", "id", "total", "diagram_dependent",
                 "claimed_solution", "source_ref", "criteria", "extras")

    def __init__(self, document_index, id, total, diagram_dependent,
                 claimed_solution, source_ref, criteria, extras):
        self.document_index = document_index
        self.id = id
        self.total = total
        self.diagram_dependent = diagram_dependent
        self.claimed_solution = claimed_solution
        self.source_ref = source_ref
        self.criteria = criteria
        self.extras = extras

    def criteria_by_id(self):
        return {criterion.id: criterion for criterion in self.criteria}

    def as_dict(self):
        return {
            "id": self.id,
            "total": self.total,
            "diagram_dependent": self.diagram_dependent,
            "claimed_solution": self.claimed_solution,
            "source_ref": self.source_ref,
            "criteria": [criterion.as_dict() for criterion in self.criteria],
            "extras": self.extras,
        }


class SchemeDocument:
    """Parsed scheme: questions (document order) plus optional provenance."""

    __slots__ = ("provenance", "questions", "extras")

    def __init__(self, provenance, questions, extras):
        self.provenance = provenance
        self.questions = questions
        self.extras = extras

    def criteria(self):
        for question in self.questions:
            for criterion in question.criteria:
                yield question, criterion

    def criterion_index(self):
        """Every criterion id in the document (first occurrence wins upstream;
        duplicates are a verification finding, not hidden here)."""
        mapping = {}
        for question in self.questions:
            for criterion in question.criteria:
                mapping.setdefault(criterion.id, criterion)
        return mapping

    def questions_by_id(self):
        mapping = {}
        for question in self.questions:
            mapping.setdefault(question.id, question)
        return mapping

    def as_dict(self):
        return {
            "provenance": self.provenance,
            "questions": [q.as_dict() for q in self.questions],
            "extras": self.extras,
        }


def _parse_criterion(raw, index, question_id):
    position = f"question[{index}] '{question_id}'"
    if not isinstance(raw, dict):
        raise SchemeModelError(f"{position}: criterion must be an object")
    if index >= MAX_CRITERIA_PER_QUESTION:
        raise SchemeModelError(
            f"{position}: exceeds the {MAX_CRITERIA_PER_QUESTION}-criterion limit")
    criterion_id = _require_nonempty_id(raw.get("id"), position, "id")
    position = f"criterion '{criterion_id}' in question '{question_id}'"

    code = raw.get("code")
    if code is not None and not isinstance(code, str):
        raise SchemeModelError(f"{position}: field 'code' must be a string or omitted")

    raw_weight = raw.get("weight")
    if type(raw_weight) is bool:
        raise SchemeModelError(
            f"{position}: field 'weight' must be an integer or omitted")
    if raw_weight == 0 and raw_weight is not None:
        weight = 0                    # well-typed integer rejected by the engine
    else:
        weight = _optional_nonnegative_int(raw_weight, position, "weight")

    deps_raw = raw.get("dependencies")
    if deps_raw is None:
        dependencies = []
    else:
        if not isinstance(deps_raw, list) or len(deps_raw) > MAX_DEPENDENCIES_PER_CRITERION:
            raise SchemeModelError(
                f"{position}: 'dependencies' must be a list of at most "
                f"{MAX_DEPENDENCIES_PER_CRITERION} ids")
        dependencies = []
        for dep in deps_raw:
            dependencies.append(_require_nonempty_id(dep, position, "dependency"))

    follow_through = _strict_bool(raw.get("follow_through"), position, "follow_through")
    note = _verbatim_string(raw.get("note"), position, "note")
    source_ref = _source_ref(raw.get("source_ref"), position)

    group = raw.get("alternative_group")
    if group is not None:
        group = _require_nonempty_id(group, position, "alternative_group")

    known = {"id", "code", "weight", "dependencies", "follow_through",
             "alternative_group", "note", "source_ref"}
    extras = {key: value for key, value in raw.items() if key not in known}
    _bounded_payload(extras, position, "extra criterion fields")

    return Criterion(
        document_index=index,
        question_id=question_id,
        id=criterion_id,
        code=code,
        weight=weight,
        dependencies=dependencies,
        follow_through=follow_through,
        alternative_group=group,
        note=note,
        source_ref=source_ref,
        extras=extras,
    )


def _parse_question(raw, index):
    position = f"questions[{index}]"
    if not isinstance(raw, dict):
        raise SchemeModelError(f"{position}: question must be an object")
    question_id = _require_nonempty_id(raw.get("id"), position, "id")
    position = f"question '{question_id}'"

    total = _optional_nonnegative_int(raw.get("total"), position, "total")
    if type(raw.get("diagram_dependent")) is not bool and raw.get("diagram_dependent") is not None:
        raise SchemeModelError(
            f"{position}: 'diagram_dependent' must be a strict bool or omitted")
    claimed = _verbatim_string(raw.get("claimed_solution"), position, "claimed_solution")
    source_ref = _source_ref(raw.get("source_ref"), position)

    criteria_raw = raw.get("criteria")
    if not isinstance(criteria_raw, list):
        raise SchemeModelError(f"{position}: 'criteria' must be a list")
    criteria = [_parse_criterion(entry, ci, question_id)
                for ci, entry in enumerate(criteria_raw)]

    known = {"id", "total", "diagram_dependent", "claimed_solution",
             "source_ref", "criteria"}
    extras = {key: value for key, value in raw.items() if key not in known}
    _bounded_payload(extras, position, "extra question fields")

    return Question(
        document_index=index,
        id=question_id,
        total=total,
        diagram_dependent=raw.get("diagram_dependent"),
        claimed_solution=claimed,
        source_ref=source_ref,
        criteria=criteria,
        extras=extras,
    )


def parse_scheme(payload):
    """Validate a scheme record and return a SchemeDocument.

    Raises SchemeModelError (ValueError) on malformed input; content-free
    position-only messages. Structural *problems* (duplicates, totals,
    unknown codes...) are deliberately NOT raised here: they become findings.
    """
    if not isinstance(payload, dict):
        raise SchemeModelError("Scheme payload must be an object")
    provenance = payload.get("provenance")
    if provenance is not None and not isinstance(provenance, dict):
        raise SchemeModelError("Field 'provenance' must be an object or omitted")
    if isinstance(provenance, dict):
        _bounded_payload(provenance, "document provenance", "provenance fields")
    questions_raw = payload.get("questions")
    if not isinstance(questions_raw, list):
        raise SchemeModelError("A 'questions' list is required")
    if len(questions_raw) > MAX_QUESTIONS:
        raise SchemeModelError(
            f"Schemes are limited to {MAX_QUESTIONS} questions per document")

    known = {"provenance", "questions"}
    extras = {key: value for key, value in payload.items() if key not in known}
    _bounded_payload(extras, "document", "extra document fields")

    questions = [_parse_question(entry, index)
                 for index, entry in enumerate(questions_raw)]
    return SchemeDocument(provenance=provenance, questions=questions, extras=extras)
