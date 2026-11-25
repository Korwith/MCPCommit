"""Engine for the batch-file era."""

import fnmatch
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

from . import catalog, paths

LOG_FMT = '%s'


def log(msg):
    sys.stdout.write(msg + '\n')
    sys.stdout.flush()


def warn(msg):
    log('!! %s !!' % msg)


# Batch file variables

_SET_RE = re.compile(r'^\s*set\s+"?([A-Za-z_0-9]+)=(.*?)"?\s*$', re.I)


def read_bat_vars(path, base=None):
    env = dict(base or {})
    if not os.path.isfile(path):
        return env
    with open(path, encoding='latin-1') as fh:
        for raw in fh:
            line = raw.strip()
            m = _SET_RE.match(line)
            if not m:
                continue
            name, value = m.group(1).upper(), m.group(2)

            def expand(match):
                key = match.group(1).upper()
                if key == 'CD':
                    return '.'
                return env.get(key, '')
            for _ in range(5):
                new = re.sub(r'%([A-Za-z_0-9]+)%', expand, value)
                if new == value:
                    break
                value = new
            value = re.sub(r'%\d', '', value)
            env[name] = value
    return env


def norm(path):
    """Windows batch path -> relative posix path."""
    path = path.strip().strip('"').replace('\\', '/')
    if path.startswith('./'):
        path = path[2:]
    return path


def cp_join(value):
    return os.pathsep.join(norm(p) for p in value.split(';') if p.strip())


# Recipes


