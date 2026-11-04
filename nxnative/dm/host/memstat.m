// Memory accounting: when DM_MEMSTAT=<tsv path> is set, one line is logged per second (common to Mac / iOS).
//   Totals: phys_footprint (task_info TASK_VM_INFO, which the iOS memory limit is based on), os_proc_available_memory (iOS only), Metal currentAllocatedSize
//   Breakdown: walk this process's VM regions (vm_region_recurse_64, VM_REGION_SUBMAP_INFO_64), classify by region, and for each class record
//         virtual = address space size (reserved-only counts too), resident = pages_resident, charged = dirty pages + compressed/swapped-out pages, deduplicated by VM object (≈ the part that goes into phys_footprint)
//         dedup key = (object id, offset within the object); reason for dedup: Metal wraps the guest memory pool with "no copy", so the same object is mapped in several places (share_mode TRUESHARED, ref_count>1), and summing region by region would double count;
// measured on Mac, after per-object dedup Σcharged is 2,139 MiB against phys_footprint 2,111 MiB (+1.3%)
//   Classes: guest (the whole guest address space block, including the SetHeapSize heap, alias, stacks; data read from RomFS through pread also lands here), code (embedded / loaded module images),
//         malloc (host heap), gpu (IOAccelerator / IOSurface / Metal tagged regions), stack, file (file mappings), other
//   The part where charged ≠ phys_footprint (GPU private memory accounted by IOKit is not in any VM region) is listed separately as "unattributed" = footprint − Σcharged.
//   With the guest in a file (DM_GUEST_FILE, hle_svc.c) its pages are file pages, which neither phys_footprint nor the guest's charge counts, so the columns after Metal's say what the file
//   holds: the guest block's dirty file pages (what the system's next flush of the file writes), the file's blocks on disk and the room left there; then the process's footprint peak, disk reads,
//   writes and page-ins (proc_pid_rusage), the whole system's paging and free memory (host_statistics64), what taking the sample took (sample_ms) and the host's clock (t_host, the log's seconds).
//   Other parts of the host write marks between the rows with memstat_note ("# <text>")
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <TargetConditionals.h>
#include <mach/mach.h>
#include <mach/vm_statistics.h>
#include <stdatomic.h>
#include <sys/mount.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>
#if TARGET_OS_IPHONE
#include <os/proc.h>
#endif
#include "dm.h"

void svc_guest_range(uint8_t **base, size_t *size);
int proc_pid_rusage(int pid, int flavor, rusage_info_t *buffer);   // libproc.h, which the iOS SDK does not have
// nvn_mtl.m: Metal's memory by kind (mtl_mem_kinds: pools resident, all pools, packaged textures, other textures, heaps, other buffers, allocated, guest in pools, pipelines) and
// every resident allocation one per line (mtl_mem_textures); a host built without the NVN layer has neither
__attribute__((weak)) int mtl_mem_kinds(uint64_t *out, int n, int full) { (void)out; (void)n; (void)full; return 0; }
__attribute__((weak)) void mtl_mem_textures(FILE *f) { (void)f; }
enum { C_GUEST, C_CODE, C_MALLOC, C_GPU, C_STACK, C_FILE, C_OTHER, NC };
static const char *k_cls[NC] = {"guest", "code", "malloc", "gpu", "stack", "file", "other"};

static int classify(vm_address_t a, const vm_region_submap_info_data_64_t *in) {
    uint8_t *gb; size_t gs; svc_guest_range(&gb, &gs);
    if (gb && a >= (vm_address_t)gb && a < (vm_address_t)gb + gs) return C_GUEST;
    for (int i = 0; i < g_nmod; i++)
        if (a >= (vm_address_t)g_mod[i].base && a < (vm_address_t)g_mod[i].base + g_mod[i].size) return C_CODE;
    switch (in->user_tag) {
        case VM_MEMORY_MALLOC: case VM_MEMORY_MALLOC_SMALL: case VM_MEMORY_MALLOC_LARGE: case VM_MEMORY_MALLOC_HUGE:
        case VM_MEMORY_MALLOC_TINY: case VM_MEMORY_MALLOC_LARGE_REUSABLE: case VM_MEMORY_MALLOC_LARGE_REUSED:
        case VM_MEMORY_MALLOC_NANO: case VM_MEMORY_MALLOC_MEDIUM: return C_MALLOC;
        case VM_MEMORY_IOACCELERATOR: case VM_MEMORY_IOSURFACE: case VM_MEMORY_IOKIT: return C_GPU;
        case VM_MEMORY_STACK: return C_STACK;
    }
    if (in->external_pager) return C_FILE;
    return C_OTHER;
}

