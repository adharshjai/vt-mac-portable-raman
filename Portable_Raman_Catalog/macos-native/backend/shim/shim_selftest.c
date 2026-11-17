/*
 * shim_selftest.c -- exercises libwinusb_shim.dylib without hardware.
 *
 *   ./shim_selftest [-v]
 *
 * What it checks:
 *   1. the struct layouts the managed side relies on (mirrors the _Static_asserts);
 *   2. every ImplMap entry point of wrrusb2a.dll resolves through dlsym(3),
 *      including the shell32 ordinal names "#2" and "#4";
 *   3. the kernel32 / user32 / shell32 stubs behave as specified;
 *   4. the SetupDi* enumeration of Hamamatsu devices (idVendor 0x0661 or
 *      WRRUSB_SHIM_VID): prints every device found, 0 is expected without
 *      hardware; the device-path round trip is additionally exercised against
 *      the Apple devices (idVendor 0x05ac) present on any Mac;
 *   5. the error paths: bad handles, unknown paths, buffer-too-small;
 *
 * With WRRUSB_SHIM_SELFTEST_OPEN=1 the first enumerated device of the target
 * vendor is also opened, initialised (interface 0 claimed) and queried, which
 * is the real-hardware path; it also blocks a read on the first IN pipe and
 * frees the interface handle from the main thread, which must abort the read
 * with ERROR_OPERATION_ABORTED like WinUSB does. -v sets WRRUSB_SHIM_DEBUG=1.
 *
 * Exit code 0 when every check passed, 1 otherwise.
 */
#include "winusb_shim.h"

#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int g_failures;
static int g_checks;

static void check(int ok, const char *what)
{
    g_checks++;
    if (!ok)
        g_failures++;
    printf("  [%s] %s\n", ok ? " ok " : "FAIL", what);
}

static void check_eq_errno(int32_t rc, int expected_rc, int expected_errno, const char *what)
{
    int e = errno;
    char line[256];
    snprintf(line, sizeof(line), "%s -> %d, errno %d (expected %d, errno %d)", what, rc, e, expected_rc, expected_errno);
    check((rc != 0) == (expected_rc != 0) && e == expected_errno, line);
}

/* ---- 1. layouts -------------------------------------------------------- */
static void test_layouts(void)
{
    puts("Struct layouts:");
    check(sizeof(GUID) == 16, "sizeof(GUID) == 16");
    check(sizeof(SP_DEVICE_INTERFACE_DATA) == 32 && offsetof(SP_DEVICE_INTERFACE_DATA, Reserved) == 24,
          "sizeof(SP_DEVICE_INTERFACE_DATA) == 32, Reserved @24");
    check(sizeof(USB_DEVICE_DESCRIPTOR) == 18 && offsetof(USB_DEVICE_DESCRIPTOR, idProduct) == 10,
          "sizeof(USB_DEVICE_DESCRIPTOR) == 18, idProduct @10");
    check(sizeof(USB_INTERFACE_DESCRIPTOR) == 9, "sizeof(USB_INTERFACE_DESCRIPTOR) == 9");
    check(sizeof(WINUSB_PIPE_INFORMATION) == 12 && offsetof(WINUSB_PIPE_INFORMATION, MaximumPacketSize) == 6,
          "sizeof(WINUSB_PIPE_INFORMATION) == 12, MaximumPacketSize @6");
    check(sizeof(WINUSB_SETUP_PACKET) == 8 && offsetof(WINUSB_SETUP_PACKET, Length) == 6,
          "sizeof(WINUSB_SETUP_PACKET) == 8, Length @6");
    check(sizeof(SHChangeNotifyEntry) == 16, "sizeof(SHChangeNotifyEntry) == 16");
}

