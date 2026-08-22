import sys
sys.path.insert(0, '.')
import tempfile
original = tempfile.mkstemp
peak = {'count': 0}
def patched(*a, **kw):
    peak['count'] += 1
    return original(*a, **kw)
tempfile.mkstemp = patched
from bench._profile_runner import crawl, make_reroute_client
client = make_reroute_client('http://127.0.0.1:8765')
pages = crawl('http://127.0.0.1:8765/', client, 200)
open('/tmp/tempfile-count-zero-200.txt', 'w').write(str(peak['count']))
