/*
 * winusb_shim.c -- maps the Windows USB API that Hamamatsu's managed library
 * wrrusb2a.dll P/Invokes (kernel32 / user32 / shell32 / setupapi / winusb)
 * onto libusb-1.0 on macOS. One dylib implements all five "libraries"; the
 * .NET host resolves every DllImport library name to it.
 *
 * Contract: macos-native/PORT_SPEC.md section 5. Managed declarations:
 * macos-native/tools/wrrusb2a_api.txt (PINVOKE lines) and
 * macos-native/tools/wrrusb2a_refs.txt ("P/Invoke (ImplMap)").
 *
 * Semantics summary
 *   - Enumeration (SetupDi*) takes a snapshot of the libusb devices with
 *     idVendor 0x0661 (env WRRUSB_SHIM_VID overrides, hex) and hands out
 *     Windows-looking device paths; CreateFile(path) opens that device with
 *     libusb_open; WinUsb_Initialize claims interface 0 and returns a separate
 *     interface handle; pipe reads/writes are bulk/interrupt transfers with
 *     the per-pipe PIPE_TRANSFER_TIMEOUT policy.
 *   - Transfers use libusb's asynchronous API driven from the calling thread
 *     (the same loop libusb's synchronous API runs), so a WinUsb_Free or
 *     CloseHandle issued from another thread while a read is blocked cancels
 *     it: the read returns FALSE with errno ERROR_OPERATION_ABORTED (995),
 *     exactly what WinUSB does when a handle with pending I/O is closed. The
 *     DLL relies on this (its CaptureThread blocks in WinUsb_ReadPipe while
 *     USB2_captureStop / USB2_close run on the API thread; it imports neither
 *     WinUsb_AbortPipe nor Thread.Abort).
 *   - Every handle is >= 0x100000 (never a file descriptor number) and the
 *     handle kinds live in distinct ranges.
 *   - Win32 error codes are stored in errno, which is what the managed side
 *     reads through Marshal.GetLastWin32Error() on Unix.
 *   - WRRUSB_SHIM_DEBUG=1 traces every call to stderr (2 = libusb debug too).
 *
 * Locking
 *   g_lock guards every table. A libusb handle or configuration descriptor is
 *   only ever used while either g_lock is held or the slot is pinned by a
 *   busy count (acquire_iface / release_op); WinUsb_Free and CloseHandle set
 *   `closing`, cancel the transfers in flight and wait for the busy count to
 *   reach zero before releasing the interface, freeing the descriptor and
 *   closing the device, so no operation ever touches freed memory.
 */
#include "winusb_shim.h"

#include <ctype.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <libusb.h>

/* ------------------------------------------------------------------------ */
/* Layout checks against the managed struct definitions (wrrusb2a_api.txt).  */
/* ------------------------------------------------------------------------ */
_Static_assert(sizeof(intptr_t) == 8, "arm64: IntPtr is 8 bytes");
_Static_assert(sizeof(int32_t) == 4 && sizeof(int64_t) == 8, "fixed-width ints");

_Static_assert(sizeof(GUID) == 16, "System.Guid is 16 bytes");
_Static_assert(offsetof(GUID, Data2) == 4 && offsetof(GUID, Data3) == 6 && offsetof(GUID, Data4) == 8,
               "System.Guid field offsets");

_Static_assert(sizeof(SP_DEVICE_INTERFACE_DATA) == 32, "SP_DEVICE_INTERFACE_DATA is 32 bytes");
_Static_assert(offsetof(SP_DEVICE_INTERFACE_DATA, cbSize) == 0, "SP_DEVICE_INTERFACE_DATA.cbSize @0");
_Static_assert(offsetof(SP_DEVICE_INTERFACE_DATA, InterfaceClassGuid) == 4, "SP_DEVICE_INTERFACE_DATA.InterfaceClassGuid @4");
_Static_assert(offsetof(SP_DEVICE_INTERFACE_DATA, Flags) == 20, "SP_DEVICE_INTERFACE_DATA.Flags @20");
_Static_assert(offsetof(SP_DEVICE_INTERFACE_DATA, Reserved) == 24, "SP_DEVICE_INTERFACE_DATA.Reserved @24");

_Static_assert(sizeof(USB_DEVICE_DESCRIPTOR) == 18, "USB_DEVICE_DESCRIPTOR is 18 bytes");
_Static_assert(offsetof(USB_DEVICE_DESCRIPTOR, bcdUSB) == 2, "USB_DEVICE_DESCRIPTOR.bcdUSB @2");
_Static_assert(offsetof(USB_DEVICE_DESCRIPTOR, bMaxPacketSize0) == 7, "USB_DEVICE_DESCRIPTOR.bMaxPacketSize0 @7");
_Static_assert(offsetof(USB_DEVICE_DESCRIPTOR, idVendor) == 8, "USB_DEVICE_DESCRIPTOR.idVendor @8");
_Static_assert(offsetof(USB_DEVICE_DESCRIPTOR, idProduct) == 10, "USB_DEVICE_DESCRIPTOR.idProduct @10");
_Static_assert(offsetof(USB_DEVICE_DESCRIPTOR, bcdDevice) == 12, "USB_DEVICE_DESCRIPTOR.bcdDevice @12");
_Static_assert(offsetof(USB_DEVICE_DESCRIPTOR, iManufacturer) == 14, "USB_DEVICE_DESCRIPTOR.iManufacturer @14");
_Static_assert(offsetof(USB_DEVICE_DESCRIPTOR, bNumConfigurations) == 17, "USB_DEVICE_DESCRIPTOR.bNumConfigurations @17");

_Static_assert(sizeof(USB_INTERFACE_DESCRIPTOR) == 9, "USB_INTERFACE_DESCRIPTOR is 9 bytes");
_Static_assert(offsetof(USB_INTERFACE_DESCRIPTOR, iInterface) == 8, "USB_INTERFACE_DESCRIPTOR.iInterface @8");

_Static_assert(sizeof(WINUSB_PIPE_INFORMATION) == 12, "WINUSB_PIPE_INFORMATION is 12 bytes");
_Static_assert(offsetof(WINUSB_PIPE_INFORMATION, PipeType) == 0, "WINUSB_PIPE_INFORMATION.PipeType @0");
_Static_assert(offsetof(WINUSB_PIPE_INFORMATION, PipeId) == 4, "WINUSB_PIPE_INFORMATION.PipeId @4");
_Static_assert(offsetof(WINUSB_PIPE_INFORMATION, MaximumPacketSize) == 6, "WINUSB_PIPE_INFORMATION.MaximumPacketSize @6");
_Static_assert(offsetof(WINUSB_PIPE_INFORMATION, Interval) == 8, "WINUSB_PIPE_INFORMATION.Interval @8");

_Static_assert(sizeof(WINUSB_SETUP_PACKET) == 8, "WINUSB_SETUP_PACKET is 8 bytes (Pack=1)");
_Static_assert(offsetof(WINUSB_SETUP_PACKET, Request) == 1, "WINUSB_SETUP_PACKET.Request @1");
_Static_assert(offsetof(WINUSB_SETUP_PACKET, Value) == 2, "WINUSB_SETUP_PACKET.Value @2");
_Static_assert(offsetof(WINUSB_SETUP_PACKET, Index) == 4, "WINUSB_SETUP_PACKET.Index @4");
_Static_assert(offsetof(WINUSB_SETUP_PACKET, Length) == 6, "WINUSB_SETUP_PACKET.Length @6");

_Static_assert(sizeof(SHChangeNotifyEntry) == 16, "SHChangeNotifyEntry is 16 bytes");
_Static_assert(offsetof(SHChangeNotifyEntry, Recursively) == 8, "SHChangeNotifyEntry.Recursively @8");

/* Enum values must match the managed enums and the USB bmAttributes codes. */
_Static_assert(UsbdPipeTypeControl == 0 && UsbdPipeTypeIsochronous == 1 &&
               UsbdPipeTypeBulk == 2 && UsbdPipeTypeInterrupt == 3, "USBD_PIPE_TYPE values");
_Static_assert((int)UsbdPipeTypeBulk == (int)LIBUSB_TRANSFER_TYPE_BULK &&
               (int)UsbdPipeTypeInterrupt == (int)LIBUSB_TRANSFER_TYPE_INTERRUPT &&
               (int)UsbdPipeTypeIsochronous == (int)LIBUSB_TRANSFER_TYPE_ISOCHRONOUS,
               "USBD_PIPE_TYPE == libusb transfer type (bmAttributes & 3)");
_Static_assert(UsbLowSpeed == 1 && UsbFullSpeed == 2 && UsbHighSpeed == 3, "USB_DEVICE_SPEED values");
_Static_assert(PIPE_TRANSFER_TIMEOUT == 3 && ALLOW_PARTIAL_READS == 5 && AUTO_FLUSH == 6 && RAW_IO == 7,
               "POLICY_TYPE values");

/* ------------------------------------------------------------------------ */
/* Debug tracing                                                             */
/* ------------------------------------------------------------------------ */
static int g_debug_level;
static pthread_once_t g_debug_once = PTHREAD_ONCE_INIT;

static void debug_init(void)
{
    const char *e = getenv(WRRUSB_SHIM_ENV_DEBUG);
    if (e == NULL || *e == '\0' || strcmp(e, "0") == 0) {
        g_debug_level = 0;
    } else {
        int v = atoi(e);
        g_debug_level = v > 0 ? v : 1;
    }
}

