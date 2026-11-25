"""every single mcp archive"""

import configparser
import io
import json
import os
import re
import shutil
import zipfile

from . import knowledge, paths

CATALOG_FILE = os.path.join(paths.DATA_DIR, 'catalog.json')
PACKZIPS = os.path.join(paths.CACHE_DIR, 'packzips')

DISPLAY_VERSION = {
    'b1.5._01': 'b1.5_01',
    'b1.9pre-5': 'b1.9-pre5',
    '1.0.0': '1.0',
    '1.1.0': '1.1',
}


def _norm(name):
    return name.replace('\\', '/')


def _version_key(v):
    """Sort key for Minecraft versions: alpha < beta < release, snapshots by date."""
    v = DISPLAY_VERSION.get(v, v)
    m = re.match(r'^(\d\d)w(\d\d)([a-z])$', v)
    if m:
        # place snapshots right after the release they precede
        year, week = int(m.group(1)), int(m.group(2))
        table = {(12, 17): (1, 2, 5, 1), (12, 26): (1, 2, 5, 2), (13, 2): (1, 4, 7, 1),
                 (13, 5): (1, 4, 7, 2), (13, 9): (1, 4, 7, 3)}
        rel = table.get((year, week), (1, 0, 0, 0))
        return (3,) + rel + (ord(m.group(3)),)
    stage = 3
    if v.startswith('a'):
        stage, v = 1, v[1:]
    elif v.startswith('b'):
        stage, v = 2, v[1:]
    nums = [int(x) for x in re.findall(r'\d+', v)]
    nums = (nums + [0, 0, 0, 0])[:4]
    return (stage,) + tuple(nums) + (0,)


def _pack_key(info):
    """Order packs for the same Minecraft version by MCP release."""
    v = str(info.get('mcp_version') or '')
    reveng = v.startswith('RevEng')
    nums = [int(x) for x in re.findall(r'\d+', v)]
    suffix = re.sub(r'[^a-z]', '', v.replace('RevEng', '').lower())
    return (0 if reveng else 1, nums, suffix, info['pack'])


# Import


def _store_packzip(version, name, data_or_path):
    dest_dir = os.path.join(PACKZIPS, version)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    if isinstance(data_or_path, (bytes, bytearray)):
        with open(dest, 'wb') as fh:
            fh.write(data_or_path)
    else:
        shutil.copyfile(data_or_path, dest)
    return dest


def _import_zip(zf, log):
    count = 0
    for info in zf.infolist():
        name = _norm(info.filename)
        if not name.lower().endswith('.zip'):
            continue
        parts = [p for p in name.split('/') if p]
        base = parts[-1]
        if len(parts) >= 2 and re.match(r'^(revengpack\d*|mcp[\w.-]+)\.zip$', base, re.I):
            version = parts[-2]
            _store_packzip(version, base, zf.read(info))
            count += 1
        elif base.lower().startswith('reobf-fix'):
            _store_packzip('_addons', base, zf.read(info))
        else:
            # a nested container zip, e.g. minecraftcoderpack.zip inside the download
            log('reading nested archive %s' % name)
            tmp = os.path.join(paths.CACHE_DIR, 'nested.zip')
            with zf.open(info) as src, open(tmp, 'wb') as out:
                shutil.copyfileobj(src, out, 1 << 20)
            with zipfile.ZipFile(tmp) as inner:
                count += _import_zip(inner, log)
            os.remove(tmp)
    return count


def import_source(path, log=print):
    path = os.path.abspath(os.path.expanduser(path))
    count = 0
    if os.path.isfile(path) and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            count = _import_zip(zf, log)
    elif os.path.isdir(path):
        for root, _dirs, files in os.walk(path):
            for f in files:
                if f.lower().endswith('.zip'):
                    full = os.path.join(root, f)
                    version = os.path.basename(root)
                    if re.match(r'^(revengpack\d*|mcp[\w.-]+)\.zip$', f, re.I):
                        _store_packzip(version, f, full)
                        count += 1
                    elif zipfile.is_zipfile(full):
                        with zipfile.ZipFile(full) as zf:
                            count += _import_zip(zf, log)
    else:
        raise IOError('not a zip file or folder: %s' % path)
    log('imported %d MCP packs' % count)
    return build()


# Fingerprinting


def _read(zf, names, wanted):
    for n in names:
        if _norm(n).lower() == wanted.lower():
            return zf.read(n).decode('latin-1')
    return None


