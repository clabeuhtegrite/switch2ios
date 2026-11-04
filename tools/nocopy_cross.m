// tools/nocopy_cross.m: can a Metal no-copy buffer, and a vm_remap run, cover a memory pool that crosses from one guest file into the next (DM_GUEST_FILE_CHUNK)?
//
// No game involved. With DM_GUEST_FILE_CHUNK the host maps the guest block from several files, each MAP_SHARED|MAP_FIXED over its own part of one PROT_NONE reservation from its
// offset 0 (hle_svc.c guest_files_map), so a memory pool across a file boundary lies in two VM objects (two vnodes), which no run with the game has tried yet. This does the same with
// three unlinked sparse files of 256 MB in the temporary folder (or the folder given), puts a 64 MB pool over the boundary of files 0 and 1 and a control pool inside file 2, and for each:
// writes half of it with the CPU and writes a quarter of it out to the file and out of memory (msync MS_INVALIDATE), makes the no-copy buffer (nvn_mtl.m mtl_pool_add), fills it with the
// GPU and reads it back with the CPU and, for the crossing pool, with pread in each file, commits it in a residency set and fills it through a queue holding the set (pool_res; macOS 15 or later), remaps the pool copy FALSE and makes
// a no-copy buffer over the remapped run with a GPU fill through it (hle_nvn.c vpool_map, vrun_set), and makes a buffer with two read-only pages at its middle (nvn_dirty.c). The VM
// regions over each range are printed as mtl_pool_add's failure dump prints them.
//
//   clang -fobjc-arc -O1 -Wall -framework Foundation -framework Metal tools/nocopy_cross.m -o work/nocopy_cross && work/nocopy_cross [folder, default $TMPDIR]
//
// Pass: every line that ends in "ok" or "FAIL" ends in "ok", for the crossing pool as for the control, with the crossing pool's times close to the control's, and the read-only line
// (no verdict of its own) says the same, made or refused, for both. A "FAIL" for the crossing pool only (a buffer refused, a GPU fill not read back, a file without the GPU's bytes, a
// remap refused) means DM_GUEST_FILE_CHUNK stays off: the host stops when Metal refuses a pool's buffer. A FAIL for the control as well points at the machine, not at the files. Exit
// status 2: the setup failed (the reservation, a file, a mapping, no Metal device). Expected, if XNU and IOKit wire one page list per map entry (vm_map_create_upl stops at the entry's
// end): every line ok. It takes a few hundred MB of memory and of the folder's disk for a few seconds; the files are unlinked at once and go when it exits.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <fcntl.h>
#include <mach/mach.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

static double ms(void) { return clock_gettime_nsec_np(CLOCK_UPTIME_RAW) / 1e6; }
static void regions(const char *what, uintptr_t a, uintptr_t b) {   // the VM regions over [a, b), as mtl_pool_add's failure dump prints them
    for (vm_address_t r = a; r < b;) {
        vm_size_t rs = 0; vm_region_basic_info_data_64_t vi; mach_msg_type_number_t cnt = VM_REGION_BASIC_INFO_COUNT_64; mach_port_t obj = MACH_PORT_NULL;
        if (vm_region_64(mach_task_self(), &r, &rs, VM_REGION_BASIC_INFO_64, (vm_region_info_t)&vi, &cnt, &obj) != KERN_SUCCESS) break;
        printf("  %s: VM region %#lx+%#lx protection %d max %d shared %d\n", what, (unsigned long)r, (unsigned long)rs, vi.protection, vi.max_protection, vi.shared);
        r += rs;
    }
}
// A GPU fill of the whole buffer through queue q, then the CPU reads it back through p
static int gpu_fill_q(id<MTLCommandQueue> q, id<MTLBuffer> b, const uint8_t *p, size_t len, uint8_t v) {
    id<MTLCommandBuffer> cb = [q commandBuffer]; id<MTLBlitCommandEncoder> bl = [cb blitCommandEncoder];
    [bl fillBuffer:b range:NSMakeRange(0, len) value:v]; [bl endEncoding]; [cb commit]; [cb waitUntilCompleted];
    size_t bad = 0; for (size_t i = 0; i < len; i++) bad += p[i] != v;
    return cb.status == MTLCommandBufferStatusCompleted && !bad;
}
static int gpu_fill(id<MTLDevice> dev, id<MTLBuffer> b, const uint8_t *p, size_t len, uint8_t v) { return gpu_fill_q([dev newCommandQueue], b, p, len, v); }
static id<MTLBuffer> nocopy(id<MTLDevice> dev, void *p, size_t len, double *t) {
    double t0 = ms(); id<MTLBuffer> b = [dev newBufferWithBytesNoCopy:p length:len options:MTLResourceStorageModeShared deallocator:nil]; *t = ms() - t0; return b;
}