// DM_MEMSTAT_DUMP=seconds: at that second, write every VM region ≥1 MiB line by line to <tsv>.regions (for diagnosing the accounting method), and every resident Metal allocation
// and memory pool to <tsv>.textures (mtl_mem_textures; DM_CTL's "texmem <path>" writes the same at any moment)
static void dump_regions(const char *path) {
    FILE *f = fopen(path, "w"); if (!f) return;
    fprintf(f, "start	size MiB	class	user_tag	share_mode	obj_id	ref_count	resident MiB	dirty MiB	swapped MiB	file\n");
    vm_address_t a = 0; vm_size_t sz; natural_t depth = 0; vm_size_t pg = vm_page_size;
    for (;;) {
        vm_region_submap_info_data_64_t in; mach_msg_type_number_t cnt = VM_REGION_SUBMAP_INFO_COUNT_64;
        if (vm_region_recurse_64(mach_task_self(), &a, &sz, &depth, (vm_region_recurse_info_t)&in, &cnt) != KERN_SUCCESS) break;
        if (in.is_submap) { depth++; continue; }
        if (((uint64_t)in.pages_resident * pg >> 20) >= 1 || sz >= (64ull << 20))
            fprintf(f, "%#lx\t%lu\t%s\t%u\t%u\t%#x\t%u\t%llu\t%llu\t%llu\t%d\n", a, sz >> 20, k_cls[classify(a, &in)], in.user_tag, in.share_mode,
                    in.object_id, in.ref_count, (uint64_t)in.pages_resident * pg >> 20, (uint64_t)in.pages_dirtied * pg >> 20, (uint64_t)in.pages_swapped_out * pg >> 20, in.external_pager);
        a += sz;
    }
    fclose(f);
}

// The memstat file has two writers, this thread's rows and memstat_note's marks from any thread: each writes and flushes under g_ms_mu, so a mark never lands inside a row.
// g_ms_file is the file once its header is written, NULL before and when it could not be opened
static pthread_mutex_t g_ms_mu = PTHREAD_MUTEX_INITIALIZER;
static _Atomic(FILE *) g_ms_file;

// One row. With walk 0 the region walk is left out (DM_MEMSTAT_WALK_EVERY, memstat_thread) and the columns it gives (the classes, charged total, unattributed, guest_dirty) read "-",
// and so do Metal's memory by kind but the pipeline count, whose walks (mtl_mem_kinds) cost as much again.
// Everything is read before g_ms_mu is taken, so a mark never waits for the walk or for Metal. Returns what taking the sample took in ms (sample_ms), the writing left out, and in *walk_ms what the region walk took of it
static double sample(FILE *f, double t, id<MTLDevice> dev, int walk, double *walk_ms) {
    uint64_t s0 = now_ns(), th0 = g_t0_ns;   // th0: the host's start (main.c host_preload), 0 until it is set (on iOS memstat starts before it)
    task_vm_info_data_t vi; mach_msg_type_number_t n = TASK_VM_INFO_COUNT;
    uint64_t fp = task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&vi, &n) == KERN_SUCCESS ? vi.phys_footprint : 0;
    uint64_t avail = 0;
#if TARGET_OS_IPHONE
    avail = os_proc_available_memory();