/* ---- 2. exports --------------------------------------------------------- */
static const char *const k_entry_points[] = {
    /* wrrusb2a_refs.txt, "P/Invoke (ImplMap)", in file order */
    "#2", "#4",
    "RegisterDeviceNotification",
    "SetupDiCreateDeviceInfoList", "SetupDiDestroyDeviceInfoList", "SetupDiEnumDeviceInterfaces",
    "SetupDiGetClassDevs", "SetupDiGetDeviceInterfaceDetail",
    "UnregisterDeviceNotification",
    "GetPrivateProfileString",
    "WinUsb_ControlTransfer", "WinUsb_Free", "WinUsb_Initialize", "WinUsb_QueryDeviceInformation",
    "WinUsb_QueryInterfaceSettings", "WinUsb_QueryPipe", "WinUsb_ReadPipe", "WinUsb_ResetPipe",
    "WinUsb_GetDescriptor", "WinUsb_SetPipePolicy", "WinUsb_WritePipe",
    "CreateFile", "CloseHandle", "QueryPerformanceFrequency", "QueryPerformanceCounter",
    /* the shell32 functions under their managed names as well */
    "SHChangeNotifyRegister", "SHChangeNotifyUnregister",
};

static void test_exports(void)
{
    puts("Entry points resolvable with dlsym (as the .NET runtime resolves DllImport names):");
    size_t n = sizeof(k_entry_points) / sizeof(k_entry_points[0]);
    for (size_t i = 0; i < n; i++) {
        void *p = dlsym(RTLD_DEFAULT, k_entry_points[i]);
        char line[128];
        snprintf(line, sizeof(line), "dlsym(\"%s\")%s", k_entry_points[i], p ? "" : " = NULL");
        check(p != NULL, line);
    }

    /* The ordinal names must be the shell32 functions. */
    typedef uint32_t (*reg_fn)(intptr_t, int32_t, int32_t, uint32_t, int32_t, SHChangeNotifyEntry);
    typedef int32_t (*unreg_fn)(uint8_t);
    reg_fn ord2 = (reg_fn)dlsym(RTLD_DEFAULT, "#2");
    unreg_fn ord4 = (unreg_fn)dlsym(RTLD_DEFAULT, "#4");
    SHChangeNotifyEntry entry = { 0, 1 };
    check(ord2 != NULL && ord2(0, 1, 32 | 64, 1025, 1, entry) == 1, "\"#2\"(...) == 1 (SHChangeNotifyRegister)");
    check(ord4 != NULL && ord4(1) != 0, "\"#4\"(1) == TRUE (SHChangeNotifyUnregister)");
}

/* ---- 3. stubs ----------------------------------------------------------- */
static void test_stubs(void)
{
    puts("kernel32 / user32 / shell32 stubs:");
    int64_t freq = 0, c1 = 0, c2 = 0;
    check(QueryPerformanceFrequency(&freq) && freq == 1000000000LL, "QueryPerformanceFrequency == 1e9");
    check(QueryPerformanceCounter(&c1) && QueryPerformanceCounter(&c2) && c2 >= c1 && c1 > 0,
          "QueryPerformanceCounter is monotonic");

    char out[16];
    memset(out, 'x', sizeof(out));
    int32_t n = GetPrivateProfileString("Section", "Key", "default-value", out, sizeof(out), "\\HUSBDCAM.ini");
    check(n == 13 && strcmp(out, "default-value") == 0, "GetPrivateProfileString copies the default (13 chars)");
    n = GetPrivateProfileString("S", "K", "0123456789", out, 4, "f.ini");
    check(n == 3 && strcmp(out, "012") == 0, "GetPrivateProfileString truncates to size-1 with NUL");
    n = GetPrivateProfileString("S", "K", NULL, out, sizeof(out), "f.ini");
    check(n == 0 && out[0] == '\0', "GetPrivateProfileString with NULL default -> empty string");
    check(GetPrivateProfileString("S", "K", "d", NULL, 8, "f.ini") == 0, "GetPrivateProfileString with NULL buffer -> 0");

    intptr_t nh = RegisterDeviceNotification(0, NULL, 5);
    check(nh != 0 && nh != (intptr_t)-1, "RegisterDeviceNotification returns a non-zero handle");
    check(UnregisterDeviceNotification(nh) != 0, "UnregisterDeviceNotification -> TRUE");

    SHChangeNotifyEntry entry = { 0, 1 };
    check(SHChangeNotifyRegister(0, 0, 32 | 64, 1025, 1, entry) == 1, "SHChangeNotifyRegister -> 1");
    check(SHChangeNotifyUnregister(1) != 0, "SHChangeNotifyUnregister -> TRUE");
}

