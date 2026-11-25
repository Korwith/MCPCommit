"""Python 2 -> Python 3 compatibility layer for the original MCP runtimes."""

import ast
import builtins
import configparser as _configparser
import functools
import hashlib as _hashlib
import importlib.abc
import importlib.util
import io
import os
import re
import subprocess as _subprocess
import sys
import types
import zipfile as _zipfile

ENCODING = 'latin-1'

# File handling


class HybridFile(object):
    """Text file (latin-1, no newline translation) that also accepts bytes."""

    def __init__(self, name, mode='r', buffering=-1):
        self.name = name
        self._mode = mode
        text_mode = mode.replace('b', '').replace('t', '').replace('U', '')
        if not text_mode:
            text_mode = 'r'
        self._fh = io.open(name, text_mode, buffering, encoding=ENCODING, newline='')

    def write(self, data):
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode(ENCODING)
        return self._fh.write(data)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __iter__(self):
        return iter(self._fh)

    def __next__(self):
        return next(self._fh)

    next = __next__

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()
        return False

    def __getattr__(self, item):
        return getattr(self._fh, item)


def hybrid_open(name, mode='r', buffering=-1, *args, **kwargs):
    if isinstance(name, int):
        return io.open(name, mode, buffering, *args, **kwargs)
    return HybridFile(name, mode, buffering)


def to_bytes(data):
    if isinstance(data, str):
        return data.encode(ENCODING)
    return data


def raw_input_auto(prompt=''):
    # auto-confirm MCP's "are you sure" prompts
    sys.stdout.write(prompt + 'yes\n')
    return 'yes'


def _cmp(a, b):
    return (a > b) - (a < b)


PY2_BUILTINS = {
    'open': hybrid_open,
    'file': hybrid_open,
    'raw_input': raw_input_auto,
    'unicode': str,
    'basestring': str,
    'xrange': range,
    'long': int,
    'unichr': chr,
    'reduce': functools.reduce,
    'cmp': _cmp,
    '_py2div': None,  # filled below
    '_py2_cmp_to_key': functools.cmp_to_key,
    '_PY2_VERSION_INFO': (2, 7, 18, 'final', 0),
}

# Shim modules


def _make_configparser_module():
    mod = types.ModuleType('ConfigParser')

    class _Py2Parser(_configparser.ConfigParser):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault('strict', False)
            kwargs.setdefault('interpolation', _configparser.BasicInterpolation())
            super().__init__(*args, **kwargs)

        def readfp(self, fp, filename=None):
            return self.read_file(fp, filename)

        def read(self, filenames, encoding=ENCODING):
            return super().read(filenames, encoding=encoding)

    class _Py2RawParser(_configparser.RawConfigParser):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault('strict', False)
            super().__init__(*args, **kwargs)

        def readfp(self, fp, filename=None):
            return self.read_file(fp, filename)

    for name in dir(_configparser):
        if name.endswith('Error') or name in ('DEFAULTSECT', 'MAX_INTERPOLATION_DEPTH'):
            setattr(mod, name, getattr(_configparser, name))
    mod.SafeConfigParser = _Py2Parser
    mod.ConfigParser = _Py2Parser
    mod.RawConfigParser = _Py2RawParser
    return mod


def _make_urllib_module():
    mod = types.ModuleType('mcpc_urllib')

    class _DeadResponse(object):
        def getcode(self):
            return 404

        def readlines(self):
            return []

        def read(self):
            return ''

    def urlopen(*_args, **_kwargs):
        # MCP's update servers are gone; answer 404 instead of hanging
        return _DeadResponse()

    def urlretrieve(*_args, **_kwargs):
        raise IOError('MCP update servers are offline')

    import urllib.parse as _parse
    mod.urlopen = urlopen
    mod.urlretrieve = urlretrieve
    mod.quote = _parse.quote
    mod.unquote = _parse.unquote
    mod.urlencode = _parse.urlencode
    return mod