#endif
    uint64_t virt[NC] = {0}, res[NC] = {0}, bill[NC] = {0}, gdirty = 0;
    static uint32_t seen[1 << 16]; static uint32_t gen; gen++; if (!gen) { memset(seen, 0, sizeof seen); gen = 1; }   // Objects already counted in this sample (open addressing)
    vm_address_t a = 0; vm_size_t sz; natural_t depth = 0; vm_size_t pg = vm_page_size; uint64_t w0 = now_ns();
    while (walk) {
        vm_region_submap_info_data_64_t in; mach_msg_type_number_t cnt = VM_REGION_SUBMAP_INFO_COUNT_64;
        if (vm_region_recurse_64(mach_task_self(), &a, &sz, &depth, (vm_region_recurse_info_t)&in, &cnt) != KERN_SUCCESS) break;
        if (in.is_submap) { depth++; continue; }
        int c = classify(a, &in);
        virt[c] += sz; res[c] += (uint64_t)in.pages_resident * pg;
        if (!in.external_pager) {                             // File pages are reclaimable and do not count toward footprint
            static uint64_t key[1 << 16];                     // Key = (object id, offset within the object): the pieces of one object split apart by mprotect cover different pages and must not be merged
            uint64_t k = (uint64_t)in.object_id << 32 ^ (in.offset >> 14);
            uint32_t h = (uint32_t)((k * 0x9E3779B97F4A7C15ull) >> 48), dup = 0;
            while (seen[h] == gen) { if (key[h] == k) { dup = 1; break; } h = (h + 1) & 0xFFFF; }
            if (!dup) { seen[h] = gen; key[h] = k; bill[c] += ((uint64_t)in.pages_dirtied + in.pages_swapped_out) * pg; }
        }
        // The guest in a file: its pages written and not back in the file yet, what the system's next flush of the file writes. Each guest address has a page of its own in one guest
        // file (each file maps its part of the block from its offset 0, and virtual pools and Metal's no-copy buffers alias them outside the block), so the regions need no deduplication
        else if (c == C_GUEST) gdirty += (uint64_t)in.pages_dirtied * pg;
        a += sz; continue;
    }
    *walk_ms = (now_ns() - w0) / 1e6;
    uint64_t sum = 0; for (int c = 0; c < NC; c++) sum += bill[c];
    uint64_t mtl = dev.currentAllocatedSize, mk[9] = {0}; mtl_mem_kinds(mk, 9, walk);   // Metal by kind (all 0 without the NVN layer; with walk 0 the pipeline count only); mk[6] repeats currentAllocatedSize, which metal_alloc (mtl) already gives
    struct rusage_info_v4 ru; int ru_ok = !proc_pid_rusage(getpid(), RUSAGE_INFO_V4, (rusage_info_t *)&ru);
    static mach_port_t host; if (!host) host = mach_host_self();   // one send right, kept: each mach_host_self() adds a reference to it
    vm_statistics64_data_t hs; mach_msg_type_number_t hn = HOST_VM_INFO64_COUNT;
    int hs_ok = host_statistics64(host, HOST_VM_INFO64, (host_info64_t)&hs, &hn) == KERN_SUCCESS;
    uint64_t kp = vm_kernel_page_size;   // the unit of host_statistics64's page counts
    // The guest files (DM_GUEST_FILE, dm.h; several with DM_GUEST_FILE_CHUNK): file_alloc is the blocks of every one, disk_free the room on the volume of the first, where they all lie
    int gfd = svc_guest_file_nth(0, NULL, NULL), gn = svc_guest_files(); struct stat st; struct statfs fs; uint64_t galloc = 0;
    int st_ok = gn > 0, fs_ok = gfd >= 0 && !fstatfs(gfd, &fs);
    for (int i = 0; st_ok && i < gn; i++) { if (fstat(svc_guest_file_nth(i, NULL, NULL), &st)) st_ok = 0; else galloc += (uint64_t)st.st_blocks * 512; }
    double ms = (now_ns() - s0) / 1e6;
    dm_host_mutex_lock(&g_ms_mu);
    fprintf(f, "%.0f\t%llu\t%llu\t%llu\t%llu\t%llu\t%lld\t%lld", t, fp >> 20, avail >> 20, (unsigned long long)(mtl >> 20),
            vi.internal >> 20, vi.compressed >> 20, (vi.ledger_tag_graphics_footprint + vi.ledger_tag_graphics_footprint_compressed) >> 20,
            (long long)(vi.limit_bytes_remaining >> 20));
    if (walk) {
        for (int c = 0; c < NC; c++) fprintf(f, "\t%llu/%llu/%llu", virt[c] >> 20, res[c] >> 20, bill[c] >> 20);
        fprintf(f, "\t%llu\t%lld", sum >> 20, ((int64_t)fp - (int64_t)sum) >> 20);
    } else for (int c = 0; c < NC + 2; c++) fputs("\t-", f);   // the NC classes, charged total, unattributed
    if (walk) fprintf(f, "\t%llu\t%llu\t%llu\t%llu\t%llu\t%llu\t%llu", mk[0] >> 20, mk[1] >> 20, mk[2] >> 20, mk[3] >> 20, mk[4] >> 20, mk[5] >> 20, mk[7] >> 20);
    else for (int i = 0; i < 7; i++) fputs("\t-", f);   // pools_resident .. guest_in_pools
    fprintf(f, "\t%llu", mk[8]);
    // The guest file and the disk, the process since launch, the whole system, the sample's cost and the host's clock (the header line says the units); "-" where there was nothing to read
    fprintf(f, "\t%llu", vi.external >> 20);
    if (walk) fprintf(f, "\t%llu", gdirty >> 20); else fputs("\t-", f);
    if (st_ok) fprintf(f, "\t%llu", galloc >> 20); else fputs("\t-", f);
    if (fs_ok) fprintf(f, "\t%llu", (uint64_t)fs.f_bavail * fs.f_bsize >> 20); else fputs("\t-", f);
    if (ru_ok) fprintf(f, "\t%llu\t%llu\t%llu\t%llu\t%llu", ru.ri_lifetime_max_phys_footprint >> 20, ru.ri_pageins, ru.ri_diskio_bytesread >> 20, ru.ri_diskio_byteswritten >> 20, ru.ri_logical_writes >> 20);
    else fputs("\t-\t-\t-\t-\t-", f);
    // host_statistics64's free_count includes the speculative pages (vm_stat takes them off too): host_free leaves them to host_inactive, so the two do not count a page twice
    if (hs_ok) fprintf(f, "\t%llu\t%llu\t%llu\t%llu\t%llu\t%llu\t%llu", hs.pageins, hs.pageouts, hs.swapins, hs.swapouts,
                       (hs.free_count > hs.speculative_count ? hs.free_count - hs.speculative_count : 0) * kp >> 20, ((uint64_t)hs.inactive_count + hs.speculative_count) * kp >> 20, hs.compressor_page_count * kp >> 20);
    else fputs("\t-\t-\t-\t-\t-\t-\t-", f);
    fprintf(f, "\t%.2f", ms);
    if (th0 && s0 >= th0) fprintf(f, "\t%.3f\n", (s0 - th0) / 1e9); else fputs("\t-\n", f);
    fflush(f);
    pthread_mutex_unlock(&g_ms_mu);
    return ms;
}

