// tools/vmprobe.m: does the kernel stop charging guest pages the host gives back, while a Metal no-copy buffer wraps them as it wraps every NVN memory pool?
//
// No game involved. It reserves a block the way the host reserves the guest's (hle_svc.c mem_init_once: mmap PROT_NONE, private, anonymous), makes slices of it
// read-write in one piece (SetHeapSize: the game's heap is one VM object, its pools sub-ranges of it), wraps slices in no-copy MTLBuffers before writing them
// (nvn_mtl.m mtl_pool_add: the game creates a pool, then fills it), fills them with data that does not compress, and then tries one way of giving pages back
// on each slice. Textures exist twice today, as the game's bytes in its pools and as Metal textures; dropping the game's copy once uploaded would save GBs if
// the kernel really stops charging it.
//
// The footprint ledger alone cannot tell "freed" from "only unmapped" (mprotect PROT_NONE and mmap MAP_FIXED take pages off it without freeing anything) or from
// "the ledger moved but the pages are wired" (MADV_FREE_REUSABLE on pages a residency set wired), so each step also prints the host-wide internal / wired / free
// page counts, the slice's pages present / dirty / reusable as the VM sees them (mach_vm_page_range_query, which faults nothing), and the VM object behind the
// slice. Then it reads the slice with the GPU and the CPU, gives it back the way the protocol says (MADV_FREE_REUSE where pages were made reusable) and writes it
// again, printing what each of those charged, and the net change.
//
//   clang -fobjc-arc -O1 -framework Foundation -framework Metal tools/vmprobe.m -o work/vmprobe && work/vmprobe && work/vmprobe --each
//
// The first run puts every scenario in one VM object, as the host's heap; --each runs each scenario in a process of its own (no coupling between slices).
// It takes about 17 slices of memory (about 1.1 GB with the default of 64 MiB a slice, `work/vmprobe 32` for half) for about ten seconds. Run it with nothing
// heavy running: memory pressure would compress or swap the slices and blur the numbers, and other processes move the host-wide counts.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <TargetConditionals.h>
#include <mach/mach.h>
#include <mach/vm_map.h>
#include <mach/vm_page_size.h>
#include <mach/vm_statistics.h>
#if TARGET_OS_OSX
#include <mach/mach_vm.h>
#else   // the iOS SDK has no mach/mach_vm.h; libsystem_kernel has the call all the same
extern kern_return_t mach_vm_page_range_query(vm_map_t target_map, mach_vm_offset_t address, mach_vm_size_t size, mach_vm_address_t dispositions, mach_vm_size_t *dispositions_count);
#endif
#include <spawn.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#ifndef VM_PAGE_QUERY_PAGE_REUSABLE
#define VM_PAGE_QUERY_PAGE_REUSABLE 0x800
#endif
extern char **environ;

static size_t PG, S;   // the host page, a slice
static id<MTLDevice> g_dev;
static id<MTLCommandQueue> g_q;
static id<MTLBuffer> g_rb;   // GPU readback
static host_t g_host;

// The task's ledgers (what the iOS memory limit counts is phys_footprint), then the host-wide page counts, in bytes
typedef struct { int64_t fp, internal, reusable, compressed, graphics, h_internal, h_wired, h_free; } ledgers;
static ledgers now(void) {
    ledgers r; memset(&r, 0, sizeof r);
    task_vm_info_data_t v; mach_msg_type_number_t n = TASK_VM_INFO_COUNT; memset(&v, 0, sizeof v);
    if (task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&v, &n) != KERN_SUCCESS) { fprintf(stderr, "task_info TASK_VM_INFO failed\n"); exit(1); }
    r.fp = (int64_t)v.phys_footprint; r.internal = (int64_t)v.internal; r.reusable = (int64_t)v.reusable; r.compressed = (int64_t)v.compressed;
    r.graphics = (int64_t)(v.ledger_tag_graphics_footprint + v.ledger_tag_graphics_footprint_compressed);
    vm_statistics64_data_t h; mach_msg_type_number_t hc = HOST_VM_INFO64_COUNT; memset(&h, 0, sizeof h);
    if (host_statistics64(g_host, HOST_VM_INFO64, (host_info64_t)&h, &hc) == KERN_SUCCESS) {
        int64_t kp = (int64_t)vm_kernel_page_size;
        r.h_internal = (int64_t)h.internal_page_count * kp; r.h_wired = (int64_t)h.wire_count * kp; r.h_free = (int64_t)h.free_count * kp;
    }
    return r;
}
static double mib(int64_t b) { return b / 1048576.0; }

