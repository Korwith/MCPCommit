#!/usr/bin/env python3
"""Headless OmniMCP mod builder run by action.yml: prepare, decompile, build."""

import argparse
import fnmatch
import hashlib
import json
import re
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from studio import catalog, gamefiles, javaenv, mods, paths, workspace  # noqa: E402

WS_ID = 'mod'
SIDE_DIR = {'client': 'minecraft', 'server': 'minecraft_server'}
ARCHIVE = 'https://archive.org/download/minecraftcoderpack/minecraftcoderpack.zip/'
ADDON_URL = 'https://archive.org/download/minecraftcoderpack/reobf-fix-mcp2.5.zip'

# Every pack in the archive.org MCP archive: folder -> packs, oldest first (the last one is the default).
PACK_INDEX = {
    'a1.1.2': ['revengpack', 'revengpack11', 'revengpack12', 'revengpack13', 'revengpack14',
               'revengpack15', 'revengpack16'],
    'a1.2.1_01': ['mcp20', 'mcp20a'], 'a1.2.2': ['mcp21', 'mcp22', 'mcp22a'], 'a1.2.3_04': ['mcp23'],
    'a1.2.5': ['mcp24'], 'a1.2.6': ['mcp25'], 'b1.1_02': ['mcp26'], 'b1.2_01': ['mcp27', 'mcp28'],
    'b1.3_01': ['mcp29', 'mcp29a'], 'b1.4': ['mcp210'], 'b1.4_01': ['mcp211', 'mcp30'],
    'b1.5._01': ['mcp212', 'mcp31'], 'b1.6.4': ['mcp32'], 'b1.6.5': ['mcp33'],
    'b1.6.6': ['mcp34', 'mcp40', 'mcp41'], 'b1.7.2': ['mcp42'], 'b1.7.3': ['mcp43'], 'b1.8.1': ['mcp44'],
    'b1.9pre-5': ['mcp45pre'], '1.0.0': ['mcp50'], '1.1.0': ['mcp56'], '1.2.3': ['mcp60'],
    '1.2.4': ['mcp61'], '1.2.5': ['mcp62'], '12w17a': ['mcp65'], '12w26a': ['mcp615'],
    '1.3.1': ['mcp70', 'mcp70a'], '1.3.2': ['mcp72'], '1.4.2': ['mcp719'], '1.4.4': ['mcp721'],
    '1.4.5': ['mcp723'], '1.4.6': ['mcp725'], '1.4.7': ['mcp726a'], '13w02b': ['mcp730c'],
    '13w05b': ['mcp734'], '13w09c': ['mcp739'], '1.5': ['mcp742'], '1.5.1': ['mcp744'],
    '1.5.2': ['mcp751'], '1.6.1': ['mcp803'], '1.6.2': ['mcp804', 'mcp805'], '1.6.4': ['mcp811'],
    '1.7.2': ['mcp903'], '1.7.10': ['mcp908'], '1.8': ['mcp910-pre1'], '1.8.8': ['mcp918'],
    '1.9': ['mcp924-beta1'], '1.9.4': ['mcp928'], '1.10.2': ['mcp931'], '1.11.2': ['mcp937'],
    '1.12': ['mcp940'],
}
# generations whose decompiler is JAD, a Windows program run through wine
JAD_GENERATIONS = {'reveng', 'rgs', 'mcp2', 'mcp3', 'mcp5', 'mcp6'}


# helpers

def log(msg):
    print(msg, flush=True)


def fail(msg):
    if os.environ.get('GITHUB_ACTIONS'):
        print('::error title=OmniMCP::%s' % msg.replace('\n', '%0A'), flush=True)
    sys.exit('error: %s' % msg)


class group(object):
    """A collapsible section in the GitHub Actions log."""

    def __init__(self, title):
        self.title = title

    def __enter__(self):
        log(('::group::%s' if os.environ.get('GITHUB_ACTIONS') else '== %s ==') % self.title)

    def __exit__(self, *exc):
        if os.environ.get('GITHUB_ACTIONS'):
            log('::endgroup::')