def recipe(meta):
    """Describe the pack's pipeline from its own batch files."""
    d = os.getcwd()
    pack = meta['pack']
    gen = meta['generation']
    r = {'gen': gen, 'pack': pack}
    if gen == 'reveng':
        if pack in ('revengpack', 'revengpack11', 'revengpack12'):
            r['sides'] = {
                'client': {'jar': 'bin/minecraft.jar', 'cls': 'minecraft', 'src': 'minecraft.src',
                           'patch': 'minecraft.patch', 'bin': 'minecraft'},
                'server': {'jar': 'minecraft_server.jar', 'cls': 'minecraft_server', 'src': 'minecraft_server.src',
                           'patch': 'minecraft_server.patch', 'bin': 'minecraft_server', 'delete': ['bn.java']},
            }
            r['natives'] = 'bin/natives'
            r['client_cp'] = 'bin/minecraft.jar:bin/jinput.jar:bin/lwjgl.jar:bin/lwjgl_util.jar'
            r['run_client_cp'] = 'minecraft:.:bin/jinput.jar:bin/lwjgl.jar:bin/lwjgl_util.jar'
            r['run_server_cp'] = 'minecraft_server'
            r['server_main'] = 'net.minecraft.server.MinecraftServer'
        else:  # revengpack13
            r['sides'] = {
                'client': {'jar': 'jars/bin/minecraft.jar', 'cls': 'temp/minecraft', 'src': 'sources/minecraft',
                           'patch': 'patches/minecraft.patch', 'bin': 'minecraft/minecraft'},
                'server': {'jar': 'jars/minecraft_server.jar', 'cls': 'temp/minecraft_server',
                           'src': 'sources/minecraft_server', 'patch': 'patches/minecraft_server.patch',
                           'bin': 'minecraft/minecraft_server'},
            }
            r['natives'] = 'jars/bin/natives'
            r['client_cp'] = 'jars/bin/minecraft.jar:jars/bin/jinput.jar:jars/bin/lwjgl.jar:jars/bin/lwjgl_util.jar'
            r['run_client_cp'] = 'minecraft/minecraft:temp/minecraft:jars/bin/jinput.jar:jars/bin/lwjgl.jar:jars/bin/lwjgl_util.jar'
            r['run_server_cp'] = 'minecraft/minecraft_server:temp/minecraft_server'
            r['server_main'] = 'net.minecraft.server.MinecraftServer'
            r['start'] = 'minecraft/Start.java' if os.path.isfile(os.path.join(d, 'minecraft', 'Start.java')) else None
        r['jad_args'] = ['-f', '-dead', '-ff', '-safe', '-stat', '-v', '-o', '-s', '.java']
        r['renames'] = [('do.java', 'dofix.java'), ('if.java', 'iffix.java')]
        return r

    if gen == 'rgs':
        v = read_bat_vars(os.path.join(d, 'decompile.bat'))
        rv = read_bat_vars(os.path.join(d, 'recompile.bat'))
        tv = read_bat_vars(os.path.join(d, 'test_game.bat'))
        r['sides'] = {
            'client': {'jar': norm(v.get('MCJAR', 'jars/bin/minecraft.jar')),
                       'rgjar': norm(v.get('MCRGJAR', 'temp/minecraft_rg.jar')),
                       'rgs': norm(v.get('MCRGSCRIPT', 'conf/minecraft.rgs')),
                       'rglog': norm(v.get('MCRGLOG', 'logs/minecraft_rg.log')),
                       'cls': norm(v.get('MCTEMP', 'temp/minecraft')),
                       'src': norm(v.get('MCJADOUT', 'sources/minecraft')),
                       'patch': norm(v.get('MCPATCH', 'patches/minecraft.patch')),
                       'bin': norm(rv.get('MCBIN', 'bin/minecraft')),
                       'pkgdir': 'net/minecraft/client'},
            'server': {'jar': norm(v.get('MCSJAR', 'jars/minecraft_server.jar')),
                       'rgjar': norm(v.get('MCSRGJAR', 'temp/minecraft_server_rg.jar')),
                       'rgs': norm(v.get('MCSRGSCRIPT', 'conf/minecraft_server.rgs')),
                       'rglog': norm(v.get('MCSRGLOG', 'logs/minecraft_server_rg.log')),
                       'cls': norm(v.get('MCSTEMP', 'temp/minecraft_server')),
                       'src': norm(v.get('MCSJADOUT', 'sources/minecraft_server')),
                       'patch': norm(v.get('MCSPATCH', 'patches/minecraft_server.patch')),
                       'bin': norm(rv.get('MCSBIN', 'bin/minecraft_server')),
                       'pkgdir': 'net/minecraft/server'},
        }
        if not os.path.isfile(os.path.join(d, r['sides']['server']['rgs'])):
            r['sides']['server'].pop('rgs')
        r['natives'] = norm(tv.get('MCNAT', 'jars/bin/natives'))
        r['client_cp'] = cp_join(rv.get('MCCP', 'jars/bin/minecraft.jar;jars/bin/jinput.jar;jars/bin/lwjgl.jar;jars/bin/lwjgl_util.jar'))
        r['run_client_cp'] = cp_join(tv.get('MCCP', 'bin/minecraft;temp/minecraft;jars/bin/jinput.jar;jars/bin/lwjgl.jar;jars/bin/lwjgl_util.jar'))
        r['run_server_cp'] = 'bin/minecraft_server:temp/minecraft_server'
        r['server_main'] = 'MinecraftServer'
        r['start'] = norm(rv.get('MCSTART', 'patches/Start.java'))
        extra = [norm(val) for key, val in rv.items() if key not in ('MCSTART',) and val.lower().endswith('.java')]
        r['extra_client'] = extra
        r['jad_root_r'] = True
        return r

    # MCP 2.x
    sv = read_bat_vars(os.path.join(d, 'setup.bat'))
    with open(os.path.join(d, 'decompile.bat'), encoding='latin-1') as fh:
        dec = fh.read()
    reindex = sv.get('REINDEX_NUMBER')
    client_reindex = server_reindex = None
    for line in dec.splitlines():
        if '%MCPRG%' in line and '%REINDEX_NUMBER%' in line:
            if '%MCJAR%' in line:
                client_reindex = reindex
            if '%MCSJAR%' in line:
                server_reindex = reindex
    r['sides'] = {
        'client': {'jar': norm(sv.get('MCJAR', 'jars/bin/minecraft.jar')),
                   'rgjar': norm(sv.get('MCRGJAR', 'temp/minecraft_rg.jar')),
                   'rgs': norm(sv.get('MCRGSCRIPT', 'conf/minecraft.rgs')),
                   'rglog': norm(sv.get('MCRGLOG', 'logs/minecraft_rg.log')),
                   'reindex': client_reindex,
                   'cls': norm(sv.get('MCTEMP', 'temp/minecraft')),
                   'src': norm(sv.get('MCJADOUT', 'sources/minecraft')),
                   'patch': norm(sv.get('MCPATCH', 'patches/minecraft.patch')),
                   'bin': norm(sv.get('MCBIN', 'bin/minecraft')),
                   'pkgdir': 'net/minecraft/client',
                   'package': sv.get('MCPACKAGE', 'net.minecraft.src'),
                   'srcdirs': [norm(sv.get('MCSRC1', 'sources/minecraft/net/minecraft/client')),
                               norm(sv.get('MCSRC2', 'sources/minecraft/net/minecraft/src'))],
                   'reobf_script': norm(sv.get('MCREOBSCRIPT', 'conf/minecraft_rev.saffx')),
                   'reobf_dir': norm(sv.get('MCREOBDIR', 'final_out/minecraft')),
                   'obf_list': 'conf/client_obfuscation.txt'},
        'server': {'jar': norm(sv.get('MCSJAR', 'jars/minecraft_server.jar')),
                   'rgjar': norm(sv.get('MCSRGJAR', 'temp/minecraft_server_rg.jar')),
                   'rgs': norm(sv.get('MCSRGSCRIPT', 'conf/minecraft_server.rgs')),
                   'rglog': norm(sv.get('MCSRGLOG', 'logs/minecraft_server_rg.log')),
                   'reindex': server_reindex,
                   'cls': norm(sv.get('MCSTEMP', 'temp/minecraft_server')),
                   'src': norm(sv.get('MCSJADOUT', 'sources/minecraft_server')),
                   'patch': norm(sv.get('MCSPATCH', 'patches/minecraft_server.patch')),
                   'bin': norm(sv.get('MCSBIN', 'bin/minecraft_server')),
                   'pkgdir': 'net/minecraft/server',
                   'package': sv.get('MCSPACKAGE', 'net.minecraft.src'),
                   'srcdirs': [norm(sv.get('MCSSRC1', 'sources/minecraft_server/net/minecraft/server')),
                               norm(sv.get('MCSSRC2', 'sources/minecraft_server/net/minecraft/src'))],
                   'reobf_script': norm(sv.get('MCSREOBSCRIPT', 'conf/minecraft_server_rev.saffx')),
                   'reobf_dir': norm(sv.get('MCSREOBDIR', 'final_out/minecraft_server')),
                   'obf_list': 'conf/server_obfuscation.txt'},
    }
    r['natives'] = norm(sv.get('MCNAT', 'jars/bin/natives'))
    r['client_cp'] = cp_join(sv.get('MCCP', 'jars/bin/minecraft.jar;jars/bin/jinput.jar;jars/bin/lwjgl.jar;jars/bin/lwjgl_util.jar'))
    r['run_client_cp'] = cp_join(sv.get('MCTESTCP', 'bin/minecraft;temp/minecraft;jars/bin/jinput.jar;jars/bin/lwjgl.jar;jars/bin/lwjgl_util.jar'))
    r['run_server_cp'] = cp_join(sv.get('MCSTESTCP', sv.get('MCSBIN', 'bin/minecraft_server')))
    r['server_main'] = 'net.minecraft.server.MinecraftServer'
    r['start'] = norm(sv.get('MCSTART', 'patches/Start.java'))
    r['sndfix'] = norm(sv['MCSNDFIX']) if sv.get('MCSNDFIX') else None
    r['splashes'] = (norm(sv['MCPSPLASHES']), norm(sv['MCSPLASHES'])) if sv.get('MCPSPLASHES') and sv.get('MCSPLASHES') else None
    r['renamer_conf'] = 'conf/renamer.conf'
    r['reobf'] = os.path.isfile(os.path.join(d, 'reobf.bat'))
    return r