/* ---- 4. enumeration ----------------------------------------------------- */
static const GUID HPK_GUID = { 0x88dc0d16u, 0xf5f0u, 0x4836u, { 0xb6, 0xd0, 0x89, 0xfd, 0x7c, 0x34, 0x95, 0xf3 } };

/* Enumerates with the current WRRUSB_SHIM_VID; stores up to `max` paths. Returns the count. */
static int enumerate(const char *label, char paths[][128], int max)
{
    printf("Enumerating %s:\n", label);
    intptr_t set = SetupDiGetClassDevs(&HPK_GUID, NULL, NULL, 0x12 /* DIGCF_PRESENT | DIGCF_DEVICEINTERFACE */);
    check(set != 0 && set != (intptr_t)-1, "SetupDiGetClassDevs returns a handle (never -1)");

    int count = 0;
    for (int index = 0;; index++) {
        SP_DEVICE_INTERFACE_DATA d;
        memset(&d, 0, sizeof(d));
        d.cbSize = (int32_t)sizeof(d);
        errno = 0;
        if (!SetupDiEnumDeviceInterfaces(set, NULL, &HPK_GUID, index, &d)) {
            check_eq_errno(0, 0, ERROR_NO_MORE_ITEMS, "SetupDiEnumDeviceInterfaces past the end");
            break;
        }
        check(d.cbSize == 32 && d.Flags == 1 && d.Reserved == index, "SP_DEVICE_INTERFACE_DATA {cbSize 32, Flags 1, Reserved = index}");

        int32_t required = 0;
        errno = 0;
        int32_t rc = SetupDiGetDeviceInterfaceDetail(set, &d, NULL, 0, &required, NULL);
        check_eq_errno(rc, 0, ERROR_INSUFFICIENT_BUFFER, "SetupDiGetDeviceInterfaceDetail(NULL) sizing call");
        check(required > 5, "required size = 4 + strlen(path) + 1");

        char *detail = calloc((size_t)required + 1, 1);
        memcpy(detail, "\x08\x00\x00\x00", 4);          /* cbSize as the managed side sets it */
        errno = 0;
        rc = SetupDiGetDeviceInterfaceDetail(set, &d, detail, required - 1, &required, NULL);
        check_eq_errno(rc, 0, ERROR_INSUFFICIENT_BUFFER, "SetupDiGetDeviceInterfaceDetail with a buffer one byte short");
        errno = 0;
        rc = SetupDiGetDeviceInterfaceDetail(set, &d, detail, required, &required, NULL);
        check_eq_errno(rc, 1, 0, "SetupDiGetDeviceInterfaceDetail with the right size");
        check(detail[0] == 8 && detail[1] == 0, "detail bytes 0..3 (cbSize) untouched");
        const char *path = detail + 4;
        check((int)strlen(path) + 5 == required, "path is NUL-terminated at offset 4 and matches the required size");
        check(strstr(path, "\\\\?\\usb#vid_") == path && strstr(path, WRRUSB_SHIM_HPK_GUID_STRING) != NULL,
              "path has the Windows device-interface shape");
        printf("    device %d: %s\n", index, path);
        if (count < max) {
            strncpy(paths[count], path, 127);
            paths[count][127] = '\0';
        }
        count++;
        free(detail);
    }
    check(SetupDiDestroyDeviceInfoList(set) != 0, "SetupDiDestroyDeviceInfoList -> TRUE");
    errno = 0;
    check_eq_errno(SetupDiDestroyDeviceInfoList(set), 0, ERROR_INVALID_HANDLE, "SetupDiDestroyDeviceInfoList twice");
    printf("  %d device(s) found for %s\n", count, label);
    return count;
}

/* A WinUsb_ReadPipe issued from a second thread, the way the DLL's CaptureThread does it. */
struct read_probe {
    intptr_t iface;
    uint8_t pipe;
    int32_t rc;
    int err;
    uint32_t got;
    int64_t ns;
};

static void *blocked_read(void *arg)
{
    struct read_probe *p = arg;
    uint8_t buf[64];
    int64_t t0 = 0, t1 = 0;
    QueryPerformanceCounter(&t0);
    errno = 0;
    p->rc = WinUsb_ReadPipe(p->iface, p->pipe, buf, sizeof(buf), &p->got, NULL);
    p->err = errno;
    QueryPerformanceCounter(&t1);
    p->ns = t1 - t0;
    return NULL;
}

