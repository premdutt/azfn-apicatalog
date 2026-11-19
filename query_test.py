import os
import sys

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from openai import AzureOpenAI
import json
import os

with open('local.settings.json') as f:
    os.environ.update({k: v for k, v in json.load(f)['Values'].items() if isinstance(v, str)})


question = ' '.join(sys.argv[1:]) or 'what is claim lifecycle?'

oai = AzureOpenAI(
    azure_endpoint=os.environ['AZURE_OPENAI_ENDPOINT'],
    api_key=os.environ['AZURE_OPENAI_API_KEY'],
    api_version='2024-10-21',
)
vector = oai.embeddings.create(
    model=os.environ['AZURE_OPENAI_EMBEDDING_DEPLOYMENT'], input=question
).data[0].embedding

client = SearchClient(
    os.environ['AZURE_SEARCH_ENDPOINT'], 'apicatalog-index', AzureKeyCredential(os.environ['AZURE_SEARCH_API_KEY'])
)
results = client.search(
    search_text=question,  # keyword leg (BM25 on title and content)
    vector_queries=[VectorizedQuery(vector=vector, k_nearest_neighbors=50, fields='contentVector')],  # vector leg
    query_type='semantic',  # optional re-ranking; remove these two lines if unavailable
    semantic_configuration_name='apicatalog-semantic-config',
    select=['apiname', 'version', 'fileName', 'title', 'content'],
    top=5,
    # filter="apiname eq 'api-claim' and contentType eq 'apispec'",
)
for r in results:
    print(round(r['@search.score'], 4), r.get('@search.reranker_score'), r['apiname'], r['version'], r['fileName'], '|', r['title'])