# Tool helpers


def run(cmd, cwd=None, check=True, log_file=None, quiet=False):
    text = ' '.join(cmd)
    sys.stderr.write('\x1b[cmd]' + (text if len(text) < 600 else text[:600] + ' ...') + '\n')
    sys.stderr.flush()
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = []
    for raw in proc.stdout:
        line = raw.decode('latin-1').rstrip('\r\n')
        out.append(line)
        if not quiet:
            sys.stdout.write('    ' + line + '\n')
    proc.wait()
    if log_file:
        os.makedirs(os.path.dirname(log_file) or '.', exist_ok=True)
        with open(log_file, 'a', encoding='latin-1') as fh:
            fh.write('\n'.join(out) + '\n')
    if check and proc.returncode:
        raise RuntimeError('%s failed with exit code %d' % (cmd[0], proc.returncode))
    return proc.returncode, out


def tool(name):
    for cand in (os.path.join('tools', name), name):
        if os.path.isfile(cand):
            return cand
    return None


def wine_path(path):
    return path.replace('/', '\\')


def jad(args, outdir, patterns):
    exe = tool('jad.exe')
    if not exe:
        raise RuntimeError('jad.exe missing from this pack')
    if not shutil.which('wine'):
        raise RuntimeError('wine is required to run JAD on Linux (sudo apt install wine)')
    files = []
    for pat in patterns:
        if glob.glob(pat):
            files.append(wine_path(pat))
    if not files:
        return
    os.makedirs(outdir, exist_ok=True)
    run(['wine', wine_path(exe)] + args + ['-d', wine_path(outdir)] + files, quiet=True,
        log_file='logs/jad.log')


