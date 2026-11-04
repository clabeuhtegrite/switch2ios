#!/usr/bin/env python3
# A store only a dispatch's very first invocation makes, taken out of a compute shader for the host to make before the dispatch (nxnative c8521ee and e07cba1, ported to the MSL this
# translation chain writes).
# The pattern: a compute shader's preamble resets a list's counter on the first invocation only, `if (-local.x == workgroup.x << log2(local size)) counter = 0`, and the other invocations
# append to that list hundreds of instructions later. The console starts the first workgroup ahead of the others; Apple's GPUs start workgroups in no order, so a workgroup that appended
# before the reset lost its entries. Mario Kart World's scenery lists, which a culling shader reads and the game rebuilds every few frames, lost whole blocks of trees, bushes and rocks
# that way, for hundreds of frames, their shadows and collisions kept (nxnative e07cba1); Splatoon Raiders' plants flickered (c8521ee).
# nxnative recognises the preamble by its text. Here every Switch 2 shader goes through nx2_lower and the Maxwell front end, so the same test comes out written another way (a negation as
# -x rather than ~x + 1, a shift as a multiply, ...). So a guard is recognised by what it computes: the straight-line code before it, integer arithmetic over the workgroup and invocation
# IDs, is evaluated for every invocation of the first workgroup and for workgroups along each axis, and a guard counts when it holds for the dispatch's first invocation and no other.
# Under such a guard, a block that only stores values known there (constants, or locals the same guard set) into storage buffer words leaves the shader, and the host makes those stores,
# in order, before the dispatch (the reflection's first_store, nvn_mtl.m dispatch); a block that only sets locals stays. Anything else under that guard (a store to a word not known, more
# than MAX_STORES stores, a value the first invocation reads from memory, the same guard further on, or a block doing more than stores) leaves the whole shader as it is.
# Usage, a dry run over a built library (read-only): dmsh_first.py <library dir b-<chain>> [key ...]: the stores the build would take from each compute shader, and for the others
# that store under a guard in their preamble, why not
import json, os, re, struct, sys

MAX_STORES = 4   # nvn_refl.h first[4]
M32 = 0xffffffff


class Unknown(Exception):
    """A value the build cannot know: memory, uniforms, floating point arithmetic, or code this reader does not follow"""


def _signed(b): return b - (1 << 32) if b & 0x80000000 else b


def _v(x):
    if isinstance(x, V): return x
    if isinstance(x, bool): return V(x, 'bool')
    if isinstance(x, int): return V(x, 'int')
    raise Unknown('value %r' % (x,))