int main(int argc, char **argv) { @autoreleasepool {
    const char *dir = argc > 1 ? argv[1] : getenv("TMPDIR"); if (!dir) dir = "/tmp";
    size_t pg = (size_t)getpagesize(), chunk = (size_t)256 << 20, len = (size_t)64 << 20; enum { N = 3 }; int fd[N];
    uint8_t *as = mmap(NULL, chunk * N, PROT_NONE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (as == MAP_FAILED) { perror("reserve"); return 2; }
    for (int k = 0; k < N; k++) {   // as guest_files_map: made, unlinked, sized, mapped shared over its own part of the block from offset 0
        char p[1024]; snprintf(p, sizeof p, "%s/ncx-%d-%d.mem", dir, (int)getpid(), k);
        if ((fd[k] = open(p, O_RDWR | O_CREAT | O_EXCL, 0600)) < 0 || unlink(p) || ftruncate(fd[k], (off_t)chunk)) { perror(p); return 2; }
        if (mmap(as + (size_t)k * chunk, chunk, PROT_NONE, MAP_SHARED | MAP_FIXED, fd[k], 0) != as + (size_t)k * chunk) { perror("map"); return 2; }
    }
    id<MTLDevice> dev = MTLCreateSystemDefaultDevice(); if (!dev) { puts("no Metal device"); return 2; }
    uint8_t *cross = as + chunk - len / 2, *inside = as + 2 * chunk + chunk / 4;   // a 64 MB pool over the 0/1 boundary; the control inside file 2
    for (int k = 0; k < 2; k++) {
        uint8_t *p = k ? inside : cross; const char *what = k ? "inside one file" : "across files 0 and 1";
        if (mprotect(p, len, PROT_READ | PROT_WRITE)) { perror("mprotect"); return 2; }   // as SetHeapSize / MapPhysicalMemory open the pages
        memset(p, 0x11, len / 2);   // the first half written by the CPU, the second half a hole of the file
        // half of what was written goes back to the file and leaves memory: the wiring then has to page it in from the vnode
        if (msync(p, len / 4, MS_SYNC | MS_INVALIDATE)) perror("msync");
        regions(what, (uintptr_t)p, (uintptr_t)p + len);
        double t; id<MTLBuffer> b = nocopy(dev, p, len, &t);
        printf("no-copy buffer %s: %s in %.2f ms, contents %s %s\n", what, b ? "made" : "refused", t, b && b.contents == p ? "at the pool" : "elsewhere", b ? "ok" : "FAIL");
        if (!b) continue;
        printf("GPU fill %s, read by the CPU: %s\n", what, gpu_fill(dev, b, p, len, 0xa7) ? "ok" : "FAIL");
        if (!k) {   // what each file holds after the GPU's writes are written out: 0xa7 on both sides of the boundary
            uint8_t x0 = 0, x1 = 0; msync(p, len, MS_SYNC); pread(fd[0], &x0, 1, (off_t)(chunk - len / 2)); pread(fd[1], &x1, 1, (off_t)(len / 2 - 1));
            printf("file 0 at its end reads %#x, file 1 at the boundary's far side %#x: %s\n", x0, x1, x0 == 0xa7 && x1 == 0xa7 ? "ok" : "FAIL");
        }
        if (@available(macOS 15.0, iOS 18.0, *)) {   // pool_res: into a residency set, committed on the spot (on iOS that wires the pool), then a GPU fill through a queue holding the set
            NSError *err = nil; id<MTLResidencySet> rs = [dev newResidencySetWithDescriptor:[MTLResidencySetDescriptor new] error:&err];
            double t0 = ms(); [rs addAllocation:b]; [rs commit]; [rs requestResidency]; double t1 = ms();
            NSUInteger na = rs ? rs.allocationCount : 0; int filled = 0;   // commit and requestResidency report nothing: the set's count and a GPU pass through it are the verdict
            if (rs) { id<MTLCommandQueue> q = [dev newCommandQueue]; [q addResidencySet:rs]; filled = gpu_fill_q(q, b, p, len, 0x5e); [q removeResidencySet:rs]; [rs endResidency]; }
            printf("residency set %s: %s, %lu allocation(s) committed in %.2f ms, a GPU fill through a queue holding it, read by the CPU: %s %s\n", what, rs ? "made" : "not made",
                   (unsigned long)na, t1 - t0, filled ? "done" : "failed", rs && na == 1 && filled ? "ok" : "FAIL");
            if (!rs) printf("  %s\n", err ? err.localizedDescription.UTF8String : "no error given");
        }
        // vpool_map: a virtual pool's run is this pool's pages remapped copy FALSE elsewhere, then a no-copy buffer over the remapped run (vrun_set → mtl_pool_add)
        vm_address_t al = 0; vm_prot_t cur = 0, max = 0;   // vm_remap as hle_nvn.c calls it (the iOS SDK has no mach_vm.h)
        kern_return_t kr = vm_remap(mach_task_self(), &al, len, 0, VM_FLAGS_ANYWHERE, mach_task_self(), (vm_address_t)p, FALSE, &cur, &max, VM_INHERIT_NONE);
        printf("vm_remap copy FALSE %s: kr %d, protection %d max %d %s\n", what, kr, cur, max, kr == KERN_SUCCESS ? "ok" : "FAIL");
        if (kr != KERN_SUCCESS) continue;
        mprotect((void *)(uintptr_t)al, len, PROT_READ | PROT_WRITE);
        regions("its remapped run", (uintptr_t)al, (uintptr_t)al + len);
        id<MTLBuffer> b2 = nocopy(dev, (void *)(uintptr_t)al, len, &t);
        printf("no-copy buffer over the remapped run: %s in %.2f ms %s\n", b2 ? "made" : "refused", t, b2 ? "ok" : "FAIL");
        if (b2) printf("GPU fill through the remapped run, read through the pool: %s\n", gpu_fill(dev, b2, p, len, 0x3c) ? "ok" : "FAIL");
        b2 = nil; vm_deallocate(mach_task_self(), al, len);
        // the dirty tracker leaves part of a pool read-only (nvn_dirty.c): a buffer made then, on either side of the boundary
        mprotect(p + len / 2 - pg, 2 * pg, PROT_READ);
        id<MTLBuffer> b3 = nocopy(dev, p, len, &t);
        printf("no-copy buffer %s with 2 pages read-only at its middle: %s in %.2f ms (the same answer as the control is what matters)\n", what, b3 ? "made" : "refused", t);
        mprotect(p + len / 2 - pg, 2 * pg, PROT_READ | PROT_WRITE);
    }
    return 0;
} }