def jadretro(target):
    jar = tool('jadretro.jar') or shared_tool('jadretro.jar')
    if jar:
        run(['java', '-jar', jar, target], check=False, quiet=True)
        return
    exe = tool('jadretro.exe')
    if exe and shutil.which('wine'):
        run(['wine', wine_path(exe), '-b', wine_path(target)], check=False, quiet=True)


def shared_tool(name):
    """A tool borrowed from the newest MCP 2.x pack (e.g. jadretro.jar)."""
    cache = os.path.join(paths.CACHE_DIR, 'legacy-tools')
    target = os.path.join(cache, name)
    if os.path.isfile(target):
        return target
    pack = catalog.get('b1.5._01__mcp212')
    if not pack:
        return None
    data = catalog.read_member(pack, 'tools/' + name)
    if data is None:
        return None
    os.makedirs(cache, exist_ok=True)
    with open(target, 'wb') as fh:
        fh.write(data)
    return target


def tools_python_dir():
    """renamer_v3.py & obfuscathon.py: the pack's own, else MCP 2.12's."""
    if os.path.isdir('tools-python') and os.path.isfile(os.path.join('tools-python', 'renamer_v3.py')):
        return os.path.abspath('tools-python')
    cache = os.path.join(paths.CACHE_DIR, 'legacy-tools', 'tools-python')
    if os.path.isfile(os.path.join(cache, 'renamer_v3.py')):
        return cache
    pack = catalog.get('b1.5._01__mcp212')
    if not pack:
        return None
    with zipfile.ZipFile(pack['zip']) as zf:
        for info in zf.infolist():
            name = info.filename.replace('\\', '/')
            if name.startswith('tools-python/') and not name.endswith('/'):
                target = os.path.join(cache, name[len('tools-python/'):])
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, 'wb') as out:
                    shutil.copyfileobj(src, out)
    return cache


