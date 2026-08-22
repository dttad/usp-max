import sys
sys.path.insert(0, '.')
from bench._profile_runner import crawl, make_reroute_client
client = make_reroute_client('http://127.0.0.1:8765')
pages = crawl('http://127.0.0.1:8765/', client, 200)
print('OK urls=', len(pages), flush=True)
