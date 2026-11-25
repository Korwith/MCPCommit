"""Locating and laying out Minecraft game files for a workspace."""

import hashlib
import json
import os
import platform
import re
import shutil
import sys
import time
import urllib.request
import zipfile

from . import paths

MANIFEST_URL = 'https://piston-meta.mojang.com/mc/game/version_manifest_v2.json'
USER_AGENT = 'MCP-Workbench/1.0'

# MCP folder names / version.cfg names that differ from Mojang's ids
MOJANG_ID_ALIASES = {
    '1.0.0': '1.0',
    '1.1.0': '1.1',
    'b1.5._01': 'b1.5_01',
    'b1.5_01': 'b1.5_01',
    'a1.2.2': 'a1.2.2b',
    # RevEng/MCP tooling for 'a1.1.2' was built against a1.1.2_01 (see MCP 2.0 csv notes)
    'a1.1.2': 'a1.1.2_01',
}


def mojang_id(version):
    return MOJANG_ID_ALIASES.get(version, version)


def os_keyword():
    if sys.platform.startswith('linux'):
        return 'linux'
    if sys.platform.startswith('darwin'):
        return 'osx'
    return 'windows'


def arch_bits():
    return '64' if platform.machine().lower() in ('x86_64', 'amd64', 'aarch64', 'arm64') else '32'


# Downloads


def _sha1(path):
    h = hashlib.sha1()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def md5sum(path):
    h = hashlib.md5()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest, sha1=None, log=None):
    if os.path.isfile(dest) and (sha1 is None or _sha1(dest) == sha1):
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if log:
        log('download %s' % url)
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    tmp = dest + '.part'
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, 'wb') as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    if sha1 and _sha1(tmp) != sha1:
        os.remove(tmp)
        raise IOError('checksum mismatch for %s' % url)
    os.replace(tmp, dest)
    return dest


def _cached_json(url, name, max_age=None):
    path = os.path.join(paths.CACHE_DIR, 'meta', name)
    if os.path.isfile(path) and (max_age is None or time.time() - os.path.getmtime(path) < max_age):
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    download(url, path + '.new')
    os.replace(path + '.new', path)
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def manifest():
    try:
        return _cached_json(MANIFEST_URL, 'version_manifest_v2.json', max_age=86400)
    except Exception:
        path = os.path.join(paths.CACHE_DIR, 'meta', 'version_manifest_v2.json')
        if os.path.isfile(path):
            with open(path, encoding='utf-8') as fh:
                return json.load(fh)
        raise


def version_json(version):
    vid = mojang_id(version)
    local = os.path.join(paths.CACHE_DIR, 'meta', 'versions', vid + '.json')
    if os.path.isfile(local):
        with open(local, encoding='utf-8') as fh:
            return json.load(fh)
    for entry in manifest()['versions']:
        if entry['id'] == vid:
            return _cached_json(entry['url'], os.path.join('versions', vid + '.json'))
    return None


# Local launcher caches


def launcher_roots():
    home = os.path.expanduser('~')
    cands = [
        os.path.join(home, '.minecraft'),
        os.path.join(home, 'Library', 'Application Support', 'minecraft'),
        os.path.join(os.environ.get('APPDATA', ''), '.minecraft'),
        os.path.join(home, '.local', 'share', 'multimc'),
        os.path.join(home, '.local', 'share', 'PrismLauncher'),
        os.path.join(home, '.local', 'share', 'PolyMC'),
        os.path.join(home, '.var', 'app', 'org.prismlauncher.PrismLauncher', 'data', 'PrismLauncher'),
        '/opt/multimc',
    ]
    return [c for c in cands if c and os.path.isdir(c)]


def find_local_jar(version, side):
    """Look for an unmodified game jar in local launcher caches."""
    vid = mojang_id(version)
    found = []
    for root in launcher_roots():
        if side == 'client':
            cands = [os.path.join(root, 'versions', vid, vid + '.jar'),
                     os.path.join(root, 'libraries', 'com', 'mojang', 'minecraft', vid,
                                  'minecraft-%s-client.jar' % vid)]
        else:
            cands = [os.path.join(root, 'versions', vid, 'minecraft_server.%s.jar' % vid),
                     os.path.join(root, 'libraries', 'com', 'mojang', 'minecraft', vid,
                                  'minecraft-%s-server.jar' % vid)]
        for c in cands:
            if os.path.isfile(c):
                found.append(c)
    return found


def find_local_library(rel_path):
    for root in launcher_roots():
        cand = os.path.join(root, 'libraries', rel_path)
        if os.path.isfile(cand):
            return cand
    return None


