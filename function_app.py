import azure.functions as func
import datetime
import json
import logging
import logging
import re


app = func.FunctionApp()

# apis/<apiname>/v<n>/<file>
PATH_RE = re.compile(
    r"^apis/(?P<api>[^/]+)/(?P<version>v\d+)/"
    r"(?P<file>apidoc\.md|apimetadata\.json|apispec\.yaml)$"
)

@app.event_grid_trigger(arg_name="event")
def api_catalog_blob_events(event: func.EventGridEvent):
    # subject: /blobServices/default/containers/apicatalog/blobs/apis/pets/v1/apidoc.md
    blob_path = event.subject.split("/blobs/", 1)[1]
    m = PATH_RE.match(blob_path)
    if not m:
        logging.info("Ignoring %s", blob_path)
        return

    api, version, file = m["api"], m["version"], m["file"]

    if event.event_type == "Microsoft.Storage.BlobCreated":
        logging.info("ADD/UPDATE: api=%s version=%s file=%s", api, version, file)
        # TODO (Step 9): download blob -> chunk -> embed -> upsert into index
    elif event.event_type == "Microsoft.Storage.BlobDeleted":
        logging.info("DELETE: api=%s version=%s file=%s", api, version, file)
        # TODO (Step 9): remove this file's chunks from the index