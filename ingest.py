import hashlib
import json
import logging
import os
import re
from datetime import timezone
from functools import lru_cache

import yaml
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.storage.blob import BlobServiceClient
from openai import AzureOpenAI

CONTENT_TYPES = {'apidoc.md': 'apidoc', 'apimetadata.json': 'apimetadata', 'apispec.yaml': 'apispec'}
HTTP_METHODS = {'get', 'put', 'post', 'delete', 'patch', 'options', 'head', 'trace'}
MAX_CHARS = 3000
OVERLAP = 300


# ---------- clients (created once per worker process) ----------

@lru_cache(maxsize=None)
def _blobs():
    return BlobServiceClient(os.environ['STORAGE_ACCOUNT_URL'], credential=DefaultAzureCredential())


@lru_cache(maxsize=None)
def _search():
    return SearchClient(
        endpoint=os.environ['AZURE_SEARCH_ENDPOINT'],
        index_name=os.environ.get('AZURE_SEARCH_INDEX', 'apicatalog-index'),
        credential=AzureKeyCredential(os.environ['AZURE_SEARCH_KEY']),
    )


@lru_cache(maxsize=None)
def _openai():
    return AzureOpenAI(
        azure_endpoint=os.environ['AZURE_OPENAI_ENDPOINT'],
        api_key=os.environ['AZURE_OPENAI_KEY'],
        api_version=os.environ.get('AZURE_OPENAI_API_VERSION', '2024-10-21'),
    )


def _embed(texts):
    vectors = []
    for i in range(0, len(texts), 16):
        resp = _openai().embeddings.create(
            model=os.environ['AZURE_OPENAI_EMBED_DEPLOYMENT'], input=texts[i:i + 16]
        )
        vectors.extend(item.embedding for item in resp.data)
    return vectors


# ---------- blob + metadata ----------

def _read_blob(container, blob_path):
    blob = _blobs().get_blob_client(container, blob_path)
    modified = blob.get_blob_properties().last_modified
    return blob.download_blob().readall().decode('utf-8-sig'), modified


def _parse_json(text):
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _read_metadata(container, api, version):
    try:
        text, _ = _read_blob(container, f'apis/{api}/{version}/apimetadata.json')
    except ResourceNotFoundError:
        logging.warning('No apimetadata.json for %s %s', api, version)
        return {}, ''
    return _parse_json(text), text


def _pick(meta, *names):
    for name in names:
        if meta.get(name) not in (None, ''):
            return meta[name]
    return None


def _metadata_fields(meta):
    # Adjust the key names here if your apimetadata.json uses different ones.
    team = _pick(meta, 'it_support_team', 'itSupportTeam')
    team = team if isinstance(team, dict) else {}
    return {
        'business_domain': _pick(meta, 'business_domain', 'businessDomain'),
        'business_sub_domain': _pick(meta, 'business_sub_domain', 'businessSubDomain'),
        'business_owner': _pick(meta, 'business_owner', 'businessOwner'),
        'it_owner': _pick(meta, 'it_owner', 'itOwner'),
        'it_support_team': {'name': team.get('name'), 'email': team.get('email')},
    }


# ---------- chunking: each function returns a list of (title, text) ----------

def _split_text(text):
    text = text.strip()
    if len(text) <= MAX_CHARS:
        return [text] if text else []
    parts, start = [], 0
    while start < len(text):
        end = min(start + MAX_CHARS, len(text))
        if end < len(text):
            cut = text.rfind('\n', start + MAX_CHARS // 2, end)
            if cut != -1:
                end = cut
        parts.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - OVERLAP, start + 1)
    return [p for p in parts if p]


def _chunk_markdown(text, default_title):
    out = []
    for section in re.split(r'(?m)^(?=#{1,3} )', text):
        section = section.strip()
        if not section:
            continue
        first = section.splitlines()[0]
        title = first.lstrip('#').strip() if first.startswith('#') else default_title
        out.extend((title, part) for part in _split_text(section))
    return out


def _dump(obj):
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True)