def set_output(**values):
    out = os.environ.get('GITHUB_OUTPUT')
    for k, v in values.items():
        log('output %s=%s' % (k, v))
        if out:
            with open(out, 'a', encoding='utf-8') as fh:
                fh.write('%s=%s\n' % (k, v))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def sha1_files(files):
    h = hashlib.sha1()
    for f in files:
        with open(f, 'rb') as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]


def download(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    log('download %s' % url)
    req = urllib.request.Request(url, headers={'User-Agent': gamefiles.USER_AGENT})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, open(dest + '.part', 'wb') as out:
                shutil.copyfileobj(resp, out, 1 << 20)
            os.replace(dest + '.part', dest)
            return dest
        except OSError as ex:
            if attempt == 3:
                raise
            log('  retrying after %s' % ex)
            time.sleep(5 * (attempt + 1))


# config

class Config(object):
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.root = os.path.dirname(self.path)
        try:
            with open(self.path, encoding='utf-8') as fh:
                self.raw = json.load(fh)
        except OSError:
            fail('config not found: %s' % path)
        except ValueError as ex:
            fail('%s is not valid JSON: %s' % (path, ex))
        r = self.raw
        for key in ('name', 'minecraft'):
            if not r.get(key):
                fail('%s: "%s" is required' % (path, key))
        self.name = str(r['name'])
        self.version = self._version(r.get('version'))
        self.minecraft = str(r['minecraft'])
        self.java = int(r.get('java') or 8)
        self.patched_jar = bool(r.get('patched_jar', False))
        self.sources_zip = bool(r.get('sources_zip', True))
        self.filename = r.get('filename')
        self.library_sources = set()  # filled by overlay()
        self.sources = self._side_paths('sources')
        self.libraries = self._side_paths('libraries', many=True)
        self.patches = self._side_paths('patches', many=True)
        self.resources = self._side_paths('resources')
        exclude = r.get('exclude') or []
        self.exclude = [exclude] if isinstance(exclude, str) else list(exclude)
        self.jar_mods = {}
        for side, items in (r.get('jar_mods') or {}).items():
            self._check_side(side, 'jar_mods')
            items = [items] if isinstance(items, (str, dict)) else items
            self.jar_mods[side] = [self._jar_mod(p) for p in items]
        sides = r.get('sides')
        if sides is None:
            sides = [s for s in ('client', 'server') if s in self.sources or s in self.patches]
        for s in sides:
            self._check_side(s, 'sides')
        if not sides:
            fail('no sides to build: add "sources" or "patches" for client and/or server')
        self.sides = sides
        self.folder, self.pack = self._pick_pack(r.get('mcp'))
        self.mcp_url = r.get('mcp_url')

    def _check_side(self, side, key):
        if side not in SIDE_DIR:
            fail('%s: unknown side "%s" (use client or server)' % (key, side))

    def _file(self, rel):
        p = os.path.normpath(os.path.join(self.root, rel))
        if not os.path.exists(p):
            fail('%s does not exist' % rel)
        return p

    def _version(self, v):
        """A version string, or {"file": path, "regex": pattern} to read it from a source file."""
        if not isinstance(v, dict):
            return str(v or 'dev')
        if not v.get('file') or not v.get('regex'):
            fail('version: give both "file" and "regex"')
        with open(self._file(v['file']), encoding='utf-8', errors='replace') as fh:
            m = re.search(v['regex'], fh.read())
        if not m:
            fail('version: /%s/ matches nothing in %s' % (v['regex'], v['file']))
        return m.group(1) if m.groups() else m.group(0)

    def _jar_mod(self, item):
        """A path in the repo, or {"url": ..., "sha256": ...} downloaded into the cache when needed."""
        if isinstance(item, str):
            return self._file(item)
        if not item.get('url'):
            fail('jar_mods: an entry without "url"')
        return dict(item)

    def jar_mod_files(self, side):
        out = []
        for item in self.jar_mods.get(side, []):
            if isinstance(item, str):
                out.append(item)
                continue
            url, want = item['url'], (item.get('sha256') or '').lower()
            name = item.get('name') or os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path))
            dest = os.path.join(paths.CACHE_DIR, 'jar_mods', hashlib.sha1(url.encode('utf-8')).hexdigest()[:12], name)
            if not (os.path.isfile(dest) and (not want or sha256_file(dest) == want)):
                download(url, dest)
                got = sha256_file(dest)
                if want and got != want:
                    os.remove(dest)
                    fail('jar_mods: %s has sha256 %s, expected %s' % (url, got, want))
            out.append(dest)
        return out

    def excluded(self, cls, source):
        """True for a class that is compiled but not shipped: from "libraries", or matched by "exclude"."""
        if source in self.library_sources:
            return True
        outer = cls.split('$', 1)[0]
        return any(fnmatch.fnmatchcase(cls, p) or fnmatch.fnmatchcase(outer, p) for p in self.exclude)

    def output_name(self, side, suffix=''):
        if not self.filename:
            return None
        name = self.filename.format(name=self.name, version=self.version, minecraft=self.minecraft, side=side)
        if suffix:
            stem, ext = os.path.splitext(name)
            name = stem + suffix + (ext or '.jar')
        return name

    def _side_paths(self, key, many=False):
        """Per side: one path, or with many=True a path or list of paths (always returned as a list)."""
        out = {}
        for side, rel in (self.raw.get(key) or {}).items():
            self._check_side(side, key)
            if many:
                out[side] = [self._file(r) for r in ([rel] if isinstance(rel, str) else rel)]
            else:
                out[side] = self._file(rel)
        return out

    def _pick_pack(self, pack):
        aliases = {v: k for k, v in catalog.DISPLAY_VERSION.items()}
        folder = aliases.get(self.minecraft, self.minecraft)
        if folder not in PACK_INDEX:
            fail('MCP has no release for Minecraft %s. Known versions: %s'
                 % (self.minecraft, ', '.join(PACK_INDEX)))
        packs = PACK_INDEX[folder]
        if pack:
            pack = str(pack).lower().replace('.', '')
            if pack not in packs:
                fail('pack %s is not for Minecraft %s; choose one of %s' % (pack, self.minecraft, ', '.join(packs)))
            return folder, pack
        return folder, packs[-1]

    @property
    def pack_id(self):
        return '%s__%s' % (self.folder, self.pack)

    def cache_key(self):
        local = [f for side in self.sides for f in self.jar_mods.get(side, []) if isinstance(f, str)]
        remote = [f['url'] + '#' + (f.get('sha256') or '') for side in self.sides
                  for f in self.jar_mods.get(side, []) if isinstance(f, dict)]
        mods_hash = 'vanilla'
        if local or remote:
            h = hashlib.sha1((sha1_files(local) if local else '').encode('utf-8'))
            for u in remote:
                h.update(u.encode('utf-8'))
            mods_hash = h.hexdigest()[:12]
        with open(__file__, 'rb') as fh:
            tool = hashlib.sha1(fh.read()).hexdigest()[:8]
        return 'omnimcp-ws-%s-%s-java%d-%s-%s' % (self.pack_id, '+'.join(self.sides), self.java, mods_hash, tool)


