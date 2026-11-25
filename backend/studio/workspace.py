"""Workspaces: one self-contained MCP folder per project."""

import ast
import json
import os
import re
import shutil
import time

from . import catalog, gamefiles, javaenv, paths

META = 'studio.json'

# Actions every pack exposes through its root scripts, in UI order
ACTION_ORDER = ['decompile', 'recompile', 'reobfuscate', 'reobfuscate_srg', 'startclient', 'startserver',
                'getchangedsrc', 'getmodsource', 'updatemd5', 'updatenames', 'updateids', 'reformat', 'cleanup']
ACTION_LABELS = {
    'decompile': 'Decompile', 'recompile': 'Recompile', 'reobfuscate': 'Reobfuscate',
    'reobfuscate_srg': 'Reobfuscate (SRG names)', 'startclient': 'Run client', 'startserver': 'Run server',
    'getchangedsrc': 'Export changed sources', 'getmodsource': 'Export changed sources',
    'updatemd5': 'Update MD5 baseline', 'updatenames': 'Update names', 'updateids': 'Renumber IDs',
    'reformat': 'Reformat sources', 'cleanup': 'Clean up', 'mods_compile': 'Compile mods',
    'update_names': 'Update names', 'test_game': 'Run client', 'test_server': 'Run server',
    'reobf': 'Reobfuscate',
}
HIDDEN_ACTIONS = {'updatemcp', 'setup', 'findjdk', 'enable_modloader'}


def _slug(text):
    s = re.sub(r'[^A-Za-z0-9._-]+', '-', text).strip('-').lower()
    return s or 'workspace'


def ws_dir(ws_id):
    d = os.path.join(paths.WORKSPACES_DIR, ws_id)
    if os.path.dirname(os.path.abspath(d)) != os.path.abspath(paths.WORKSPACES_DIR):
        raise ValueError('bad workspace id')
    return d


def load(ws_id):
    with open(os.path.join(ws_dir(ws_id), META), encoding='utf-8') as fh:
        return json.load(fh)


def save(meta):
    with open(os.path.join(ws_dir(meta['id']), META), 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, indent=2)


def list_all():
    out = []
    for name in sorted(os.listdir(paths.WORKSPACES_DIR)):
        meta_file = os.path.join(paths.WORKSPACES_DIR, name, META)
        if os.path.isfile(meta_file):
            try:
                with open(meta_file, encoding='utf-8') as fh:
                    meta = json.load(fh)
                meta['state'] = state(meta)
                out.append(meta)
            except (OSError, ValueError):
                pass
    out.sort(key=lambda m: m.get('created', 0), reverse=True)
    return out


# Java adaptation

_SOURCE_TARGET = re.compile(r'-source\s+1\.\d\s+-target\s+1\.\d')


def adapt_config_for_jdk(directory, jdk):
    """Adapt mcp.cfg to the JDK: --release 8 instead of -source 1.6 on javac 9+, no -Xincgc."""
    cfg = os.path.join(directory, 'conf', 'mcp.cfg')
    if not os.path.isfile(cfg):
        return []
    orig = cfg + '.orig'
    if not os.path.isfile(orig):
        shutil.copyfile(cfg, orig)
    with open(orig, encoding='latin-1', newline='') as fh:
        lines = fh.read().split('\n')
    changes = []
    major = jdk['major'] if jdk else 8
    out = []
    for line in lines:
        new = line
        key = line.split('=', 1)[0].strip().lower()
        if major > 8:
            if key.startswith('cmdrecomp') and 'scala' not in key:
                if _SOURCE_TARGET.search(new):
                    new = _SOURCE_TARGET.sub('--release 8', new)
                elif '%s' in new and '--release' not in new:
                    new = new.replace('%s', '%s --release 8', 1)
                new = new.replace('-Xlint:-options', '-Xlint:-options -nowarn')
            if key.startswith('cmdstart'):
                new = new.replace('-Xincgc ', '')
        if new != line:
            changes.append('%s: %s' % (line.split('=', 1)[0].strip(), new.split('=', 1)[1].strip()))
        out.append(new)
    with open(cfg, 'w', encoding='latin-1', newline='') as fh:
        fh.write('\n'.join(out))
    return changes