// The slice's pages as the VM sees them; faults nothing, works on PROT_NONE ranges
typedef struct { unsigned present, dirty, reusable, copied, pagedout; int ok; } pstate;
static pstate pages(void *p, size_t n) {
    pstate s; memset(&s, 0, sizeof s);
    mach_vm_size_t cnt = n / PG; int *d = calloc((size_t)cnt, sizeof *d);
    if (!d) return s;
    if (mach_vm_page_range_query(mach_task_self(), (mach_vm_offset_t)(uintptr_t)p, (mach_vm_size_t)n, (mach_vm_address_t)(uintptr_t)d, &cnt) == KERN_SUCCESS) {
        s.ok = 1;
        for (mach_vm_size_t i = 0; i < cnt; i++) {
            int f = d[i];
            s.present += !!(f & VM_PAGE_QUERY_PAGE_PRESENT); s.dirty += !!(f & VM_PAGE_QUERY_PAGE_DIRTY); s.reusable += !!(f & VM_PAGE_QUERY_PAGE_REUSABLE);
            s.copied += !!(f & VM_PAGE_QUERY_PAGE_COPIED); s.pagedout += !!(f & VM_PAGE_QUERY_PAGE_PAGED_OUT);
        }
    }
    free(d); return s;
}

// The VM object behind an address: shared with another mapping, shadowed, how many references
static void objinfo(const char *tag, const void *p) {
    vm_address_t a = (vm_address_t)p; vm_size_t sz = 0; natural_t depth = 0;
    vm_region_submap_info_data_64_t i; mach_msg_type_number_t c = VM_REGION_SUBMAP_INFO_COUNT_64; memset(&i, 0, sizeof i);
    if (vm_region_recurse_64(mach_task_self(), &a, &sz, &depth, (vm_region_recurse_info_t)&i, &c) != KERN_SUCCESS) { printf("    %s: no VM region\n", tag); return; }
    static const char *sm[] = {"?", "COW", "PRIVATE", "EMPTY", "SHARED", "TRUESHARED", "PRIVATE_ALIASED", "SHARED_ALIASED", "LARGE_PAGE"};
    printf("    %s: region %#lx+%lu MiB prot %d/%d, object %#x, %u refs, shadow depth %u, %s, %u pages resident, %u dirtied\n", tag, (unsigned long)a, (unsigned long)(sz >> 20),
           i.protection, i.max_protection, i.object_id, i.ref_count, (unsigned)i.shadow_depth, i.share_mode < 9 ? sm[i.share_mode] : "?", i.pages_resident, i.pages_dirtied);
}

// Data that does not compress (the compressor cannot shrink a slice behind the measurement's back), never 0, and known at every offset
static uint64_t pat(uint64_t i) { uint64_t x = i * 0x9E3779B97F4A7C15ull + 0x7F4A7C159E3779B9ull; x ^= x >> 31; x *= 0xBF58476D1CE4E5B9ull; x ^= x >> 29; return x | 1; }
static void fill(uint8_t *p, size_t n, uint64_t seed) { uint64_t *q = (uint64_t *)p; for (size_t i = 0; i < n / 8; i++) q[i] = pat(seed + i); }
static const char *seen(uint64_t v, uint64_t seed) { return v == pat(seed) ? "old data" : v == 0 ? "zeros" : "other"; }

// The first 8 bytes of a buffer at off, as the GPU reads them (a blit into a shared readback buffer); 0 with the command buffer's error in err
static int gpu_read(id<MTLBuffer> b, size_t off, uint64_t *out, char *err, size_t errn) {
    memset(g_rb.contents, 0xEE, 16);
    id<MTLCommandBuffer> cb = [g_q commandBuffer];
    id<MTLBlitCommandEncoder> e = [cb blitCommandEncoder];
    [e copyFromBuffer:b sourceOffset:off toBuffer:g_rb destinationOffset:0 size:16];
    [e endEncoding]; [cb commit]; [cb waitUntilCompleted];
    *out = *(volatile uint64_t *)g_rb.contents;
    if (cb.error) { snprintf(err, errn, "command buffer error %ld: %s", (long)cb.error.code, cb.error.localizedDescription.UTF8String); return 0; }
    return 1;
}

