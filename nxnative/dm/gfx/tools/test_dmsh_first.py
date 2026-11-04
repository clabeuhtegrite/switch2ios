#!/usr/bin/env python3
"""dmsh_first.py: the first invocation's stores taken out of a compute shader for the host. nxnative's own cases (test_dmsh_first_store.py at e07cba1: Splatoon Raiders' Hoian_Proc and
Mario Kart World's scenery lists), the same written as this chain's Maxwell front end writes them, and shapes that must stay. Run: python3 test_dmsh_first.py"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dmsh_first as F

HEAD = """#include <metal_stdlib>
using namespace metal;
kernel void main0(constant spvDescriptorSetBuffer0& spvDescriptorSet0 [[buffer(0)]], uint3 gl_LocalInvocationID [[thread_position_in_threadgroup]], uint3 gl_WorkGroupID [[threadgroup_position_in_grid]])
{
"""
TAIL = """    uint _200 = atomic_fetch_add_explicit((device atomic_uint*)&(*spvDescriptorSet0.cp_s0)._m0[1], 1u, memory_order_relaxed);
}
"""


def k(body): return HEAD + body + TAIL


# nxnative's cases, unchanged (their Ampere front end writes a negation as ~x + 1)
BODY = """    float _59 = as_type<float>(gl_WorkGroupID.x);
    float _61 = as_type<float>(gl_LocalInvocationID.x);
    int _63 = as_type<int>(_59) << %d;
    int _65 = ~as_type<int>(_61);
    int _67 = _65 + 1;
    bool _71 = _67 %s _63;
    if (!_71)
    {
        (*spvDescriptorSet0.cp_s0)._m0[1] = 0u;
    }
    int _73 = as_type<int>(_61) + _63;
"""
SPLIT = """    float _53 = as_type<float>(gl_WorkGroupID.x);
    float _55 = as_type<float>(gl_LocalInvocationID.x);
    int _57 = as_type<int>(_53) << 6;
    int _59 = ~as_type<int>(_55);
    int _61 = _59 + 1;
    bool _65 = _61 != _57;
    int _67 = _61;
    if (!_65)
    {
        _67 = 1;
    }
    int _69 = _67;
    if (!_65)
    {
        (*spvDescriptorSet0.cp_s0)._m0[0] = 0u;
    }
    if (!_65)
    {
        (*spvDescriptorSet0.cp_s0)._m0[2] = uint(_69);
    }
    if (!_65)
    {
        (*spvDescriptorSet0.cp_s0)._m0[3] = uint(_69);
    }
    if (!_65)
    {
        (*spvDescriptorSet0.cp_s0)._m0[1] = 0u;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);
    int _71 = as_type<int>(_55) + _57;
"""
ROWS = """    float _74 = as_type<float>(gl_WorkGroupID.y);
    float _76 = as_type<float>(gl_LocalInvocationID.y);
    float _78 = as_type<float>(gl_LocalInvocationID.x);
    float _80 = as_type<float>(gl_WorkGroupID.x);
    float _82 = as_type<float>(gl_WorkGroupID.z);
    int _84 = as_type<int>(_74) << 3;
    int _86 = ~as_type<int>(_76);
    int _88 = _86 + 1;
    bool _92 = _88 != _84;
    int _94 = as_type<int>(_80) << 3;
    int _96 = ~as_type<int>(_78);
    int _98 = _96 + 1;
    bool _100 = _98 == _94;
    bool _102 = !_92;
    bool _104 = _100 && _102;
    bool _106 = as_type<int>(_82) == 0;
    bool _108 = _106 && _104;
    int _110 = as_type<int>(_82) * 24;
    int _112 = _110 + 8;
    if (_108)
    {
        (*spvDescriptorSet0.cp_s0)._m0[1] = 0u;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);