// Another part of the host marks the memstat file between the rows (an event of its own, a part of the route): "# <line>", a line break in it written as a space. It is written under the rows'
// lock and flushed at once, so a mark made just before the app is ended is kept. Thread-safe; nothing happens while no memstat file is open (DM_MEMSTAT unset, or before the thread opened it). Not async-signal-safe (a mutex and stdio): never from a signal handler (crash.c); the caller may wait for the memstat thread's write to the disk, so not per frame, per punch or under g_map_lock
void memstat_note(const char *line) {
    FILE *f = atomic_load(&g_ms_file);
    if (!f || !line) return;
    dm_host_mutex_lock(&g_ms_mu);
    fputs("# ", f);
    for (const char *s = line; *s;) { size_t k = strcspn(s, "\r\n"); fwrite(s, 1, k, f); s += k; if (*s) { fputc(' ', f); s++; } }
    fputc('\n', f); fflush(f);
    pthread_mutex_unlock(&g_ms_mu);
}

// DM_MEMSTAT_WALK_EVERY=<n> (default 1: every sample): while samples take over 5 ms, the region walk and Metal's memory by kind run only every n samples, their columns reading "-" in between,
// and the rest is still read every second. The walk (vm_region_recurse_64, VM_REGION_SUBMAP_INFO_64) counts the pages of every region one by one and may hold the task's map and each region's object locked meanwhile;
// with the guest in a file every guest region maps a large file object (the one file's, or with DM_GUEST_FILE_CHUNK the one of the file that holds it), whose pages it then counts across the whole block, and the game's page faults and mprotect calls on it wait.
// A [RED] line says when a sample first takes over 5 ms
static void *memstat_thread(void *p) {
    FILE *f = fopen((const char *)p, "w");
    if (!f) { LOG("memstat: cannot open %s", (const char *)p); return NULL; }
    fprintf(f, "# unit MiB; kernel accounting internal / compressed / graphics ledger / headroom to the limit; class columns = virtual/resident/charged (deduplicated per object); charged total = Σ private charged; unattributed = phys_footprint − charged total; Metal by kind (nvn_mtl.m mtl_mem_kinds): the pools' no-copy buffers in the residency set / all of them (guest memory), packaged textures (copies of pool bytes), other textures, heaps, other buffers; guest_in_pools = the pools' pages in memory or paged out; pipelines = a count; "
               "external = the file pages mapped (kernel accounting, outside phys_footprint); guest_dirty = the guest block's file pages written and not back in the file yet (DM_GUEST_FILE), file_alloc = the guest files' blocks on disk (every file's, with DM_GUEST_FILE_CHUNK), disk_free = the room left on its volume; lifetime_max_footprint = phys_footprint's peak since launch; since launch (proc_pid_rusage): pageins = page-ins (a count), disk_read / disk_written = what the process read from and wrote to disk, logical_writes = what it wrote or dirtied in files, on disk yet or not; "
               "the whole system (host_statistics64): host_pageins / host_pageouts / host_swapins / host_swapouts = pages of %lu bytes since it started (counts), host_free = its free pages (the speculative ones left out), host_inactive = inactive + speculative, host_compressor = what the compressor holds; sample_ms = what taking the row took, in ms; t_host = the seconds since the host started, as the log counts them; - = not read for that row (no guest file, a call that failed, or the region walk left out: DM_MEMSTAT_WALK_EVERY); "
               "a line starting with # below this header is a mark another part of the host wrote (memstat_note)\nseconds\tphys_footprint\tavailable\tmetal_alloc\tinternal\tcompressed\tgraphics\theadroom", (unsigned long)vm_kernel_page_size);
    for (int c = 0; c < NC; c++) fprintf(f, "\t%s", k_cls[c]);
    fprintf(f, "\tcharged total\tunattributed\tpools_resident\tpools_all\ttex_packaged\ttex_other\theaps\tbuffers\tguest_in_pools\tpipelines"
               "\texternal\tguest_dirty\tfile_alloc\tdisk_free\tlifetime_max_footprint\tpageins\tdisk_read\tdisk_written\tlogical_writes"
               "\thost_pageins\thost_pageouts\thost_swapins\thost_swapouts\thost_free\thost_inactive\thost_compressor\tsample_ms\tt_host\n");
    atomic_store(&g_ms_file, f);   // memstat_note writes from here on, after the header
    id<MTLDevice> dev = MTLCreateSystemDefaultDevice();   // The same device object as the graphics backend (the system default device singleton)
    uint64_t t0 = now_ns();
    const char *ds = getenv("DM_MEMSTAT_DUMP"); int dump_at = ds ? atoi(ds) : -1;
    const char *we = getenv("DM_MEMSTAT_WALK_EVERY"); int every = we && atoi(we) > 1 ? atoi(we) : 1, skipped = 0, slow = 0, warned = 0;   // slow: the last walk's sample took over 5 ms
    if (every > 1) LOG("[mem] memstat: while samples take over 5 ms, the region walk and Metal's memory by kind run every %d samples, the rest every second (DM_MEMSTAT_WALK_EVERY)", every);
    char dp[1100], tp[1100]; snprintf(dp, sizeof dp, "%s.regions", (const char *)p); snprintf(tp, sizeof tp, "%s.textures", (const char *)p);
    for (int sec = 0;; sec++) {
        int walk = every == 1 || !slow || ++skipped >= every;
        if (walk) skipped = 0;
        double wms = 0, ms = sample(f, (now_ns() - t0) / 1e9, dev, walk, &wms);
        if (walk) slow = ms > 5;
        if (ms > 5 && !warned) {
            char tail[160]; warned = 1;
            if (every > 1) snprintf(tail, sizeof tail, "from now on it runs every %d samples while they take that long (DM_MEMSTAT_WALK_EVERY)", every);
            else snprintf(tail, sizeof tail, "DM_MEMSTAT_WALK_EVERY=<n> runs it only every n samples while they take that long");
            LOG("[RED] [mem] memstat: a sample took %.1f ms, %.1f of it the region walk, over 5 ms: the region walk counts the pages of every region one by one (with the guest in a file, all of the guest block's) and may hold "
                "the task's map and each region's object locked meanwhile, holding up the game's page faults on them; %s", ms, wms, tail);
        }
        if (sec == dump_at) { dump_regions(dp); FILE *tf = fopen(tp, "w"); if (tf) { mtl_mem_textures(tf); fclose(tf); } }
        sleep(1);
    }
    return NULL;
}