def apply_patch(patchfile, srcdir, strip=None):
    if not os.path.isfile(patchfile):
        warn('patch %s missing' % patchfile)
        return
    with open(patchfile, 'rb') as fh:
        data = fh.read()
    # same as MCP's linux scripts: sed '/^[-+]\{3\} /s/\\/\//g'
    lines = data.split(b'\n')
    for i, line in enumerate(lines):
        if line.startswith(b'--- ') or line.startswith(b'+++ ') or line.startswith(b'diff '):
            lines[i] = line.replace(b'\\', b'/')
    tmp = os.path.join('temp', 'temp.patch')
    os.makedirs('temp', exist_ok=True)
    with open(tmp, 'wb') as fh:
        fh.write(b'\n'.join(lines))
    cmd = ['patch', '--binary', '-u', '-i', os.path.abspath(tmp), '-d', srcdir, '-N']
    if strip is not None:
        cmd.insert(2, '-p%d' % strip)
    code, out = run(cmd, check=False, quiet=True, log_file='logs/patch.log')
    failed = [l for l in out if 'FAILED --' in l]
    already = sum(1 for l in out if 'previously applied' in l)
    if already:
        log('    %d patches were already applied by the class fixer (jadretro) and were skipped' % already)
    if failed:
        warn('%d files had patch hunks that did not apply, see logs/patch.log' % len(failed))
        for l in failed[:20]:
            log('    ' + l)


def unzip(jar, dest):
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(jar) as zf:
        for info in zf.infolist():
            name = info.filename.replace('\\', '/')
            if name.endswith('/'):
                os.makedirs(os.path.join(dest, name), exist_ok=True)
                continue
            target = os.path.join(dest, name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out)
    for f in glob.glob(os.path.join(dest, 'META-INF', 'MOJANG_C.*')):
        os.remove(f)
    if os.path.isfile(os.path.join(dest, 'null')):
        os.remove(os.path.join(dest, 'null'))


def retroguard(side):
    rg = tool('retroguard.jar')
    cmd = ['java', '-cp', rg, 'RetroGuard', side['jar'], side['rgjar'], side['rgs'], side['rglog']]
    if side.get('reindex'):
        cmd.append(side['reindex'])
    os.makedirs(os.path.dirname(side['rgjar']), exist_ok=True)
    os.makedirs(os.path.dirname(side['rglog']), exist_ok=True)
    run(cmd, quiet=True, log_file='logs/minecraft.log')


def repackage(srcdir, package):
    """tools/repackage.sh: add the package line and move default-package files."""
    pkg_dir = os.path.join(srcdir, *package.split('.'))
    os.makedirs(pkg_dir, exist_ok=True)
    moved = 0
    for f in glob.glob(os.path.join(srcdir, '*.java')):
        with open(f, 'rb') as fh:
            data = fh.read()
        marker = b'// Decompiled by Jad'
        idx = data.find(marker)
        line = ('package %s;' % package).encode() + (b'\r\n' if b'\r\n' in data else b'\n')
        data = data[:idx] + line + data[idx:] if idx >= 0 else line + data
        with open(os.path.join(pkg_dir, os.path.basename(f)), 'wb') as fh:
            fh.write(data)
        os.remove(f)
        moved += 1
    log('    moved %d classes into %s' % (moved, package))


def java_files(*dirs):
    out = []
    for d in dirs:
        out.extend(sorted(glob.glob(os.path.join(d, '*.java'))))
    return out


def javac(cp, sourcepath, outdir, files, log_file):
    if not files:
        warn('no source files to compile')
        return []
    os.makedirs(outdir, exist_ok=True)
    cmd = ['javac', '-g', '-nowarn', '-encoding', 'ISO-8859-1']
    major = java_major()
    if major > 8:
        cmd += ['--release', '8']
    elif major >= 6:
        cmd += ['-source', '1.6', '-target', '1.6']
    if cp:
        cmd += ['-cp', cp]
    if sourcepath:
        cmd += ['-sourcepath', sourcepath]
    cmd += ['-d', outdir]
    argfile = os.path.join('temp', 'javac_files.txt')
    os.makedirs('temp', exist_ok=True)
    with open(argfile, 'w') as fh:
        fh.write('\n'.join(files))
    cmd.append('@' + argfile)
    code, out = run(cmd, check=False, quiet=True, log_file=log_file)
    errors = [l for l in out if ': error' in l or l.strip().endswith('error') or 'errors' in l]
    if code:
        log('== ERRORS FOUND ==')
        for l in out:
            if l.strip() and not l.startswith('Note:') and not l.startswith('warning'):
                log(l)
        raise RuntimeError('recompile failed')
    return errors