enum { NOTHING, NONE_ONLY, NONE_ONLY_BUF, FREE_ONLY, NONE_THEN_FREE, FREE_THEN_NONE, REUSABLE_NOBUF, REUSABLE_BUF, REUSABLE_THEN_NONE_BUF, REUSABLE_BUF_GPU,
       REUSABLE_BUF_GPU_IDLE, REUSABLE_RESIDENT, DONTNEED_BUF, FIXED_BUF, REUSABLE_NO_REUSE, ALIAS_NOBUF, ALIAS_TOUCHED_BUF, N };
// buf: a no-copy buffer over the slice; gpu: before the step, 1 = the GPU reads it just before, 2 = reads it, then 3 s idle, 3 = in a residency set attached to the queue
static const struct { const char *name; int buf, gpu; } SC[N] = {
    [NOTHING]                = {"nothing done, a buffer over it (noise floor)", 1, 0},
    [NONE_ONLY]              = {"PROT_NONE only, no buffer (control)", 0, 0},
    [NONE_ONLY_BUF]          = {"PROT_NONE only, a buffer the GPU read just before (control)", 1, 1},
    [FREE_ONLY]              = {"MADV_FREE only, no buffer (control)", 0, 0},
    [NONE_THEN_FREE]         = {"PROT_NONE then MADV_FREE (UnmapPhysicalMemory today), no buffer", 0, 0},
    [FREE_THEN_NONE]         = {"MADV_FREE then PROT_NONE, no buffer", 0, 0},
    [REUSABLE_NOBUF]         = {"MADV_FREE_REUSABLE, no buffer", 0, 0},
    [REUSABLE_BUF]           = {"MADV_FREE_REUSABLE, a buffer the GPU never used (a texture pool with DM_NVN_DESWZ_GPU=2)", 1, 0},
    [REUSABLE_THEN_NONE_BUF] = {"MADV_FREE_REUSABLE then PROT_NONE (to catch the next access), a buffer the GPU never used", 1, 0},
    [REUSABLE_BUF_GPU]       = {"MADV_FREE_REUSABLE, a buffer the GPU read just before", 1, 1},
    [REUSABLE_BUF_GPU_IDLE]  = {"MADV_FREE_REUSABLE, a buffer the GPU read 3 s before", 1, 2},
    [REUSABLE_RESIDENT]      = {"MADV_FREE_REUSABLE, a buffer in a residency set (a resident pool)", 1, 3},
    [DONTNEED_BUF]           = {"MADV_DONTNEED, a buffer the GPU never used", 1, 0},
    [FIXED_BUF]              = {"new pages mapped over it (mmap MAP_FIXED), a buffer the GPU read just before", 1, 1},
    [REUSABLE_NO_REUSE]      = {"MADV_FREE_REUSABLE, then written again WITHOUT MADV_FREE_REUSE, no buffer", 0, 0},
    [ALIAS_NOBUF]            = {"PROT_NONE, then MADV_FREE_REUSABLE through a read-write alias never touched, no buffer", 0, 0},
    [ALIAS_TOUCHED_BUF]      = {"PROT_NONE, then MADV_FREE_REUSABLE through a read-write alias read once, a buffer the GPU read just before", 1, 1},
};
static int made_reusable(int k) { return k == REUSABLE_NOBUF || k == REUSABLE_BUF || k == REUSABLE_THEN_NONE_BUF || k == REUSABLE_BUF_GPU || k == REUSABLE_BUF_GPU_IDLE ||
                                         k == REUSABLE_RESIDENT || k == ALIAS_NOBUF || k == ALIAS_TOUCHED_BUF; }
static int left_prot_none(int k) { return k == NONE_ONLY || k == NONE_ONLY_BUF || k == NONE_THEN_FREE || k == FREE_THEN_NONE || k == REUSABLE_THEN_NONE_BUF || k == ALIAS_NOBUF ||
                                          k == ALIAS_TOUCHED_BUF; }