def _make_subprocess_module():
    mod = types.ModuleType('mcpc_subprocess')
    for name in dir(_subprocess):
        if not name.startswith('__'):
            setattr(mod, name, getattr(_subprocess, name))

    class Popen(_subprocess.Popen):
        def __init__(self, args, *a, **kw):
            if not kw.get('text') and not kw.get('universal_newlines') and 'encoding' not in kw:
                kw['encoding'] = ENCODING
                kw['errors'] = 'replace'
            if kw.get('bufsize') == 1 and 'encoding' in kw:
                kw['bufsize'] = 1
            emit_command(args)
            super().__init__(args, *a, **kw)

    def _wrap(fn):
        @functools.wraps(fn)
        def inner(*a, **kw):
            if not kw.get('text') and not kw.get('universal_newlines') and 'encoding' not in kw:
                kw['encoding'] = ENCODING
                kw['errors'] = 'replace'
            if a:
                emit_command(a[0])
            return fn(*a, **kw)
        return inner

    mod.Popen = Popen
    mod.check_output = _wrap(_subprocess.check_output)
    mod.call = _wrap(_subprocess.call)
    mod.check_call = _wrap(_subprocess.check_call)
    return mod


def emit_command(args):
    """Report every external tool invocation to the UI log stream."""
    try:
        if isinstance(args, (list, tuple)):
            text = ' '.join(str(a) for a in args)
        else:
            text = str(args)
        if len(text) > 600:
            text = text[:600] + ' ...'
        sys.stderr.write('\x1b[cmd]' + text + '\n')
        sys.stderr.flush()
    except Exception:  # pragma: no cover - logging must never break a run
        pass


def _make_zipfile_module():
    mod = types.ModuleType('mcpc_zipfile')
    for name in dir(_zipfile):
        if not name.startswith('__'):
            setattr(mod, name, getattr(_zipfile, name))

    class ZipFile(_zipfile.ZipFile):
        def __init__(self, file, *a, **kw):
            if isinstance(file, HybridFile):
                mode = file._mode
                path = file.name
                file.close()
                if 'mode' not in kw and not a:
                    kw['mode'] = 'w' if ('w' in mode) else ('a' if 'a' in mode else 'r')
                file = path
            super().__init__(file, *a, **kw)

        def writestr(self, zinfo_or_arcname, data, *a, **kw):
            return super().writestr(zinfo_or_arcname, to_bytes(data), *a, **kw)

    mod.ZipFile = ZipFile
    return mod


def _make_hashlib_module():
    mod = types.ModuleType('mcpc_hashlib')
    for name in dir(_hashlib):
        if not name.startswith('__'):
            setattr(mod, name, getattr(_hashlib, name))

    def _wrap(ctor):
        class _H(object):
            def __init__(self, data=None):
                self._h = ctor()
                if data is not None:
                    self._h.update(to_bytes(data))

            def update(self, data):
                self._h.update(to_bytes(data))

            def __getattr__(self, item):
                return getattr(self._h, item)
        return _H

    mod.md5 = _wrap(_hashlib.md5)
    mod.sha1 = _wrap(_hashlib.sha1)
    mod.sha256 = _wrap(_hashlib.sha256)
    return mod


def _make_winreg_module():
    mod = types.ModuleType('_winreg')

    def _unavailable(*_a, **_k):
        raise OSError('_winreg is not available')

    for name in ('OpenKey', 'QueryValueEx', 'CloseKey', 'EnumKey'):
        setattr(mod, name, _unavailable)
    for name in ('KEY_WOW64_64KEY', 'KEY_WOW64_32KEY', 'KEY_READ', 'HKEY_LOCAL_MACHINE'):
        setattr(mod, name, 0)
    return mod


def _make_struct_module():
    import struct as _struct
    mod = types.ModuleType('mcpc_struct')
    for name in dir(_struct):
        if not name.startswith('__'):
            setattr(mod, name, getattr(_struct, name))

    def _conv(v):
        return v.decode(ENCODING) if isinstance(v, bytes) else v

    def pack(fmt, *args):
        return _struct.pack(fmt, *[to_bytes(a) for a in args]).decode(ENCODING)

    def unpack(fmt, data):
        return tuple(_conv(v) for v in _struct.unpack(fmt, to_bytes(data)))

    def unpack_from(fmt, data, offset=0):
        return tuple(_conv(v) for v in _struct.unpack_from(fmt, to_bytes(data), offset))

    class Struct(_struct.Struct):
        def pack(self, *args):
            return _struct.Struct.pack(self, *[to_bytes(a) for a in args]).decode(ENCODING)

        def unpack(self, data):
            return tuple(_conv(v) for v in _struct.Struct.unpack(self, to_bytes(data)))

    mod.pack, mod.unpack, mod.unpack_from, mod.Struct = pack, unpack, unpack_from, Struct
    return mod