static void test_open_sequence(const char *path)
{
    printf("Opening %s:\n", path);
    errno = 0;
    intptr_t dev = CreateFile(path, 0x80000000u | 0x40000000u, 3, NULL, 3, 0x40000000, 0);
    if (dev == (intptr_t)-1) {
        printf("  CreateFile failed, errno %d (device busy or inaccessible); skipping the open sequence\n", errno);
        return;
    }
    check(dev >= 0x10000, "CreateFile handle >= 0x10000");

    intptr_t iface = 0;
    errno = 0;
    int32_t rc = WinUsb_Initialize(dev, &iface);
    if (!rc) {
        printf("  WinUsb_Initialize failed, errno %d (interface 0 not claimable); closing\n", errno);
        check(CloseHandle(dev) != 0, "CloseHandle -> TRUE");
        return;
    }
    check(iface >= 0x10000 && iface != dev, "WinUsb_Initialize returns a separate interface handle");

    USB_DEVICE_DESCRIPTOR dd;
    memset(&dd, 0, sizeof(dd));
    uint32_t got = 0;
    rc = WinUsb_GetDescriptor(iface, (uint8_t)USB_DEVICE_DESCRIPTOR_TYPE, 0, 0, &dd, sizeof(dd), &got);
    check(rc && got == 18 && dd.bLength == 18 && dd.bDescriptorType == 1, "WinUsb_GetDescriptor(device) -> 18 bytes");
    printf("    device descriptor: vid %04x pid %04x bcdDevice %04x bcdUSB %04x, %u configuration(s)\n",
           dd.idVendor, dd.idProduct, dd.bcdDevice, dd.bcdUSB, dd.bNumConfigurations);

    USB_INTERFACE_DESCRIPTOR id;
    memset(&id, 0, sizeof(id));
    rc = WinUsb_QueryInterfaceSettings(iface, 0, &id);
    check(rc && id.bLength == 9 && id.bDescriptorType == 4, "WinUsb_QueryInterfaceSettings(alt 0)");
    printf("    interface %u alt %u: class %02x, %u endpoint(s)\n", id.bInterfaceNumber, id.bAlternateSetting,
           id.bInterfaceClass, id.bNumEndpoints);
    uint8_t in_pipe = 0;
    for (uint8_t p = 0; p < id.bNumEndpoints; p++) {
        WINUSB_PIPE_INFORMATION pi;
        memset(&pi, 0, sizeof(pi));
        rc = WinUsb_QueryPipe(iface, 0, p, &pi);
        check(rc != 0, "WinUsb_QueryPipe");
        printf("      pipe %u: type %d id 0x%02x maxpacket %u interval %u\n", p, pi.PipeType, pi.PipeId,
               pi.MaximumPacketSize, pi.Interval);
        uint32_t ms = 2000;
        check(WinUsb_SetPipePolicy(iface, pi.PipeId, PIPE_TRANSFER_TIMEOUT, 4, &ms) != 0, "WinUsb_SetPipePolicy(PIPE_TRANSFER_TIMEOUT)");
        if (rc && in_pipe == 0 && (pi.PipeId & 0x80) &&
            (pi.PipeType == UsbdPipeTypeBulk || pi.PipeType == UsbdPipeTypeInterrupt))
            in_pipe = pi.PipeId;
    }
    errno = 0;
    WINUSB_PIPE_INFORMATION pi;
    check_eq_errno(WinUsb_QueryPipe(iface, 0, id.bNumEndpoints, &pi), 0, ERROR_NO_MORE_ITEMS, "WinUsb_QueryPipe past the end");

    uint8_t speed = 0;
    uint32_t len = 1;
    rc = WinUsb_QueryDeviceInformation(iface, WINUSB_DEVICE_SPEED, &len, &speed);
    check(rc && len == 1 && speed >= 1 && speed <= 3, "WinUsb_QueryDeviceInformation(DEVICE_SPEED)");
    printf("    speed: %u (1 low, 2 full, 3 high)\n", speed);

    if (in_pipe != 0) {
        /* WinUSB aborts the I/O pending on an interface handle that is freed, and the DLL
         * relies on it (CaptureThread blocks in WinUsb_ReadPipe while USB2_captureStop /
         * USB2_close run on the API thread). Block a read with an infinite timeout, then
         * free the handle from this thread: the read must return promptly with 995. */
        uint32_t forever = 0;
        check(WinUsb_SetPipePolicy(iface, in_pipe, PIPE_TRANSFER_TIMEOUT, 4, &forever) != 0,
              "PIPE_TRANSFER_TIMEOUT = 0 (infinite) on the first IN pipe");
        struct read_probe probe = { iface, in_pipe, -1, 0, 0, 0 };
        pthread_t th;
        check(pthread_create(&th, NULL, blocked_read, &probe) == 0, "reader thread started");
        usleep(300 * 1000);
        int64_t f0 = 0, f1 = 0;
        QueryPerformanceCounter(&f0);
        check(WinUsb_Free(iface) != 0, "WinUsb_Free while a read is pending -> TRUE");
        QueryPerformanceCounter(&f1);
        pthread_join(th, NULL);
        char line[160];
        if (probe.rc) {
            printf("    (the device answered the pending read with %u byte(s) before WinUsb_Free)\n", probe.got);
        } else {
            snprintf(line, sizeof(line), "pending WinUsb_ReadPipe(pipe 0x%02x) -> FALSE, errno %d after %.1f ms (expected 995 ERROR_OPERATION_ABORTED)",
                     in_pipe, probe.err, (double)probe.ns / 1e6);
            check(probe.err == ERROR_OPERATION_ABORTED, line);
        }
        snprintf(line, sizeof(line), "WinUsb_Free returned in %.1f ms", (double)(f1 - f0) / 1e6);
        check(f1 - f0 < 2000000000LL, line);
    } else {
        check(WinUsb_Free(iface) != 0, "WinUsb_Free -> TRUE");
    }
    errno = 0;
    check_eq_errno(WinUsb_Free(iface), 0, ERROR_INVALID_HANDLE, "WinUsb_Free twice");
    check(CloseHandle(dev) != 0, "CloseHandle -> TRUE");
    errno = 0;
    check_eq_errno(CloseHandle(dev), 0, ERROR_INVALID_HANDLE, "CloseHandle twice");
}

