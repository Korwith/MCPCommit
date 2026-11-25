"""Mod packaging: what changed in a workspace, and exports of it."""

import hashlib
import io
import json
import os
import re
import time
import zipfile

from . import workspace

BASELINE = os.path.join('.workbench', 'baseline.json')
SIDES = ('minecraft', 'minecraft_server')
SIDE_LABEL = {'minecraft': 'client', 'minecraft_server': 'server'}

# where each generation kept decompiled sources, compiled classes and reobfuscated output
SOURCE_DIRS = ['src/{side}', 'sources/{side}', '{side}.src']
REOBF_DIRS = ['reobf/{side}', 'final_out/{side}']
BIN_DIRS = ['bin/{side}', '{side}']


def _sha1(path):
    h = hashlib.sha1()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _first_dir(base, templates, side):
    for t in templates:
        rel = t.format(side=side)
        if os.path.isdir(os.path.join(base, rel)):
            return rel
    return None


def _walk(root, ext=None):
    for r, _dirs, files in os.walk(root):
        for f in files:
            if ext is None or f.endswith(ext):
                full = os.path.join(r, f)
                yield os.path.relpath(full, root).replace(os.sep, '/'), full


# Baseline


def snapshot(meta, log=None):
    """Record the hash of every decompiled source file as the vanilla baseline."""
    base = workspace.ws_dir(meta['id'])
    data = {'created': time.time(), 'sides': {}}
    for side in SIDES:
        rel = _first_dir(base, SOURCE_DIRS, side)
        if not rel:
            continue
        files = {p: _sha1(full) for p, full in _walk(os.path.join(base, rel), '.java')}
        data['sides'][side] = {'root': rel, 'files': files}
        if log:
            log('baseline: %d %s sources recorded' % (len(files), SIDE_LABEL[side]))
    path = os.path.join(base, BASELINE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh)
    return data