class V:
    """A 32-bit value and its MSL type (int, uint, float, bool), with C++'s rules for the integer operations; a float is only a bit pattern (its arithmetic is not followed)"""
    __slots__ = ('b', 't')
    __hash__ = None

    def __init__(self, b, t):
        self.t = t; self.b = (1 if b else 0) if t == 'bool' else b & M32

    def _int(self):   # the operand's type after integer promotion
        if self.t == 'float': raise Unknown('floating point arithmetic')
        return 'int' if self.t == 'bool' else self.t

    def _bin(self, o, f, bools=False):
        o = _v(o)
        if bools and self.t == 'bool' and o.t == 'bool': return V(f(self.b, o.b), 'bool')
        return V(f(self.b, o.b), 'uint' if 'uint' in (self._int(), o._int()) else 'int')

    def __add__(self, o): return self._bin(o, lambda a, b: a + b)
    def __radd__(self, o): return _v(o).__add__(self)
    def __sub__(self, o): return self._bin(o, lambda a, b: a - b)
    def __rsub__(self, o): return _v(o).__sub__(self)
    def __mul__(self, o): return self._bin(o, lambda a, b: a * b)
    def __rmul__(self, o): return _v(o).__mul__(self)
    def __and__(self, o): return self._bin(o, lambda a, b: a & b, True)
    def __rand__(self, o): return _v(o).__and__(self)
    def __or__(self, o): return self._bin(o, lambda a, b: a | b, True)
    def __ror__(self, o): return _v(o).__or__(self)
    def __xor__(self, o): return self._bin(o, lambda a, b: a ^ b, True)
    def __rxor__(self, o): return _v(o).__xor__(self)

    def _div(self, o, rem):   # C's: the quotient rounds toward zero
        o = _v(o); t = 'uint' if 'uint' in (self._int(), o._int()) else 'int'
        if not o.b: raise Unknown('division by zero')
        if t == 'uint': return V(self.b % o.b if rem else self.b // o.b, t)
        a, b = _signed(self.b), _signed(o.b); q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)
        return V(a - q * b if rem else q, t)

    def __truediv__(self, o): return self._div(o, False)
    def __rtruediv__(self, o): return _v(o)._div(self, False)
    def __mod__(self, o): return self._div(o, True)
    def __rmod__(self, o): return _v(o)._div(self, True)

    def __lshift__(self, o):
        o = _v(o); o._int(); return V(self.b << (o.b & 31), self._int())

    def __rlshift__(self, o): return _v(o).__lshift__(self)

    def __rshift__(self, o):
        o = _v(o); o._int(); t = self._int(); n = o.b & 31
        return V(_signed(self.b) >> n if t == 'int' else self.b >> n, t)

    def __rrshift__(self, o): return _v(o).__rshift__(self)
    def __neg__(self): return V(-self.b, self._int())
    def __invert__(self): return V(~self.b, self._int())
    def __pos__(self): return V(self.b, self._int())

    def _cmp(self, o, f):
        o = _v(o); t = 'uint' if 'uint' in (self._int(), o._int()) else 'int'
        return V(f(self.b, o.b) if t == 'uint' else f(_signed(self.b), _signed(o.b)), 'bool')

    def __eq__(self, o): return self._cmp(o, lambda a, b: a == b)
    def __ne__(self, o): return self._cmp(o, lambda a, b: a != b)
    def __lt__(self, o): return self._cmp(o, lambda a, b: a < b)
    def __le__(self, o): return self._cmp(o, lambda a, b: a <= b)
    def __gt__(self, o): return self._cmp(o, lambda a, b: a > b)
    def __ge__(self, o): return self._cmp(o, lambda a, b: a >= b)

    def __bool__(self):
        if self.t == 'float': raise Unknown('a floating point condition')
        return self.b != 0


def _float(b):
    f = struct.unpack('<f', struct.pack('<I', b))[0]
    if f != f or f in (float('inf'), float('-inf')): raise Unknown('NaN or infinity converted')
    return f


def _bitcast(t):   # as_type<t>(x)
    def cast(x):
        x = _v(x)
        if x.t == 'bool': raise Unknown('bit cast of a bool')
        return V(x.b, t)
    return cast


def _convert(t):   # t(x)
    def conv(x):
        x = _v(x)
        if t == 'bool': return V(_float(x.b) != 0 if x.t == 'float' else x.b != 0, 'bool')
        if t == 'float': raise Unknown('conversion to floating point')
        return V(int(_float(x.b)) if x.t == 'float' else x.b, t)
    return conv


class _Ids:
    __slots__ = ('x', 'y', 'z')

    def __init__(self, x, y, z): self.x, self.y, self.z = V(x, 'uint'), V(y, 'uint'), V(z, 'uint')


_GLOBALS = {'__builtins__': {}, '_L': V, '_T': V(1, 'bool'), '_F': V(0, 'bool'), '_not': lambda x: V(not _v(x), 'bool'),
            **{'_bc_' + t: _bitcast(t) for t in ('int', 'uint', 'float')}, **{'_cv_' + t: _convert(t) for t in ('int', 'uint', 'float', 'bool')}}