"""
SETUP = """    float _78 = as_type<float>(gl_WorkGroupID.y);
    float _80 = as_type<float>(gl_LocalInvocationID.y);
    float _82 = as_type<float>(gl_LocalInvocationID.x);
    float _84 = as_type<float>(gl_WorkGroupID.x);
    float _86 = as_type<float>(gl_WorkGroupID.z);
    int _88 = as_type<int>(_78) << 3;
    int _90 = ~as_type<int>(_80);
    int _91 = _90 + 1;
    bool _94 = _91 != _88;
    int _96 = as_type<int>(_84) << 3;
    int _97 = ~as_type<int>(_82);
    int _98 = _97 + 1;
    bool _100 = _98 == _96;
    bool _102 = !_94;
    bool _104 = _100 && _102;
    bool _106 = 0 == as_type<int>(_86);
    bool _108 = _106 && _104;
    bool _110 = !_108;
    if (!_110)
    {
        (*spvDescriptorSet0.cp_s0)._m0[0] = 0u;
        bool _112 = 0u < as_type<uint>((*spvDescriptorSet0.cp_c4)._m0[0].x);
    }
"""
# The same as this chain writes them: the Maxwell front end negates with OpSNegate (-x), and other compilers' shifts come as multiplies
OURS = """    float _40 = as_type<float>(gl_WorkGroupID.x);
    float _42 = as_type<float>(gl_LocalInvocationID.x);
    int _44 = as_type<int>(_40) * 64;
    int _46 = -as_type<int>(_42);
    bool _48 = _46 == _44;
    int _50 = _46;
    if (_48)
    {
        _50 = 1;
    }
    if (_48)
    {
        spvDescriptorSet0.cp_s0->_m0[0u] = 0u;
    }
    if (_48)
    {
        spvDescriptorSet0.cp_s0->_m0[2u] = as_type<uint>(_50);
    }
    if (_48)
    {
        spvDescriptorSet0.cp_s0->_m0[1u] = 0u;
    }
    threadgroup_barrier(mem_flags::mem_device);
"""
UINTS = """    uint _10 = gl_WorkGroupID.x << 5u;
    uint _12 = 0u - gl_LocalInvocationID.x;
    bool _14 = !(_12 != _10);
    if (_14)
    {
        (*spvDescriptorSet0.cp_s1)._m0[3] = 7u;
    }
    for (int _i = 0; _i < 4; _i++) { }