def _make_stringio_module():
    mod = types.ModuleType('mcpc_StringIO')

    class StringIO(io.StringIO):
        def __init__(self, initial=''):
            if isinstance(initial, (bytes, bytearray)):
                initial = bytes(initial).decode(ENCODING)
            super().__init__(initial, newline='')

        def write(self, data):
            if isinstance(data, (bytes, bytearray)):
                data = bytes(data).decode(ENCODING)
            return super().write(data)

    mod.StringIO = StringIO
    return mod


def _make_md5_module():
    mod = types.ModuleType('md5')
    hl = _make_hashlib_module()
    mod.new = hl.md5
    mod.md5 = hl.md5
    mod.digest_size = 16
    return mod


SHIM_MODULES = {
    'md5': _make_md5_module(),
    'mcpc_struct': _make_struct_module(),
    'mcpc_StringIO': _make_stringio_module(),
    'mcpc_cStringIO': _make_stringio_module(),
    'ConfigParser': _make_configparser_module(),
    'mcpc_urllib': _make_urllib_module(),
    'mcpc_urllib2': _make_urllib_module(),
    'mcpc_subprocess': _make_subprocess_module(),
    'mcpc_zipfile': _make_zipfile_module(),
    'mcpc_hashlib': _make_hashlib_module(),
    '_winreg': _make_winreg_module(),
}

# Source translation

_IMPORT_REWRITES = [
    (re.compile(r'^(\s*)import urllib2\s*$'), r'\1import mcpc_urllib2 as urllib2'),
    (re.compile(r'^(\s*)import urllib\s*$'), r'\1import mcpc_urllib as urllib'),
    (re.compile(r'^(\s*)import subprocess\s*$'), r'\1import mcpc_subprocess as subprocess'),
    (re.compile(r'^(\s*)import zipfile\s*$'), r'\1import mcpc_zipfile as zipfile'),
    (re.compile(r'^(\s*)import hashlib\s*$'), r'\1import mcpc_hashlib as hashlib'),
    (re.compile(r'^(\s*)from hashlib import'), r'\1from mcpc_hashlib import'),
    (re.compile(r'^(\s*)from zipfile import'), r'\1from mcpc_zipfile import'),
    (re.compile(r'^(\s*)from subprocess import'), r'\1from mcpc_subprocess import'),
    (re.compile(r'^(\s*)from StringIO import StringIO'), r'\1from mcpc_StringIO import StringIO'),
    (re.compile(r'^(\s*)from cStringIO import StringIO'), r'\1from mcpc_StringIO import StringIO'),
    (re.compile(r'^(\s*)import StringIO\s*$'), r'\1import mcpc_StringIO as StringIO'),
    (re.compile(r'^(\s*)import cStringIO\s*$'), r'\1import mcpc_StringIO as cStringIO'),
    (re.compile(r'^(\s*)import struct\s*$'), r'\1import mcpc_struct as struct'),
    (re.compile(r'^(\s*)from struct import'), r'\1from mcpc_struct import'),
    (re.compile(r'^(\s*)import cPickle as pickle'), r'\1import pickle'),
    (re.compile(r'^(\s*)import cPickle'), r'\1import pickle as cPickle'),
]

_INLINE_PRINT_RE = re.compile(r'^(?P<head>\s*(?:for|if|elif|else|while|try|except|with|finally)\b[^#]*:)\s*print(?P<rest>\s.*)?$')
_PRINT_RE = re.compile(r'^(?P<indent>\s*)print(?P<rest>(\s.*)?)$')
_EXCEPT_RE = re.compile(r'^(?P<head>\s*except\s+[\w.]+(?:\s*\([^)]*\))?)\s*,\s*(?P<var>\w+)\s*:')
_EXCEPT_TUPLE_RE = re.compile(r'^(?P<head>\s*except\s+\([^)]*\))\s*,\s*(?P<var>\w+)\s*:')
_OCTAL_RE = re.compile(r'(?<![\w.])0([0-7]+)(?![\w.])')
_NEXT_RE = re.compile(r'(?<![\w.])([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*)\.next\(\)')
_RAISE_RE = re.compile(r'^(?P<indent>\s*)raise\s+(?P<exc>[\w.]+)\s*,\s*(?P<msg>.+?)\s*$')