def run_env(meta):
    env = dict(os.environ)
    jdk = meta.get('jdk')
    if jdk and jdk.get('home'):
        env['JAVA_HOME'] = jdk['home']
        env['PATH'] = os.path.join(jdk['home'], 'bin') + os.pathsep + env.get('PATH', '')
    env['WINEDEBUG'] = '-all'
    env['PYTHONPATH'] = paths.APP_DIR
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHON_COLORS'] = '0'
    env['NO_COLOR'] = '1'
    return env


# Actions and options


def _script_actions(directory):
    """Read the pack's own root scripts: name -> (runtime script, fixed args)."""
    actions = {}
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith('.sh'):
            continue
        name = fname[:-3]
        with open(os.path.join(directory, fname), encoding='latin-1') as fh:
            text = fh.read()
        m = re.search(r'runtime/(\w+)\.py([^\n"]*)', text)
        if not m:
            continue
        fixed = [a for a in m.group(2).split() if a]
        actions[name] = {'script': m.group(1), 'args': fixed}
    return actions


def _options_of(runtime_script):
    """Extract boolean optparse flags from a runtime script."""
    try:
        with open(runtime_script, encoding='latin-1') as fh:
            src = fh.read()
    except OSError:
        return []
    from . import py2compat
    try:
        tree = ast.parse(py2compat.translate(src))
    except SyntaxError:
        return []
    opts = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'add_option'):
            continue
        flags = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        kw = {}
        for k in node.keywords:
            if isinstance(k.value, ast.Constant):
                kw[k.arg] = k.value.value
            elif isinstance(k.value, ast.JoinedStr) or isinstance(k.value, ast.BinOp):
                kw[k.arg] = None
        action = kw.get('action')
        if action not in ('store_true', 'store_false'):
            continue
        help_text = kw.get('help') or ''
        if 'Undocumented' in help_text or 'MCP dev only' in help_text:
            continue
        long_flag = next((f for f in flags if f.startswith('--')), flags[-1] if flags else None)
        if not long_flag:
            continue
        opts.append({'flag': long_flag, 'help': help_text, 'kind': action,
                     'dest': kw.get('dest')})
    return opts


def actions(meta):
    directory = ws_dir(meta['id'])
    if meta.get('engine') == 'legacy':
        from . import legacy
        return legacy.actions(meta)
    acts = _script_actions(directory)
    result = []
    for name in sorted(acts, key=lambda n: (ACTION_ORDER.index(n) if n in ACTION_ORDER else 99, n)):
        if name in HIDDEN_ACTIONS:
            continue
        info = acts[name]
        opts = _options_of(os.path.join(directory, 'runtime', info['script'] + '.py'))
        if name == 'reobfuscate_srg':
            opts = [o for o in opts if o['flag'] != '--srgnames']
        result.append({'name': name, 'label': ACTION_LABELS.get(name, name), 'script': info['script'],
                       'args': info['args'], 'options': opts})
    return result


def command_for(meta, action_name, flags):
    if meta.get('engine') == 'legacy':
        from . import legacy
        return legacy.command_for(meta, action_name, flags)
    for act in actions(meta):
        if act['name'] == action_name:
            allowed = {o['flag'] for o in act['options']}
            extra = [f for f in flags if f in allowed]
            import sys
            return [sys.executable, '-m', 'studio.runner', ws_dir(meta['id']), act['script']] + act['args'] + extra
    raise KeyError('unknown action %s' % action_name)


# State


def source_roots(meta):
    d = ws_dir(meta['id'])
    roots = []
    for rel in ('src/minecraft', 'src/minecraft_server', 'sources/minecraft', 'sources/minecraft_server',
                'minecraft.src', 'minecraft_server.src', 'modsrc/minecraft', 'modsrc/minecraft_server'):
        if os.path.isdir(os.path.join(d, rel)):
            roots.append(rel)
    return roots


def output_roots(meta):
    d = ws_dir(meta['id'])
    roots = []
    for rel in ('reobf/minecraft', 'reobf/minecraft_server', 'final_out/minecraft', 'final_out/minecraft_server',
                'bin/minecraft', 'bin/minecraft_server', 'modsrc/minecraft', 'modsrc/minecraft_server',
                'minecraft/minecraft', 'minecraft/minecraft_server'):
        if os.path.isdir(os.path.join(d, rel)):
            roots.append(rel)
    if meta.get('layout') == 'root':
        # RevEng 1.0-1.2 compiled straight back into the unpacked class folders
        roots += [r for r in ('minecraft', 'minecraft_server') if os.path.isdir(os.path.join(d, r))]
    return roots