# Library resolution (same rules as MCP's MinecraftDiscovery.py)


def _rules_allow(rules):
    if not rules:
        return True
    allowed = False
    kw = os_keyword()
    for rule in rules:
        applies = True
        if 'os' in rule:
            if rule['os'].get('name') and rule['os']['name'] != kw:
                applies = False
            elif 'version' in rule['os']:
                ver = platform.mac_ver()[0] if kw == 'osx' else platform.release()
                if not re.match(rule['os']['version'], ver):
                    applies = False
        if applies:
            allowed = rule['action'] == 'allow'
    return allowed


def libraries(vjson):
    """Yield dicts: name, path (relative to libraries/), url, sha1, natives, exclude."""
    kw = os_keyword()
    out = []
    for lib in vjson.get('libraries', []):
        if not _rules_allow(lib.get('rules')):
            continue
        group, artifact, version = lib['name'].split(':')[:3]
        base = '%s/%s/%s/%s-%s' % (group.replace('.', '/'), artifact, version, artifact, version)
        downloads = lib.get('downloads', {})
        if 'natives' in lib:
            if kw not in lib['natives']:
                continue
            classifier = lib['natives'][kw].replace('${arch}', arch_bits())
            info = downloads.get('classifiers', {}).get(classifier, {})
            out.append({'name': lib['name'], 'artifact': artifact, 'native': True,
                        'path': info.get('path', base + '-%s.jar' % classifier),
                        'url': info.get('url', 'https://libraries.minecraft.net/' + base + '-%s.jar' % classifier),
                        'sha1': info.get('sha1'),
                        'exclude': lib.get('extract', {}).get('exclude', [])})
        if 'artifact' in downloads or 'natives' not in lib:
            info = downloads.get('artifact', {})
            out.append({'name': lib['name'], 'artifact': artifact, 'native': False,
                        'path': info.get('path', base + '.jar'),
                        'url': info.get('url', 'https://libraries.minecraft.net/' + base + '.jar'),
                        'sha1': info.get('sha1'), 'exclude': []})
    return out


def fetch_library(lib, log=None):
    """Return a local path for a library, from a launcher cache or Mojang."""
    local = find_local_library(lib['path'])
    if local and (not lib.get('sha1') or _sha1(local) == lib['sha1']):
        return local
    dest = os.path.join(paths.CACHE_DIR, 'libraries', lib['path'])
    return download(lib['url'], dest, lib.get('sha1'), log)


def extract_natives(jar, dest, exclude, log=None):
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(jar) as zf:
        for name in zf.namelist():
            if any(name.startswith(e) or e in name for e in exclude):
                continue
            if name.endswith('/'):
                os.makedirs(os.path.join(dest, name), exist_ok=True)
                continue
            target = os.path.join(dest, name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(name) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out)


# LWJGL 2 donor for versions Mojang does not list
LWJGL_DONOR_VERSION = '1.5.2'


def _copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)


def get_jar(version, side, log=None):
    """Find (locally) or download the vanilla jar for a version/side."""
    local = find_local_jar(version, side)
    if local:
        return local[0], 'local'
    vjson = version_json(version)
    if not vjson or side not in vjson.get('downloads', {}):
        return None, None
    info = vjson['downloads'][side]
    dest = os.path.join(paths.CACHE_DIR, 'jars', mojang_id(version), '%s.jar' % side)
    download(info['url'], dest, info.get('sha1'), log)
    return dest, 'mojang'


def jar_availability(version):
    """Where a vanilla jar for each side can come from (without downloading)."""
    out = {}
    try:
        vjson = version_json(version)
    except Exception:
        vjson = None
    for side in ('client', 'server'):
        local = find_local_jar(version, side)
        cached = os.path.join(paths.CACHE_DIR, 'jars', mojang_id(version), '%s.jar' % side)
        if local:
            out[side] = {'source': 'local', 'path': local[0]}
        elif os.path.isfile(cached):
            out[side] = {'source': 'cache', 'path': cached}
        elif vjson and side in vjson.get('downloads', {}):
            out[side] = {'source': 'mojang', 'path': None}
        else:
            out[side] = {'source': None, 'path': None}
    return out