_TOKEN = re.compile(r'(0[xX][0-9a-fA-F]+[uU]?|[1-9]\d*[uU]?|0[uU]?)(?![\w.])|([A-Za-z_]\w*)|(<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%&|^~!<>().,])')
_SPACE = re.compile(r'\s*')
_PY = {'and', 'or', 'not', 'is', 'in', 'if', 'else', 'elif', 'lambda', 'None', 'True', 'False', 'for', 'while', 'def', 'class', 'return', 'yield', 'import', 'from', 'as', 'with',
       'try', 'except', 'finally', 'raise', 'del', 'global', 'nonlocal', 'pass', 'break', 'continue', 'assert', 'async', 'await'}
_IDS = ('gl_WorkGroupID', 'gl_LocalInvocationID', 'gl_GlobalInvocationID')
_TYPES = ('int', 'uint', 'float', 'bool')
_CMP = {'==', '!=', '<', '>', '<=', '>='}
_BIT = {'&', '|', '^'}


def compile_expr(expr):
    """An MSL scalar expression as Python code over V values. Only what reads the same in both is let through: integer literals, names, the IDs' .x .y .z, as_type<T>( ), T( ),
    unary - ~ !, + - * / % << >> & | ^, one comparison per operand of && / || (Python chains comparisons, and binds & | ^ tighter than them where C does not)"""
    toks, at = [], 0
    while True:
        at = _SPACE.match(expr, at).end()
        if at >= len(expr): break
        m = _TOKEN.match(expr, at)
        if not m: raise Unknown('cannot read the expression')
        toks.append(('n', m.group(1)) if m.group(1) else ('w', m.group(2)) if m.group(2) else ('o', m.group(3))); at = m.end()
    out, seg, i = [], [[0, 0]], 0
    nxt = lambda k: toks[k] if k < len(toks) else ('', '')
    while i < len(toks):
        k, s = toks[i]
        if k == 'n':
            u = s[-1] in 'uU'; out.append('_L(%d, %r)' % (int(s.rstrip('uU'), 0), 'uint' if u else 'int')); i += 1; continue
        if k == 'w':
            if s == 'as_type':
                if nxt(i + 1) != ('o', '<') or nxt(i + 2)[1] not in ('int', 'uint', 'float') or nxt(i + 3) != ('o', '>') or nxt(i + 4) != ('o', '('): raise Unknown('a cast this reader does not follow')
                out.append('_bc_' + nxt(i + 2)[1]); i += 4; continue
            if s in _TYPES:
                if nxt(i + 1) != ('o', '('): raise Unknown('a type name outside a conversion')
                out.append('_cv_' + s); i += 1; continue
            if s in ('true', 'false'): out.append('_T' if s == 'true' else '_F'); i += 1; continue
            if '__' in s or s in _PY or s.startswith(('_bc_', '_cv_')) or s in _GLOBALS: raise Unknown('a name this reader does not follow')
            if nxt(i + 1) == ('o', '.'):
                if s not in _IDS or nxt(i + 2) not in (('w', 'x'), ('w', 'y'), ('w', 'z')): raise Unknown('a member this reader does not follow')
                out.append(s + '.' + nxt(i + 2)[1]); i += 3; continue
            if nxt(i + 1) == ('o', '('): raise Unknown('a function call')
            out.append(s); i += 1; continue
        if s == '(': seg.append([0, 0]); out.append(s)
        elif s == ')':
            if len(seg) == 1: raise Unknown('unbalanced parentheses')
            seg.pop(); out.append(s)
        elif s in ('&&', '||', ','): seg[-1] = [0, 0]; out.append({'&&': 'and', '||': 'or', ',': ','}[s])
        elif s == '!':
            a = nxt(i + 1)
            if a == ('o', '('): out.append('_not')   # the group in parentheses becomes its argument
            elif a[0] == 'w' and a[1] not in _TYPES and a[1] not in ('as_type', 'true', 'false') and '__' not in a[1] and a[1] not in _PY and a[1] not in _GLOBALS \
                    and nxt(i + 2) not in (('o', '('), ('o', '.')):
                out.append('_not(%s)' % a[1]); i += 2; continue
            else: raise Unknown('a negation this reader does not follow')
        elif s == '.': raise Unknown('a member this reader does not follow')
        else:
            if s in _CMP: seg[-1][0] += 1
            if s in _BIT: seg[-1][1] += 1
            if seg[-1][0] > 1 or (seg[-1][0] and seg[-1][1]): raise Unknown('operators whose precedence differs from Python')
            out.append(s)
        i += 1
    if len(seg) != 1 or not out: raise Unknown('an incomplete expression')
    try: return compile(' '.join(out), '<msl>', 'eval')
    except SyntaxError: raise Unknown('an expression Python does not parse')