def _chunk_openapi(text, default_title):
    try:
        spec = yaml.safe_load(text)
        if not isinstance(spec, dict):
            raise ValueError('not a mapping')
    except (yaml.YAMLError, ValueError):
        logging.warning('apispec.yaml is not valid YAML; using plain text chunks')
        return [(default_title, part) for part in _split_text(text)]

    info = spec.get('info')
    title = info.get('title') if isinstance(info, dict) and info.get('title') else default_title
    overview = {k: spec[k] for k in ('openapi', 'info', 'servers') if spec.get(k)}
    out = [(title, part) for part in _split_text('API overview\n' + _dump(overview))]

    for path, item in (spec.get('paths') or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() in HTTP_METHODS and isinstance(op, dict):
                label = f'{method.upper()} {path}'
                op_title = f"{label} {op.get('summary', '')}".strip()
                out.extend((op_title, part) for part in _split_text(f'{label}\n' + _dump(op)))

    schemas = (spec.get('components') or {}).get('schemas') or {}
    for name, schema in schemas.items():
        out.extend((f'Schema {name}', part) for part in _split_text(f'Schema {name}\n' + _dump({name: schema})))
    return out


def _chunk_metadata(text, default_title):
    try:
        text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        pass
    return [(default_title, part) for part in _split_text(text)]


# ---------- index operations ----------

def _quote(value):
    return value.replace("'", "''")


def _doc_key(api, version, file):
    return hashlib.sha1(f'{api}/{version}/{file}'.encode()).hexdigest()[:20]


def _file_filter(api, version, file):
    return f"apiname eq '{_quote(api)}' and version eq '{_quote(version)}' and fileName eq '{_quote(file)}'"


def _ids(filter_expr):
    results = _search().search(search_text='*', filter=filter_expr, select=['id'], top=1000)
    return {r['id'] for r in results}


def _check(results):
    failed = [r.key for r in results if not r.succeeded]
    if failed:
        raise RuntimeError(f'Search rejected {len(failed)} documents, for example {failed[:3]}')


def _refresh_siblings(api, version, meta_fields, meta_text):
    # apimetadata.json changed: copy the new metadata onto this version's other chunks.
    flt = f"apiname eq '{_quote(api)}' and version eq '{_quote(version)}' and contentType ne 'apimetadata'"
    patch = [{'id': i, **meta_fields, 'metadataJson': meta_text or None} for i in _ids(flt)]
    for start in range(0, len(patch), 100):
        _check(_search().merge_documents(patch[start:start + 100]))


def upsert_blob(container, blob_path, api, version, file):
    try:
        text, modified = _read_blob(container, blob_path)
    except ResourceNotFoundError:
        logging.warning('%s no longer exists; the delete event will clean up', blob_path)
        return 0

    if file == 'apimetadata.json':
        meta, meta_text = _parse_json(text), text
    else:
        meta, meta_text = _read_metadata(container, api, version)
    meta_fields = _metadata_fields(meta)

    default_title = f'{api} {version}'
    if file == 'apidoc.md':
        pairs = _chunk_markdown(text, default_title)
    elif file == 'apispec.yaml':
        pairs = _chunk_openapi(text, default_title)
    else:
        pairs = _chunk_metadata(text, default_title)

    if not pairs:
        return delete_blob(api, version, file)

    old_ids = _ids(_file_filter(api, version, file))
    contents = [f'API: {api} | Version: {version} | {title}\n{chunk}' for title, chunk in pairs]
    vectors = _embed(contents)
    key = _doc_key(api, version, file)
    updated = modified.astimezone(timezone.utc).isoformat()

    docs = [
        {
            'id': f'{key}-{i:04d}',
            'apiname': api,
            'version': version,
            **meta_fields,
            'contentType': CONTENT_TYPES[file],
            'fileName': file,
            'title': pairs[i][0],
            'content': contents[i],
            'contentVector': vectors[i],
            'metadataJson': meta_text or None,
            'lastUpdated': updated,
        }
        for i in range(len(pairs))
    ]
    for start in range(0, len(docs), 100):
        _check(_search().merge_or_upload_documents(docs[start:start + 100]))

    stale = old_ids - {d['id'] for d in docs}
    if stale:
        _search().delete_documents([{'id': i} for i in stale])

    if file == 'apimetadata.json':
        _refresh_siblings(api, version, meta_fields, meta_text)
    return len(docs)


def delete_blob(api, version, file):
    ids = list(_ids(_file_filter(api, version, file)))
    for start in range(0, len(ids), 100):
        _search().delete_documents([{'id': i} for i in ids[start:start + 100]])
    return len(ids)