"""


class Nxnative(unittest.TestCase):
    def test_the_pattern(self):
        out, stores = F.first_store(k(BODY % (5, '!=')), [32, 1, 1])
        self.assertEqual(stores, [('cp_s0', 1, 0)])
        self.assertNotIn('_m0[1] = 0u', out)
        self.assertNotIn('if (!_71)', out)
        self.assertIn('int _73 = as_type<int>(_61) + _63;', out)
        self.assertIn('bool _71 = _67 != _63;', out)
        self.assertIn('atomic_fetch_add_explicit', out)

    def test_two_stores(self):
        src = k((BODY % (5, '!=')).replace('= 0u;', '= 0u;\n        (*spvDescriptorSet0.cp_s2)._m0[3] = 7u;'))
        self.assertEqual(F.first_store(src, [32, 1, 1])[1], [('cp_s0', 1, 0), ('cp_s2', 3, 7)])

    def test_other_shapes_stay(self):
        for src, local in ((k(BODY % (5, '==')), [32, 1, 1]),   # the opposite condition: all but the first
                           (k(BODY % (5, '!=')), [32, 2, 1]),   # a second row of invocations passes the test too
                           (k(BODY % (5, '!=')), None)):
            self.assertEqual(F.first_store(src, local), (src, []))
        read = k((BODY % (5, '!=')).replace('= 0u;', '= as_type<uint>((*spvDescriptorSet0.cp_c4)._m0[0].x);'))   # a value read from memory
        self.assertEqual(F.first_store(read, [32, 1, 1]), (read, []))

    def test_what_the_first_invocation_computes_is_known(self):
        # nxnative leaves these two: a value computed from the IDs (0 here) and a shift past the local size. Neither changes which invocation passes the test: -local.x is 0 or at least
        # 2^32 - 1023, workgroup.x << 4 never reaches that for a dispatch of fewer than 2^28 workgroups
        self.assertEqual(F.first_store(k((BODY % (5, '!=')).replace('= 0u;', '= as_type<uint>(_63);')), [32, 1, 1])[1], [('cp_s0', 1, 0)])
        self.assertEqual(F.first_store(k(BODY % (4, '!=')), [32, 1, 1])[1], [('cp_s0', 1, 0)])

    def test_split_blocks(self):
        out, stores = F.first_store(k(SPLIT), [64, 1, 1])
        self.assertEqual(stores, [('cp_s0', 0, 0), ('cp_s0', 2, 1), ('cp_s0', 3, 1), ('cp_s0', 1, 0)])   # in the shader's order
        self.assertEqual(out.count('_m0['), 1)   # only the atomic is left
        self.assertIn('int _69 = _67;', out)
        self.assertIn('_67 = 1;', out)   # the block that sets a local stays
        self.assertIn('threadgroup_barrier', out)
        self.assertIn('int _71 = as_type<int>(_55) + _57;', out)

    def test_split_blocks_that_stay(self):
        for src in (k(SPLIT.replace('[2] = uint(_69)', '[2] = uint(_71)')),   # a value only later code computes
                    k(SPLIT + '    if (!_65)\n    {\n        (*spvDescriptorSet0.cp_s0)._m0[9] = 2u;\n    }\n'),   # the same guard further on
                    k(SPLIT.replace('[1] = 0u;', '[1] = 0u;\n        (*spvDescriptorSet0.cp_s0)._m0[4] = 0u;'))):   # more than the host keeps
            self.assertEqual(F.first_store(src, [64, 1, 1]), (src, []))

    def test_rows(self):
        out, stores = F.first_store(k(ROWS), [8, 8, 1])
        self.assertEqual(stores, [('cp_s0', 1, 0)])
        self.assertEqual(out.count('_m0['), 1)
        self.assertIn('int _112 = _110 + 8;', out)
        self.assertIn('threadgroup_barrier', out)
        self.assertEqual(F.first_store(k(ROWS), [8, 8, 2]), (k(ROWS), []))   # local z is not tested: (0, 0, 1) passes too

    def test_more_than_stores_stays(self):
        self.assertEqual(F.first_store(k(SETUP), [8, 8, 1]), (k(SETUP), []))


class OurSpelling(unittest.TestCase):
    def test_negation_multiply_and_member_pointers(self):
        out, stores = F.first_store(k(OURS), [64, 1, 1])
        self.assertEqual(stores, [('cp_s0', 0, 0), ('cp_s0', 2, 1), ('cp_s0', 1, 0)])
        self.assertNotIn('->_m0[', out)
        self.assertIn('_50 = 1;', out)

    def test_four_stores_then_a_barrier(self):   # e00784275819936d as the Mac read it: 4 words under -local.x == workgroup.x << 6, the last two -local.x, a barrier, an atomic later
        body = """    float _40 = as_type<float>(gl_WorkGroupID.x);
    float _42 = as_type<float>(gl_LocalInvocationID.x);
    int _44 = as_type<int>(_40) << 6;
    int _46 = -as_type<int>(_42);
    bool _48 = _46 == _44;
    if (_48)
    {
        (*spvDescriptorSet0.cp_s0)._m0[0] = 0u;
        (*spvDescriptorSet0.cp_s0)._m0[1] = 0u;
        (*spvDescriptorSet0.cp_s0)._m0[2] = as_type<uint>(_46);
        (*spvDescriptorSet0.cp_s0)._m0[3] = as_type<uint>(_46);
    }
    threadgroup_barrier(mem_flags::mem_device);