def _eval(code, env):
    try: return _v(eval(code, _GLOBALS, env))
    except Unknown: raise
    except Exception as e: raise Unknown(type(e).__name__)


def _to(val, t):   # assignment to a variable of type t
    if t == 'bool': return V(bool(val), 'bool')
    if val.t == 'float' if t != 'float' else val.t != 'float': raise Unknown('an implicit floating point conversion')
    return V(val.b, t)


_BLANK = re.compile(r'(?:\s+|//[^\n]*|/\*.*?\*/)*', re.S)
_IF = re.compile(r'if\s*\(')
_ELSE = re.compile(r'else\b')
_DECL = re.compile(r'(?:const\s+)?(float|int|uint|bool)\s+(\w+)\s*=(?!=)\s*(.+)\Z', re.S)
_BARE = re.compile(r'(float|int|uint|bool)\s+(\w+)\Z')
_ASSIGN = re.compile(r'(\w+)\s*(<<|>>|[-+*/%&|^])?=(?!=)\s*(.+)\Z', re.S)
_STORE = re.compile(r'(?:\(\*\w+\.(\w+)\)\.|\w+\.(\w+)->)_m0\[(\d+)u?\]\s*=(?!=)\s*(.+)\Z', re.S)
_OPS = {'+': '+', '-': '-', '*': '*', '/': '/', '%': '%', '&': '&', '|': '|', '^': '^', '<<': '<<', '>>': '>>'}


def _close(s, at, op, cl):   # the position after the bracket that closes the one at `at`, -1 if none
    depth = 0
    for i in range(at, len(s)):
        if s[i] == op: depth += 1
        elif s[i] == cl:
            depth -= 1
            if not depth: return i + 1
    return -1


def _split(s, at, end):
    """The statements from `at` to `end`: ('if', condition, (body start, body end), span) and ('stmt', text, span), until one it does not follow (a loop, a scope, an else, an
    if without braces): (statements, where they stop, why)"""
    out = []
    while True:
        at = _BLANK.match(s, at).end()
        if at >= end: return out, end, 'the end of the program'
        m = _IF.match(s, at)
        if m:
            c = _close(s, m.end() - 1, '(', ')')
            b0 = _BLANK.match(s, c).end() if c > 0 else -1
            if c < 0 or not s.startswith('{', b0): return out, at, 'an if without braces'
            b1 = _close(s, b0, '{', '}')
            if b1 < 0 or b1 > end: return out, at, 'unbalanced braces'
            if _ELSE.match(s, _BLANK.match(s, b1).end()): return out, at, 'an if with an else'
            out.append(('if', s[m.end():c - 1], (b0 + 1, b1 - 1), (at, b1))); at = b1; continue
        i, depth = at, 0
        while i < end:
            ch = s[i]
            if ch in '([': depth += 1
            elif ch in ')]': depth -= 1
            elif ch in '{}': return out, at, 'a loop' if re.match(r'(for|while|do)\b', s[at:at + 6]) else 'a switch' if s.startswith('switch', at) else 'a block'
            elif ch == ';' and not depth: break
            i += 1
        else: return out, at, 'a statement with no end'
        out.append(('stmt', s[at:i].strip(), (at, i + 1))); at = i + 1


