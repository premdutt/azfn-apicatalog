import hashlib
import json
import logging
import re

import azure.functions as func

from ingest import delete_blob, upsert_blob
from rag import answer

app = func.FunctionApp()

SUBJECT_RE = re.compile(r'^/blobServices/default/containers/(?P<container>[^/]+)/blobs/(?P<blob>.+)$')
PATH_RE = re.compile(
    r'^apis/(?P<api>[^/]+)/(?P<version>v\d+)/(?P<file>apidoc\.md|apimetadata\.json|apispec\.yaml)$'
)


def _user_id(req):
    raw = req.headers.get('x-ms-client-principal-name') or req.headers.get('x-forwarded-for') or 'anonymous'
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _reply(payload, status):
    return func.HttpResponse(json.dumps(payload), status_code=status, mimetype='application/json')


def _json_body(req):
    try:
        body = req.get_json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


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


@app.route(route='ask', methods=['POST'], auth_level=func.AuthLevel.FUNCTION)
def ask(req: func.HttpRequest) -> func.HttpResponse:
    body = _json_body(req)
    question = (body.get('question') or '').strip()
    if not question:
        return _reply({'error': 'Send JSON like {"question": "..."}'}, 400)
    try:
        result = answer(question, body.get('apiname'), body.get('business_domain'))
    except Exception:
        logging.exception('ask failed')
        return _reply({'error': 'The answer service failed. Try again.'}, 500)

    stats = result.pop('stats')
    logging.info('ASK_EVENT %s', json.dumps({
        'user': _user_id(req),
        'q_len': len(question),
        'apiname': body.get('apiname'),
        **stats,
    }))
    return _reply(result, 200)


@app.route(route='feedback', methods=['POST'], auth_level=func.AuthLevel.FUNCTION)
def feedback(req: func.HttpRequest) -> func.HttpResponse:
    body = _json_body(req)
    logging.info('FEEDBACK_EVENT %s', json.dumps({
        'user': _user_id(req),
        'helpful': bool(body.get('helpful')),
        'comment_len': len(body.get('comment') or ''),
    }))
    return _reply({'ok': True}, 200)