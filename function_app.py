import logging
import re
from rag import answer
import json
import azure.functions as func

from ingest import delete_blob, upsert_blob

app = func.FunctionApp()

SUBJECT_RE = re.compile(r'^/blobServices/default/containers/(?P<container>[^/]+)/blobs/(?P<blob>.+)$')
PATH_RE = re.compile(
    r'^apis/(?P<api>[^/]+)/(?P<version>v\d+)/(?P<file>apidoc\.md|apimetadata\.json|apispec\.yaml)$'
)


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


def _reply(payload, status):
    return func.HttpResponse(json.dumps(payload), status_code=status, mimetype='application/json')


@app.route(route='ask', methods=['POST'], auth_level=func.AuthLevel.FUNCTION)
def ask(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    question = (body.get('question') or '').strip()
    if not question:
        return _reply({'error': 'Send JSON like {"question": "..."}'}, 400)
    try:
        result = answer(question, body.get('apiname'), body.get('business_domain'))
    except Exception:
        logging.exception('ask failed')
        return _reply({'error': 'The answer service failed. Try again.'}, 500)
    return _reply(result, 200)