def fingerprint(zip_path, version, pack):
    with zipfile.ZipFile(zip_path) as zf:
        raw = zf.namelist()
        names = set(_norm(n) for n in raw)
        lower = set(n.lower() for n in names)
        has = lambda p: p.lower() in lower  # noqa: E731
        anyp = lambda pred: any(pred(n) for n in lower)  # noqa: E731

        info = {
            'id': '%s__%s' % (version, pack),
            'folder_version': version,
            'version': DISPLAY_VERSION.get(version, version),
            'pack': pack,
            'zip': zip_path,
            'files': len(names),
        }

        mcp_cfg = _read(zf, raw, 'conf/mcp.cfg')
        version_cfg = _read(zf, raw, 'conf/version.cfg')
        commands = _read(zf, raw, 'runtime/commands.py')
        changelog = (_read(zf, raw, 'CHANGELOG') or _read(zf, raw, 'CHANGELOG.txt') or '')

    mcp_version = None
    if commands:
        m = re.search(r"MCPVersion\s*=\s*'([^']+)'", commands)
        if m:
            mcp_version = m.group(1)
    client_version = server_version = None
    if version_cfg:
        m = re.search(r'MCPVersion\s*=\s*(\S+)', version_cfg)
        if m:
            mcp_version = m.group(1)
        m = re.search(r'ClientVersion\s*=\s*(\S+)', version_cfg)
        client_version = m.group(1) if m else None
        m = re.search(r'ServerVersion\s*=\s*(\S+)', version_cfg)
        server_version = m.group(1) if m else None
    if not mcp_version:
        m = re.search(r'(\d+)(\d+)?', pack.replace('mcp', ''))
        if pack.startswith('mcp'):
            digits = re.match(r'mcp(\d)(\d+)?([a-z]*)', pack)
            if digits:
                mcp_version = digits.group(1) + '.' + (digits.group(2) or '0') + digits.group(3)
        elif pack.startswith('revengpack'):
            digits = re.match(r'revengpack(\d)?(\d)?', pack)
            mcp_version = 'RevEng %s.%s' % (digits.group(1) or '1', digits.group(2) or '0')
    if not mcp_version and changelog:
        m = re.search(r'v(\d+\.\d+\w*)', changelog)
        if m:
            mcp_version = m.group(1)
    info['mcp_version'] = mcp_version
    info['client_version'] = client_version or DISPLAY_VERSION.get(version, version)
    info['server_version'] = server_version or info['client_version']

    md5c = md5s = None
    if mcp_cfg:
        cp = configparser.RawConfigParser(strict=False, interpolation=None)
        try:
            cp.read_string(mcp_cfg)
            md5c = cp.get('JAR', 'MD5Client', fallback=None)
            md5s = cp.get('JAR', 'MD5Server', fallback=None)
        except configparser.Error:
            pass
    info['md5_client'] = (md5c or '').lower() or None
    info['md5_server'] = (md5s or '').lower() or None

    # engine
    if commands:
        info['engine'] = 'runtime'
        try:
            num = float(re.match(r'(\d+\.\d+)', mcp_version).group(1))
        except (AttributeError, ValueError):
            num = 0
        info['mcp_number'] = num
        info['layout'] = 'launcher' if num >= 8.0 else 'bin'
    else:
        info['engine'] = 'legacy'
        info['mcp_number'] = 0
        info['layout'] = 'root' if pack in ('revengpack', 'revengpack11', 'revengpack12') else 'bin'

    # tools
    tools = []
    for label, pred in [
        ('RetroGuard', lambda: anyp(lambda n: n.endswith('retroguard.jar'))),
        ('SpecialSource', lambda: anyp(lambda n: n.endswith('specialsource.jar'))),
        ('MCInjector', lambda: anyp(lambda n: n.endswith('mcinjector.jar'))),
        ('Exceptor', lambda: anyp(lambda n: n.endswith('exceptor.jar'))),
        ('JadRetro', lambda: anyp(lambda n: 'jadretro' in n)),
        ('JAD', lambda: anyp(lambda n: n.endswith('jad.exe'))),
        ('Fernflower', lambda: anyp(lambda n: n.endswith('fernflower.jar'))),
        ('AStyle', lambda: anyp(lambda n: 'astyle' in n and n.endswith('.exe'))),
        ('Renamer', lambda: anyp(lambda n: 'renamer' in n)),
        ('Obfuscathon', lambda: anyp(lambda n: 'obfuscathon' in n)),
    ]:
        if pred():
            tools.append(label)
    info['tools'] = tools

    # mappings
    if has('conf/joined.srg'):
        mapping = 'joined.srg (merged client + server SRG)'
    elif has('conf/client.srg'):
        mapping = 'client.srg / server.srg'
    elif has('conf/classes.csv') and has('conf/mcp.cfg'):
        mapping = 'CSV mappings -> generated SRG'
    elif anyp(lambda n: n.endswith('.rgs')):
        mapping = 'RetroGuard .rgs scripts'
    else:
        mapping = 'none (obfuscated names kept)'
    info['mappings'] = mapping

    exc = []
    if has('conf/exceptor.json'):
        exc.append('exceptor.json')
    if has('conf/joined.exc'):
        exc.append('joined.exc')
    elif has('conf/client.exc'):
        exc.append('client.exc / server.exc')
    info['injector_data'] = exc

    jad_patch = has('conf/patches/minecraft.patch') or has('patches/minecraft.patch') or has('minecraft.patch')
    ff_patch = (has('conf/patches/minecraft_ff.patch') or
                anyp(lambda n: n.startswith('conf/patches/minecraft_ff/')) or
                anyp(lambda n: n.startswith('conf/patches/minecraft_merged_ff/')))
    decompilers = []
    if jad_patch or 'JAD' in tools:
        decompilers.append('jad')
    if ff_patch:
        decompilers.append('fernflower')
    info['decompilers'] = decompilers
    info['ff_bundled'] = 'Fernflower' in tools
    info['jad_patches'] = jad_patch
    info['ff_patches'] = ff_patch
    info['scripts'] = sorted(set(os.path.splitext(os.path.basename(n))[0] for n in names
                                 if '/' not in n and n.endswith(('.sh', '.bat'))))
    info['changelog_head'] = '\n'.join(changelog.strip().splitlines()[:12]) if changelog else ''
    info['generation'] = knowledge.classify(info)
    return info