def layout_bin(ws_dir, version, client_jar, server_jar, log=None, client_path=None, server_path=None):
    """Populate jars/ in the classic .minecraft/bin layout."""
    jars = os.path.join(ws_dir, 'jars')
    os.makedirs(os.path.join(jars, 'bin', 'natives'), exist_ok=True)
    client_path = client_path or os.path.join(jars, 'bin', 'minecraft.jar')
    server_path = server_path or os.path.join(jars, 'minecraft_server.jar')
    if client_jar:
        _copy(client_jar, client_path)
        if log:
            log('client jar -> %s' % os.path.relpath(client_path, ws_dir))
    if server_jar:
        _copy(server_jar, server_path)
        if log:
            log('server jar -> %s' % os.path.relpath(server_path, ws_dir))
    vjson = None
    try:
        vjson = version_json(version)
    except Exception:
        vjson = None
    if not vjson or not any(l['artifact'] == 'lwjgl' for l in libraries(vjson)):
        vjson = version_json(LWJGL_DONOR_VERSION)
    natives_dir = os.path.join(os.path.dirname(client_path), 'natives')
    wanted = {'lwjgl': 'lwjgl.jar', 'lwjgl_util': 'lwjgl_util.jar', 'jinput': 'jinput.jar'}
    for lib in libraries(vjson):
        if lib['native'] and lib['artifact'] in ('lwjgl-platform', 'jinput-platform'):
            jar = fetch_library(lib, log)
            extract_natives(jar, natives_dir, lib['exclude'] or ['META-INF/'], log)
        elif not lib['native'] and lib['artifact'] in wanted:
            jar = fetch_library(lib, log)
            _copy(jar, os.path.join(os.path.dirname(client_path), wanted[lib['artifact']]))
    if log:
        log('lwjgl, jinput and natives -> %s' % os.path.relpath(os.path.dirname(client_path), ws_dir))


def layout_launcher(ws_dir, version, client_jar, server_jar, log=None):
    """Populate jars/ like the 2013+ launcher's .minecraft directory."""
    jars = os.path.join(ws_dir, 'jars')
    vdir = os.path.join(jars, 'versions', version)
    os.makedirs(vdir, exist_ok=True)
    vjson = version_json(version)
    if vjson is None:
        raise IOError('Mojang has no version manifest entry for %s; cannot resolve its libraries' % version)
    with open(os.path.join(vdir, version + '.json'), 'w', encoding='utf-8') as fh:
        json.dump(vjson, fh, indent=2)
    if client_jar:
        _copy(client_jar, os.path.join(vdir, version + '.jar'))
        if log:
            log('client jar -> jars/versions/%s/%s.jar' % (version, version))
    if server_jar:
        _copy(server_jar, os.path.join(jars, 'minecraft_server.jar'))
        _copy(server_jar, os.path.join(jars, 'minecraft_server.%s.jar' % version))
        if log:
            log('server jar -> jars/minecraft_server.jar')
    natives_dir = os.path.join(vdir, version + '-natives')
    count = 0
    for lib in libraries(vjson):
        jar = fetch_library(lib, log)
        _copy(jar, os.path.join(jars, 'libraries', lib['path']))
        if lib['native']:
            extract_natives(jar, natives_dir, lib['exclude'], log)
        count += 1
    if log:
        log('%d libraries -> jars/libraries, natives -> jars/versions/%s/%s-natives' % (count, version, version))


def download_assets(ws_dir, version, index_name=None, log=None):
    """Download game assets (sounds, language files) for running the client."""
    vjson = version_json(version)
    if not vjson or 'assetIndex' not in vjson:
        raise IOError('no asset index known for %s' % version)
    info = vjson['assetIndex']
    assets = os.path.join(ws_dir, 'jars', 'assets')
    index_name = index_name or info['id']
    index_path = os.path.join(assets, 'indexes', index_name + '.json')
    download(info['url'], index_path, info.get('sha1'), log)
    with open(index_path, encoding='utf-8') as fh:
        index = json.load(fh)
    legacy = index.get('map_to_resources') or index.get('virtual')
    objects = index.get('objects', {})
    total = len(objects)
    for i, (name, obj) in enumerate(sorted(objects.items())):
        h = obj['hash']
        cache = os.path.join(paths.CACHE_DIR, 'assets', h[:2], h)
        download('https://resources.download.minecraft.net/%s/%s' % (h[:2], h), cache, h)
        _copy(cache, os.path.join(assets, 'objects', h[:2], h))
        if legacy:
            # pre-1.6 clients read loose files from .minecraft/resources
            _copy(cache, os.path.join(ws_dir, 'jars', 'resources', name))
            _copy(cache, os.path.join(assets, 'virtual', 'legacy', name))
        if log and (i % 250 == 0 or i == total - 1):
            log('assets %d/%d' % (i + 1, total))
