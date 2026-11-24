import csv, datetime, io, json, os, re, subprocess, sys, urllib.request, zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

PLAYLIST, STATE = 'clean_playlist.m3u', 'state.json'
GROUPS = {'PH': 'Philippines', 'UK': 'UK', 'US': 'USA', 'CA': 'Canada', 'AU': 'Australia'}
LANGS = {'eng', 'fil', 'tgl', 'ceb', 'ilo', 'hil', 'war', 'pam', 'bik', 'pag'}
DB = 'https://raw.githubusercontent.com/iptv-org/database/master/data/'
REPO_ZIP = 'https://codeload.github.com/iptv-org/iptv/zip/refs/heads/master'
DEAD_AFTER = 2
HLS_HELP = subprocess.run(['ffmpeg', '-hide_banner', '-h', 'demuxer=hls'], capture_output=True, text=True).stdout
HLS_OPTS = [x for k, v in (('extension_picky', '0'), ('allowed_segment_extensions', 'ALL'), ('allowed_extensions', 'ALL'))
            if '-' + k in HLS_HELP for x in ('-' + k, v)]


def get(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def parse(text):
    info, opts = '', []
    for l in text.splitlines():
        l = l.strip()
        if l.startswith('#EXTINF'):
            info, opts = l, []
        elif l.startswith('#EXTVLCOPT'):
            opts.append(l)
        elif re.match(r'https?://', l) and info:
            yield info, opts, l


def candidates():
    country, nsfw, feeds, main = {}, set(), {}, {}
    for r in csv.DictReader(io.StringIO(get(DB + 'channels.csv').decode())):
        country[r['id']] = 'UK' if r['country'] == 'GB' else r['country']
        if r['is_nsfw'].upper() == 'TRUE':
            nsfw.add(r['id'])
    for r in csv.DictReader(io.StringIO(get(DB + 'feeds.csv').decode())):
        langs = set(r['languages'].split(';'))
        feeds[r['channel'], r['id']] = langs
        if r['is_main'].upper() == 'TRUE':
            main[r['channel']] = langs
    z = zipfile.ZipFile(io.BytesIO(get(REPO_ZIP)))
    out = {}
    for n in z.namelist():
        if '/streams/' not in n or not n.endswith('.m3u'):
            continue
        for info, opts, url in parse(z.read(n).decode('utf-8', 'ignore')):
            m = re.search(r'tvg-id="([^"]*)"', info)
            cid, _, fid = (m.group(1) if m else '').partition('@')
            c = country.get(cid)
            if c not in GROUPS or cid in nsfw or url in out:
                continue
            if c != 'PH' and not (feeds.get((cid, fid)) or main.get(cid) or set()) & LANGS:
                continue
            info = re.sub(r'^(#EXTINF:-?\d+)', rf'\1 group-title="{GROUPS[c]}"', info)
            out[url] = (info, opts, url)
    return out


def test(it):
    """Returns 'ok', 'dead' (address or file is gone) or 'unsure' (region block, slow, ...)."""
    _, opts, url = it
    cmd = ['ffmpeg', '-hide_banner', '-nostdin', '-rw_timeout', '15000000']
    for o in opts:
        k, _, v = o.partition(':')[2].partition('=')
        if k == 'http-user-agent':
            cmd += ['-user_agent', v]
        elif k == 'http-referrer':
            cmd += ['-referer', v]
    if '.m3u8' in url:
        cmd += HLS_OPTS
    cmd += ['-protocol_whitelist', 'http,https,tcp,tls,crypto,hls', '-i', url,
            '-t', '3', '-map', '0:v:0', '-c', 'copy', '-f', 'null', '-']
    try:
        e = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore',
                           timeout=60, stdin=subprocess.DEVNULL).stderr
    except subprocess.TimeoutExpired:
        return 'unsure'
    f = re.findall(r'frame=\s*(\d+)', e)
    if f and int(f[-1]) >= 30:
        return 'ok'
    return 'dead' if re.search(r'Failed to resolve|\b404 Not Found|\b410 Gone', e) else 'unsure'


def decide(cur, cand, res, fails):
    keep, new_fails = {}, {}
    for url, it in cur.items():
        n = fails.get(url, 0) + 1 if res[url] == 'dead' and '[Not 24/7]' not in it[0] else 0
        if n >= DEAD_AFTER:
            continue
        keep[url] = it
        if n:
            new_fails[url] = n
    for url, it in cand.items():
        if url not in cur and res[url] == 'ok':
            keep[url] = it
    return keep, new_fails


def main():
    cur = {it[2]: it for it in parse(open(PLAYLIST, encoding='utf-8').read())}
    fails = json.load(open(STATE))['fails'] if os.path.exists(STATE) else {}
    # Streams that failed on the home network, even if they play from GitHub's servers
    home_blocked = set(open('home_blocked.txt', encoding='utf-8').read().split())
    cand = {u: it for u, it in candidates().items() if u not in home_blocked}
    everything = {**cand, **cur}
    print('current', len(cur), 'candidates', len(cand), 'to test', len(everything), flush=True)
    with ThreadPoolExecutor(16) as x:
        res = dict(zip(everything, x.map(test, everything.values())))
    print('current:', Counter(res[u] for u in cur))
    print('new:', Counter(res[u] for u in cand if u not in cur))

    if sum(res[u] == 'dead' for u in cur) > 0.3 * len(cur):
        sys.exit('Over 30% of the list looks dead: probably a network fault. Playlist left unchanged.')
    keep, new_fails = decide(cur, cand, res, fails)
    if len(keep) < 0.8 * len(cur):
        sys.exit('New list is suspiciously small. Playlist left unchanged.')

    order = list(GROUPS.values())

    def key(it):
        m = re.search(r'group-title="([^"]*)"', it[0])
        g = m.group(1) if m else ''
        return order.index(g) if g in order else len(order), it[0].split(',', 1)[-1].lower()

    with open(PLAYLIST, 'w', encoding='utf-8', newline='\n') as f:
        f.write('#EXTM3U\n')
        for info, opts, url in sorted(keep.values(), key=key):
            f.write('\n'.join([info, *opts, url]) + '\n')
    with open(STATE, 'w', newline='\n') as f:
        json.dump({'updated': datetime.date.today().isoformat(), 'fails': new_fails}, f, indent=1)
    print('saved', len(keep), 'added', len(keep.keys() - cur.keys()), 'removed', len(cur.keys() - keep.keys()),
          'on watch', len(new_fails))


if __name__ == '__main__':
    main()