static void step_line(const char *what, ledgers b, ledgers a, pstate ps, int with_pages) {
    printf("    %-15s footprint %+7.1f  internal %+7.1f  reusable %+7.1f  compressed %+6.1f  graphics %+6.1f | host: internal %+7.1f  wired %+7.1f  free %+7.1f", what,
           mib(a.fp - b.fp), mib(a.internal - b.internal), mib(a.reusable - b.reusable), mib(a.compressed - b.compressed), mib(a.graphics - b.graphics),
           mib(a.h_internal - b.h_internal), mib(a.h_wired - b.h_wired), mib(a.h_free - b.h_free));
    if (with_pages) { if (ps.ok) printf(" | pages present %u dirty %u reusable %u copied %u paged out %u", ps.present, ps.dirty, ps.reusable, ps.copied, ps.pagedout);
                      else printf(" | page query failed"); }
    printf("\n");
}

// One process's run: every scenario (only < 0) in one VM object, or only scenario `only` (the same layout, one slice used)
static int run(int only) { @autoreleasepool {
    g_dev = MTLCreateSystemDefaultDevice();
    if (!g_dev) { fprintf(stderr, "no Metal device\n"); return 1; }
    g_q = [g_dev newCommandQueue];
    g_rb = [g_dev newBufferWithLength:PG options:MTLResourceStorageModeShared];
    size_t R = S * (N + 4);
    uint8_t *base = mmap(NULL, R, PROT_NONE, MAP_PRIVATE | MAP_ANON, -1, 0);   // the guest block: reserved, nothing mapped
    if (base == MAP_FAILED) { perror("mmap"); return 1; }
    if (mprotect(base, S * N, PROT_READ | PROT_WRITE)) { perror("mprotect"); return 1; }   // the game's heap, in one piece
    int lo = only < 0 ? 0 : only, hi = only < 0 ? N : only + 1;

    // No-copy buffers first, as for a pool the game creates and then fills; does creating one wire or charge anything?
    id<MTLBuffer> buf[N] = {nil};
    ledgers c0 = now();
    for (int k = lo; k < hi; k++) if (SC[k].buf) {
        buf[k] = [g_dev newBufferWithBytesNoCopy:base + (size_t)k * S length:S options:MTLResourceStorageModeShared deallocator:nil];
        if (!buf[k]) { fprintf(stderr, "newBufferWithBytesNoCopy refused slice %d\n", k); return 1; }
    }
    ledgers c1 = now();
    for (int k = lo; k < hi; k++) fill(base + (size_t)k * S, S, (uint64_t)k << 40);
    ledgers c2 = now();
    if (only < 0) {
        printf("all scenarios in one VM object, as the guest heap (%d slices of %zu MiB)\n", N, S >> 20);
        step_line("buffers made:", c0, c1, (pstate){0}, 0);
        step_line("slices filled:", c1, c2, (pstate){0}, 0);
        printf("    (filling wrote %.1f MiB)\n\n", mib((int64_t)(S * (size_t)(hi - lo))));
    }

    // A buffer the GPU reads every time, to see whether the GPU still takes work after a scenario's error
    id<MTLBuffer> ctl = [g_dev newBufferWithLength:PG options:MTLResourceStorageModeShared];
    fill(ctl.contents, PG, 7);
    id rs = nil;
    for (int k = lo; k < hi; k++) {
        uint8_t *p = base + (size_t)k * S; uint64_t seed = (uint64_t)k << 40, v = 0; char err[256] = "";
        vm_address_t alias = 0;
        printf("[%d] %s\n", k, SC[k].name);
        // the GPU's use of the slice before the step
        if (SC[k].gpu == 3) {
            if (@available(macOS 15.0, iOS 18.0, *)) {
                MTLResidencySetDescriptor *d = [MTLResidencySetDescriptor new]; d.initialCapacity = 4;
                NSError *e = nil; id<MTLResidencySet> s = [g_dev newResidencySetWithDescriptor:d error:&e];
                if (!s) { printf("    residency set refused: %s\n", e.localizedDescription.UTF8String); continue; }
                [s addAllocation:buf[k]]; [s commit]; [s requestResidency]; [g_q addResidencySet:s]; rs = s;
            } else { printf("    (no residency sets on this OS: skipped)\n"); continue; }
        }
        if (SC[k].gpu) {
            ledgers g0 = now(); int ok = gpu_read(buf[k], 0, &v, err, sizeof err); ledgers g1 = now();
            if (!ok || v != pat(seed)) printf("    GPU read before the step: %s %s\n", seen(v, seed), err);
            step_line("GPU use before:", g0, g1, pages(p, S), 1);
            if (SC[k].gpu == 2) sleep(3);
        }
        if (k == ALIAS_NOBUF || k == ALIAS_TOUCHED_BUF) {   // a second mapping of the slice, made while it is read-write
            vm_prot_t cur = 0, max = 0;
            kern_return_t kr = vm_remap(mach_task_self(), &alias, S, 0, VM_FLAGS_ANYWHERE, mach_task_self(), (vm_address_t)p, FALSE, &cur, &max, VM_INHERIT_NONE);
            if (kr != KERN_SUCCESS) { printf("    vm_remap failed (%d): skipped\n", kr); continue; }
            if (k == ALIAS_TOUCHED_BUF) { volatile uint64_t sum = 0; for (size_t o = 0; o < S; o += PG) sum += *(volatile uint64_t *)((uint8_t *)alias + o); (void)sum; }
            objinfo("guest view", p); objinfo("alias", (void *)alias);
        } else objinfo("object", p);

        ledgers b = now();
        switch (k) {
        case NOTHING: break;
        case NONE_ONLY: case NONE_ONLY_BUF: mprotect(p, S, PROT_NONE); break;
        case FREE_ONLY: if (madvise(p, S, MADV_FREE)) perror("madvise MADV_FREE"); break;
        case NONE_THEN_FREE: mprotect(p, S, PROT_NONE); if (madvise(p, S, MADV_FREE)) perror("madvise MADV_FREE"); break;
        case FREE_THEN_NONE: if (madvise(p, S, MADV_FREE)) perror("madvise MADV_FREE"); mprotect(p, S, PROT_NONE); break;
        case REUSABLE_NOBUF: case REUSABLE_BUF: case REUSABLE_BUF_GPU: case REUSABLE_BUF_GPU_IDLE: case REUSABLE_RESIDENT: case REUSABLE_NO_REUSE:
            if (madvise(p, S, MADV_FREE_REUSABLE)) perror("madvise MADV_FREE_REUSABLE");
            break;
        case REUSABLE_THEN_NONE_BUF: if (madvise(p, S, MADV_FREE_REUSABLE)) perror("madvise MADV_FREE_REUSABLE"); mprotect(p, S, PROT_NONE); break;
        case DONTNEED_BUF: if (madvise(p, S, MADV_DONTNEED)) perror("madvise MADV_DONTNEED"); break;
        case FIXED_BUF: if (mmap(p, S, PROT_READ | PROT_WRITE, MAP_FIXED | MAP_PRIVATE | MAP_ANON, -1, 0) == MAP_FAILED) perror("mmap MAP_FIXED"); break;
        case ALIAS_NOBUF: case ALIAS_TOUCHED_BUF:
            mprotect(p, S, PROT_NONE);
            if (madvise((void *)alias, S, MADV_FREE_REUSABLE)) perror("madvise MADV_FREE_REUSABLE (alias)");
            break;
        }
        ledgers a = now(); pstate ps = pages(p, S);
        step_line("step:", b, a, ps, 1);
        if (alias) { pstate pa = pages((void *)alias, S); if (pa.ok) printf("    (alias pages present %u dirty %u reusable %u)\n", pa.present, pa.dirty, pa.reusable); }

        // What is there now, the GPU through the buffer (whatever the CPU view's protection) and the CPU through the guest view (made readable first), and what reading charged
        ledgers r0 = now(), r1 = r0;
        if (buf[k]) {
            int ok = gpu_read(buf[k], 0, &v, err, sizeof err); r1 = now();
            printf("    GPU read:       %s%s%s\n", seen(v, seed), ok ? "" : ", ", err);
            step_line("  it charged:", r0, r1, pages(p, S), 1);
            if (!ok) { uint64_t cv = 0; char ce[256] = ""; int cok = gpu_read(ctl, 0, &cv, ce, sizeof ce);
                       printf("    the GPU %s (a control read: %s)\n", cok && cv == pat(7) ? "still takes work" : "no longer takes work", cok ? seen(cv, 7) : ce); }
        }
        if (left_prot_none(k)) mprotect(p, S, PROT_READ | PROT_WRITE);
        ledgers q0 = now(); const char *cpu = seen(*(volatile uint64_t *)p, seed); ledgers q1 = now();
        printf("    CPU read:       %s, footprint %+.1f\n", cpu, mib(q1.fp - q0.fp));

        // Given back as the protocol says (MADV_FREE_REUSE where pages were made reusable, except the slice that skips it), then written whole, as the game reusing the memory
        ledgers u0 = now();
        if (made_reusable(k)) madvise(alias ? (void *)alias : p, S, MADV_FREE_REUSE);
        ledgers u1 = now(); fill(p, S, seed + 1); ledgers w = now();
        if (made_reusable(k)) step_line("MADV_FREE_REUSE:", u0, u1, (pstate){0}, 0);
        step_line("written again:", u1, w, pages(p, S), 1);
        printf("    net             footprint %+7.1f  reusable %+7.1f (after the rewrite against before the step; 0 = charged in full again)\n\n", mib(w.fp - b.fp), mib(w.reusable - b.reusable));
        if (alias) vm_deallocate(mach_task_self(), alias, S);
    }
    if (rs) { if (@available(macOS 15.0, iOS 18.0, *)) { [g_q removeResidencySet:rs]; [(id<MTLResidencySet>)rs endResidency]; } }
    munmap(base, R);
    return 0;
} }

