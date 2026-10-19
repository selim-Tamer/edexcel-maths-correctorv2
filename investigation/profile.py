"""Measure source configuration and synthetic inputs offline, without importing the app."""
import ast
import base64
import json
from pathlib import Path
from .diagnose import build_request


def profile():
    tree=ast.parse(Path(__file__).resolve().parents[1].joinpath('app5_1_automated.py').read_text(encoding='utf-8'))
    constants={}
    for node in tree.body:
        if isinstance(node,ast.Assign):
            for name in node.targets:
                if isinstance(name,ast.Name) and name.id in ('MARKING_SYSTEM','OUTPUT_SCHEMA','PRIMARY_MODELS'):
                    constants[name.id]=ast.literal_eval(node.value)
    grade=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='grade_with_model')
    prompt=next(ast.literal_eval(n.value) for n in grade.body if isinstance(n,ast.Assign)
                and any(isinstance(t,ast.Name) and t.id=='prompt' for t in n.targets))
    profiles={}
    for level,variant in [('A','digital'),('B','digital'),('C','digital'),('C','strokes')]:
        request=build_request(level,variant)
        parts=request['contents'][0]['parts']
        profiles[level+('-'+variant if level=='C' else '')]={
            'text_characters':sum(len(p.get('text','')) for p in parts),
            'schema_characters':len(json.dumps(request['generationConfig']['responseSchema'],separators=(',',':'))),
            'pdf_bytes':[len(base64.b64decode(p['inlineData']['data'])) for p in parts if 'inlineData' in p]}
    return {'network_requests':0,'token_counts':'unknown; no tokenize or generation requests performed',
        'production':{'models':constants['PRIMARY_MODELS'],
            'base_text_characters':len(constants['MARKING_SYSTEM'])+len(prompt),
            'schema_characters':len(json.dumps(constants['OUTPUT_SCHEMA'],separators=(',',':'))),
            'question_fields':len(constants['OUTPUT_SCHEMA']['properties']['questions']['items']['properties']),
            'production_pdf_bytes':None,'thinking_override':None,'output_token_cap':None},
        'synthetic':profiles}


if __name__=='__main__': print(json.dumps(profile(),indent=2))