# phase 1: prepare

def ensure_pack(cfg):
    pack = catalog.get(cfg.pack_id)
    if pack and os.path.isfile(pack['zip']):
        return pack
    dest = os.path.join(catalog.PACKZIPS, cfg.folder, cfg.pack + '.zip')
    if not os.path.isfile(dest):
        url = cfg.mcp_url or ARCHIVE + urllib.parse.quote('minecraftcoderpack/%s/%s.zip' % (cfg.folder, cfg.pack),
                                                            safe='')
        download(url, dest)
        if not zipfile.is_zipfile(dest):
            os.remove(dest)
            fail('download of %s is not a zip file' % url)
    if cfg.pack == 'mcp25':
        addon = os.path.join(catalog.PACKZIPS, '_addons', 'reobf-fix-mcp2.5.zip')
        if not os.path.isfile(addon):
            download(ADDON_URL, addon)
    catalog.build(log)
    pack = catalog.get(cfg.pack_id)
    if not pack:
        fail('could not read MCP pack %s' % cfg.pack_id)
    tools_pack = os.path.join(catalog.PACKZIPS, 'b1.5._01', 'mcp212.zip')
    if pack['engine'] == 'legacy' and not os.path.isfile(tools_pack):
        # batch-era packs borrow jadretro and the renamer from MCP 2.12
        download(ARCHIVE + urllib.parse.quote('minecraftcoderpack/b1.5._01/mcp212.zip', safe=''), tools_pack)
        catalog.build(log)
    return pack


