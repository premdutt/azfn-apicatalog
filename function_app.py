import hashlib
import json
import logging
import re
from pathlib import Path

import azure.functions as func

from ingest import delete_blob, upsert_blob
from rag import answer

app = func.FunctionApp()

_chat_file = Path(__file__).parent / 'chat.html'
CHAT_HTML = (
    _chat_file.read_text(encoding='utf-8')
    if _chat_file.exists()
    else '<p>Chat page not deployed.</p>'
)

SUBJECT_RE = re.compile(r'^/blobServices/default/containers/(?P<container>[^/]+)/blobs/(?P<blob>.+)$')
PATH_RE = re.compile(
    r'^apis/(?P<api>[^/]+)/(?P<version>v\d+)/(?P<file>apidoc\.md|apimetadata\.json|apispec\.yaml)$'
)


def _reply(payload, status):
    return func.HttpResponse(json.dumps(payload), status_code=status, mimetype='application/json')


def _json_body(req):
    try:
        body = req.get_json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _caller(req):
    return req.headers.get('x-ms-client-principal-name')


def _user_id(req):
    raw = _caller(req) or req.headers.get('x-forwarded-for') or 'anon'
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


@app.event_grid_trigger(arg_name='event')
def api_catalog_blob_events(event: func.EventGridEvent):
    subject = SUBJECT_RE.match(event.subject or '')
    path = PATH_RE.match(subject['blob']) if subject else None
    if not path:
        logging.info('Ignoring subject %s', event.subject)
        return

    container, blob_path = subject['container'], subject['blob']
    api, version, file = path['api'], path['version'], path['file']

    if event.event_type == 'Microsoft.Storage.BlobCreated':
        count = upsert_blob(container, blob_path, api, version, file)
        logging.info('ADD/UPDATE: api=%s version=%s file=%s chunks_indexed=%d', api, version, file, count)
    elif event.event_type == 'Microsoft.Storage.BlobDeleted':
        count = delete_blob(api, version, file)
        logging.info('DELETE: api=%s version=%s file=%s chunks_removed=%d', api, version, file, count)
    else:
        logging.info('Unhandled event type %s', event.event_type)


@app.route(route='chat', methods=['GET'], auth_level=func.AuthLevel.ANONYMOUS)
def chat(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(CHAT_HTML, mimetype='text/html')


@app.route(route='ask', methods=['POST'], auth_level=func.AuthLevel.ANONYMOUS)
def ask(req: func.HttpRequest) -> func.HttpResponse:
    if not _caller(req):
        return _reply({'error': 'Sign in required'}, 401)
    body = _json_body(req)
    question = (body.get('question') or '').strip()
    if not question:
        return _reply({'error': 'Send JSON like {"question": "..."}'}, 400)
    if len(question) > 2000:
        return _reply({'error': 'Question is too long'}, 400)
    try:
        result = answer(question, body.get('apiname'), body.get('business_domain'))
    except Exception:
        logging.exception('ask failed')
        return _reply({'error': 'The answer service failed. Try again.'}, 500)
    stats = result.pop('stats', {})
    logging.info('ASK_EVENT %s', json.dumps({
        'user': _user_id(req), 'q_len': len(question),
        'apiname': body.get('apiname'), **stats}))
    return _reply(result, 200)


@app.route(route='feedback', methods=['POST'], auth_level=func.AuthLevel.ANONYMOUS)
def feedback(req: func.HttpRequest) -> func.HttpResponse:
    if not _caller(req):
        return _reply({'error': 'Sign in required'}, 401)
    body = _json_body(req)
    logging.info('FEEDBACK_EVENT %s', json.dumps({
        'user': _user_id(req), 'helpful': bool(body.get('helpful')),
        'comment_len': len(body.get('comment') or '')}))
    return _reply({'ok': True}, 200)