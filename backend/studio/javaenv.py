"""Discovery of installed JDKs and of the external tools MCP shells out to."""

import glob
import os
import re
import shutil
import subprocess

_VERSION_RE = re.compile(r'version "(?P<v>[^"]+)"')


def _java_major(version):
    # "1.8.0_504" -> 8, "21.0.13" -> 21
    parts = re.split(r'[._+-]', version)
    try:
        if parts[0] == '1':
            return int(parts[1])
        return int(parts[0])
    except (IndexError, ValueError):
        return 0


def probe(home):
    """Return a JDK description for a java home, or None."""
    java = os.path.join(home, 'bin', 'java')
    javac = os.path.join(home, 'bin', 'javac')
    if not (os.path.isfile(java) and os.access(java, os.X_OK)):
        return None
    try:
        out = subprocess.run([java, '-version'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=20).stdout.decode('utf-8', 'replace')
    except (OSError, subprocess.SubprocessError):
        return None
    m = _VERSION_RE.search(out)
    if not m:
        return None
    version = m.group('v')
    return {
        'home': os.path.realpath(home),
        'version': version,
        'major': _java_major(version),
        'has_javac': os.path.isfile(javac),
        'label': out.splitlines()[1].strip() if len(out.splitlines()) > 1 else version,
    }


def candidate_homes():
    homes = []
    env = os.environ.get('JAVA_HOME')
    if env:
        homes.append(env)
    patterns = [
        '/usr/lib/jvm/*', '/usr/java/*', '/opt/*jdk*', '/opt/*java*', '/opt/java/*',
        os.path.expanduser('~/.jdks/*'), os.path.expanduser('~/.sdkman/candidates/java/*'),
        '/Library/Java/JavaVirtualMachines/*/Contents/Home',
        os.path.expanduser('~/.local/share/multimc/java/*'),
        os.path.expanduser('~/.local/share/PrismLauncher/java/*'),
    ]
    for pattern in patterns:
        homes.extend(sorted(glob.glob(pattern)))
    path_java = shutil.which('java')
    if path_java:
        homes.append(os.path.dirname(os.path.dirname(os.path.realpath(path_java))))
    path_javac = shutil.which('javac')
    if path_javac:
        homes.append(os.path.dirname(os.path.dirname(os.path.realpath(path_javac))))
    return homes


_CACHE = None


def find_jdks(refresh=False):
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE
    seen = set()
    jdks = []
    for home in candidate_homes():
        if not os.path.isdir(home):
            continue
        real = os.path.realpath(home)
        if real in seen:
            continue
        seen.add(real)
        info = probe(real)
        if info:
            jdks.append(info)
    jdks.sort(key=lambda j: (not j['has_javac'], j['major']))
    _CACHE = jdks
    return jdks


def preferred_jdk(jdks=None):
    """Prefer JDK 8, the newest javac that accepts -source 1.6."""
    jdks = find_jdks() if jdks is None else jdks
    full = [j for j in jdks if j['has_javac']]
    for j in full:
        if j['major'] == 8:
            return j
    older_ok = [j for j in full if j['major'] >= 8]
    if older_ok:
        return sorted(older_ok, key=lambda j: j['major'])[0]
    return full[0] if full else None


def tools():
    """External, non-Java helpers used by some MCP generations."""
    return {
        'wine': shutil.which('wine'),
        'patch': shutil.which('patch'),
        'astyle': shutil.which('astyle'),
    }