def load_baseline(meta):
    try:
        with open(os.path.join(workspace.ws_dir(meta['id']), BASELINE), encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _decompile_time(base):
    """When decompilation finished, for workspaces without a baseline."""
    for rel in ('logs/studio-decompile.log', 'logs/client_compile.log', 'logs/mcp.log'):
        p = os.path.join(base, rel)
        if os.path.isfile(p):
            return os.path.getmtime(p)
    return None


# Class name mapping

_CL = re.compile(r'^CL:\s+(\S+)\s+(\S+)')
_OUTPUTTED = re.compile(r'Outputted\s+(\S+)\s+to\s+\S*?(minecraft(?:_server)?)\S*\s+as\s+(\S+?)\.class')


def obf_map(base, side):
    """deobfuscated class name -> obfuscated name, from the SRGs and reobf logs."""
    label = SIDE_LABEL[side]
    m = {}
    cands = [os.path.join(base, 'temp', f) for f in (
        '%s_ro.srg' % label, '%s_rg.srg' % label, '%s_ro_cls.srg' % label, 'client_ro_cls.srg')]
    cands += [os.path.join(base, 'conf', f) for f in ('%s.srg' % label, 'joined.srg')]
    for path in cands:
        if not os.path.isfile(path):
            continue
        with open(path, encoding='latin-1') as fh:
            for line in fh:
                mm = _CL.match(line)
                if mm:
                    m.setdefault(mm.group(2), mm.group(1))
    # MCP 3-7 log each class written by Reobfuscate; that is the ground truth when present
    logs = os.path.join(base, 'logs')
    if os.path.isdir(logs):
        for f in os.listdir(logs):
            if 'reob' not in f:
                continue
            with open(os.path.join(logs, f), encoding='latin-1') as fh:
                for line in fh:
                    mm = _OUTPUTTED.search(line)
                    if mm and mm.group(2) == side:
                        m[mm.group(1)] = mm.group(3)
    return m


def _obf_name(cls, mapping):
    if cls in mapping:
        return mapping[cls]
    if '$' in cls:
        outer, inner = cls.split('$', 1)
        if outer in mapping:
            return mapping[outer] + '$' + inner
    return cls  # a class the mod added keeps its own name


# Status


def _side_status(base, side, baseline, generation=None):
    src_rel = _first_dir(base, SOURCE_DIRS, side)
    if not src_rel:
        return None
    src_root = os.path.join(base, src_rel)
    current = {p: full for p, full in _walk(src_root, '.java')}
    changed, added, removed = [], [], []
    method = 'baseline'
    base_side = (baseline or {}).get('sides', {}).get(side)
    if base_side:
        old = base_side['files']
        for p, full in current.items():
            if p not in old:
                added.append(p)
            elif _sha1(full) != old[p]:
                changed.append(p)
        removed = sorted(p for p in old if p not in current)
    else:
        method = 'file times'
        t0 = _decompile_time(base)
        if t0 is not None:
            for p, full in current.items():
                if os.path.getmtime(full) > t0 + 2:
                    changed.append(p)
    changed.sort()
    added.sort()
    mod_sources = sorted(changed + added)

    reobf_rel = _first_dir(base, REOBF_DIRS, side)
    bin_rel = _first_dir(base, BIN_DIRS, side)
    mapping = obf_map(base, side) if mod_sources else {}
    if generation == 'reveng' and not reobf_rel:
        # RevEng never renamed anything: the compiled classes already carry obfuscated names
        reobf_rel, mapping = bin_rel, {}
    classes, missing = [], []
    reobf_root = os.path.join(base, reobf_rel) if reobf_rel else None
    bin_root = os.path.join(base, bin_rel) if bin_rel else None
    newest_src = max((os.path.getmtime(current[p]) for p in mod_sources), default=0)
    for p in mod_sources:
        cls = p[:-5]
        # the source's own class plus its inner/anonymous classes, as compiled
        names = [cls]
        if bin_root:
            d = os.path.join(bin_root, os.path.dirname(cls))
            stem = os.path.basename(cls) + '$'
            if os.path.isdir(d):
                names += sorted(os.path.join(os.path.dirname(cls), f[:-6]).replace(os.sep, '/')
                                for f in os.listdir(d) if f.startswith(stem) and f.endswith('.class'))
        for n in names:
            obf = _obf_name(n, mapping)
            found = reobf_root and os.path.isfile(os.path.join(reobf_root, obf + '.class'))
            entry = {'source': p, 'class': n, 'obf': obf}
            if found:
                classes.append(entry)
            elif n == cls:
                missing.append(entry)
    stale = bool(reobf_root and mod_sources and newest_src > os.path.getmtime(reobf_root))
    total_reobf = sum(1 for _ in _walk(reobf_root, '.class')) if reobf_root else 0
    return {
        'side': side, 'label': SIDE_LABEL[side], 'source_root': src_rel, 'reobf_root': reobf_rel,
        'method': method, 'sources_total': len(current),
        'changed': changed, 'added': added, 'removed': removed,
        'classes': classes, 'missing': missing, 'stale': stale, 'reobf_total': total_reobf,
    }


def status(meta):
    base = workspace.ws_dir(meta['id'])
    baseline = load_baseline(meta)
    sides = [s for s in (_side_status(base, side, baseline, meta.get('generation')) for side in SIDES) if s]
    return {'baseline': baseline['created'] if baseline else None, 'sides': sides}


# Exports

KINDS = ('obf', 'src', 'fullsrc', 'bundle')


def export_name(meta, kind, side=None):
    stem = '%s-%s' % (meta['name'], meta['client_version'])
    part = {'obf': 'obf', 'src': 'src', 'fullsrc': 'decompiled', 'bundle': 'mod'}[kind]
    if side and kind != 'bundle':
        return '%s-%s-%s.zip' % (stem, SIDE_LABEL[side], part)
    return '%s-%s.zip' % (stem, part)


def _write_obf(zf, base, st, prefix=''):
    root = os.path.join(base, st['reobf_root'])
    for c in st['classes']:
        zf.write(os.path.join(root, c['obf'] + '.class'), prefix + c['obf'] + '.class')
    return len(st['classes'])


def _write_src(zf, base, st, prefix=''):
    root = os.path.join(base, st['source_root'])
    files = st['changed'] + st['added']
    for p in files:
        zf.write(os.path.join(root, p), prefix + p)
    return len(files)


def _manifest(meta, sts):
    lines = ['%s for Minecraft %s (MCP %s)' % (meta['name'], meta['client_version'], meta['mcp_version']),
             'Built %s by MCP Workbench' % time.strftime('%Y-%m-%d %H:%M'), '']
    for st in sts:
        if not (st['classes'] or st['changed'] or st['added'] or st['removed']):
            continue
        lines.append('[%s]' % st['label'])
        lines.append('  install: copy the files in %s/obf/ into the %s jar (delete META-INF from it)'
                     % (st['label'], st['label']))
        for c in st['classes']:
            lines.append('  %-50s -> %s.class' % (c['class'], c['obf']))
        for p in st['removed']:
            lines.append('  (deleted source) %s' % p)
        lines.append('')
    return '\n'.join(lines)


def build_zip(meta, kind, side=None, dest=None):
    """Write an export zip to dest (or return its bytes)."""
    if kind not in KINDS:
        raise ValueError('unknown export kind %s' % kind)
    base = workspace.ws_dir(meta['id'])
    st_all = status(meta)['sides']
    sts = [s for s in st_all if side in (None, s['side'])]
    if not sts:
        raise IOError('this workspace has no decompiled sources yet; run Decompile first')
    target = dest or io.BytesIO()
    count = 0
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as zf:
        if kind == 'obf':
            for st in sts:
                if not st['reobf_root'] or not st['classes']:
                    continue
                count += _write_obf(zf, base, st, '' if len(sts) == 1 else st['label'] + '/')
            if not count:
                raise IOError('no reobfuscated mod classes yet: change some sources, then run '
                              'Recompile and Reobfuscate')
        elif kind == 'src':
            for st in sts:
                count += _write_src(zf, base, st, '' if len(sts) == 1 else st['label'] + '/')
            if not count:
                raise IOError('no changed sources: nothing differs from the decompiled game yet')
        elif kind == 'fullsrc':
            for st in sts:
                root = os.path.join(base, st['source_root'])
                for p, full in _walk(root):
                    zf.write(full, ('' if len(sts) == 1 else st['label'] + '/') + p)
                    count += 1
        else:
            for st in sts:
                if st['reobf_root']:
                    count += _write_obf(zf, base, st, st['label'] + '/obf/')
                count += _write_src(zf, base, st, st['label'] + '/src/')
            if not count:
                raise IOError('nothing to package yet: change some sources first')
            zf.writestr('README.txt', _manifest(meta, sts))
    if dest:
        return None, count
    return target.getvalue(), count