/* ---- 5. error paths ----------------------------------------------------- */
static void test_errors(void)
{
    puts("Error paths:");
    errno = 0;
    intptr_t h = CreateFile("\\\\?\\usb#vid_0661&pid_ffff#255-255#" WRRUSB_SHIM_HPK_GUID_STRING,
                            0x80000000u, 0, NULL, 3, 0, 0);
    check_eq_errno(h == (intptr_t)-1, 1, ERROR_FILE_NOT_FOUND, "CreateFile(unknown device) == -1");
    errno = 0;
    h = CreateFile("C:\\not\\a\\usb\\path", 0x80000000u, 0, NULL, 3, 0, 0);
    check_eq_errno(h == (intptr_t)-1, 1, ERROR_FILE_NOT_FOUND, "CreateFile(non-USB path) == -1");
    errno = 0;
    h = CreateFile(NULL, 0x80000000u, 0, NULL, 3, 0, 0);
    check_eq_errno(h == (intptr_t)-1, 1, ERROR_FILE_NOT_FOUND, "CreateFile(NULL) == -1");

    errno = 0;
    check_eq_errno(CloseHandle(12345), 0, ERROR_INVALID_HANDLE, "CloseHandle(bogus)");
    intptr_t iface = 0;
    errno = 0;
    check_eq_errno(WinUsb_Initialize(12345, &iface), 0, ERROR_INVALID_HANDLE, "WinUsb_Initialize(bogus)");
    errno = 0;
    check_eq_errno(WinUsb_Free(12345), 0, ERROR_INVALID_HANDLE, "WinUsb_Free(bogus)");

    uint8_t buf[8];
    uint32_t got = 7;
    errno = 0;
    check_eq_errno(WinUsb_ReadPipe(12345, 0x81, buf, sizeof(buf), &got, NULL), 0, ERROR_INVALID_HANDLE, "WinUsb_ReadPipe(bogus)");
    check(got == 0, "WinUsb_ReadPipe failure reports 0 bytes transferred");
    errno = 0;
    check_eq_errno(WinUsb_WritePipe(12345, 0x01, buf, sizeof(buf), &got, NULL), 0, ERROR_INVALID_HANDLE, "WinUsb_WritePipe(bogus)");
    WINUSB_SETUP_PACKET setup = { 0x80, 6, 0x0100, 0, 8 };
    errno = 0;
    check_eq_errno(WinUsb_ControlTransfer(12345, setup, buf, sizeof(buf), &got, NULL), 0, ERROR_INVALID_HANDLE, "WinUsb_ControlTransfer(bogus)");
    errno = 0;
    check_eq_errno(WinUsb_ResetPipe(12345, 0x81), 0, ERROR_INVALID_HANDLE, "WinUsb_ResetPipe(bogus)");
    WINUSB_PIPE_INFORMATION pi;
    errno = 0;
    check_eq_errno(WinUsb_QueryPipe(12345, 0, 0, &pi), 0, ERROR_INVALID_HANDLE, "WinUsb_QueryPipe(bogus)");
    USB_INTERFACE_DESCRIPTOR id;
    errno = 0;
    check_eq_errno(WinUsb_QueryInterfaceSettings(12345, 0, &id), 0, ERROR_INVALID_HANDLE, "WinUsb_QueryInterfaceSettings(bogus)");
    USB_DEVICE_DESCRIPTOR dd;
    errno = 0;
    check_eq_errno(WinUsb_GetDescriptor(12345, 1, 0, 0, &dd, sizeof(dd), &got), 0, ERROR_INVALID_HANDLE, "WinUsb_GetDescriptor(bogus)");
    uint32_t len = 1;
    uint8_t speed = 0;
    errno = 0;
    check_eq_errno(WinUsb_QueryDeviceInformation(12345, 1, &len, &speed), 0, ERROR_INVALID_HANDLE, "WinUsb_QueryDeviceInformation(bogus)");
    uint32_t ms = 100;
    errno = 0;
    check_eq_errno(WinUsb_SetPipePolicy(12345, 0x81, PIPE_TRANSFER_TIMEOUT, 4, &ms), 0, ERROR_INVALID_HANDLE, "WinUsb_SetPipePolicy(bogus)");

    SP_DEVICE_INTERFACE_DATA d;
    memset(&d, 0, sizeof(d));
    errno = 0;
    check_eq_errno(SetupDiEnumDeviceInterfaces(12345, NULL, &HPK_GUID, 0, &d), 0, ERROR_INVALID_HANDLE, "SetupDiEnumDeviceInterfaces(bogus set)");
    int32_t req = 0;
    errno = 0;
    check_eq_errno(SetupDiGetDeviceInterfaceDetail(12345, &d, NULL, 0, &req, NULL), 0, ERROR_INVALID_HANDLE, "SetupDiGetDeviceInterfaceDetail(bogus set)");

    int32_t list = SetupDiCreateDeviceInfoList(&HPK_GUID, 0);
    check(list != 0 && list != -1, "SetupDiCreateDeviceInfoList returns a handle");
    errno = 0;
    check_eq_errno(SetupDiEnumDeviceInterfaces((intptr_t)list, NULL, &HPK_GUID, 0, &d), 0, ERROR_NO_MORE_ITEMS, "empty device-info list enumerates nothing");
    check(SetupDiDestroyDeviceInfoList((intptr_t)list) != 0, "SetupDiDestroyDeviceInfoList(created list)");
}