def build(log=None):
    packs = []
    if os.path.isdir(PACKZIPS):
        for version in os.listdir(PACKZIPS):
            if version.startswith('_'):
                continue
            vdir = os.path.join(PACKZIPS, version)
            for f in os.listdir(vdir):
                if not f.endswith('.zip'):
                    continue
                try:
                    packs.append(fingerprint(os.path.join(vdir, f), version, f[:-4]))
                except (zipfile.BadZipFile, OSError) as ex:
                    if log:
                        log('skipping %s/%s: %s' % (version, f, ex))
    packs.sort(key=lambda p: (_version_key(p['folder_version']), _pack_key(p)))
    # mark the newest pack per Minecraft version as recommended
    by_version = {}
    for p in packs:
        by_version.setdefault(p['folder_version'], []).append(p)
    for plist in by_version.values():
        for p in plist:
            p['recommended'] = p is plist[-1]
    with open(CATALOG_FILE, 'w', encoding='utf-8') as fh:
        json.dump(packs, fh, indent=1)
    return packs


def load():
    if os.path.isfile(CATALOG_FILE):
        with open(CATALOG_FILE, encoding='utf-8') as fh:
            return json.load(fh)
    return []


def get(pack_id):
    for p in load():
        if p['id'] == pack_id:
            return p
    return None


def extract(pack, dest):
    """Extract a pack zip (fixing Windows backslash paths) into dest."""
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(pack['zip']) as zf:
        for info in zf.infolist():
            name = _norm(info.filename)
            if not name or name.startswith('/') or '..' in name.split('/'):
                continue
            target = os.path.join(dest, name)
            if name.endswith('/'):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                os.chmod(target, mode | 0o600)
    # the shipped shell scripts and mac binaries need +x
    for root, _dirs, files in os.walk(dest):
        for f in files:
            if f.endswith('.sh') or f in ('jad-osx', 'astyle-osx'):
                p = os.path.join(root, f)
                os.chmod(p, os.stat(p).st_mode | 0o755)
    addon_dir = os.path.join(PACKZIPS, '_addons')
    if pack['pack'] == 'mcp25' and os.path.isdir(addon_dir):
        # "reobf-fix-mcp2.5.zip" replaced the broken MCP 2.5 obfuscathon
        for f in os.listdir(addon_dir):
            with zipfile.ZipFile(os.path.join(addon_dir, f)) as zf:
                for info in zf.infolist():
                    name = _norm(info.filename)
                    if name.endswith('/'):
                        continue
                    target = os.path.join(dest, name)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src, open(target, 'wb') as out:
                        shutil.copyfileobj(src, out)


def read_member(pack, member):
    with zipfile.ZipFile(pack['zip']) as zf:
        for n in zf.namelist():
            if _norm(n) == member:
                return zf.read(n)
    return None
