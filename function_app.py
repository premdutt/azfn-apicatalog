import hashlib
import json
import logging
import re
from pathlib import Path

import azure.functions as func

from ingest import delete_blob, upsert_blob
from rag import answer

app = func.FunctionApp()

CHAT_HTML = (Path(__file__).parent / 'chat.html').read_text(encoding='utf-8')

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


@app.route(route='ask', methods=['POST'],