def prepare(cfg):
    with group('MCP pack'):
        pack = ensure_pack(cfg)
        log('%s: MCP %s for Minecraft %s (%s generation, %s)'
            % (cfg.pack_id, pack['mcp_version'], pack['version'], pack['generation'], pack['engine']))
    # wine runs JAD and astyle.exe (MCP 5.6+); without it the decompiled source differs
    needs_wine = (pack['generation'] in JAD_GENERATIONS or 'AStyle' in pack['tools']
                  or (pack['generation'] == 'mcp7' and not pack['ff_bundled']))
    set_output(pack=cfg.pack_id, java=cfg.java, version=cfg.version, wine=str(needs_wine).lower(), cache_key=cfg.cache_key(),
               artifact='%s-%s-mc%s' % (cfg.name, cfg.version, cfg.minecraft), sides=','.join(cfg.sides),
               workspace=workspace.ws_dir(WS_ID))
    return pack


# phase 2: decompile

def current_jdk(cfg):
    jdks = javaenv.find_jdks(refresh=True)
    full = [j for j in jdks if j['has_javac']]
    home = os.environ.get('JAVA_HOME')
    jdk = next((j for j in full if home and j['home'] == os.path.realpath(home)), None)
    jdk = jdk or next((j for j in full if j['major'] == cfg.java), None) or javaenv.preferred_jdk(jdks)
    if not jdk:
        fail('no JDK found; install JDK %d (CI: actions/setup-java)' % cfg.java)
    if jdk['major'] != cfg.java:
        log('WARNING: config asks for Java %d, using %s' % (cfg.java, jdk['version']))
    return jdk


def input_jar(cfg, pack, side):
    version = pack['client_version'] if side == 'client' else pack['server_version']
    jar, source = gamefiles.get_jar(version, side, log)
    if not jar:
        fail('Mojang has no %s jar for %s' % (side, version))
    log('%s jar (%s): %s' % (side, source, jar))
    extras = cfg.jar_mod_files(side)
    if not extras:
        return jar
    merged = os.path.join(paths.CACHE_DIR, 'input', '%s-%s.jar' % (pack['id'], side))
    os.makedirs(os.path.dirname(merged), exist_ok=True)
    entries = {}
    for src in [jar] + extras:
        with zipfile.ZipFile(src) as zf:
            for info in zf.infolist():
                if info.filename.endswith('/') or info.filename.upper().startswith('META-INF/'):
                    continue
                entries[info.filename] = zf.read(info)
        if src is not jar:
            log('merged %s into the %s jar' % (os.path.relpath(src, cfg.root), side))
    with zipfile.ZipFile(merged, 'w', zipfile.ZIP_DEFLATED) as out:
        for name in sorted(entries):
            out.writestr(name, entries[name])
    return merged


