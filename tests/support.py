"""Load production functions without executing Streamlit UI, OAuth or reading secrets.

Only literal module constants and function definitions are loaded. SDK/HTTP
boundaries fail closed until a test supplies an explicit fake. No real SDKs
or third-party packages are installed by the offline suite.
"""
import ast
import sys
import copy
import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from queue import Queue, Empty
import io
import json
import os
import re
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import document_pipeline as documents

SOURCE = Path(__file__).resolve().parents[1] / "app5_1_automated.py"


def load_app():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    scope = dict(documents=documents, ast=ast, io=io, json=json, os=os, re=re,
                 tempfile=tempfile, time=time, copy=copy, hashlib=hashlib, uuid=uuid,
                 ThreadPoolExecutor=ThreadPoolExecutor, as_completed=as_completed,
                 wait=wait, FIRST_COMPLETED=FIRST_COMPLETED, Queue=Queue, Empty=Empty)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    scope[target.id] = value
    scope["requests"] = SimpleNamespace(
        request=Mock(side_effect=AssertionError("Unexpected HTTP request")),
        get=Mock(side_effect=AssertionError("Unexpected HTTP download")),
    )
    scope["types"] = SimpleNamespace(GenerateContentConfig=lambda **kw: kw)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    # New decorators/defaults must be reviewed before this headless loader runs.
    for fn in functions:
        if fn.decorator_list:
            raise AssertionError("Headless loader does not execute decorators")
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"), scope)
    return scope


def fake_client(payloads=()):
    client = SimpleNamespace(
        models=SimpleNamespace(generate_content=Mock(
            side_effect=[SimpleNamespace(text=json.dumps(p)) if isinstance(p, dict)
                         else p for p in payloads])),
        files=SimpleNamespace(
            upload=Mock(side_effect=AssertionError("Unexpected file upload")),
            get=Mock(side_effect=AssertionError("Unexpected file poll")),
            delete=Mock(),
        ),
    )
    return client


def remote(state="ACTIVE", name="synthetic-file"):
    return SimpleNamespace(name=name, state=SimpleNamespace(name=state))


def response(data=None, status=200, content=b"{}", text="synthetic error"):
    return SimpleNamespace(status_code=status, content=content, text=text,
                           json=lambda: data if data is not None else {})


def benchmark():
    return json.loads((Path(__file__).parent / "fixtures" / "synthetic_marking.json")
                      .read_text(encoding="utf-8"))


def model_question(case, variant):
    sample = case["responses"][variant]
    return {
        "question_number": case["id"], "max_marks": case["max_marks"],
        "awarded_marks": sample["expected_marks"],
        "student_answer": sample["answer"],
        "working_summary": sample["working"],
        "correct_method": case["method"],
        "full_mark_solution": case["answer"],
        "scheme_answer_evidence": case["answer"],
        "confidence": "High",
        "loss_reason": "" if variant == "correct" else "Criterion not demonstrated",
        "mark_breakdown": [
            {"code": step["code"], "awarded": index < sample["expected_marks"],
             "reason": sample["working"] if index < sample["expected_marks"]
             else "Criterion not demonstrated"}
            for index, step in enumerate(case["allocation"])
        ],
    }
