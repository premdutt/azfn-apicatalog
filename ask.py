import json
import os
import sys

with open('local.settings.json') as f:
    os.environ.update({k: v for k, v in json.load(f)['Values'].items() if isinstance(v, str)})

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from openai import AzureOpenAI

question = ' '.join(sys.argv[1:]) or 'Which APIs are in the catalog?'

oai = AzureOpenAI(
    azure_endpoint=os.environ['AZURE_OPENAI_ENDPOINT'],
    api_key=os.environ['AZURE_OPENAI_API_KEY'],
    api_version='2024-10-21',
)
search = SearchClient(
    os.environ['AZURE_SEARCH_ENDPOINT'], 'apicatalog-index', AzureKeyCredential(os.environ['AZURE_SEARCH_API_KEY'])
)

# 1. Catalog overview: answers list and count questions
facets = search.search(
    search_text='*', facets=['apiname,count:100', 'business_domain,count:50'], top=0
).get_facets()
overview = '\n'.join(
    '%s: %s' % (name, ', '.join('%s (%d chunks)' % (f['value'], f['count']) for f in values))
    for name, values in facets.items()
)

# 2. Hybrid retrieval: keyword + vector + semantic re-rank
vector = oai.embeddings.create(
    model=os.environ['AZURE_OPENAI_EMBEDDING_DEPLOYMENT'], input=question
).data[0].embedding
hits = list(search.search(
    search_text=question,
    vector_queries=[VectorizedQuery(vector=vector, k_nearest_neighbors=50, fields='contentVector')],
    query_type='semantic',
    semantic_configuration_name='apicatalog-semantic-config',
    select=['apiname', 'version', 'fileName', 'title', 'content'],
    top=6,
))
context = '\n\n'.join(
    '[%d] %s %s / %s / %s\n%s' % (i, h['apiname'], h['version'], h['fileName'], h['title'], h['content'])
    for i, h in enumerate(hits, 1)
)

# 3. Generate the answer
messages = [
    {'role': 'system', 'content': (
        'You answer questions about an internal API catalog using only the overview and context provided. '
        'Cite sources as [n]. If the answer is not in the material, say you do not know. '
        'Never invent APIs, endpoints or fields.'
    )},
    {'role': 'user', 'content': 'Catalog overview:\n%s\n\nContext:\n%s\n\nQuestion: %s' % (overview, context, question)},
]
resp = oai.chat.completions.create(
    model=os.environ['AZURE_OPENAI_CHAT_DEPLOYMENT'], messages=messages, temperature=0
)
print(resp.choices[0].message.content)
print('\nSources:')
for i, h in enumerate(hits, 1):
    print('[%d] %s %s / %s / %s' % (i, h['apiname'], h['version'], h['fileName'], h['title']))