def _count(d, ext):
    n = 0
    for _root, _dirs, files in os.walk(d):
        n += sum(1 for f in files if f.endswith(ext))
    return n


def state(meta):
    d = ws_dir(meta['id'])
    st = {'sources': {}, 'outputs': {}}
    for rel in source_roots(meta):
        st['sources'][rel] = _count(os.path.join(d, rel), '.java')
    for rel in output_roots(meta):
        st['outputs'][rel] = _count(os.path.join(d, rel), '.class')
    return st


# Creation


def create(name, pack_id, client_jar=None, server_jar=None, jdk_home=None, log=print):
    pack = catalog.get(pack_id)
    if not pack:
        raise KeyError('unknown pack %s' % pack_id)
    base = _slug(name or '%s-%s' % (pack['version'], pack['pack']))
    ws_id = base
    i = 2
    while os.path.exists(os.path.join(paths.WORKSPACES_DIR, ws_id)):
        ws_id = '%s-%d' % (base, i)
        i += 1
    d = ws_dir(ws_id)
    log('extracting %s (%s) into workspace %s' % (pack['pack'], pack['version'], ws_id))
    catalog.extract(pack, d)

    jdks = javaenv.find_jdks()
    jdk = None
    if jdk_home:
        jdk = next((j for j in jdks if j['home'] == os.path.realpath(jdk_home)), None) or javaenv.probe(jdk_home)
    if not jdk:
        jdk = javaenv.preferred_jdk(jdks)
    meta = {
        'id': ws_id,
        'name': name or ws_id,
        'pack_id': pack_id,
        'pack': pack['pack'],
        'version': pack['version'],
        'client_version': pack['client_version'],
        'server_version': pack['server_version'],
        'mcp_version': pack['mcp_version'],
        'generation': pack['generation'],
        'engine': pack['engine'],
        'layout': pack['layout'],
        'created': time.time(),
        'jdk': jdk,
        'jars': {},
    }
    save(meta)
    if jdk:
        log('using JDK %s at %s' % (jdk['version'], jdk['home']))
        for change in adapt_config_for_jdk(d, jdk):
            log('adapted for Java %d: %s' % (jdk['major'], change))
    else:
        log('WARNING: no JDK found; install a JDK (8 recommended)')

    place_jars(meta, client_jar, server_jar, log)
    return load(ws_id)


def place_jars(meta, client_jar, server_jar, log=print):
    d = ws_dir(meta['id'])
    pack = catalog.get(meta['pack_id'])
    layout = meta['layout']
    if layout == 'launcher':
        gamefiles.layout_launcher(d, meta['client_version'], client_jar, server_jar, log)
    elif layout == 'root':
        gamefiles.layout_bin(d, meta['client_version'], client_jar, server_jar, log,
                             client_path=os.path.join(d, 'bin', 'minecraft.jar'),
                             server_path=os.path.join(d, 'minecraft_server.jar'))
    else:
        gamefiles.layout_bin(d, meta['client_version'], client_jar, server_jar, log)
    jars = meta.setdefault('jars', {})
    for side, jar, expected in (('client', client_jar, pack.get('md5_client')),
                                ('server', server_jar, pack.get('md5_server'))):
        if not jar:
            continue
        md5 = gamefiles.md5sum(jar)
        jars[side] = {'source': jar, 'md5': md5, 'expected': expected,
                      'match': (expected == md5) if expected else None}
        if expected and expected != md5:
            log('WARNING: %s jar md5 %s does not match the %s expected by this MCP (%s); '
                'patches may fail' % (side, md5, side, expected))
        elif expected:
            log('%s jar md5 verified (%s)' % (side, md5))
    save(meta)


def set_jdk(meta, jdk_home):
    jdk = javaenv.probe(jdk_home)
    if not jdk:
        raise ValueError('not a JDK: %s' % jdk_home)
    if not jdk['has_javac']:
        raise ValueError('%s is a Java runtime without javac; MCP needs a full JDK' % jdk_home)
    meta['jdk'] = jdk
    changes = adapt_config_for_jdk(ws_dir(meta['id']), jdk)
    save(meta)
    return changes


def delete(ws_id):
    shutil.rmtree(ws_dir(ws_id))