int main(int argc, char **argv)
{
    if (argc > 1 && strcmp(argv[1], "-v") == 0)
        setenv(WRRUSB_SHIM_ENV_DEBUG, "1", 1);
    setvbuf(stdout, NULL, _IOLBF, 0);

    puts("libwinusb_shim self-test");
    test_layouts();
    test_exports();
    test_stubs();

    /* Target vendor (Hamamatsu 0x0661 unless WRRUSB_SHIM_VID is set). */
    char paths[8][128];
    const char *vid_env = getenv(WRRUSB_SHIM_ENV_VID);
    char label[64];
    snprintf(label, sizeof(label), "idVendor 0x%s (%s)", vid_env && *vid_env ? vid_env : "0661",
             vid_env && *vid_env ? WRRUSB_SHIM_ENV_VID : "Hamamatsu");
    int hamamatsu = enumerate(label, paths, 8);
    if (hamamatsu > 0 && getenv("WRRUSB_SHIM_SELFTEST_OPEN") != NULL)
        test_open_sequence(paths[0]);
    else if (hamamatsu > 0)
        puts("  (set WRRUSB_SHIM_SELFTEST_OPEN=1 to also open and initialise the first device)");

    /* Without hardware, exercise the enumeration + detail-buffer path with
     * synthetic entries (the shim's WRRUSB_SHIM_FAKE_DEVICES test hook). */
    if (hamamatsu == 0 && getenv(WRRUSB_SHIM_ENV_FAKE_DEVICES) == NULL && (vid_env == NULL || *vid_env == '\0')) {
        setenv(WRRUSB_SHIM_ENV_FAKE_DEVICES, "0661:2902@1-5,0661:2903@1-6,1234:0001@1-7", 1);
        char fake[8][128];
        int n = enumerate("synthetic Hamamatsu entries (WRRUSB_SHIM_FAKE_DEVICES, no hardware)", fake, 8);
        check(n == 2, "2 synthetic entries of idVendor 0x0661 listed, the foreign-vendor one filtered out");
        check(n >= 1 && strcmp(fake[0], "\\\\?\\usb#vid_0661&pid_2902#1-5#" WRRUSB_SHIM_HPK_GUID_STRING) == 0,
              "path format \\\\?\\usb#vid_0661&pid_2902#1-5#{guid}");
        if (n >= 1) {
            errno = 0;
            intptr_t h = CreateFile(fake[0], 0x80000000u | 0x40000000u, 3, NULL, 3, 0x40000000, 0);
            check_eq_errno(h == (intptr_t)-1, 1, ERROR_FILE_NOT_FOUND, "CreateFile(synthetic path) == -1: no such libusb device");
        }
        unsetenv(WRRUSB_SHIM_ENV_FAKE_DEVICES);
    }

    /* Device-path round trip against devices that exist on every Mac. */
    if (vid_env == NULL || *vid_env == '\0') {
        setenv(WRRUSB_SHIM_ENV_VID, "05ac", 1);
        char apple[8][128];
        int n = enumerate("idVendor 0x05ac (Apple, path round-trip check)", apple, 8);
        if (n > 0) {
            errno = 0;
            intptr_t h = CreateFile(apple[0], 0x80000000u | 0x40000000u, 3, NULL, 3, 0x40000000, 0);
            if (h == (intptr_t)-1) {
                printf("  CreateFile(%s) refused with errno %d; acceptable for a system device\n", apple[0], errno);
                check(errno == ERROR_ACCESS_DENIED || errno == ERROR_GEN_FAILURE, "CreateFile failure uses a Win32 error code");
            } else {
                check(h >= 0x10000, "CreateFile(Apple device) handle >= 0x10000");
                check(CloseHandle(h) != 0, "CloseHandle(Apple device) -> TRUE");
            }
            /* The same device, upper-cased path: CreateFile must parse it case-insensitively. */
            char upper[128];
            for (size_t i = 0; i <= strlen(apple[0]); i++)
                upper[i] = (apple[0][i] >= 'a' && apple[0][i] <= 'z') ? (char)(apple[0][i] - 32) : apple[0][i];
            errno = 0;
            h = CreateFile(upper, 0x80000000u, 3, NULL, 3, 0, 0);
            check(h != (intptr_t)-1 || errno == ERROR_ACCESS_DENIED || errno == ERROR_GEN_FAILURE,
                  "CreateFile accepts the path in upper case");
            if (h != (intptr_t)-1)
                CloseHandle(h);
        }
        unsetenv(WRRUSB_SHIM_ENV_VID);
    }

    test_errors();

    printf("\nHamamatsu devices enumerated: %d\n", hamamatsu);
    printf("%d check(s), %d failure(s)\n", g_checks, g_failures);
    puts(g_failures == 0 ? "SHIM SELFTEST OK" : "SHIM SELFTEST FAILED");
    return g_failures == 0 ? 0 : 1;
}