_SIDE = re.compile(r'\batomic_\w+|\bbarrier\b|_barrier\(|\.write\(|\.fence\(')
_ANYDECL = re.compile(r'(?:(?:const|threadgroup|thread|device|constant)\s+)*[A-Za-z_][\w:]*(?:\s*<[^;=]*?>)?\s*[*&]?\s*(\w+)(?:\s*\[[^\]]*\])*\s*(?:=(?!=)(.*))?\Z', re.S)
_LOCAL_PART = re.compile(r'(\w+)\s*(?:\[[^\]]*\]|\.\w+)+\s*(?:<<|>>|[-+*/%&|^])?=(?!=)')


def _simple(text):
    """A simple statement as an operation: ('decl', name, type, code or None) for a scalar, ('unknown', name) for any other declaration, ('assign', name, operator or None, code),
    ('store', binding, word, code) into a storage buffer word, ('skip',) for a write into part of a local; ('other', why) for anything else"""
    if _SIDE.search(text): return ('other', 'an atomic, a barrier or a texture write')
    if 'spvDescriptorSet' in text.split('=', 1)[0] and re.search(r'=(?!=)', text):   # a write to memory
        m = _STORE.match(text)
        if not m: return ('other', 'a store to memory this reader does not follow')
        try: return ('store', m.group(1) or m.group(2), int(m.group(3)), compile_expr(m.group(4)))
        except Unknown: return ('store', m.group(1) or m.group(2), int(m.group(3)), None)
    m = _DECL.match(text)
    if m:
        try: code = compile_expr(m.group(3))
        except Unknown: code = None
        return ('decl', m.group(2), m.group(1), code)
    m = _BARE.match(text)
    if m: return ('decl', m.group(2), m.group(1), None)
    m = _ASSIGN.match(text)
    if m and m.group(1) not in _TYPES:
        try: code = compile_expr(m.group(3))
        except Unknown: code = None
        return ('assign', m.group(1), m.group(2), code)
    if _LOCAL_PART.match(text): return ('skip',)   # an element or a member of a local (threadgroup arrays included): nothing the guards read
    m = _ANYDECL.match(text)
    if m and not re.match(r'(return|discard_fragment)\b', text): return ('unknown', m.group(1))
    return ('other', 'a call or a statement this reader does not follow')


def _kernel_body(msl):
    k = re.search(r'\bkernel\s+void\s+\w+\s*\(', msl)
    if not k: return None
    c = _close(msl, k.end() - 1, '(', ')')
    b0 = _BLANK.match(msl, c).end() if c > 0 else -1
    if c < 0 or not msl.startswith('{', b0): return None
    b1 = _close(msl, b0, '{', '}')
    return (b0 + 1, b1 - 1) if b1 > 0 else None


def _invocations(local, wys=(0, 1, 2, 255, 65535), wzs=(0, 1, 3)):
    lx, ly, lz = local
    yield (0, 0, 0), (0, 0, 0)
    for z in range(lz):
        for y in range(ly):
            for x in range(lx):
                if x or y or z: yield (0, 0, 0), (x, y, z)
    corners = sorted({(0, 0, 0), (lx - 1, ly - 1, lz - 1), (1 % lx, 0, 0), (0, 1 % ly, 0), (0, 0, 1 % lz)})
    for wz in wzs:
        for wy in wys:
            for wx in (0, 1, 2, 3, 5, 64, 1023, 65535, 1 << 20):
                if wx or wy or wz:
                    for lid in corners: yield (wx, wy, wz), lid