def java_major():
    try:
        out = subprocess.run(['javac', '-version'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.decode()
        m = re.search(r'javac (\d+)(?:\.(\d+))?', out)
        if m:
            return int(m.group(2)) if m.group(1) == '1' else int(m.group(1))
    except OSError:
        pass
    return 8


# Actions


def do_decompile(r, sides):
    for name in sides:
        s = r['sides'][name]
        if not os.path.isfile(s['jar']):
            warn('%s jar %s not found, skipping' % (name, s['jar']))
            continue
        marker = os.path.join(s['src'], s.get('pkgdir', ''), 'Minecraft.java' if name == 'client' else 'MinecraftServer.java')
        if os.path.isdir(s['src']) and glob.glob(os.path.join(s['src'], '*')):
            warn('%s already decompiled, run cleanup first' % name)
            continue
        log('== Decompiling %s ==' % name)
        jar = s['jar']
        if s.get('rgs'):
            log('> Deobfuscating with RetroGuard (%s)' % s['rgs'])
            retroguard(s)
            jar = s['rgjar']
        log('> Unpacking jar')
        if os.path.isdir(s['cls']):
            shutil.rmtree(s['cls'])
        unzip(jar, s['cls'])
        if r['gen'] != 'reveng':
            log('> Fixing classes with jadretro')
            jadretro(s['cls'])
        log('> Decompiling with JAD')
        if r['gen'] == 'reveng':
            jad(r['jad_args'], s['src'], [os.path.join(s['cls'], '*.class')])
            for f in s.get('delete', []):
                p = os.path.join(s['src'], f)
                if os.path.isfile(p):
                    os.remove(p)
            for old, new in r['renames']:
                p = os.path.join(s['src'], old)
                if os.path.isfile(p):
                    q = os.path.join(s['src'], new)
                    if os.path.isfile(q):
                        os.remove(q)
                    os.rename(p, q)
            log('> Applying patches')
            apply_patch(s['patch'], s['src'])
        elif r['gen'] == 'rgs':
            jad(['-b', '-dead', '-o', '-r', '-s', '.java', '-stat', '-v'], s['src'],
                [os.path.join(s['cls'], '*.class')])
            jad(['-b', '-dead', '-o', '-s', '.java', '-stat', '-v'], s['src'],
                [os.path.join(s['cls'], *s['pkgdir'].split('/'), '*.class')])
            log('> Applying patches')
            apply_patch(s['patch'], s['src'])
        else:
            jad(['-b', '-dead', '-o', '-r', '-s', '.java', '-stat', '-v'], s['src'],
                [os.path.join(s['cls'], '*.class'), os.path.join(s['cls'], *s['pkgdir'].split('/'), '*.class')])
            log('> Repackaging into %s' % s['package'])
            repackage(s['src'], s['package'])
            log('> Applying patches')
            apply_patch(s['patch'], s['src'], strip=1)
    if r['gen'] == 'mcp2':
        if r.get('splashes') and os.path.isfile(r['splashes'][0]) and os.path.isdir(r['sides']['client']['cls']):
            os.makedirs(os.path.dirname(r['splashes'][1]), exist_ok=True)
            shutil.copyfile(r['splashes'][0], r['splashes'][1])
        log('== Renaming methods and fields ==')
        rename(r)
    log('== Decompile finished ==')


def renamer_config(r):
    """renamer.conf, completed with the keys renamer_v3 needs (MCP 2.0-2.4 lack some)."""
    with open(r['renamer_conf'], encoding='latin-1') as fh:
        text = fh.read()
    keys = {l.split('=')[0].strip() for l in text.splitlines() if '=' in l}
    defaults = {
        'md5_file_client': 'temp/class_client_hash.md5',
        'md5_file_server': 'temp/class_server_hash.md5',
        'unknown_name': 'searge',
        'known_name': 'csv',
        'client_rgs_out': 'conf/minecraft_rev.saffx',
        'server_rgs_out': 'conf/minecraft_server_rev.saffx',
    }
    extra = ['%s = %s' % (k, v) for k, v in defaults.items() if k not in keys]
    if not extra:
        return r['renamer_conf']
    out = os.path.join('temp', 'renamer_studio.conf')
    os.makedirs('temp', exist_ok=True)
    with open(out, 'w', encoding='latin-1') as fh:
        fh.write(text.rstrip() + '\n' + '\n'.join(extra) + '\n')
    log('    renamer.conf completed with defaults: %s' % ', '.join(sorted(k for k in defaults if k not in keys)))
    return out


def rename(r):
    tp = tools_python_dir()
    if not tp:
        warn('renamer sources not available (needs MCP 2.12 in the catalog)')
        return
    for side in r['sides'].values():
        os.makedirs(side['src'], exist_ok=True)
    conf = renamer_config(r)
    log('> renamer_v3.py -R -c %s' % conf)
    sys.stderr.write('\x1b[cmd]python renamer_v3.py -R -c %s (via py2compat)\n' % conf)
    code = run([sys.executable, '-c',
                'import sys,os; sys.path.insert(0, os.environ["MCPSTUDIO_APP"]); '
                'from studio import py2compat; py2compat.run_script(sys.argv[1], "renamer_v3", sys.argv[2:])',
                tp, '-R', '-c', conf], check=False, quiet=False)[0]
    if code:
        warn('renamer exited with %d' % code)


def do_recompile(r, sides):
    for name in sides:
        s = r['sides'][name]
        if not os.path.isdir(s['src']):
            warn('%s not decompiled' % name)
            continue
        log('== Recompiling %s ==' % name)
        if os.path.isdir(s['bin']) and r['gen'] != 'reveng':
            shutil.rmtree(s['bin'])
        if r['gen'] == 'mcp2':
            files = java_files(*s['srcdirs'])
            if name == 'client':
                files += [f for f in (r.get('start'), r.get('sndfix')) if f and os.path.isfile(f)]
            cp = r['client_cp'] if name == 'client' else ''
            javac(cp, s['src'], s['bin'], files, 'logs/minecraft_compile.log')
        elif r['gen'] == 'rgs':
            files = java_files(s['src'])
            cp = r['client_cp'] if name == 'client' else ''
            javac(cp, s['src'], s['bin'], files, 'logs/minecraft_compile.log')
            if name == 'client':
                for extra in [r.get('start')] + r.get('extra_client', []):
                    if extra and os.path.isfile(extra):
                        javac(s['bin'] + os.pathsep + r['client_cp'], None, s['bin'], [extra], 'logs/minecraft_compile.log')
        else:
            files = java_files(s['src'])
            cp = r['client_cp'] if name == 'client' else s['jar']
            javac(cp, s['src'], s['bin'], files, 'logs/compile.log')
            if name == 'client':
                start = r.get('start') or ('Start.java' if os.path.isfile('Start.java') else None)
                if start:
                    javac(s['bin'] + os.pathsep + r['client_cp'], None,
                          os.path.dirname(s['bin']) if r['pack'] == 'revengpack13' else '.', [start],
                          'logs/compile.log')
        log('- %s recompiled' % name)


def do_reobfuscate(r, sides):
    if r['gen'] != 'mcp2' or not r.get('reobf'):
        warn('this release has no reobfuscation step')
        return
    tp = tools_python_dir()
    for name in sides:
        s = r['sides'][name]
        if not os.path.isdir(s['bin']):
            warn('%s not recompiled' % name)
            continue
        if not os.path.isfile(s['reobf_script']):
            warn('%s missing, run decompile (renamer writes it)' % s['reobf_script'])
            continue
        log('== Reobfuscating %s ==' % name)
        if os.path.isdir(s['reobf_dir']):
            shutil.rmtree(s['reobf_dir'])
        os.makedirs(s['reobf_dir'], exist_ok=True)
        run([sys.executable, '-c',
             'import sys,os; sys.path.insert(0, os.environ["MCPSTUDIO_APP"]); '
             'from studio import py2compat; py2compat.run_script(sys.argv[1], "obfuscathon", sys.argv[2:])',
             tp, '-c', s['obf_list'], '-d', s['reobf_script'], '-i', s['bin'], '-o', s['reobf_dir']],
            check=False)
        log('- output in %s' % s['reobf_dir'])


def do_start(r, side):
    if side == 'client':
        natives = os.path.abspath(r['natives'])
        cmd = ['java', '-Xms512M', '-Xmx1024M', '-cp', r['run_client_cp'],
               '-Djava.library.path=' + natives, '-Dorg.lwjgl.librarypath=' + natives,
               '-Dnet.java.games.input.librarypath=' + natives, 'Start']
        run(cmd, check=False)
    else:
        run(['java', '-Xms512M', '-Xmx1024M', '-cp', r['run_server_cp'], r['server_main'], 'nogui'], check=False)


def do_cleanup(r):
    for s in r['sides'].values():
        for key in ('src', 'cls', 'bin', 'reobf_dir'):
            p = s.get(key)
            if p and os.path.isdir(p) and not p.startswith('jars'):
                log('> removing %s' % p)
                shutil.rmtree(p)
    for d in ('temp', 'logs'):
        if os.path.isdir(d):
            for f in os.listdir(d):
                p = os.path.join(d, f)
                if os.path.isfile(p):
                    os.remove(p)
                elif os.path.isdir(p):
                    shutil.rmtree(p)


ACTIONS = [
    ('decompile', 'Decompile'), ('recompile', 'Recompile'), ('reobfuscate', 'Reobfuscate'),
    ('startclient', 'Run client'), ('startserver', 'Run server'), ('cleanup', 'Clean up'),
]


def actions(meta):
    side_opts = [{'flag': '--client', 'help': 'only process client', 'kind': 'store_true'},
                 {'flag': '--server', 'help': 'only process server', 'kind': 'store_true'}]
    out = []
    for name, label in ACTIONS:
        if name == 'reobfuscate' and meta['generation'] != 'mcp2':
            continue
        if name == 'reobfuscate' and meta['pack'] in ('mcp20', 'mcp20a', 'mcp21'):
            continue
        opts = side_opts if name in ('decompile', 'recompile', 'reobfuscate') else []
        out.append({'name': name, 'label': label, 'script': name, 'args': [], 'options': opts})
    return out


def command_for(meta, action, flags):
    from . import workspace
    return [sys.executable, '-m', 'studio.legacy', workspace.ws_dir(meta['id']), action] + \
        [f for f in flags if f in ('--client', '--server')]


def main():
    ws, action = sys.argv[1], sys.argv[2]
    flags = sys.argv[3:]
    os.chdir(ws)
    os.environ['MCPSTUDIO_APP'] = paths.APP_DIR
    with open('studio.json', encoding='utf-8') as fh:
        meta = json.load(fh)
    r = recipe(meta)
    sides = ['client', 'server']
    if '--client' in flags:
        sides = ['client']
    elif '--server' in flags:
        sides = ['server']
    log('== MCP %s (%s, %s) ==' % (meta['mcp_version'], meta['pack'], meta['version']))
    try:
        if action == 'decompile':
            do_decompile(r, sides)
        elif action == 'recompile':
            do_recompile(r, sides)
        elif action == 'reobfuscate':
            do_reobfuscate(r, sides)
        elif action == 'startclient':
            do_start(r, 'client')
        elif action == 'startserver':
            do_start(r, 'server')
        elif action == 'cleanup':
            do_cleanup(r)
        else:
            raise SystemExit('unknown action %s' % action)
    except RuntimeError as ex:
        log('!! %s !!' % ex)
        sys.exit(1)


if __name__ == '__main__':
    main()
