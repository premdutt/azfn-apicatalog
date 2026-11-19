import os
import time

from azure.search.documents.models import VectorizedQuery

from ingest import _embed, _openai, _quote, _search

SYSTEM_PROMPT = (
    'You answer questions about an internal API catalog using only the overview and context provided. '
    'Cite sources as [n]. If the answer is not in the material, say you do not know. '
    'Never invent APIs, endpoints or fields.'
)


def _overview():
    facets = _search().search(
        search_text='*', facets=['apiname,count:100', 'business_domain,count:50'], top=0
    ).get_facets()
    return '\n'.join(
        '%s: %s' % (name, ', '.join('%s (%d chunks)' % (f['value'], f['count']) for f in values))
        for name, values in facets.items()
    )


def _filter(apiname, business_domain):
    parts = []
    if apiname:
        parts.append("apiname eq '%s'" % _quote(apiname))
    if business_domain:
        parts.append("business_domain eq '%s'" % _quote(business_domain))
    return ' and '.join(parts) or None


def answer(question, apiname=None, business_domain=None, top=6):
    started = time.time()
    vector = _embed([question])[0]
    hits = list(_search().search(
        search_text=question,
        vector_queries=[VectorizedQuery(vector=vector, k_nearest_neighbors=50, fields='contentVector')],
        filter=_filter(apiname, business_domain),
        query_type='semantic',
        semantic_configuration_name='apicatalog-semantic-config',
        select=['apiname', 'version', 'fileName', 'title', 'content'],
        top=top,
    ))
    context = '\n\n'.join(
        '[%d] %s %s / %s / %s\n%s' % (i, h['apiname'], h['version'], h['fileName'], h['title'], h['content'])
        for i, h in enumerate(hits, 1)
    )
    user_message = 'Catalog overview:\n%s\n\nContext:\n%s\n\nQuestion: %s' % (_overview(), context, question)
    resp = _openai().chat.completions.create(
        model=os.environ['AZURE_OPENAI_CHAT_DEPLOYMENT'],
        messages=[
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user_message},
        ],
    )
    usage = resp.usage
    return {
        'answer': resp.choices[0].message.content,
        'sources': [
            {'n': i, 'apiname': h['apiname'], 'version': h['version'], 'fileName': h['fileName'], 'title': h['title']}
            for i, h in enumerate(hits, 1)
        ],
        'stats': {
            'hits': len(hits),
            'top_reranker': hits[0].get('@search.reranker_score') if hits else None,
            'prompt_tokens': usage.prompt_tokens if usage else None,
            'completion_tokens': usage.completion_tokens if usage else None,
            'latency_ms': int((time.time() - started) * 1000),
        },
    }