def _run(prog, wg, lid, local, seen, origin):
    """One invocation through the preamble: each guard's value goes into seen[i] (True, False, None for not known); at the origin, the values of the stores under a true guard"""
    gid = tuple(w * n + l for w, n, l in zip(wg, local, lid))
    env = {'gl_WorkGroupID': _Ids(*wg), 'gl_LocalInvocationID': _Ids(*lid), 'gl_GlobalInvocationID': _Ids(*gid),
           'gl_LocalInvocationIndex': V(lid[0] + local[0] * (lid[1] + local[1] * lid[2]), 'uint')}
    types = {}

    def step(op, env, types, outer):
        kind = op[0]
        if kind == 'decl':
            types[op[1]] = op[2]
            try:
                if op[3] is None: raise Unknown('not set')
                env[op[1]] = _to(_eval(op[3], env), op[2])
            except Unknown: env.pop(op[1], None)
            return
        if kind == 'unknown': env.pop(op[1], None); types.pop(op[1], None); return
        if kind == 'skip': return
        if kind == 'assign':
            name, t = op[1], types.get(op[1])
            try:
                if t is None or op[3] is None: raise Unknown('not followed')
                val = _eval(op[3], env)
                if op[2]: val = _eval(compile('_a %s _b' % _OPS[op[2]], '<msl>', 'eval'), {'_a': env[name], '_b': val})
                val = _to(val, t)
            except (Unknown, KeyError): val = None
            for e in (env,) + ((outer,) if outer is not None and name not in op_locals else ()):
                if val is None: e.pop(name, None)
                else: e[name] = val
    for n, op in enumerate(prog):
        if op[0] != 'if': op_locals = (); step(op, env, types, None); continue
        _, cond, body = op[:3]
        try: c = bool(_eval(cond, env)) if cond is not None else None
        except Unknown: c = None
        seen[n] = c
        if c is False: continue
        inner, itypes = dict(env), dict(types); op_locals = set()
        for b in body:
            if b[0] in ('decl', 'unknown'): op_locals.add(b[1])
            if b[0] == 'store':
                if origin is not None and c:
                    try:
                        if b[3] is None: raise Unknown('not followed')
                        val = _eval(b[3], inner)
                        if val.t == 'float': raise Unknown('a floating point value')
                        origin.setdefault(n, []).append((b[1], b[2], val.b))
                    except Unknown: origin.setdefault(n, []).append((b[1], b[2], None))
                continue
            if c: step(b, inner, itypes, env)
            elif b[0] == 'assign' and b[1] not in op_locals: env.pop(b[1], None)   # set or not, depending on what is not known
        for name in list(env):
            if name in inner and name not in op_locals: env[name] = inner[name]