def run_action(meta, action, sides):
    names = [a['name'] for a in workspace.actions(meta)]
    if action not in names:
        return False
    flags = ['--client'] if sides == ['client'] else ['--server'] if sides == ['server'] else []
    cmd = workspace.command_for(meta, action, flags)
    logpath = os.path.join(workspace.ws_dir(meta['id']), 'logs', 'studio-%s.log' % action)
    os.makedirs(os.path.dirname(logpath), exist_ok=True)
    with group('MCP %s' % action), open(logpath, 'w', encoding='utf-8') as logfile:
        proc = subprocess.Popen(cmd, cwd=paths.APP_DIR, env=workspace.run_env(meta),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        for raw in proc.stdout:
            line = raw.decode('utf-8', 'replace').rstrip('\r\n')
            print(line, flush=True)
            logfile.write(line + '\n')
        code = proc.wait()
    if code != 0:
        fail('MCP %s failed (exit %d); see the log above' % (action, code))
    return True


def decompile(cfg, pack):
    d = workspace.ws_dir(WS_ID)
    jdk = current_jdk(cfg)
    if os.path.isfile(os.path.join(d, workspace.META)) and os.path.isfile(os.path.join(d, mods.BASELINE)):
        meta = workspace.load(WS_ID)
        if meta.get('pack_id') == cfg.pack_id:
            workspace.set_jdk(meta, jdk['home'])
            log('decompiled workspace restored from cache (%s); skipping decompile' % cfg.pack_id)
            return meta
    if os.path.isdir(d):
        shutil.rmtree(d)
    with group('Game jars'):
        jars = {side: input_jar(cfg, pack, side) for side in cfg.sides}
        meta = workspace.create(WS_ID, cfg.pack_id, jars.get('client'), jars.get('server'), jdk['home'], log)
        if meta['id'] != WS_ID:
            fail('workspace id clash: %s' % meta['id'])
    run_action(meta, 'decompile', cfg.sides)
    meta = workspace.load(WS_ID)
    for side in cfg.sides:
        root = mods._first_dir(d, mods.SOURCE_DIRS, SIDE_DIR[side])
        if not root or not any(True for _ in mods._walk(os.path.join(d, root), '.java')):
            fail('decompile produced no %s sources' % side)
    mods.snapshot(meta, log)
    save_pristine(meta)
    return meta


PRISTINE = os.path.join('.workbench', 'pristine-{side}.zip')


def save_pristine(meta):
    """Zip the freshly decompiled sources so later builds can start from them again."""
    d = workspace.ws_dir(meta['id'])
    for side, info in mods.load_baseline(meta)['sides'].items():
        with zipfile.ZipFile(os.path.join(d, PRISTINE.format(side=side)), 'w', zipfile.ZIP_DEFLATED) as zf:
            for rel in info['files']:
                zf.write(os.path.join(d, info['root'], rel), rel)


def reset_sources(meta):
    """Undo an earlier build in this workspace: restore changed sources, remove added ones."""
    d = workspace.ws_dir(meta['id'])
    baseline = mods.load_baseline(meta)
    reset = 0
    for side, info in baseline['sides'].items():
        root = os.path.join(d, info['root'])
        pristine = os.path.join(d, PRISTINE.format(side=side))
        if not os.path.isfile(pristine):
            continue
        with zipfile.ZipFile(pristine) as zf:
            for rel, full in list(mods._walk(root, '.java')):
                old = info['files'].get(rel)
                if old is None:
                    os.remove(full)
                    reset += 1
                elif mods._sha1(full) != old:
                    with open(full, 'wb') as fh:
                        fh.write(zf.read(rel))
                    reset += 1
            for rel in info['files']:
                full = os.path.join(root, rel)
                if not os.path.isfile(full):
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    with open(full, 'wb') as fh:
                        fh.write(zf.read(rel))
                    reset += 1
    if reset:
        log('reset %d source files left over from an earlier build' % reset)


# phase 3: build

def overlay(cfg, meta):
    d = workspace.ws_dir(meta['id'])
    with group('Apply mod'):
        for side in cfg.sides:
            root_rel = mods._first_dir(d, mods.SOURCE_DIRS, SIDE_DIR[side])
            root = os.path.join(d, root_rel)
            n = 0
            if side in cfg.libraries:
                # compiled with the mod, never shipped (APIs of other mods, stubs)
                for lib in cfg.libraries[side]:
                    for rel, full in mods._walk(lib, '.java'):
                        target = os.path.join(root, rel)
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        shutil.copyfile(full, target)
                        cfg.library_sources.add(rel.replace(os.sep, '/'))
                        n += 1
                log('%s: %d library source files (not shipped) -> %s' % (side, n, root_rel))
                n = 0
            if side in cfg.sources:
                for rel, full in mods._walk(cfg.sources[side], '.java'):
                    target = os.path.join(root, rel)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copyfile(full, target)
                    n += 1
                log('%s: %d source files -> %s' % (side, n, root_rel))
            if side in cfg.patches:
                files = []
                for pdir in cfg.patches[side]:
                    files += [pdir] if os.path.isfile(pdir) else sorted(
                        os.path.join(r, f) for r, _ds, fs in os.walk(pdir) for f in fs if f.endswith(('.patch', '.diff')))
                for pf in files:
                    log('%s: patch %s' % (side, os.path.relpath(pf, cfg.root)))
                    res = subprocess.run(['patch', '-p1', '-N', '--no-backup-if-mismatch', '-d', root, '-i', pf],
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    out = res.stdout.decode('utf-8', 'replace')
                    print(out, end='', flush=True)
                    if res.returncode != 0:
                        fail('patch %s does not apply to the decompiled %s source'
                             % (os.path.relpath(pf, cfg.root), side))


def _jar_name(cfg, side, suffix=''):
    return cfg.output_name(side, suffix) or '%s-%s-mc%s-%s%s.jar' % (cfg.name, cfg.version, cfg.minecraft, side, suffix)


def _shipped(cfg, st):
    """The side's mod classes minus those cfg.excluded() keeps out of the jar."""
    keep, dropped = [], []
    for c in st['classes']:
        (dropped if cfg.excluded(c['class'], c['source'].replace(os.sep, '/')) else keep).append(c)
    if dropped:
        log('not shipped (libraries/exclude): %s' % ', '.join(c['class'] for c in dropped))
    return keep


def _add_resources(zf, cfg, side, seen):
    if side not in cfg.resources:
        return 0
    n = 0
    for rel, full in mods._walk(cfg.resources[side]):
        if rel not in seen:
            zf.write(full, rel)
            seen.add(rel)
            n += 1
    return n


def package(cfg, meta, pack, out_dir):
    d = workspace.ws_dir(meta['id'])
    status = {s['side']: s for s in mods.status(meta)['sides']}
    os.makedirs(out_dir, exist_ok=True)
    stale = set(_jar_name(cfg, side, sfx) for side in cfg.sides for sfx in ('', '-patched'))
    for f in os.listdir(out_dir):  # drop files from an earlier build so the artifact is never stale
        if f == 'BUILD.txt' or f in stale or (f.startswith(cfg.name + '-') and f.endswith(('.jar', '.zip'))):
            os.remove(os.path.join(out_dir, f))
    produced = []
    with group('Package'):
        for side in cfg.sides:
            st = status.get(SIDE_DIR[side])
            if not st:
                fail('no %s sources in the workspace' % side)
            missing = [m for m in st['missing'] if not cfg.excluded(m['class'], m['source'].replace(os.sep, '/'))]
            if missing:
                fail('these %s sources produced no reobfuscated class (compile error, or this MCP release '
                     'cannot reobfuscate): %s' % (side, ', '.join(m['source'] for m in missing)))
            st['classes'] = _shipped(cfg, st)
            if not st['classes'] and side not in cfg.resources:
                fail('nothing to package for %s: the mod does not change any game source' % side)
            reobf = os.path.join(d, st['reobf_root']) if st['reobf_root'] else None
            jar = os.path.join(out_dir, _jar_name(cfg, side))
            seen = set()
            with zipfile.ZipFile(jar, 'w', zipfile.ZIP_DEFLATED) as zf:
                for c in st['classes']:
                    name = c['obf'] + '.class'
                    zf.write(os.path.join(reobf, name), name)
                    seen.add(name)
                    log('  %-55s -> %s' % (c['class'], name))
                res = _add_resources(zf, cfg, side, seen)
            log('%s: %d classes, %d resources -> %s' % (side, len(st['classes']), res, os.path.basename(jar)))
            produced.append(jar)
            set_output(**{side + '_jar': jar})
            if side == cfg.sides[0]:
                set_output(mod_file=jar)

            if cfg.patched_jar:
                base = input_jar(cfg, pack, side)
                full = os.path.join(out_dir, _jar_name(cfg, side, '-patched'))
                seen = set()
                with zipfile.ZipFile(full, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for c in st['classes']:
                        name = c['obf'] + '.class'
                        zf.write(os.path.join(reobf, name), name)
                        seen.add(name)
                    _add_resources(zf, cfg, side, seen)
                    with zipfile.ZipFile(base) as vz:
                        for info in vz.infolist():
                            n = info.filename
                            if n in seen or n.endswith('/') or n.upper().startswith('META-INF/'):
                                continue
                            zf.writestr(info, vz.read(info))
                log('%s: full game jar with the mod installed -> %s' % (side, os.path.basename(full)))
                produced.append(full)
                set_output(**{side + '_patched_jar': full})

        if cfg.sources_zip:
            src = os.path.join(out_dir, '%s-%s-mc%s-sources.zip' % (cfg.name, cfg.version, cfg.minecraft))
            mods.build_zip(meta, 'src', None if len(cfg.sides) > 1 else SIDE_DIR[cfg.sides[0]], src)
            produced.append(src)
            set_output(sources_zip=src)
        lines = ['%s %s for Minecraft %s (MCP %s, %s)' % (cfg.name, cfg.version, meta['client_version'],
                                                         meta['mcp_version'], meta['pack']),
                 'Built %s by OmniMCP' % time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime()), '',
                 'Install: copy the contents of the mod jar into the game jar and delete its META-INF folder.', '']
        for side in cfg.sides:
            st = status[SIDE_DIR[side]]
            lines.append('[%s] %s' % (side, _jar_name(cfg, side)))
            lines += ['  %-55s -> %s.class' % (c['class'], c['obf']) for c in st['classes']]
            lines.append('')
        with open(os.path.join(out_dir, 'BUILD.txt'), 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(lines))
    set_output(out_dir=out_dir)
    log('built %d files in %s' % (len(produced), out_dir))
    return produced


def build(cfg, pack, out_dir):
    meta = workspace.load(WS_ID)
    meta['name'] = cfg.name
    workspace.set_jdk(meta, current_jdk(cfg)['home'])
    reset_sources(meta)
    overlay(cfg, meta)
    run_action(meta, 'recompile', cfg.sides)
    if not run_action(meta, 'reobfuscate', cfg.sides) and meta.get('generation') != 'reveng':
        fail('MCP %s (%s) has no Reobfuscate step; pick a newer MCP pack for %s'
             % (meta['mcp_version'], meta['pack'], cfg.minecraft))
    return package(cfg, workspace.load(WS_ID), pack, out_dir)


def main():
    ap = argparse.ArgumentParser(description='Build an MCP jar mod from omnimcp.json')
    ap.add_argument('phase', choices=('prepare', 'decompile', 'build', 'all'))
    ap.add_argument('--config', default='omnimcp.json')
    ap.add_argument('--out', default='build/omnimcp', help='output folder for jars (build phase)')
    args = ap.parse_args()
    if sys.version_info < (3, 8):
        sys.exit('needs Python 3.8+')
    cfg = Config(args.config)
    log('OmniMCP: %s %s for Minecraft %s, %s, sides %s'
        % (cfg.name, cfg.version, cfg.minecraft, cfg.pack_id, '+'.join(cfg.sides)))
    pack = prepare(cfg)
    if args.phase in ('decompile', 'all'):
        decompile(cfg, pack)
    if args.phase in ('build', 'all'):
        if args.phase == 'build' and not os.path.isfile(os.path.join(workspace.ws_dir(WS_ID), mods.BASELINE)):
            decompile(cfg, pack)
        build(cfg, pack, os.path.abspath(args.out))


if __name__ == '__main__':
    main()