int main(int argc, char **argv) { @autoreleasepool {
    PG = (size_t)getpagesize(); g_host = mach_host_self();
    int each = 0, only = -1; size_t mb = 64;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--each")) each = 1;
        else if (!strcmp(argv[i], "--one") && i + 1 < argc) only = atoi(argv[++i]);
        else if (atoi(argv[i]) > 0) mb = (size_t)atoi(argv[i]);
        else { fprintf(stderr, "usage: vmprobe [slice MiB] [--each]\n"); return 2; }
    }
    S = ((mb << 20) + PG - 1) & ~(PG - 1);
    if (only >= N) { fprintf(stderr, "no scenario %d\n", only); return 2; }
    if (!each) {
        if (only < 0) {
            id<MTLDevice> d = MTLCreateSystemDefaultDevice();
            printf("vmprobe: %s, %s, page %zu KB (kernel %lu KB), slice %zu MiB\n", d ? d.name.UTF8String : "no Metal device", [NSProcessInfo processInfo].operatingSystemVersionString.UTF8String,
                   PG >> 10, (unsigned long)(vm_kernel_page_size >> 10), S >> 20);
            printf("all values in MiB; host-wide counts move with other processes too\n\n");
        }
        int rc = run(only);
        if (only < 0) printf("Reading it: a step that only unmaps shows footprint -%zu with the host's internal pages and the slice's present pages unchanged; a step that frees\n"
                             "shows the host's internal pages -%zu too, or the slice's pages reusable; pages wired by the GPU stay present and dirty whatever the ledger says.\n"
                             "For the host's guest-dup lever, the row to read is [%d] (a texture pool's pages after upload) with its net and the GPU read after it.\n", S >> 20, S >> 20, REUSABLE_BUF);
        return rc;
    }
    printf("each scenario in a process of its own (slice %zu MiB)\n\n", S >> 20);
    char sz[16], ks[16]; snprintf(sz, sizeof sz, "%zu", mb);
    for (int k = 0; k < N; k++) {
        snprintf(ks, sizeof ks, "%d", k);
        char *args[] = {argv[0], "--one", ks, sz, NULL};
        pid_t pid; int st = 0;
        fflush(stdout);
        if (posix_spawn(&pid, argv[0], NULL, NULL, args, environ)) { perror("posix_spawn"); return 1; }
        waitpid(pid, &st, 0);
        if (!WIFEXITED(st) || WEXITSTATUS(st)) printf("    (scenario %d: the process ended with status %#x)\n\n", k, st);
    }
    return 0;
} }