// Called by the constructor below (the Mac, or switches set at launch: Xcode's scheme, devicectl) and again by the iOS app's main.m once it has read its env files (Documents/env.txt,
// the bundle's dm/env.txt), which it sets after the constructors ran: the first call that finds the switch starts the thread, later ones do nothing. Likewise dm_thrcpu_start
void dm_memstat_start(void) {
    static _Atomic int started;
    const char *p = getenv("DM_MEMSTAT");
    if (!p || !*p || atomic_exchange(&started, 1)) return;
    char *path = strdup(p);
#if TARGET_OS_IPHONE
    if (*p != '/') { size_t n = strlen(getenv("HOME")) + strlen(p) + 16; path = malloc(n); snprintf(path, n, "%s/Documents/%s", getenv("HOME"), p); }   // Relative name → the container's Documents (the container path is not known before launch)
#endif
    pthread_t th; pthread_create(&th, NULL, memstat_thread, path); pthread_detach(th);
}
__attribute__((constructor)) static void memstat_start(void) { dm_memstat_start(); }

// ---------- Per-thread CPU: DM_THRCPU=<tsv path>[,interval in seconds (default 10)] (common to Mac / iOS; on iOS a relative name → the container's Documents) ----------
// Every interval, walk all threads of this process (task_threads), take the thread name and user + system CPU time (ns) from THREAD_EXTENDED_INFO, and difference them by thread id:
// each line is "seconds, thread id, thread name, CPU% (100 = one core)"; the last line is "seconds, -, total, CPU%". Used for heat attribution (which thread eats CPU); enable together with DM_HEAT to compare against thermal state.
#include <mach/thread_info.h>
#define THR_MAX 512
static void *thrcpu_thread(void *p) {
    char path[1024]; int iv = 10; { const char *c = strchr((const char *)p, ','); snprintf(path, sizeof path, "%.*s", c ? (int)(c - (const char *)p) : (int)strlen(p), (const char *)p); if (c) iv = atoi(c + 1); }
    FILE *f = fopen(path, "w"); if (!f) return NULL;
    fprintf(f, "seconds\tthread ID\tthread name\tCPU%%\n"); fflush(f);
    static uint64_t ids[THR_MAX], last[THR_MAX]; int n = 0; uint64_t t0 = now_ns(), tl = t0;
    for (;;) {
        sleep(iv); uint64_t t = now_ns(); double dt = (t - tl) / 1e9, tot = 0; tl = t;
        thread_act_array_t th; mach_msg_type_number_t nt = 0;
        if (task_threads(mach_task_self(), &th, &nt) != KERN_SUCCESS) continue;
        for (mach_msg_type_number_t i = 0; i < nt; i++) {
            thread_extended_info_data_t ei; mach_msg_type_number_t c1 = THREAD_EXTENDED_INFO_COUNT;
            thread_identifier_info_data_t ii; mach_msg_type_number_t c2 = THREAD_IDENTIFIER_INFO_COUNT;
            if (thread_info(th[i], THREAD_EXTENDED_INFO, (thread_info_t)&ei, &c1) == KERN_SUCCESS && thread_info(th[i], THREAD_IDENTIFIER_INFO, (thread_info_t)&ii, &c2) == KERN_SUCCESS) {
                uint64_t cpu = ei.pth_user_time + ei.pth_system_time; int k = 0; while (k < n && ids[k] != ii.thread_id) k++;
                if (k == n && n < THR_MAX) { ids[n] = ii.thread_id; last[n] = cpu; n++; }   // New thread: only record the baseline this round
                else if (k < n) { double pct = (cpu - last[k]) / 1e9 / dt * 100; last[k] = cpu; tot += pct;
                    if (pct >= 0.5) { char gn[96]; int thr_name_of(pthread_t, char *, size_t); pthread_t pt = pthread_from_mach_thread_np(th[i]);
                        const char *nm = pt && thr_name_of(pt, gn, sizeof gn) ? gn : ei.pth_name[0] ? ei.pth_name : "-";   // Guest thread: handle + guest name; host thread: pthread name
                        fprintf(f, "%.0f\t%llu\t%s\t%.1f\n", (t - t0) / 1e9, ii.thread_id, nm, pct); } }
            }
            mach_port_deallocate(mach_task_self(), th[i]);
        }
        vm_deallocate(mach_task_self(), (vm_address_t)th, nt * sizeof *th);
        fprintf(f, "%.0f\t-\ttotal\t%.1f\n", (t - t0) / 1e9, tot); fflush(f);
    }
    return NULL;
}
void dm_thrcpu_start(void) {
    static _Atomic int started;
    const char *p = getenv("DM_THRCPU");
    if (!p || !*p || atomic_exchange(&started, 1)) return;
    char *spec = strdup(p);
#if TARGET_OS_IPHONE
    if (*p != '/') { size_t n = strlen(getenv("HOME")) + strlen(p) + 16; spec = malloc(n); snprintf(spec, n, "%s/Documents/%s", getenv("HOME"), p); }
#endif
    pthread_t th; pthread_create(&th, NULL, thrcpu_thread, spec); pthread_detach(th);
}
__attribute__((constructor)) static void thrcpu_start(void) { dm_thrcpu_start(); }