static int debug_level(void)
{
    pthread_once(&g_debug_once, debug_init);
    return g_debug_level;
}

__attribute__((format(printf, 1, 2)))
static void shim_log(const char *fmt, ...)
{
    if (debug_level() <= 0)
        return;
    int saved = errno;
    va_list ap;
    va_start(ap, fmt);
    fputs("[winusb_shim] ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    fflush(stderr);
    va_end(ap);
    errno = saved;
}

#define TRACE(...) do { if (debug_level() > 0) shim_log(__VA_ARGS__); } while (0)

/* ------------------------------------------------------------------------ */
/* Win32-style results                                                       */
/* ------------------------------------------------------------------------ */
static int32_t win_ok(void)
{
    errno = ERROR_SUCCESS;
    return 1;
}

static int32_t win_fail(int code)
{
    errno = code;
    return 0;
}

/* PORT_SPEC section 5: libusb timeout -> ERROR_SEM_TIMEOUT, anything else -> ERROR_GEN_FAILURE
 * (return codes of libusb calls, e.g. a failed submission). */
static int map_libusb_error(int rc)
{
    return rc == LIBUSB_ERROR_TIMEOUT ? ERROR_SEM_TIMEOUT : ERROR_GEN_FAILURE;
}

/* Completion status of a transfer: timeout -> 121; cancelled by WinUsb_Free /
 * CloseHandle -> ERROR_OPERATION_ABORTED (995), what WinUSB reports for I/O
 * aborted by a handle closure; anything else -> 31. */
static int map_transfer_status(enum libusb_transfer_status st)
{
    switch (st) {
    case LIBUSB_TRANSFER_TIMED_OUT: return ERROR_SEM_TIMEOUT;
    case LIBUSB_TRANSFER_CANCELLED: return ERROR_OPERATION_ABORTED;
    default:                        return ERROR_GEN_FAILURE;
    }
}

static const char *status_name(enum libusb_transfer_status st)
{
    switch (st) {
    case LIBUSB_TRANSFER_COMPLETED: return "completed";
    case LIBUSB_TRANSFER_ERROR:     return "error";
    case LIBUSB_TRANSFER_TIMED_OUT: return "timed out";
    case LIBUSB_TRANSFER_CANCELLED: return "cancelled";
    case LIBUSB_TRANSFER_STALL:     return "stall";
    case LIBUSB_TRANSFER_NO_DEVICE: return "no device";
    case LIBUSB_TRANSFER_OVERFLOW:  return "overflow";
    }
    return "unknown";
}

/* ------------------------------------------------------------------------ */
/* Configuration from the environment                                        */
/* ------------------------------------------------------------------------ */
static uint16_t target_vid(void)
{
    const char *e = getenv(WRRUSB_SHIM_ENV_VID);
    if (e != NULL && *e != '\0') {
        char *end = NULL;
        unsigned long v = strtoul(e, &end, 16);
        if (end != e && v <= 0xFFFFul)
            return (uint16_t)v;
        shim_log("ignoring invalid %s=\"%s\"", WRRUSB_SHIM_ENV_VID, e);
    }
    return (uint16_t)WRRUSB_SHIM_DEFAULT_VID;
}

static unsigned int default_timeout_ms(void)
{
    const char *e = getenv(WRRUSB_SHIM_ENV_DEFAULT_TIMEOUT);
    if (e != NULL && *e != '\0') {
        char *end = NULL;
        unsigned long v = strtoul(e, &end, 10);
        if (end != e && *end == '\0' && v <= UINT_MAX)
            return (unsigned int)v;
        shim_log("ignoring invalid %s=\"%s\"", WRRUSB_SHIM_ENV_DEFAULT_TIMEOUT, e);
    }
    return WRRUSB_SHIM_DEFAULT_TIMEOUT_MS;
}

/* ------------------------------------------------------------------------ */
/* libusb context (process lifetime)                                         */
/* ------------------------------------------------------------------------ */
static libusb_context *g_ctx;
static int g_ctx_rc = LIBUSB_ERROR_OTHER;
static pthread_once_t g_ctx_once = PTHREAD_ONCE_INIT;

static void ctx_init(void)
{
    g_ctx_rc = libusb_init(&g_ctx);
    if (g_ctx_rc != 0) {
        shim_log("libusb_init failed: %s", libusb_strerror(g_ctx_rc));
        g_ctx = NULL;
        return;
    }
    if (debug_level() >= 2)
        libusb_set_option(g_ctx, LIBUSB_OPTION_LOG_LEVEL, LIBUSB_LOG_LEVEL_DEBUG);
}

static libusb_context *ctx(void)
{
    pthread_once(&g_ctx_once, ctx_init);
    return g_ctx;
}

/* ------------------------------------------------------------------------ */
/* Handle tables                                                             */
/* ------------------------------------------------------------------------ */
#define MAX_DEVICES    16
#define MAX_IFACES     16
#define MAX_SETS       WRRUSB_SHIM_SET_TABLE_CAPACITY   /* set handles 0x300000..0x3FFFFE; the table grows on
                                                         * demand up to this and then recycles its oldest set
                                                         * (see alloc_set_slot_locked) */
#define PIPE_SLOTS     32
#define IN_PIPE_SLOTS  16
#define PATH_MAX_LEN   128

/* The sentinel set is returned when no set slot can be allocated (realloc
 * failure): it enumerates nothing and destroying it is a no-op, so
 * SetupDiGetClassDevs never fails. Its handle is never a slot index. */
#define EMPTY_SET_HANDLE (WRRUSB_SHIM_SET_HANDLE_BASE + MAX_SETS)

/* A transfer in flight on an interface, linked from the interface slot so a
 * concurrent WinUsb_Free / CloseHandle can cancel it. The node lives on the
 * transferring thread's stack for the duration of submit_and_wait(). */
struct inflight {
    struct libusb_transfer *xfer;
    int completed;                 /* set by the libusb completion callback */
    struct inflight *next;
};

/* Per IN pipe: WinUSB's ALLOW_PARTIAL_READS (default TRUE) / AUTO_FLUSH
 * (default FALSE) policies and the bytes a device returned beyond what the
 * last WinUsb_ReadPipe asked for, which WinUSB saves and hands to the next
 * read of that pipe. */
struct pipe_stash {
    int allow_partial;
    int auto_flush;
    uint8_t *data;
    uint32_t len;
    int ends_short;                /* the saved bytes were the end of a short packet */
};

struct shim_device {
    int in_use;
    int closing;                   /* CloseHandle in progress: no new operation starts */
    int busy;                      /* operations using h outside g_lock */
    unsigned gen;                  /* bumped whenever the slot is (re)allocated */
    libusb_device_handle *h;
    uint8_t bus, addr;
    uint16_t vid, pid;
    char path[PATH_MAX_LEN];
};

struct shim_iface {
    int in_use;
    int closing;                   /* WinUsb_Free / CloseHandle in progress */
    int busy;                      /* operations pinning this interface */
    unsigned gen;
    int dev_slot;
    int claimed;                           /* interface 0 claimed */
    struct libusb_config_descriptor *cfg;  /* active configuration, may be NULL; used only under g_lock */
    struct inflight *inflight;             /* transfers in flight on this interface */
    int64_t timeout_ms[PIPE_SLOTS];        /* PIPE_TRANSFER_TIMEOUT per pipe, -1 = unset */
    struct pipe_stash in[IN_PIPE_SLOTS];   /* IN pipes, indexed by endpoint number */
};

struct set_entry {
    uint8_t bus, addr;
    uint16_t vid, pid;
    char path[PATH_MAX_LEN];
};

struct shim_set {
    int in_use;
    int count;
    struct set_entry *entries;
};

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_idle = PTHREAD_COND_INITIALIZER;   /* signalled when a busy count drops */
static struct shim_device g_devices[MAX_DEVICES];
static struct shim_iface g_ifaces[MAX_IFACES];
static struct shim_set *g_sets;   /* grows on demand (a caller that never destroys its sets must not starve) */
static int g_set_cap;
static int g_set_hint;            /* every set slot below this index is in use */
static int g_set_victim;          /* round-robin recycling point once the table is full */
static unsigned g_gen;
static intptr_t g_notify_counter;

static void lock(void)   { pthread_mutex_lock(&g_lock); }
static void unlock(void) { pthread_mutex_unlock(&g_lock); }

/* All lookups: caller holds g_lock. */
static struct shim_device *dev_from_handle(intptr_t h)
{
    intptr_t idx = h - WRRUSB_SHIM_DEVICE_HANDLE_BASE;
    if (idx < 0 || idx >= MAX_DEVICES || !g_devices[idx].in_use)
        return NULL;
    return &g_devices[idx];
}

static struct shim_iface *iface_from_handle(intptr_t h)
{
    intptr_t idx = h - WRRUSB_SHIM_IFACE_HANDLE_BASE;
    if (idx < 0 || idx >= MAX_IFACES || !g_ifaces[idx].in_use)
        return NULL;
    return &g_ifaces[idx];
}

/* An interface handle that can still be used: known, not being closed, and
 * its device neither closed nor closing. */
static struct shim_iface *live_iface_from_handle(intptr_t h)
{
    struct shim_iface *iface = iface_from_handle(h);
    if (iface == NULL || iface->closing)
        return NULL;
    struct shim_device *dev = &g_devices[iface->dev_slot];
    if (!dev->in_use || dev->closing)
        return NULL;
    return iface;
}

static struct shim_set *set_from_handle(intptr_t h)
{
    intptr_t idx = h - WRRUSB_SHIM_SET_HANDLE_BASE;
    if (idx < 0 || idx >= g_set_cap || !g_sets[idx].in_use)
        return NULL;
    return &g_sets[idx];
}

static int pipe_slot(uint8_t ep)
{
    return (ep & 0x0F) | ((ep & 0x80) ? 0x10 : 0);
}

/* Timeout for a pipe: the stored policy, else the environment default. 0 = infinite
 * (same meaning in WinUSB and libusb). Caller holds g_lock. */
static unsigned int timeout_locked(const struct shim_iface *iface, uint8_t ep)
{
    int64_t t = iface->timeout_ms[pipe_slot(ep)];
    if (t < 0)
        return default_timeout_ms();
    return (unsigned int)t;
}

/* ------------------------------------------------------------------------ */
/* Pinning a handle for an operation that uses libusb outside g_lock          */
/* ------------------------------------------------------------------------ */
struct op_ref {
    int iface_slot;                /* -1 when only the device is pinned */
    int dev_slot;
    libusb_device_handle *lh;
};

/* Pins the interface handle and its device: the slots cannot be released or
 * closed until release_op(). Fails with ERROR_INVALID_HANDLE when the handle is
 * stale or being closed. */
static int acquire_iface(intptr_t h, struct op_ref *ref)
{
    lock();
    struct shim_iface *iface = live_iface_from_handle(h);
    if (iface == NULL) {
        unlock();
        errno = ERROR_INVALID_HANDLE;
        return 0;
    }
    iface->busy++;
    g_devices[iface->dev_slot].busy++;
    ref->iface_slot = (int)(iface - g_ifaces);
    ref->dev_slot = iface->dev_slot;
    ref->lh = g_devices[iface->dev_slot].h;
    unlock();
    return 1;
}

static void release_op(const struct op_ref *ref)
{
    lock();
    if (ref->iface_slot >= 0)
        g_ifaces[ref->iface_slot].busy--;
    g_devices[ref->dev_slot].busy--;
    pthread_cond_broadcast(&g_idle);
    unlock();
}

/* Caller holds g_lock; returns with it held. */
static void wait_idle_locked(const int *busy)
{
    while (*busy > 0)
        pthread_cond_wait(&g_idle, &g_lock);
}

/* ------------------------------------------------------------------------ */
/* Transfers (asynchronous API driven from the calling thread)                */
/* ------------------------------------------------------------------------ */
static void LIBUSB_CALL transfer_done(struct libusb_transfer *t)
{
    *(int *)t->user_data = 1;
}

/* Submits a filled transfer on the pinned interface and waits for it,
 * handling libusb events on this thread (the loop of libusb's synchronous
 * API, sync.c sync_transfer_wait_for_completion). The transfer is registered
 * on the interface while in flight so WinUsb_Free / CloseHandle can cancel
 * it; an aborted transfer completes with LIBUSB_TRANSFER_CANCELLED.
 * Returns a libusb error when the submission failed, else 0 with t->status
 * and t->actual_length filled in. */
static int submit_and_wait(const struct op_ref *ref, struct libusb_transfer *t)
{
    struct inflight node;
    node.xfer = t;
    node.completed = 0;
    node.next = NULL;
    t->callback = transfer_done;
    t->user_data = &node.completed;

    struct shim_iface *iface = &g_ifaces[ref->iface_slot];
    lock();
    if (iface->closing || g_devices[ref->dev_slot].closing) {
        unlock();
        t->status = LIBUSB_TRANSFER_CANCELLED;
        t->actual_length = 0;
        return 0;
    }
    /* Submitted under g_lock: a closer that holds the lock sees every transfer
     * that has been submitted and none that is about to be. */
    int rc = libusb_submit_transfer(t);
    if (rc != 0) {
        unlock();
        return rc;
    }
    node.next = iface->inflight;
    iface->inflight = &node;
    unlock();

    while (!node.completed) {
        rc = libusb_handle_events_completed(ctx(), &node.completed);
        if (rc < 0) {
            if (rc == LIBUSB_ERROR_INTERRUPTED)
                continue;
            shim_log("libusb_handle_events failed: %s; cancelling the transfer", libusb_strerror(rc));
            libusb_cancel_transfer(t);
            continue;
        }
        if (t->dev_handle == NULL) {          /* completion after libusb_close() */
            t->status = LIBUSB_TRANSFER_NO_DEVICE;
            node.completed = 1;
        }
    }

    lock();
    for (struct inflight **pp = &iface->inflight; *pp != NULL; pp = &(*pp)->next) {
        if (*pp == &node) {
            *pp = node.next;
            break;
        }
    }
    unlock();
    return 0;
}

/* Marks an interface as closing and cancels its transfers in flight: they
 * complete with LIBUSB_TRANSFER_CANCELLED on the threads that wait for them.
 * Caller holds g_lock. */
static void begin_close_iface_locked(struct shim_iface *iface)
{
    iface->closing = 1;
    for (struct inflight *n = iface->inflight; n != NULL; n = n->next) {
        int rc = libusb_cancel_transfer(n->xfer);
        if (rc == 0)
            TRACE("  cancelled a transfer in flight on interface slot %d", (int)(iface - g_ifaces));
        else if (rc != LIBUSB_ERROR_NOT_FOUND)   /* NOT_FOUND: it just completed */
            shim_log("libusb_cancel_transfer: %s", libusb_strerror(rc));
    }
}

/* Releases interface 0 and frees everything the slot owns. Caller holds
 * g_lock and has waited for iface->busy == 0 (no transfer in flight). */
static void destroy_iface_locked(struct shim_iface *iface)
{
    struct shim_device *dev = &g_devices[iface->dev_slot];
    if (iface->claimed && dev->in_use && dev->h != NULL) {
        int rc = libusb_release_interface(dev->h, 0);
        if (rc != 0)
            shim_log("libusb_release_interface(0): %s", libusb_strerror(rc));
    }
    if (iface->cfg != NULL)
        libusb_free_config_descriptor(iface->cfg);
    for (int i = 0; i < IN_PIPE_SLOTS; i++)
        free(iface->in[i].data);
    memset(iface, 0, sizeof(*iface));
}

/* ------------------------------------------------------------------------ */
/* Device path helpers                                                       */
/* ------------------------------------------------------------------------ */
static void format_path(char *out, size_t n, uint16_t vid, uint16_t pid, uint8_t bus, uint8_t addr)
{
    snprintf(out, n, WRRUSB_SHIM_PATH_FORMAT, (unsigned)vid, (unsigned)pid, (unsigned)bus, (unsigned)addr);
}

/* Accepts the path in any letter case (the managed side may normalise it). */
static int parse_path(const char *path, uint16_t *vid, uint16_t *pid, unsigned *bus, unsigned *addr)
{
    char low[256];
    size_t i;
    for (i = 0; path[i] != '\0' && i < sizeof(low) - 1; i++)
        low[i] = (char)tolower((unsigned char)path[i]);
    low[i] = '\0';

    const char *p = strstr(low, "vid_");
    if (p == NULL)
        return 0;
    unsigned v = 0, pr = 0, b = 0, a = 0;
    if (sscanf(p, "vid_%4x&pid_%4x#%u-%u#", &v, &pr, &b, &a) != 4)
        return 0;
    if (v > 0xFFFF || pr > 0xFFFF || b > 255 || a > 255)
        return 0;
    *vid = (uint16_t)v;
    *pid = (uint16_t)pr;
    *bus = b;
    *addr = a;
    return 1;
}

static const GUID HPK_GUID = { 0x88dc0d16u, 0xf5f0u, 0x4836u, { 0xb6, 0xd0, 0x89, 0xfd, 0x7c, 0x34, 0x95, 0xf3 } };

static void guid_to_string(const GUID *g, char *out, size_t n)
{
    if (g == NULL) {
        snprintf(out, n, "(null)");
        return;
    }
    snprintf(out, n, "{%08x-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x}",
             (unsigned)g->Data1, (unsigned)g->Data2, (unsigned)g->Data3,
             g->Data4[0], g->Data4[1], g->Data4[2], g->Data4[3],
             g->Data4[4], g->Data4[5], g->Data4[6], g->Data4[7]);
}

/* ------------------------------------------------------------------------ */
/* Descriptor helpers (the descriptor is only read under g_lock)             */
/* ------------------------------------------------------------------------ */

/* The interface WinUsb_Initialize claimed (bInterfaceNumber 0, falling back to
 * the first interface of the configuration). */
static const struct libusb_interface *claimed_interface(const struct libusb_config_descriptor *cfg)
{
    if (cfg == NULL || cfg->bNumInterfaces == 0)
        return NULL;
    for (int i = 0; i < cfg->bNumInterfaces; i++) {
        const struct libusb_interface *itf = &cfg->interface[i];
        if (itf->num_altsetting > 0 && itf->altsetting[0].bInterfaceNumber == 0)
            return itf;
    }
    return &cfg->interface[0];
}

static const struct libusb_interface_descriptor *find_altsetting(const struct libusb_config_descriptor *cfg, uint8_t alt)
{
    const struct libusb_interface *itf = claimed_interface(cfg);
    if (itf == NULL)
        return NULL;
    for (int j = 0; j < itf->num_altsetting; j++)
        if (itf->altsetting[j].bAlternateSetting == alt)
            return &itf->altsetting[j];
    return NULL;
}

static const struct libusb_endpoint_descriptor *find_endpoint(const struct libusb_config_descriptor *cfg, uint8_t ep)
{
    if (cfg == NULL)
        return NULL;
    for (int i = 0; i < cfg->bNumInterfaces; i++) {
        const struct libusb_interface *itf = &cfg->interface[i];
        for (int j = 0; j < itf->num_altsetting; j++) {
            const struct libusb_interface_descriptor *alt = &itf->altsetting[j];
            for (int k = 0; k < alt->bNumEndpoints; k++)
                if (alt->endpoint[k].bEndpointAddress == ep)
                    return &alt->endpoint[k];
        }
    }
    return NULL;
}

/* ======================================================================== */
/* kernel32                                                                 */
/* ======================================================================== */

intptr_t CreateFile(const char *path_utf8, uint32_t access, int32_t share, void *sec,
                    int32_t disposition, int32_t flags, int32_t template_)
{
    (void)sec; (void)template_;
    TRACE("CreateFile(\"%s\", access=0x%x, share=%d, disposition=%d, flags=0x%x)",
          path_utf8 ? path_utf8 : "(null)", access, share, disposition, flags);

    uint16_t vid = 0, pid = 0;
    unsigned bus = 0, addr = 0;
    if (path_utf8 == NULL || !parse_path(path_utf8, &vid, &pid, &bus, &addr)) {
        shim_log("CreateFile: path is not a shim device path -> ERROR_FILE_NOT_FOUND");
        errno = ERROR_FILE_NOT_FOUND;
        return (intptr_t)-1;
    }

    libusb_context *c = ctx();
    if (c == NULL) {
        errno = ERROR_GEN_FAILURE;
        return (intptr_t)-1;
    }

    libusb_device **list = NULL;
    ssize_t n = libusb_get_device_list(c, &list);
    if (n < 0) {
        shim_log("CreateFile: libusb_get_device_list: %s", libusb_strerror((int)n));
        errno = ERROR_GEN_FAILURE;
        return (intptr_t)-1;
    }

    libusb_device *match = NULL;
    for (ssize_t i = 0; i < n; i++) {
        if (libusb_get_bus_number(list[i]) != bus || libusb_get_device_address(list[i]) != addr)
            continue;
        struct libusb_device_descriptor d;
        if (libusb_get_device_descriptor(list[i], &d) != 0)
            continue;
        if (d.idVendor == vid && d.idProduct == pid) {
            match = list[i];
            break;
        }
    }

    if (match == NULL) {
        libusb_free_device_list(list, 1);
        shim_log("CreateFile: no device vid_%04x pid_%04x at %u-%u -> ERROR_FILE_NOT_FOUND", vid, pid, bus, addr);
        errno = ERROR_FILE_NOT_FOUND;
        return (intptr_t)-1;
    }

    libusb_device_handle *lh = NULL;
    int rc = libusb_open(match, &lh);
    libusb_free_device_list(list, 1);   /* the open handle keeps its own reference */
    if (rc != 0) {
        shim_log("CreateFile: libusb_open: %s", libusb_strerror(rc));
        errno = rc == LIBUSB_ERROR_ACCESS ? ERROR_ACCESS_DENIED : ERROR_GEN_FAILURE;
        return (intptr_t)-1;
    }

    lock();
    int slot = -1;
    for (int i = 0; i < MAX_DEVICES; i++) {
        if (!g_devices[i].in_use) {
            slot = i;
            break;
        }
    }
    if (slot < 0) {
        unlock();
        libusb_close(lh);
        shim_log("CreateFile: device table full");
        errno = ERROR_TOO_MANY_OPEN_FILES;
        return (intptr_t)-1;
    }
    struct shim_device *dev = &g_devices[slot];
    memset(dev, 0, sizeof(*dev));
    dev->in_use = 1;
    dev->gen = ++g_gen;
    dev->h = lh;
    dev->bus = (uint8_t)bus;
    dev->addr = (uint8_t)addr;
    dev->vid = vid;
    dev->pid = pid;
    format_path(dev->path, sizeof(dev->path), vid, pid, (uint8_t)bus, (uint8_t)addr);
    unlock();

    intptr_t handle = WRRUSB_SHIM_DEVICE_HANDLE_BASE + slot;
    TRACE("CreateFile -> handle 0x%lx", (long)handle);
    errno = ERROR_SUCCESS;
    return handle;
}

int32_t CloseHandle(intptr_t h)
{
    TRACE("CloseHandle(0x%lx)", (long)h);
    lock();
    struct shim_device *dev = dev_from_handle(h);
    if (dev == NULL || dev->closing) {
        unlock();
        shim_log("CloseHandle: invalid handle 0x%lx", (long)h);
        return win_fail(ERROR_INVALID_HANDLE);
    }
    int slot = (int)(dev - g_devices);
    dev->closing = 1;

    /* Like WinUSB, closing the device aborts the I/O pending on its
     * interfaces: cancel every transfer in flight, then wait until no thread
     * uses the libusb handle any more before releasing and closing. */
    for (int i = 0; i < MAX_IFACES; i++)
        if (g_ifaces[i].in_use && g_ifaces[i].dev_slot == slot)
            begin_close_iface_locked(&g_ifaces[i]);
    wait_idle_locked(&dev->busy);

    for (int i = 0; i < MAX_IFACES; i++)
        if (g_ifaces[i].in_use && g_ifaces[i].dev_slot == slot)
            destroy_iface_locked(&g_ifaces[i]);
    libusb_device_handle *lh = dev->h;
    memset(dev, 0, sizeof(*dev));
    unlock();

    libusb_close(lh);
    TRACE("CloseHandle(0x%lx) -> TRUE", (long)h);
    return win_ok();
}

int32_t QueryPerformanceCounter(int64_t *out)
{
    if (out == NULL)
        return 0;
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return 0;
    *out = (int64_t)ts.tv_sec * 1000000000LL + (int64_t)ts.tv_nsec;
    return 1;
}

int32_t QueryPerformanceFrequency(int64_t *out)
{
    if (out == NULL)
        return 0;
    *out = 1000000000LL;
    return 1;
}

/* No HUSBDCAM.ini on macOS: every key resolves to its default. */
int32_t GetPrivateProfileString(const char *app, const char *key, const char *def,
                                char *out, uint64_t size, const char *file)
{
    TRACE("GetPrivateProfileString(app=\"%s\", key=\"%s\", def=\"%s\", size=%llu, file=\"%s\")",
          app ? app : "(null)", key ? key : "(null)", def ? def : "(null)",
          (unsigned long long)size, file ? file : "(null)");
    if (out == NULL || size == 0)
        return 0;
    const char *src = def ? def : "";
    size_t n = strlen(src);
    if (n > size - 1)
        n = (size_t)(size - 1);
    if (n > 0)
        memcpy(out, src, n);
    out[n] = '\0';
    return (int32_t)n;
}

/* ======================================================================== */
/* user32 / shell32 (no device-change notifications on macOS: stubs)        */
/* ======================================================================== */

intptr_t RegisterDeviceNotification(intptr_t recipient, void *filter, int32_t flags)
{
    (void)filter;
    lock();
    intptr_t handle = WRRUSB_SHIM_NOTIFY_HANDLE_BASE + (++g_notify_counter);
    unlock();
    TRACE("RegisterDeviceNotification(recipient=0x%lx, flags=0x%x) -> 0x%lx", (long)recipient, flags, (long)handle);
    errno = ERROR_SUCCESS;
    return handle;
}

int32_t UnregisterDeviceNotification(intptr_t h)
{
    TRACE("UnregisterDeviceNotification(0x%lx)", (long)h);
    return win_ok();
}

uint32_t SHChangeNotifyRegister(intptr_t hwnd, int32_t sources, int32_t events,
                                uint32_t msg, int32_t count, SHChangeNotifyEntry entry)
{
    TRACE("SHChangeNotifyRegister(hwnd=0x%lx, sources=0x%x, events=0x%x, msg=%u, count=%d, entry={pIdl=0x%lx, recursive=%d}) -> 1",
          (long)hwnd, sources, events, msg, count, (long)entry.pIdl, entry.Recursively);
    return 1;
}

int32_t SHChangeNotifyUnregister(uint8_t h)
{
    TRACE("SHChangeNotifyUnregister(%u) -> TRUE", (unsigned)h);
    return 1;
}

/*
 * The DLL imports the two shell32 functions by ordinal (EntryPoint="#2" and
 * "#4"), so the same functions are exported under those literal names.
 * dlsym(3) on macOS prepends the C underscore, hence the asm labels "_#2" and
 * "_#4": dlsym(handle, "#2") finds "_#2" exactly like dlsym(handle, "CreateFile")
 * finds "_CreateFile".
 */
WINUSB_SHIM_EXPORT uint32_t shim_shell32_ordinal_2(intptr_t hwnd, int32_t sources, int32_t events,
                                                   uint32_t msg, int32_t count, SHChangeNotifyEntry entry)
    __asm__("_#2");
uint32_t shim_shell32_ordinal_2(intptr_t hwnd, int32_t sources, int32_t events,
                                uint32_t msg, int32_t count, SHChangeNotifyEntry entry)
{
    return SHChangeNotifyRegister(hwnd, sources, events, msg, count, entry);
}

WINUSB_SHIM_EXPORT int32_t shim_shell32_ordinal_4(uint8_t h) __asm__("_#4");
int32_t shim_shell32_ordinal_4(uint8_t h)
{
    return SHChangeNotifyUnregister(h);
}

/* ======================================================================== */
/* setupapi                                                                 */
/* ======================================================================== */

/*
 * Allocates a device-info-set slot. The table grows on demand up to MAX_SETS
 * entries. The DLL never destroys some of the sets it creates (one per
 * USB2_initialize, found by tracing it), so once the table is full the oldest
 * slots are recycled round-robin instead of failing: Windows'
 * SetupDiGetClassDevs never stops working because of leaked sets either.
 * Returns -1 only when realloc fails. Caller holds g_lock.
 */
static int alloc_set_slot_locked(void)
{
    for (int i = g_set_hint; i < g_set_cap; i++) {
        if (!g_sets[i].in_use) {
            memset(&g_sets[i], 0, sizeof(g_sets[i]));
            g_sets[i].in_use = 1;
            g_set_hint = i + 1;
            return i;
        }
    }
    if (g_set_cap < MAX_SETS) {
        int ncap = g_set_cap > 0 ? g_set_cap * 2 : 16;
        if (ncap > MAX_SETS)
            ncap = MAX_SETS;
        struct shim_set *ns = realloc(g_sets, (size_t)ncap * sizeof(*ns));
        if (ns == NULL)
            return -1;
        memset(ns + g_set_cap, 0, (size_t)(ncap - g_set_cap) * sizeof(*ns));
        int slot = g_set_cap;
        g_sets = ns;
        g_set_cap = ncap;
        g_sets[slot].in_use = 1;
        g_set_hint = slot + 1;
        return slot;
    }
    int slot = g_set_victim;
    g_set_victim = (g_set_victim + 1) % MAX_SETS;
    shim_log("set table full (%d sets never destroyed): recycling set 0x%lx",
             MAX_SETS, (long)(WRRUSB_SHIM_SET_HANDLE_BASE + slot));
    free(g_sets[slot].entries);
    memset(&g_sets[slot], 0, sizeof(g_sets[slot]));
    g_sets[slot].in_use = 1;
    g_set_hint = g_set_cap;
    return slot;
}

static void free_set_slot_locked(struct shim_set *s)
{
    int idx = (int)(s - g_sets);
    free(s->entries);
    memset(s, 0, sizeof(*s));
    if (idx < g_set_hint)
        g_set_hint = idx;
}

/*
 * Test hook: WRRUSB_SHIM_FAKE_DEVICES="vid:pid@bus-addr,..." (hex ids, decimal
 * bus/address) appends synthetic entries of the target vendor to the snapshot
 * so the enumeration and detail-buffer path can be exercised without
 * hardware. CreateFile on such a path fails with ERROR_FILE_NOT_FOUND unless a
 * real libusb device with that identity exists.
 */
static int append_fake_devices(struct set_entry **entries, int count, uint16_t vid)
{
    const char *e = getenv(WRRUSB_SHIM_ENV_FAKE_DEVICES);
    if (e == NULL || *e == '\0')
        return count;
    const char *p = e;
    while (*p != '\0') {
        unsigned v = 0, pr = 0, b = 0, a = 0;
        int used = 0;
        if (sscanf(p, "%4x:%4x@%u-%u%n", &v, &pr, &b, &a, &used) != 4 || used <= 0) {
            shim_log("ignoring invalid %s entry at \"%s\"", WRRUSB_SHIM_ENV_FAKE_DEVICES, p);
            break;
        }
        if (v == vid && pr <= 0xFFFF && b <= 255 && a <= 255) {
            struct set_entry *ne = realloc(*entries, (size_t)(count + 1) * sizeof(**entries));
            if (ne == NULL)
                break;
            *entries = ne;
            struct set_entry *x = &ne[count++];
            memset(x, 0, sizeof(*x));
            x->bus = (uint8_t)b;
            x->addr = (uint8_t)a;
            x->vid = (uint16_t)v;
            x->pid = (uint16_t)pr;
            format_path(x->path, sizeof(x->path), x->vid, x->pid, x->bus, x->addr);
            TRACE("  [%d] %s (synthetic)", count - 1, x->path);
        }
        p += used;
        if (*p == ',')
            p++;
    }
    return count;
}

int32_t SetupDiCreateDeviceInfoList(const GUID *g, int32_t hwnd)
{
    char gs[48];
    guid_to_string(g, gs, sizeof(gs));
    lock();
    int slot = alloc_set_slot_locked();
    unlock();
    if (slot < 0) {
        shim_log("SetupDiCreateDeviceInfoList: out of memory");
        errno = ERROR_NOT_ENOUGH_MEMORY;
        return -1;
    }
    intptr_t handle = WRRUSB_SHIM_SET_HANDLE_BASE + slot;
    TRACE("SetupDiCreateDeviceInfoList(%s, hwnd=%d) -> 0x%lx", gs, hwnd, (long)handle);
    errno = ERROR_SUCCESS;
    return (int32_t)handle;
}

/* Snapshot of the libusb devices whose idVendor matches (0x0661 or WRRUSB_SHIM_VID). */
intptr_t SetupDiGetClassDevs(const GUID *g, void *enumerator, void *hwnd, int32_t flags)
{
    (void)enumerator; (void)hwnd;
    char gs[48];
    guid_to_string(g, gs, sizeof(gs));
    uint16_t vid = target_vid();
    TRACE("SetupDiGetClassDevs(%s, flags=0x%x) enumerating idVendor 0x%04x", gs, flags, vid);

    struct set_entry *entries = NULL;
    int count = 0;

    libusb_context *c = ctx();
    libusb_device **list = NULL;
    ssize_t n = c ? libusb_get_device_list(c, &list) : (ssize_t)LIBUSB_ERROR_OTHER;
    if (n < 0) {
        shim_log("SetupDiGetClassDevs: libusb_get_device_list: %s", libusb_strerror((int)n));
        n = 0;
        list = NULL;
    }
    if (n > 0) {
        entries = calloc((size_t)n, sizeof(*entries));
        if (entries == NULL)
            n = 0;
    }
    for (ssize_t i = 0; i < n; i++) {
        struct libusb_device_descriptor d;
        if (libusb_get_device_descriptor(list[i], &d) != 0 || d.idVendor != vid)
            continue;
        struct set_entry *e = &entries[count++];
        e->bus = libusb_get_bus_number(list[i]);
        e->addr = libusb_get_device_address(list[i]);
        e->vid = d.idVendor;
        e->pid = d.idProduct;
        format_path(e->path, sizeof(e->path), e->vid, e->pid, e->bus, e->addr);
        TRACE("  [%d] %s", count - 1, e->path);
    }
    if (list)
        libusb_free_device_list(list, 1);
    count = append_fake_devices(&entries, count, vid);

    lock();
    int slot = alloc_set_slot_locked();
    if (slot >= 0) {
        g_sets[slot].count = count;
        g_sets[slot].entries = entries;
    }
    unlock();
    if (slot < 0) {
        free(entries);
        shim_log("SetupDiGetClassDevs: out of memory, returning the empty set");
        errno = ERROR_NOT_ENOUGH_MEMORY;
        return EMPTY_SET_HANDLE;
    }
    intptr_t handle = WRRUSB_SHIM_SET_HANDLE_BASE + slot;
    TRACE("SetupDiGetClassDevs -> 0x%lx (%d device(s))", (long)handle, count);
    errno = ERROR_SUCCESS;
    return handle;
}

int32_t SetupDiEnumDeviceInterfaces(intptr_t set, void *devinfo, const GUID *g,
                                    int32_t index, SP_DEVICE_INTERFACE_DATA *out)
{
    (void)devinfo;
    TRACE("SetupDiEnumDeviceInterfaces(set=0x%lx, index=%d)", (long)set, index);
    if (out == NULL)
        return win_fail(ERROR_INVALID_PARAMETER);

    lock();
    int count = 0;
    if (set != EMPTY_SET_HANDLE) {
        struct shim_set *s = set_from_handle(set);
        if (s == NULL) {
            unlock();
            shim_log("SetupDiEnumDeviceInterfaces: invalid set 0x%lx", (long)set);
            return win_fail(ERROR_INVALID_HANDLE);
        }
        count = s->count;
    }
    unlock();

    if (index < 0 || index >= count)
        return win_fail(ERROR_NO_MORE_ITEMS);

    out->cbSize = (int32_t)sizeof(SP_DEVICE_INTERFACE_DATA);
    out->InterfaceClassGuid = g ? *g : HPK_GUID;
    out->Flags = 1;               /* SPINT_ACTIVE */
    out->Reserved = (intptr_t)index;
    return win_ok();
}

int32_t SetupDiGetDeviceInterfaceDetail(intptr_t set, SP_DEVICE_INTERFACE_DATA *d,
                                        void *detail, int32_t size, int32_t *required, void *devinfo)
{
    (void)devinfo;
    TRACE("SetupDiGetDeviceInterfaceDetail(set=0x%lx, index=%ld, detail=%p, size=%d)",
          (long)set, d ? (long)d->Reserved : -1L, detail, size);
    if (d == NULL)
        return win_fail(ERROR_INVALID_PARAMETER);

    lock();
    struct shim_set *s = set_from_handle(set);
    if (s == NULL) {
        unlock();
        return win_fail(ERROR_INVALID_HANDLE);
    }
    if (d->Reserved < 0 || d->Reserved >= (intptr_t)s->count) {
        unlock();
        return win_fail(ERROR_INVALID_PARAMETER);
    }
    const char *path = s->entries[d->Reserved].path;
    size_t plen = strlen(path);
    size_t need = 4 + plen + 1;      /* cbSize field, then the UTF-8 path */
    if (required != NULL)
        *required = (int32_t)need;
    if (detail == NULL || size < 0 || (size_t)size < need) {
        unlock();
        return win_fail(ERROR_INSUFFICIENT_BUFFER);
    }
    memcpy((char *)detail + 4, path, plen + 1);   /* bytes 0..3 (cbSize) belong to the caller */
    unlock();
    TRACE("SetupDiGetDeviceInterfaceDetail -> \"%s\"", path);
    return win_ok();
}

int32_t SetupDiDestroyDeviceInfoList(intptr_t set)
{
    TRACE("SetupDiDestroyDeviceInfoList(0x%lx)", (long)set);
    if (set == EMPTY_SET_HANDLE)
        return win_ok();
    lock();
    struct shim_set *s = set_from_handle(set);
    if (s == NULL) {
        unlock();
        return win_fail(ERROR_INVALID_HANDLE);
    }
    free_set_slot_locked(s);
    unlock();
    return win_ok();
}

/* ======================================================================== */
/* winusb                                                                   */
/* ======================================================================== */

int32_t WinUsb_Initialize(intptr_t fileHandle, intptr_t *ifaceHandle)
{
    TRACE("WinUsb_Initialize(0x%lx)", (long)fileHandle);
    if (ifaceHandle == NULL)
        return win_fail(ERROR_INVALID_PARAMETER);

    lock();
    struct shim_device *dev = dev_from_handle(fileHandle);
    if (dev == NULL || dev->closing) {
        unlock();
        shim_log("WinUsb_Initialize: invalid device handle 0x%lx", (long)fileHandle);
        return win_fail(ERROR_INVALID_HANDLE);
    }
    int dev_slot = (int)(dev - g_devices);
    libusb_device_handle *lh = dev->h;
    int slot = -1;
    for (int i = 0; i < MAX_IFACES; i++) {
        if (!g_ifaces[i].in_use) {
            slot = i;
            break;
        }
    }
    if (slot < 0) {
        unlock();
        shim_log("WinUsb_Initialize: interface table full");
        return win_fail(ERROR_TOO_MANY_OPEN_FILES);
    }
    struct shim_iface *iface = &g_ifaces[slot];
    memset(iface, 0, sizeof(*iface));
    iface->in_use = 1;             /* reserved; the handle is not visible to callers yet */
    iface->gen = ++g_gen;
    iface->dev_slot = dev_slot;
    for (int i = 0; i < PIPE_SLOTS; i++)
        iface->timeout_ms[i] = -1;
    for (int i = 0; i < IN_PIPE_SLOTS; i++) {
        iface->in[i].allow_partial = 1;    /* WinUSB defaults: ALLOW_PARTIAL_READS TRUE, AUTO_FLUSH FALSE */
        iface->in[i].auto_flush = 0;
    }
    dev->busy++;                   /* the device handle is used outside the lock below */
    unlock();

    libusb_device *ld = libusb_get_device(lh);

    /* Detach a kernel driver if libusb reports one (not applicable on macOS,
     * where the calls return NOT_SUPPORTED; harmless). */
    libusb_set_auto_detach_kernel_driver(lh, 1);
    int kd = libusb_kernel_driver_active(lh, 0);
    if (kd == 1) {
        int rc = libusb_detach_kernel_driver(lh, 0);
        shim_log("WinUsb_Initialize: detach kernel driver: %s", libusb_strerror(rc));
    }

    /* Make sure the device is configured before claiming (usually already done by the OS). */
    int cfgv = 0;
    if (libusb_get_configuration(lh, &cfgv) == 0 && cfgv == 0) {
        struct libusb_config_descriptor *c0 = NULL;
        if (libusb_get_config_descriptor(ld, 0, &c0) == 0) {
            int rc = libusb_set_configuration(lh, c0->bConfigurationValue);
            shim_log("WinUsb_Initialize: set configuration %u: %s", c0->bConfigurationValue, libusb_strerror(rc));
            libusb_free_config_descriptor(c0);
        }
    }

    int rc = libusb_claim_interface(lh, 0);
    if (rc != 0) {
        shim_log("WinUsb_Initialize: libusb_claim_interface(0): %s", libusb_strerror(rc));
        lock();
        memset(iface, 0, sizeof(*iface));
        dev->busy--;
        pthread_cond_broadcast(&g_idle);
        unlock();
        return win_fail(rc == LIBUSB_ERROR_ACCESS || rc == LIBUSB_ERROR_BUSY ? ERROR_ACCESS_DENIED : ERROR_GEN_FAILURE);
    }

    struct libusb_config_descriptor *cfg = NULL;
    if (libusb_get_active_config_descriptor(ld, &cfg) != 0) {
        cfg = NULL;
        if (libusb_get_config_descriptor(ld, 0, &cfg) != 0)
            cfg = NULL;
    }
    if (cfg == NULL)
        shim_log("WinUsb_Initialize: no configuration descriptor; pipe queries will fail and transfers assume bulk");

    lock();
    if (dev->closing) {
        /* CloseHandle raced us: it is waiting for the device to become idle. */
        unlock();
        libusb_release_interface(lh, 0);
        if (cfg != NULL)
            libusb_free_config_descriptor(cfg);
        lock();
        memset(iface, 0, sizeof(*iface));
        dev->busy--;
        pthread_cond_broadcast(&g_idle);
        unlock();
        shim_log("WinUsb_Initialize: device handle 0x%lx closed meanwhile", (long)fileHandle);
        return win_fail(ERROR_INVALID_HANDLE);
    }
    iface->claimed = 1;
    iface->cfg = cfg;
    dev->busy--;
    pthread_cond_broadcast(&g_idle);
    unlock();

    *ifaceHandle = WRRUSB_SHIM_IFACE_HANDLE_BASE + slot;
    TRACE("WinUsb_Initialize -> interface handle 0x%lx", (long)*ifaceHandle);
    return win_ok();
}

int32_t WinUsb_Free(intptr_t h)
{
    TRACE("WinUsb_Free(0x%lx)", (long)h);
    lock();
    struct shim_iface *iface = iface_from_handle(h);
    if (iface == NULL || iface->closing) {
        unlock();
        return win_fail(ERROR_INVALID_HANDLE);
    }
    unsigned gen = iface->gen;
    /* WinUSB aborts the I/O pending on an interface handle that is freed. */
    begin_close_iface_locked(iface);
    wait_idle_locked(&iface->busy);
    if (iface->in_use && iface->gen == gen)   /* unless a concurrent CloseHandle took it down already */
        destroy_iface_locked(iface);
    unlock();
    TRACE("WinUsb_Free(0x%lx) -> TRUE", (long)h);
    return win_ok();
}

/* Standard control transfer on the pinned interface's device; `buf` holds the
 * data stage (wLength bytes, read for OUT requests, written for IN). Sets
 * *transferred to the data-stage bytes moved. */
static int32_t control_transfer(intptr_t h, uint8_t bmRequestType, uint8_t bRequest, uint16_t wValue,
                                uint16_t wIndex, uint8_t *buf, uint16_t wLength, uint32_t *transferred,
                                const char *who)
{
    struct op_ref ref;
    if (!acquire_iface(h, &ref))
        return 0;
    unsigned int timeout;
    lock();
    timeout = timeout_locked(&g_ifaces[ref.iface_slot], 0);
    unlock();

    uint8_t *cb = malloc(LIBUSB_CONTROL_SETUP_SIZE + (size_t)wLength);
    struct libusb_transfer *t = libusb_alloc_transfer(0);
    if (cb == NULL || t == NULL) {
        free(cb);
        libusb_free_transfer(t);
        release_op(&ref);
        return win_fail(ERROR_NOT_ENOUGH_MEMORY);
    }
    libusb_fill_control_setup(cb, bmRequestType, bRequest, wValue, wIndex, wLength);
    int is_in = (bmRequestType & LIBUSB_ENDPOINT_IN) != 0;
    if (!is_in && wLength > 0)
        memcpy(cb + LIBUSB_CONTROL_SETUP_SIZE, buf, wLength);
    libusb_fill_control_transfer(t, ref.lh, cb, NULL, NULL, timeout);

    int rc = submit_and_wait(&ref, t);
    enum libusb_transfer_status status = t->status;
    uint32_t actual = rc == 0 && t->actual_length > 0 ? (uint32_t)t->actual_length : 0;
    if (rc == 0 && is_in && actual > 0)
        memcpy(buf, cb + LIBUSB_CONTROL_SETUP_SIZE, actual > wLength ? wLength : actual);
    libusb_free_transfer(t);
    free(cb);
    release_op(&ref);

    if (rc != 0) {
        shim_log("%s: libusb_submit_transfer: %s", who, libusb_strerror(rc));
        return win_fail(map_libusb_error(rc));
    }
    if (transferred != NULL)
        *transferred = actual;
    if (status != LIBUSB_TRANSFER_COMPLETED) {
        shim_log("%s: %s (%u bytes)", who, status_name(status), actual);
        return win_fail(map_transfer_status(status));
    }
    TRACE("%s -> %u bytes", who, actual);
    return win_ok();
}

int32_t WinUsb_GetDescriptor(intptr_t h, uint8_t type, uint8_t index, uint16_t lang,
                             USB_DEVICE_DESCRIPTOR *buf, uint32_t len, uint32_t *transferred)
{
    TRACE("WinUsb_GetDescriptor(0x%lx, type=%u, index=%u, lang=0x%04x, len=%u)", (long)h, type, index, lang, len);
    if (transferred != NULL)
        *transferred = 0;
    if (buf == NULL && len > 0)
        return win_fail(ERROR_INVALID_PARAMETER);

    if (type == USB_DEVICE_DESCRIPTOR_TYPE) {
        struct op_ref ref;
        if (!acquire_iface(h, &ref))
            return 0;
        struct libusb_device_descriptor d;
        int rc = libusb_get_device_descriptor(libusb_get_device(ref.lh), &d);
        release_op(&ref);
        if (rc != 0) {
            shim_log("WinUsb_GetDescriptor: %s", libusb_strerror(rc));
            return win_fail(map_libusb_error(rc));
        }
        USB_DEVICE_DESCRIPTOR out;
        out.bLength = d.bLength;
        out.bDescriptorType = d.bDescriptorType;
        out.bcdUSB = d.bcdUSB;
        out.bDeviceClass = d.bDeviceClass;
        out.bDeviceSubClass = d.bDeviceSubClass;
        out.bDeviceProtocol = d.bDeviceProtocol;
        out.bMaxPacketSize0 = d.bMaxPacketSize0;
        out.idVendor = d.idVendor;
        out.idProduct = d.idProduct;
        out.bcdDevice = d.bcdDevice;
        out.iManufacturer = d.iManufacturer;
        out.iProduct = d.iProduct;
        out.iSerialNumber = d.iSerialNumber;
        out.bNumConfigurations = d.bNumConfigurations;
        size_t n = len < sizeof(out) ? len : sizeof(out);
        if (n > 0)
            memcpy(buf, &out, n);
        if (transferred != NULL)
            *transferred = (uint32_t)n;
        TRACE("WinUsb_GetDescriptor -> vid_%04x pid_%04x bcdDevice %04x (%zu bytes)", d.idVendor, d.idProduct, d.bcdDevice, n);
        return win_ok();
    }

    /* Any other descriptor: a standard GET_DESCRIPTOR control request. */
    uint16_t wlen = len > 0xFFFF ? 0xFFFF : (uint16_t)len;
    return control_transfer(h, LIBUSB_ENDPOINT_IN | LIBUSB_REQUEST_TYPE_STANDARD | LIBUSB_RECIPIENT_DEVICE,
                            LIBUSB_REQUEST_GET_DESCRIPTOR, (uint16_t)((type << 8) | index), lang,
                            (uint8_t *)buf, wlen, transferred, "WinUsb_GetDescriptor");
}

int32_t WinUsb_QueryInterfaceSettings(intptr_t h, uint8_t alt, USB_INTERFACE_DESCRIPTOR *out)
{
    TRACE("WinUsb_QueryInterfaceSettings(0x%lx, alt=%u)", (long)h, alt);
    if (out == NULL)
        return win_fail(ERROR_INVALID_PARAMETER);
    lock();
    struct shim_iface *iface = live_iface_from_handle(h);
    if (iface == NULL) {
        unlock();
        return win_fail(ERROR_INVALID_HANDLE);
    }
    const struct libusb_interface_descriptor *a = find_altsetting(iface->cfg, alt);
    if (a == NULL) {
        unlock();
        return win_fail(ERROR_NO_MORE_ITEMS);
    }
    out->bLength = a->bLength;
    out->bDescriptorType = a->bDescriptorType;
    out->bInterfaceNumber = a->bInterfaceNumber;
    out->bAlternateSetting = a->bAlternateSetting;
    out->bNumEndpoints = a->bNumEndpoints;
    out->bInterfaceClass = a->bInterfaceClass;
    out->bInterfaceSubClass = a->bInterfaceSubClass;
    out->bInterfaceProtocol = a->bInterfaceProtocol;
    out->iInterface = a->iInterface;
    unlock();
    TRACE("WinUsb_QueryInterfaceSettings -> interface %u alt %u, %u endpoint(s), class %02x",
          out->bInterfaceNumber, out->bAlternateSetting, out->bNumEndpoints, out->bInterfaceClass);
    return win_ok();
}

int32_t WinUsb_QueryPipe(intptr_t h, uint8_t alt, uint8_t pipeIndex, WINUSB_PIPE_INFORMATION *out)
{
    TRACE("WinUsb_QueryPipe(0x%lx, alt=%u, pipeIndex=%u)", (long)h, alt, pipeIndex);
    if (out == NULL)
        return win_fail(ERROR_INVALID_PARAMETER);
    lock();
    struct shim_iface *iface = live_iface_from_handle(h);
    if (iface == NULL) {
        unlock();
        return win_fail(ERROR_INVALID_HANDLE);
    }
    const struct libusb_interface_descriptor *a = find_altsetting(iface->cfg, alt);
    if (a == NULL || pipeIndex >= a->bNumEndpoints) {
        unlock();
        return win_fail(ERROR_NO_MORE_ITEMS);
    }
    const struct libusb_endpoint_descriptor *ep = &a->endpoint[pipeIndex];
    out->PipeType = (int32_t)(ep->bmAttributes & LIBUSB_TRANSFER_TYPE_MASK);
    out->PipeId = ep->bEndpointAddress;
    out->MaximumPacketSize = ep->wMaxPacketSize;
    out->Interval = ep->bInterval;
    unlock();
    TRACE("WinUsb_QueryPipe -> type %d, id 0x%02x, maxpacket %u, interval %u",
          out->PipeType, out->PipeId, out->MaximumPacketSize, out->Interval);
    return win_ok();
}

int32_t WinUsb_QueryDeviceInformation(intptr_t h, uint32_t type, uint32_t *len, uint8_t *buf)
{
    TRACE("WinUsb_QueryDeviceInformation(0x%lx, type=%u, len=%u)", (long)h, type, len ? *len : 0u);
    struct op_ref ref;
    if (!acquire_iface(h, &ref))
        return 0;
    if (type != WINUSB_DEVICE_SPEED || len == NULL || buf == NULL) {
        release_op(&ref);
        return win_fail(ERROR_INVALID_PARAMETER);
    }
    if (*len < 1) {
        release_op(&ref);
        *len = 1;
        return win_fail(ERROR_INSUFFICIENT_BUFFER);
    }
    int speed = libusb_get_device_speed(libusb_get_device(ref.lh));
    release_op(&ref);
    uint8_t v;
    switch (speed) {
    case LIBUSB_SPEED_LOW:  v = UsbLowSpeed;  break;
    case LIBUSB_SPEED_FULL: v = UsbFullSpeed; break;
    default:                v = UsbHighSpeed; break;   /* high, super and unknown all report high */
    }
    buf[0] = v;
    *len = 1;
    TRACE("WinUsb_QueryDeviceInformation -> speed %u (libusb %d)", v, speed);
    return win_ok();
}

/* Policy values arrive as 1, 2 or 4 bytes (the DLL has `ref byte` and `ref uint` overloads). */
static int read_policy_value(uint32_t valueLen, const void *value, uint32_t *out)
{
    if (value == NULL)
        return 0;
    if (valueLen >= 4) {
        memcpy(out, value, 4);
    } else if (valueLen == 2) {
        uint16_t v16;
        memcpy(&v16, value, 2);
        *out = v16;
    } else if (valueLen == 1) {
        *out = *(const uint8_t *)value;
    } else {
        return 0;
    }
    return 1;
}

int32_t WinUsb_SetPipePolicy(intptr_t h, uint8_t pipe, uint32_t policy, uint32_t valueLen, const void *value)
{
    TRACE("WinUsb_SetPipePolicy(0x%lx, pipe=0x%02x, policy=%u, valueLen=%u)", (long)h, pipe, policy, valueLen);
    if (policy < SHORT_PACKET_TERMINATE || policy > RAW_IO)
        return win_fail(ERROR_INVALID_PARAMETER);
    uint32_t v = 0;
    int have_value = read_policy_value(valueLen, value, &v);
    if (policy == PIPE_TRANSFER_TIMEOUT && !have_value)
        return win_fail(ERROR_INVALID_PARAMETER);

    lock();
    struct shim_iface *iface = live_iface_from_handle(h);
    if (iface == NULL) {
        unlock();
        return win_fail(ERROR_INVALID_HANDLE);
    }
    switch (policy) {
    case PIPE_TRANSFER_TIMEOUT:
        iface->timeout_ms[pipe_slot(pipe)] = v;
        break;
    case ALLOW_PARTIAL_READS:
        if (have_value)
            iface->in[pipe & 0x0F].allow_partial = v != 0;
        break;
    case AUTO_FLUSH:
        if (have_value)
            iface->in[pipe & 0x0F].auto_flush = v != 0;
        break;
    default:
        /* SHORT_PACKET_TERMINATE, AUTO_CLEAR_STALL, IGNORE_SHORT_PACKETS, RAW_IO:
         * accepted and ignored (PORT_SPEC section 5); the transfer semantics
         * already match WinUSB's defaults (a short packet ends a read). */
        break;
    }
    unlock();
    if (policy == PIPE_TRANSFER_TIMEOUT)
        TRACE("WinUsb_SetPipePolicy: pipe 0x%02x timeout = %u ms%s", pipe, v, v == 0 ? " (infinite)" : "");
    else
        TRACE("WinUsb_SetPipePolicy: pipe 0x%02x policy %u = %u", pipe, policy, have_value ? v : 0u);
    return win_ok();
}

/* Appends surplus bytes to a pipe's stash. Caller holds g_lock. */
static void stash_append(struct pipe_stash *st, const uint8_t *data, uint32_t n, int ends_short)
{
    uint8_t *nd = realloc(st->data, (size_t)st->len + n);
    if (nd == NULL) {
        shim_log("out of memory: dropping %u surplus byte(s)", n);
        return;
    }
    memcpy(nd + st->len, data, n);
    st->data = nd;
    st->len += n;
    st->ends_short = ends_short;
}

/*
 * Bulk / interrupt transfer on a pipe. Reads follow WinUSB's default pipe
 * policies: the read completes on a short packet or when the buffer is full;
 * with ALLOW_PARTIAL_READS (default TRUE) a device returning more than the
 * caller asked for does not fail the read -- the request is rounded up to a
 * whole packet, the caller gets what it asked for and the surplus is saved
 * and returned by the next read of that pipe (discarded instead when
 * AUTO_FLUSH is TRUE). On macOS the un-rounded request would otherwise end in
 * an IOKit overrun (LIBUSB_TRANSFER_OVERFLOW) and the surplus would be lost.
 */
static int32_t pipe_transfer(intptr_t h, uint8_t pipe, uint8_t *buf, uint32_t len,
                             uint32_t *transferred, int is_read, const char *who)
{
    if (transferred != NULL)
        *transferred = 0;
    struct op_ref ref;
    if (!acquire_iface(h, &ref))
        return 0;
    if (is_read ? !(pipe & LIBUSB_ENDPOINT_IN) : (pipe & LIBUSB_ENDPOINT_IN)) {
        release_op(&ref);
        shim_log("%s: pipe 0x%02x has the wrong direction", who, pipe);
        return win_fail(ERROR_INVALID_PARAMETER);
    }
    if (len > (uint32_t)INT_MAX - 4096u || (len > 0 && buf == NULL)) {
        release_op(&ref);
        return win_fail(ERROR_INVALID_PARAMETER);
    }

    /* Endpoint attributes, the timeout policy and the saved surplus of an IN
     * pipe are read under the lock (the descriptor is never used outside it). */
    struct shim_iface *iface = &g_ifaces[ref.iface_slot];
    struct pipe_stash *st = is_read ? &iface->in[pipe & 0x0F] : NULL;
    uint32_t done = 0;             /* bytes already delivered from the stash */
    int stash_ended_short = 0;
    lock();
    const struct libusb_endpoint_descriptor *ep = find_endpoint(iface->cfg, pipe);
    int type = ep ? (ep->bmAttributes & LIBUSB_TRANSFER_TYPE_MASK) : LIBUSB_TRANSFER_TYPE_BULK;
    uint32_t maxpacket = ep ? (uint32_t)(ep->wMaxPacketSize & 0x07FF) : 0;
    unsigned int timeout = timeout_locked(iface, pipe);
    int allow_partial = st ? st->allow_partial : 1;
    int auto_flush = st ? st->auto_flush : 0;
    if (st != NULL && st->len > 0) {
        done = st->len < len ? st->len : len;
        memcpy(buf, st->data, done);
        st->len -= done;
        if (st->len > 0) {
            memmove(st->data, st->data + done, st->len);
        } else {
            stash_ended_short = st->ends_short;
            st->ends_short = 0;
        }
        TRACE("%s: %u saved byte(s) returned first on pipe 0x%02x", who, done, pipe);
    }
    unlock();

    if (type != LIBUSB_TRANSFER_TYPE_BULK && type != LIBUSB_TRANSFER_TYPE_INTERRUPT) {
        release_op(&ref);
        shim_log("%s: pipe 0x%02x is not a bulk or interrupt endpoint (type %d)", who, pipe, type);
        return win_fail(ERROR_NOT_SUPPORTED);
    }
    if (done > 0 && (done == len || stash_ended_short)) {
        /* Satisfied from the saved bytes, or they ended a short packet. */
        release_op(&ref);
        if (transferred != NULL)
            *transferred = done;
        TRACE("%s -> %u of %u bytes on pipe 0x%02x (saved data)", who, done, len, pipe);
        return win_ok();
    }

    uint8_t *target = buf + done;
    uint32_t want = len - done;
    uint32_t req = want;
    uint8_t *tmp = NULL;
    if (is_read && allow_partial && maxpacket > 0 && want % maxpacket != 0) {
        req = want + (maxpacket - want % maxpacket);   /* whole packets, like WinUSB */
        tmp = malloc(req);
        if (tmp == NULL) {
            release_op(&ref);
            return win_fail(ERROR_NOT_ENOUGH_MEMORY);
        }
        target = tmp;
    }
    struct libusb_transfer *t = libusb_alloc_transfer(0);
    if (t == NULL) {
        free(tmp);
        release_op(&ref);
        return win_fail(ERROR_NOT_ENOUGH_MEMORY);
    }
    if (type == LIBUSB_TRANSFER_TYPE_BULK)
        libusb_fill_bulk_transfer(t, ref.lh, pipe, target, (int)req, NULL, NULL, timeout);
    else
        libusb_fill_interrupt_transfer(t, ref.lh, pipe, target, (int)req, NULL, NULL, timeout);

    int rc = submit_and_wait(&ref, t);
    enum libusb_transfer_status status = t->status;
    uint32_t actual = rc == 0 && t->actual_length > 0 ? (uint32_t)t->actual_length : 0;
    libusb_free_transfer(t);

    if (tmp != NULL) {
        uint32_t give = actual < want ? actual : want;
        memcpy(buf + done, tmp, give);
        if (rc == 0 && status == LIBUSB_TRANSFER_COMPLETED && actual > want) {
            if (auto_flush) {
                TRACE("%s: AUTO_FLUSH: %u surplus byte(s) discarded on pipe 0x%02x", who, actual - want, pipe);
            } else {
                lock();
                stash_append(st, tmp + want, actual - want, actual < req);
                unlock();
                TRACE("%s: %u surplus byte(s) saved for the next read of pipe 0x%02x", who, actual - want, pipe);
            }
        }
        free(tmp);
        actual = give;
    }
    release_op(&ref);

    if (rc != 0) {
        shim_log("%s: pipe 0x%02x: libusb_submit_transfer: %s", who, pipe, libusb_strerror(rc));
        if (transferred != NULL)
            *transferred = done;
        return win_fail(map_libusb_error(rc));
    }
    if (transferred != NULL)
        *transferred = done + actual;
    if (status != LIBUSB_TRANSFER_COMPLETED) {
        shim_log("%s: pipe 0x%02x, %u bytes, timeout %u ms: %s (%u transferred)", who, pipe, len, timeout,
                 status_name(status), done + actual);
        return win_fail(map_transfer_status(status));
    }
    TRACE("%s -> %u of %u bytes on pipe 0x%02x", who, done + actual, len, pipe);
    return win_ok();
}

int32_t WinUsb_ReadPipe(intptr_t h, uint8_t pipe, uint8_t *buf, uint32_t len,
                        uint32_t *transferred, void *overlapped)
{
    TRACE("WinUsb_ReadPipe(0x%lx, pipe=0x%02x, len=%u, overlapped=%p)", (long)h, pipe, len, overlapped);
    return pipe_transfer(h, pipe, buf, len, transferred, 1, "WinUsb_ReadPipe");
}

int32_t WinUsb_WritePipe(intptr_t h, uint8_t pipe, const uint8_t *buf, uint32_t len,
                         uint32_t *transferred, void *overlapped)
{
    TRACE("WinUsb_WritePipe(0x%lx, pipe=0x%02x, len=%u, overlapped=%p)", (long)h, pipe, len, overlapped);
    /* libusb takes a non-const buffer; OUT transfers never write to it. */
    return pipe_transfer(h, pipe, (uint8_t *)(uintptr_t)buf, len, transferred, 0, "WinUsb_WritePipe");
}

int32_t WinUsb_ControlTransfer(intptr_t h, WINUSB_SETUP_PACKET setup, uint8_t *buf,
                               uint32_t len, uint32_t *transferred, void *overlapped)
{
    TRACE("WinUsb_ControlTransfer(0x%lx, bmRequestType=0x%02x, bRequest=0x%02x, wValue=0x%04x, wIndex=0x%04x, wLength=%u, len=%u, overlapped=%p)",
          (long)h, setup.RequestType, setup.Request, setup.Value, setup.Index, setup.Length, len, overlapped);
    if (transferred != NULL)
        *transferred = 0;
    uint16_t wlen = setup.Length;
    if (wlen > len) {
        shim_log("WinUsb_ControlTransfer: wLength %u exceeds buffer %u, clamping", wlen, len);
        wlen = (uint16_t)len;
    }
    if (wlen > 0 && buf == NULL)
        return win_fail(ERROR_INVALID_PARAMETER);
    return control_transfer(h, setup.RequestType, setup.Request, setup.Value, setup.Index,
                            buf, wlen, transferred, "WinUsb_ControlTransfer");
}

int32_t WinUsb_ResetPipe(intptr_t h, uint8_t pipe)
{
    TRACE("WinUsb_ResetPipe(0x%lx, pipe=0x%02x)", (long)h, pipe);
    struct op_ref ref;
    if (!acquire_iface(h, &ref))
        return 0;
    int rc = libusb_clear_halt(ref.lh, pipe);
    release_op(&ref);
    if (rc != 0) {
        shim_log("WinUsb_ResetPipe: libusb_clear_halt(0x%02x): %s", pipe, libusb_strerror(rc));
        return win_fail(map_libusb_error(rc));
    }
    return win_ok();
}