def _convert_print(indent, rest):
    rest = rest.strip()
    if rest.startswith('(') and _balanced_single_group(rest):
        return '%sprint%s' % (indent, rest)
    # strip a trailing comment
    code, comment = _split_comment(rest)
    code = code.strip()
    target = None
    if code.startswith('>>'):
        target, _, code = code[2:].partition(',')
        target = target.strip()
        code = code.strip()
    end = None
    if code.endswith(','):
        code = code[:-1].rstrip()
        end = "' '"
    args = [code] if code else []
    if target:
        args.append('file=%s' % target)
    if end:
        args.append('end=%s' % end)
    out = '%sprint(%s)' % (indent, ', '.join(args))
    if comment:
        out += '  ' + comment
    return out


def _balanced_single_group(text):
    """True if text is a single parenthesised group, e.g. "('a' % b)"."""
    depth = 0
    in_str = None
    for i, ch in enumerate(text):
        if in_str:
            if ch == '\\':
                continue
            if ch == in_str:
                in_str = None
            continue
        if ch in '\'"':
            in_str = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                tail = text[i + 1:].strip()
                return tail == '' or tail.startswith('#')
    return False


def _split_comment(code):
    in_str = None
    prev = ''
    for i, ch in enumerate(code):
        if in_str:
            if ch == in_str and prev != '\\':
                in_str = None
        elif ch in '\'"':
            in_str = ch
        elif ch == '#':
            return code[:i], code[i:]
        prev = ch
    return code, ''


# MCP 9.08-9.40 relied on Python 2 leaking comprehension variables
_LINE_FIXES = [
    ("if not k in meta['old']['access']:", "if not key in meta['old']['access']:"),
    ("return meta['old']['access'][k]['ops'] == value['ops']", "return meta['old']['access'][key]['ops'] == value['ops']"),
]


def translate(source):
    """Translate MCP's Python 2 source to Python 3 source."""
    out_lines = []
    in_triple = None
    for line in source.splitlines():
        stripped = line.strip()
        if in_triple:
            out_lines.append(line)
            if line.count(in_triple) % 2 == 1:
                in_triple = None
            continue
        for quote in ('"""', "'''"):
            if line.count(quote) % 2 == 1:
                in_triple = quote
                break
        if in_triple:
            out_lines.append(line)
            continue
        if stripped.startswith('#') or not stripped:
            out_lines.append(line)
            continue
        new = line
        multi = re.match(r'^(\s*)import\s+([\w.]+(?:\s*,\s*[\w.]+)+)\s*(#.*)?$', new)
        if multi:
            parts = []
            for mod in [m.strip() for m in multi.group(2).split(',')]:
                stmt = multi.group(1) + 'import ' + mod
                for regex, repl in _IMPORT_REWRITES:
                    if regex.match(stmt):
                        stmt = regex.sub(repl, stmt)
                        break
                parts.append(stmt.strip())
            new = multi.group(1) + '; '.join(parts)
        else:
            for regex, repl in _IMPORT_REWRITES:
                if regex.match(new):
                    new = regex.sub(repl, new)
                    break
        m = _PRINT_RE.match(new)
        if m and not re.match(r'^\s*print\s*[=.\[]', new):
            new = _convert_print(m.group('indent'), m.group('rest') or '')
        else:
            m = _INLINE_PRINT_RE.match(new)
            if m:
                new = m.group('head') + ' ' + _convert_print('', m.group('rest') or '').strip()
        m = _EXCEPT_TUPLE_RE.match(new) or _EXCEPT_RE.match(new)
        if m:
            new = new[:m.start()] + '%s as %s:' % (m.group('head'), m.group('var')) + new[m.end():]
        m = _RAISE_RE.match(new)
        if m and '(' not in m.group('exc'):
            new = '%sraise %s(%s)' % (m.group('indent'), m.group('exc'), m.group('msg'))
        if not ("'" in new or '"' in new):
            new = _OCTAL_RE.sub(r'0o\1', new)
        new = new.replace('.iteritems()', '.items()').replace('.itervalues()', '.values()')
        new = new.replace('.iterkeys()', '.keys()')
        new = _NEXT_RE.sub(r'next(\1)', new)
        new = new.replace('.has_key(', '.__contains__(')
        # the runtimes refuse to start on python 3; after translation they are python 2 compatible
        new = new.replace('sys.version_info', '_PY2_VERSION_INFO')
        # python 2's object.__new__ silently ignored constructor arguments
        new = re.sub(r'(super\([^)]*\)\.__new__\(\s*cls)\s*,\s*\*args\s*,\s*\*\*kwargs\s*\)', r'\1)', new)
        for bad, good in _LINE_FIXES:
            if bad in new:
                new = new.replace(bad, good)
        out_lines.append(new)
    return '\n'.join(out_lines) + '\n'