def analyse(msl, local):
    """(the shader with the first invocation's stores taken out, [(binding's MSL name, word, value)], why nothing was taken or '')"""
    if not local or len(local) != 3 or any(not isinstance(n, int) or n < 1 for n in local) or local[0] * local[1] * local[2] > 1024: return msl, [], 'no local size'
    body = _kernel_body(msl)
    if not body: return msl, [], 'no kernel'
    stmts, stop, why = _split(msl, body[0], body[1])
    prog = []
    for st in stmts:
        if st[0] == 'stmt':
            op = _simple(st[1])
            if op[0] == 'store': stop, why = st[2][0], 'a store to memory'; break
            if op[0] == 'other': stop, why = st[2][0], op[1]; break
            prog.append(op); continue
        inner, istop, iwhy = _split(msl, st[2][0], st[2][1])
        ops = [_simple(x[1]) if x[0] == 'stmt' else ('other', 'an if') for x in inner]
        bad = [o[1] for o in ops if o[0] == 'other']
        if istop < st[2][1] or bad: stop, why = st[3][0], 'a guarded block with ' + (bad[0] if bad else iwhy); break   # a block this reader does not follow ends the preamble
        try: cond = compile_expr(st[1])
        except Unknown: cond = None
        prog.append(('if', cond, ops, st))
    if not any(op[0] == 'if' and any(o[0] == 'store' for o in op[2]) for op in prog): return msl, [], 'no store under a guard before ' + why
    origin = {}
    alive = {n for n, op in enumerate(prog) if op[0] == 'if'}
    # A test that leaves out workgroup y (or z) holds in every row of a dispatch more than one workgroup deep: the console would reset the counter again as each row started, so the game
    # dispatches such a shader one workgroup deep there (Mario Kart World's lists are one row of 64 invocations a workgroup), and those rows are left out of the check, as nxnative's
    # pattern leaves them out of its test
    flat = []
    for wg in ((0, 1, 0), (0, 0, 1)):
        seen = {}; _run(prog, wg, (0, 0, 0), local, seen, None); flat.append({n for n in alive if seen.get(n)})
    rows = {n: ((0,) if n in flat[0] else (0, 1, 2, 255, 65535), (0,) if n in flat[1] else (0, 1, 3)) for n in alive}
    for wg, lid in _invocations(local):
        seen = {}
        first = wg == (0, 0, 0) and lid == (0, 0, 0)
        _run(prog, wg, lid, local, seen, origin if first else None)
        for n in list(alive):
            if wg[1] not in rows[n][0] or wg[2] not in rows[n][1]: continue
            c = seen.get(n, None)
            if c is None or c != first: alive.discard(n)
        if not alive: return msl, [], 'no guard holds for the first invocation alone'
    stores, cut = [], []
    for n in sorted(alive):
        ops = prog[n][2]
        kinds = {o[0] for o in ops}
        if kinds == {'store'}:
            for b, w, v in origin.get(n, []):
                if v is None: return msl, [], 'a value the first invocation does not know'
                stores.append((b, w, v))
            cut.append(prog[n][3][3])
        elif 'store' in kinds: return msl, [], 'the first invocation does more than stores under its guard'
    if not stores: return msl, [], 'no store under the first invocation\'s guard'
    if len(stores) > MAX_STORES: return msl, [], 'more than %d stores' % MAX_STORES
    rest = msl[stop:body[1]]
    for n in alive:
        if re.search(r'if\s*\(\s*%s\s*\)' % re.escape(prog[n][3][1].strip()), rest): return msl, [], 'the same guard further on'
    before = msl[body[0]:cut[0][0]]
    for b in {b for b, _, _ in stores}:   # the host's store comes first: a read of that buffer before it in the shader would see the reset early
        if re.search(r'\.%s\b' % re.escape(b), before): return msl, [], 'the buffer is read before its reset'
    out, at = [], 0
    for a, b in cut:
        out.append(msl[at:a]); at = _BLANK.match(msl, b).end()
        out.append(msl[b:at][msl[b:at].rfind('\n'):] if '\n' in msl[b:at] else '')   # the next line's indentation stays
    out.append(msl[at:])
    return ''.join(out), stores, ''


def first_store(msl, local):
    """dmsh_build.py: the shader without its first invocation's stores, and [(binding's MSL name, word, value)] for the host; the shader as it is and [] when nothing is taken"""
    out, stores, _ = analyse(msl, local)
    return out, stores


if __name__ == '__main__':
    if len(sys.argv) < 2: sys.exit('usage: dmsh_first.py <library dir b-<chain>> [key ...]')
    lib, want = sys.argv[1], set(sys.argv[2:])
    n = taken = 0
    for f in sorted(os.listdir(lib)):
        if not f.endswith('_b.json') or (want and f[:-7] not in want): continue
        try: j = json.load(open(os.path.join(lib, f)))
        except (OSError, ValueError): continue
        if j.get('stage') != 'cs' or not os.path.exists(os.path.join(lib, f[:-5] + '.metal')): continue
        n += 1
        msl = open(os.path.join(lib, f[:-5] + '.metal'), errors='replace').read()
        if j.get('first_store'): print(f'{f[:-7]}  already built with first_store {j["first_store"]}'); continue
        out, stores, why = analyse(msl, j.get('threadgroup_size'))
        if stores: taken += 1; print(f'{f[:-7]}  taken: {stores}')
        elif not why.startswith('no store under a guard') and why not in ('no local size', 'no kernel'): print(f'{f[:-7]}  not taken: {why}')
    print(f'{n} compute shaders, {taken} with stores taken')