"""
        out, stores = F.first_store(k(body), [64, 1, 1])
        self.assertEqual(stores, [('cp_s0', 0, 0), ('cp_s0', 1, 0), ('cp_s0', 2, 0), ('cp_s0', 3, 0)])
        self.assertNotIn('if (_48)', out)
        self.assertIn('threadgroup_barrier', out)
        self.assertIn('atomic_fetch_add_explicit', out)
        self.assertEqual(out.count('{'), out.count('}'))
        self.assertEqual(len(F.first_store(k(body.replace('<< 6', '<< 5')), [64, 1, 1])[1]), 4)   # -local.x is 0 or at least 2^32 - 63: still the first invocation alone
        self.assertEqual(F.first_store(k(body.replace('bool _48 = _46 == _44;', 'bool _48 = _46 == 0;')), [64, 1, 1])[1], [])   # each workgroup's first invocation: stays
        self.assertEqual(F.first_store(k(body.replace('= 0u;\n        (*spvDescriptorSet0.cp_s0)._m0[1]', '= 0u;\n        (*spvDescriptorSet0.cp_s0)._m0[4] = 0u;\n        (*spvDescriptorSet0.cp_s0)._m0[1]')), [64, 1, 1])[1], [])   # 5 stores

    def test_unsigned_and_a_negated_group(self):
        out, stores = F.first_store(k(UINTS), [32, 1, 1])
        self.assertEqual(stores, [('cp_s1', 3, 7)])
        self.assertIn('for (int _i = 0', out)

    def test_every_workgroup_first_invocation_stays(self):   # a reset per workgroup is not the dispatch's first invocation
        src = k("""    bool _10 = gl_LocalInvocationID.x == 0u;
    if (_10)
    {
        (*spvDescriptorSet0.cp_s0)._m0[1] = 0u;
    }
""")
        self.assertEqual(F.first_store(src, [64, 1, 1]), (src, []))

    def test_global_id(self):
        src = k("""    bool _10 = gl_GlobalInvocationID.x == 0u;
    if (_10)
    {
        (*spvDescriptorSet0.cp_s0)._m0[1] = 0u;
    }
""")
        self.assertEqual(F.first_store(src, [64, 1, 1])[1], [('cp_s0', 1, 0)])
        self.assertEqual(F.first_store(src, [8, 8, 1]), (src, []))   # (0, 1) passes too

    def test_a_guard_on_a_uniform_stays(self):
        src = k("""    bool _10 = gl_LocalInvocationID.x == 0u;
    bool _12 = as_type<uint>((*spvDescriptorSet0.cp_c4)._m0[0].x) == 0u;
    bool _14 = _10 && _12;
    if (_14)
    {
        (*spvDescriptorSet0.cp_s0)._m0[1] = 0u;
    }
""")
        self.assertEqual(F.first_store(src, [64, 1, 1]), (src, []))

    def test_after_a_loop_stays(self):
        src = k("    for (int _i = 0; _i < 4; _i++) { }\n" + BODY % (5, '!='))
        self.assertEqual(F.first_store(src, [32, 1, 1]), (src, []))

    def test_why(self):
        self.assertEqual(F.analyse(k(SETUP), [8, 8, 1])[2], 'the first invocation does more than stores under its guard')
        self.assertEqual(F.analyse(k(BODY % (5, '==')), [32, 1, 1])[2], 'no guard holds for the first invocation alone')
        self.assertEqual(F.analyse(k(BODY % (5, '!=')), [32, 1, 1])[2], '')


class Expressions(unittest.TestCase):
    def ev(self, e, env=None): return F._eval(F.compile_expr(e), dict(env or {}))

    def test_c_semantics(self):
        self.assertEqual(self.ev('-7 / 2').b & 0xffffffff, (-3) & 0xffffffff)
        self.assertEqual(self.ev('-7 % 2').b, (-1) & 0xffffffff)
        self.assertEqual(self.ev('0u - 1u').b, 0xffffffff)
        self.assertTrue(self.ev('0u - 1u > 0u'))   # unsigned
        self.assertFalse(self.ev('0 - 1 > 0'))   # signed
        self.assertEqual(self.ev('as_type<int>(0xffffffffu) >> 4').b, 0xffffffff)   # arithmetic shift
        self.assertEqual(self.ev('0xffffffffu >> 4u').b, 0x0fffffff)
        self.assertTrue(self.ev('!(1 == 2) && true'))

    def test_what_python_reads_otherwise_is_refused(self):
        for e in ('a == b == c', 'a & b == c', '1.0 + x', 'x ? 1 : 2', 'f(x)', 'p->q', 'a[1]', '__import__', 'x.__class__', 'lambda: 0', '!f(x)'):
            self.assertRaises(F.Unknown, F.compile_expr, e)


if __name__ == '__main__':
    unittest.main()