# Import machinery


class RuntimeFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Finds and loads modules from an MCP ``runtime`` directory."""

    def __init__(self, runtime_dir):
        self.runtime_dir = os.path.abspath(runtime_dir)
        self.subdirs = []
        for name in sorted(os.listdir(self.runtime_dir)):
            path = os.path.join(self.runtime_dir, name)
            if os.path.isfile(os.path.join(path, '__init__.py')):
                self.subdirs.append(path)

    # finder
    def _locate(self, fullname):
        parts = fullname.split('.')
        base = os.path.join(self.runtime_dir, *parts)
        if os.path.isfile(os.path.join(base, '__init__.py')):
            return os.path.join(base, '__init__.py'), True
        if os.path.isfile(base + '.py'):
            return base + '.py', False
        if len(parts) == 1:
            # python 2 implicit relative imports inside runtime packages
            for sub in self.subdirs:
                cand = os.path.join(sub, fullname + '.py')
                if os.path.isfile(cand):
                    return cand, False
        return None, False

    def find_spec(self, fullname, path=None, target=None):
        if fullname in SHIM_MODULES:
            return importlib.util.spec_from_loader(fullname, self)
        filename, is_pkg = self._locate(fullname)
        if filename is None:
            return None
        spec = importlib.util.spec_from_file_location(
            fullname, filename, loader=self,
            submodule_search_locations=[os.path.dirname(filename)] if is_pkg else None)
        return spec

    # loader
    def create_module(self, spec):
        if spec.name in SHIM_MODULES:
            return SHIM_MODULES[spec.name]
        return None

    def exec_module(self, module):
        if module.__name__ in SHIM_MODULES:
            return
        filename = module.__spec__.origin
        load_translated(filename, module.__dict__)


_CODE_CACHE = {}

_FROM_RE = re.compile(r'^(?P<indent>\s*)from\s+(?P<mod>\w+)(?P<rest>(?:\.\w+)*\s+import\s+.+)$')
_IMPORT_RE = re.compile(r'^(?P<indent>\s*)import\s+(?P<mods>\w+(?:\s+as\s+\w+)?(?:\s*,\s*\w+(?:\s+as\s+\w+)?)*)\s*(?P<comment>#.*)?$')


def _py2div(a, b):
    if isinstance(a, int) and isinstance(b, int):
        return a // b
    return a / b


class _Py2Semantics(ast.NodeTransformer):
    """Give Python 3 the Python 2 meaning of a few builtins and operators."""

    LISTIFY_METHODS = {'keys', 'values', 'items'}
    LISTIFY_FUNCS = {'map', 'filter', 'zip', 'range'}

    def visit_Call(self, node):
        self.generic_visit(node)
        func = node.func
        # sort(cmp=f) / sorted(x, cmp=f)
        for kw in list(node.keywords):
            if kw.arg == 'cmp':
                kw.arg = 'key'
                kw.value = ast.Call(func=ast.Name(id='_py2_cmp_to_key', ctx=ast.Load()), args=[kw.value],
                                    keywords=[])
        if isinstance(func, ast.Attribute) and func.attr in ('decode', 'encode'):
            helper = '_py2_decode' if func.attr == 'decode' else '_py2_encode'
            return ast.Call(func=ast.Name(id=helper, ctx=ast.Load()), args=[func.value] + node.args,
                            keywords=node.keywords)
        if isinstance(func, ast.Attribute) and func.attr in self.LISTIFY_METHODS and not node.args:
            return ast.Call(func=ast.Name(id='list', ctx=ast.Load()), args=[node], keywords=[])
        if isinstance(func, ast.Name) and func.id in self.LISTIFY_FUNCS:
            return ast.Call(func=ast.Name(id='list', ctx=ast.Load()), args=[node], keywords=[])
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Div):
            return ast.Call(func=ast.Name(id='_py2div', ctx=ast.Load()), args=[node.left, node.right],
                            keywords=[])
        return node

    def visit_AugAssign(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Div):
            target_load = ast.parse(ast.unparse(node.target), mode='eval').body
            return ast.Assign(targets=[node.target],
                              value=ast.Call(func=ast.Name(id='_py2div', ctx=ast.Load()),
                                             args=[target_load, node.value], keywords=[]))
        return node


def _py2_decode(obj, encoding='ascii', errors='strict'):
    """py2 ``str.decode``: byte string -> unicode."""
    if isinstance(obj, str):
        if encoding in ('hex', 'hex_codec'):
            import binascii
            return binascii.unhexlify(obj.encode(ENCODING)).decode(ENCODING)
        return obj.encode(ENCODING).decode(encoding, errors)
    return obj.decode(encoding, errors)


def _py2_encode(obj, encoding='ascii', errors='strict'):
    """py2 ``unicode.encode``: unicode -> byte string (latin-1 str here)."""
    if isinstance(obj, str):
        if encoding in ('hex', 'hex_codec'):
            import binascii
            return binascii.hexlify(obj.encode(ENCODING)).decode('ascii')
        return obj.encode(encoding, errors).decode(ENCODING)
    return obj.encode(encoding, errors)


PY2_BUILTINS['_py2div'] = _py2div
PY2_BUILTINS['_py2_decode'] = _py2_decode
PY2_BUILTINS['_py2_encode'] = _py2_encode


def _implicit_relative(source, filename):
    """Python 2 resolved ``import x`` inside a package to a sibling module first."""
    here = os.path.dirname(os.path.abspath(filename))
    if not os.path.isfile(os.path.join(here, '__init__.py')):
        return source

    def sibling(name):
        return (os.path.isfile(os.path.join(here, name + '.py')) or
                os.path.isfile(os.path.join(here, name, '__init__.py')))

    out = []
    for line in source.split('\n'):
        m = _FROM_RE.match(line)
        if m and sibling(m.group('mod')):
            line = '%sfrom .%s%s' % (m.group('indent'), m.group('mod'), m.group('rest'))
        else:
            m = _IMPORT_RE.match(line)
            if m:
                parts = [p.strip() for p in m.group('mods').split(',')]
                if any(sibling(p.split()[0]) for p in parts):
                    new = []
                    for p in parts:
                        name = p.split()[0]
                        alias = p.split()[-1]
                        if sibling(name):
                            new.append('%sfrom . import %s as %s' % (m.group('indent'), name, alias))
                        else:
                            new.append('%simport %s' % (m.group('indent'), p))
                    line = '; '.join([new[0]] + [n.strip() for n in new[1:]])
        out.append(line)
    return '\n'.join(out)


def compile_translated(filename, in_package=False):
    filename = os.path.abspath(filename)
    key = (filename, in_package)
    if key in _CODE_CACHE:
        return _CODE_CACHE[key]
    with io.open(filename, 'r', encoding=ENCODING, newline=None) as fh:
        source = fh.read()
    source = translate(source)
    if in_package:
        source = _implicit_relative(source, filename)
    tree = ast.parse(source, filename)
    tree = ast.fix_missing_locations(_Py2Semantics().visit(tree))
    code = compile(tree, filename, 'exec')
    _CODE_CACHE[key] = code
    return code


def load_translated(filename, namespace):
    namespace.update({k: v for k, v in PY2_BUILTINS.items()})
    namespace.setdefault('__file__', filename)
    namespace.setdefault('__builtins__', builtins)
    in_package = '.' in (namespace.get('__name__') or '')
    exec(compile_translated(filename, in_package), namespace)


def install(runtime_dir):
    finder = RuntimeFinder(runtime_dir)
    sys.meta_path.insert(0, finder)
    # make sure nothing cached shadows the runtime modules
    for name in ('commands', 'mcp', 'decompile', 'recompile', 'reobfuscate'):
        sys.modules.pop(name, None)
    return finder


def run_script(runtime_dir, script, argv):
    """Run ``runtime/<script>.py`` as __main__, like ``python runtime/x.py``."""
    install(runtime_dir)
    filename = os.path.join(runtime_dir, script + '.py')
    sys.argv = [os.path.join('runtime', script + '.py')] + list(argv)
    namespace = {'__name__': '__main__', '__file__': filename, '__package__': None}
    load_translated(filename, namespace)


def check_all(runtime_dir):
    """Translate every module of a runtime and report syntax errors."""
    errors = []
    for path, _dirs, files in os.walk(runtime_dir):
        if os.sep + 'bin' in path[len(runtime_dir):]:
            continue
        for name in files:
            if name.endswith('.py'):
                fn = os.path.join(path, name)
                try:
                    _CODE_CACHE.pop((os.path.abspath(fn), False), None)
                    compile_translated(fn)
                except SyntaxError as ex:
                    errors.append('%s:%s: %s' % (fn, ex.lineno, ex.msg))
    return errors
