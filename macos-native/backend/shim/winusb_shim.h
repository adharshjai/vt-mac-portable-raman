/*
 * winusb_shim.h -- C view of the Windows API surface that Hamamatsu's managed
 * library wrrusb2a.dll P/Invokes, as .NET 9 marshals it on macOS/arm64.
 *
 * Source of truth for the managed declarations:
 *   macos-native/tools/wrrusb2a_api.txt   (PINVOKE lines + struct layouts)
 *   macos-native/tools/wrrusb2a_refs.txt  ("P/Invoke (ImplMap)" section)
 * Contract: macos-native/PORT_SPEC.md section 5.
 *
 * Unix marshaling rules applied here:
 *   BOOL            -> int32_t (nonzero = TRUE)
 *   HANDLE / IntPtr -> intptr_t
 *   SafeFileHandle  -> intptr_t (the raw handle value)
 *   CharSet.Auto / CharSet.None strings and Char[] -> UTF-8 `char *`
 *   ref T           -> T *
 *   T[]             -> T *
 *   struct by value -> struct by value (WINUSB_SETUP_PACKET, SHChangeNotifyEntry)
 *   SetLastError=true -> the managed side reads errno after the call
 *                        (Marshal.GetLastWin32Error on Unix), so the shim
 *                        stores Win32 error codes in errno.
 */
#ifndef WINUSB_SHIM_H
#define WINUSB_SHIM_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define WINUSB_SHIM_EXPORT __attribute__((visibility("default")))

/* ---- Win32 error codes the shim writes to errno ------------------------ */
#define ERROR_SUCCESS              0
#define ERROR_FILE_NOT_FOUND       2
#define ERROR_TOO_MANY_OPEN_FILES  4
#define ERROR_ACCESS_DENIED        5
#define ERROR_INVALID_HANDLE       6
#define ERROR_NOT_ENOUGH_MEMORY    8
#define ERROR_GEN_FAILURE          31
#define ERROR_NOT_SUPPORTED        50
#define ERROR_INVALID_PARAMETER    87
#define ERROR_SEM_TIMEOUT          121
#define ERROR_INSUFFICIENT_BUFFER  122
#define ERROR_NO_MORE_ITEMS        259
#define ERROR_OPERATION_ABORTED    995   /* a pending transfer cancelled by WinUsb_Free / CloseHandle */

/* ---- Environment variables understood by the shim ---------------------- */
#define WRRUSB_SHIM_ENV_DEBUG            "WRRUSB_SHIM_DEBUG"             /* 1 = trace every call to stderr, 2 = also libusb debug */
#define WRRUSB_SHIM_ENV_VID              "WRRUSB_SHIM_VID"               /* hex idVendor to enumerate (default 0661 = Hamamatsu) */
#define WRRUSB_SHIM_ENV_DEFAULT_TIMEOUT  "WRRUSB_SHIM_DEFAULT_TIMEOUT_MS" /* pipe timeout when no policy was set (default 5000) */
#define WRRUSB_SHIM_ENV_FAKE_DEVICES     "WRRUSB_SHIM_FAKE_DEVICES"      /* test hook: "vid:pid@bus-addr,..." synthetic enumeration entries */

#define WRRUSB_SHIM_DEFAULT_VID          0x0661u
#define WRRUSB_SHIM_DEFAULT_TIMEOUT_MS   5000u

/* The device interface GUID the DLL enumerates with
 * (WRRUSB2_DLL.SetupAPI.D_HPK29_USB1DEVICE_GUID in wrrusb2a_api.txt). */
#define WRRUSB_SHIM_HPK_GUID_STRING      "{88dc0d16-f5f0-4836-b6d0-89fd7c3495f3}"
/* Device path produced by the enumeration and accepted by CreateFile:
 * \\?\usb#vid_0661&pid_XXXX#bus-addr#{88dc0d16-f5f0-4836-b6d0-89fd7c3495f3} */
#define WRRUSB_SHIM_PATH_FORMAT          "\\\\?\\usb#vid_%04x&pid_%04x#%u-%u#" WRRUSB_SHIM_HPK_GUID_STRING

/* Handles returned by the shim are never small numbers, so that a stray
 * close(2) issued by SafeFileHandle on dispose is harmless (EBADF). */
#define WRRUSB_SHIM_DEVICE_HANDLE_BASE   ((intptr_t)0x00100000)
#define WRRUSB_SHIM_IFACE_HANDLE_BASE    ((intptr_t)0x00200000)
#define WRRUSB_SHIM_SET_HANDLE_BASE      ((intptr_t)0x00300000)
#define WRRUSB_SHIM_NOTIFY_HANDLE_BASE   ((intptr_t)0x00400000)
/* Device-info sets: handles SET_HANDLE_BASE .. SET_HANDLE_BASE + CAPACITY - 1
 * (always below NOTIFY_HANDLE_BASE and within Int32, the managed return type of
 * SetupDiCreateDeviceInfoList). The table grows on demand; once CAPACITY sets
 * exist that were never destroyed, the oldest ones are recycled so enumeration
 * keeps working for a caller that leaks sets (the DLL leaks one per
 * USB2_initialize). SET_HANDLE_BASE + CAPACITY is the empty-set sentinel. */
#define WRRUSB_SHIM_SET_TABLE_CAPACITY   0xFFFFF

/* ---- Struct layouts (wrrusb2a_api.txt) --------------------------------- */

/* System.Guid: 16 bytes {u32, u16, u16, u8[8]}, alignment 4. */
typedef struct GUID {
    uint32_t Data1;
    uint16_t Data2;
    uint16_t Data3;
    uint8_t  Data4[8];
} GUID;

/* WRRUSB2_DLL.SetupAPI.SP_DEVICE_INTERFACE_DATA [Sequential, Pack=0] = 32 bytes */
typedef struct SP_DEVICE_INTERFACE_DATA {
    int32_t  cbSize;             /* @0  */
    GUID     InterfaceClassGuid; /* @4  */
    int32_t  Flags;              /* @20 */
    intptr_t Reserved;           /* @24 */
} SP_DEVICE_INTERFACE_DATA;

/* WRRUSB2_DLL.WinUSBDevice.USB_DEVICE_DESCRIPTOR [Sequential, Pack=0] = 18 bytes,
 * identical to the USB specification layout. */
typedef struct USB_DEVICE_DESCRIPTOR {
    uint8_t  bLength;            /* @0  */
    uint8_t  bDescriptorType;    /* @1  */
    uint16_t bcdUSB;             /* @2  */
    uint8_t  bDeviceClass;       /* @4  */
    uint8_t  bDeviceSubClass;    /* @5  */
    uint8_t  bDeviceProtocol;    /* @6  */
    uint8_t  bMaxPacketSize0;    /* @7  */
    uint16_t idVendor;           /* @8  */
    uint16_t idProduct;          /* @10 */
    uint16_t bcdDevice;          /* @12 */
    uint8_t  iManufacturer;      /* @14 */
    uint8_t  iProduct;           /* @15 */
    uint8_t  iSerialNumber;      /* @16 */
    uint8_t  bNumConfigurations; /* @17 */
} USB_DEVICE_DESCRIPTOR;

/* WRRUSB2_DLL.WinUSBDevice.USB_INTERFACE_DESCRIPTOR [Sequential, Pack=0] = 9 bytes */
typedef struct USB_INTERFACE_DESCRIPTOR {
    uint8_t bLength;
    uint8_t bDescriptorType;
    uint8_t bInterfaceNumber;
    uint8_t bAlternateSetting;
    uint8_t bNumEndpoints;
    uint8_t bInterfaceClass;
    uint8_t bInterfaceSubClass;
    uint8_t bInterfaceProtocol;
    uint8_t iInterface;
} USB_INTERFACE_DESCRIPTOR;

/* WRRUSB2_DLL.WinUSBDevice.USBD_PIPE_TYPE */
typedef enum USBD_PIPE_TYPE {
    UsbdPipeTypeControl     = 0,
    UsbdPipeTypeIsochronous = 1,
    UsbdPipeTypeBulk        = 2,
    UsbdPipeTypeInterrupt   = 3
} USBD_PIPE_TYPE;

/* WRRUSB2_DLL.WinUSBDevice.WINUSB_PIPE_INFORMATION [Sequential, Pack=0] = 12 bytes */
typedef struct WINUSB_PIPE_INFORMATION {
    int32_t  PipeType;           /* @0 (USBD_PIPE_TYPE) */
    uint8_t  PipeId;             /* @4 */
    uint16_t MaximumPacketSize;  /* @6 */
    uint8_t  Interval;           /* @8 */
} WINUSB_PIPE_INFORMATION;

/* WRRUSB2_DLL.WinUSBDevice.WINUSB_SETUP_PACKET [Sequential, Pack=1] = 8 bytes,
 * passed BY VALUE to WinUsb_ControlTransfer. */
#pragma pack(push, 1)
typedef struct WINUSB_SETUP_PACKET {
    uint8_t  RequestType;        /* @0 */
    uint8_t  Request;            /* @1 */
    uint16_t Value;              /* @2 */
    uint16_t Index;              /* @4 */
    uint16_t Length;             /* @6 */
} WINUSB_SETUP_PACKET;
#pragma pack(pop)

/* WRRUSB2_DLL.WinUSBDevice.POLICY_TYPE */
typedef enum POLICY_TYPE {
    SHORT_PACKET_TERMINATE = 1,
    AUTO_CLEAR_STALL       = 2,
    PIPE_TRANSFER_TIMEOUT  = 3,
    IGNORE_SHORT_PACKETS   = 4,
    ALLOW_PARTIAL_READS    = 5,
    AUTO_FLUSH             = 6,
    RAW_IO                 = 7
} POLICY_TYPE;

/* WRRUSB2_DLL.WinUSBDevice.USB_DEVICE_SPEED */
typedef enum USB_DEVICE_SPEED {
    UsbLowSpeed  = 1,
    UsbFullSpeed = 2,
    UsbHighSpeed = 3
} USB_DEVICE_SPEED;

/* WinUsb_QueryDeviceInformation InformationType (WRRUSB2_DLL.WinUSBDevice.DEVICE_SPEED) */
#define WINUSB_DEVICE_SPEED          1u
/* WinUsb_GetDescriptor DescriptorType (WRRUSB2_DLL.WinUSB.USB_DEVICE_DESCRIPTOR_TYPE) */
#define USB_DEVICE_DESCRIPTOR_TYPE   1u

/* WRRUSB2_DLL.USBRemovableCheck.SHChangeNotifyEntry [Sequential, Pack=0]
 * {IntPtr pIdl; bool Recursively [MarshalAs(Bool)]} -> 16 bytes, passed BY VALUE. */
typedef struct SHChangeNotifyEntry {
    intptr_t pIdl;               /* @0 */
    int32_t  Recursively;        /* @8, 4-byte BOOL */
} SHChangeNotifyEntry;

/* ---- Exported entry points (one per ImplMap row) ----------------------- */

/* kernel32 */
WINUSB_SHIM_EXPORT intptr_t CreateFile(const char *path_utf8, uint32_t access, int32_t share, void *sec,
                                       int32_t disposition, int32_t flags, int32_t template_);
WINUSB_SHIM_EXPORT int32_t  CloseHandle(intptr_t h);
WINUSB_SHIM_EXPORT int32_t  QueryPerformanceCounter(int64_t *out);
WINUSB_SHIM_EXPORT int32_t  QueryPerformanceFrequency(int64_t *out);
WINUSB_SHIM_EXPORT int32_t  GetPrivateProfileString(const char *app, const char *key, const char *def,
                                                    char *out, uint64_t size, const char *file);

/* user32 */
WINUSB_SHIM_EXPORT intptr_t RegisterDeviceNotification(intptr_t recipient, void *filter, int32_t flags);
WINUSB_SHIM_EXPORT int32_t  UnregisterDeviceNotification(intptr_t h);

/* shell32 (imported by ordinal: also exported as the literal symbols "#2" and "#4") */
WINUSB_SHIM_EXPORT uint32_t SHChangeNotifyRegister(intptr_t hwnd, int32_t sources, int32_t events,
                                                   uint32_t msg, int32_t count, SHChangeNotifyEntry entry);
WINUSB_SHIM_EXPORT int32_t  SHChangeNotifyUnregister(uint8_t h);

/* setupapi */
WINUSB_SHIM_EXPORT int32_t  SetupDiCreateDeviceInfoList(const GUID *g, int32_t hwnd);
WINUSB_SHIM_EXPORT intptr_t SetupDiGetClassDevs(const GUID *g, void *enumerator, void *hwnd, int32_t flags);
WINUSB_SHIM_EXPORT int32_t  SetupDiEnumDeviceInterfaces(intptr_t set, void *devinfo, const GUID *g,
                                                        int32_t index, SP_DEVICE_INTERFACE_DATA *out);
WINUSB_SHIM_EXPORT int32_t  SetupDiGetDeviceInterfaceDetail(intptr_t set, SP_DEVICE_INTERFACE_DATA *d,
                                                            void *detail, int32_t size, int32_t *required,
                                                            void *devinfo);
WINUSB_SHIM_EXPORT int32_t  SetupDiDestroyDeviceInfoList(intptr_t set);

/* winusb */
WINUSB_SHIM_EXPORT int32_t  WinUsb_Initialize(intptr_t fileHandle, intptr_t *ifaceHandle);
WINUSB_SHIM_EXPORT int32_t  WinUsb_Free(intptr_t h);
WINUSB_SHIM_EXPORT int32_t  WinUsb_GetDescriptor(intptr_t h, uint8_t type, uint8_t index, uint16_t lang,
                                                 USB_DEVICE_DESCRIPTOR *buf, uint32_t len, uint32_t *transferred);
WINUSB_SHIM_EXPORT int32_t  WinUsb_QueryInterfaceSettings(intptr_t h, uint8_t alt, USB_INTERFACE_DESCRIPTOR *out);
WINUSB_SHIM_EXPORT int32_t  WinUsb_QueryPipe(intptr_t h, uint8_t alt, uint8_t pipeIndex, WINUSB_PIPE_INFORMATION *out);
WINUSB_SHIM_EXPORT int32_t  WinUsb_QueryDeviceInformation(intptr_t h, uint32_t type, uint32_t *len, uint8_t *buf);
WINUSB_SHIM_EXPORT int32_t  WinUsb_SetPipePolicy(intptr_t h, uint8_t pipe, uint32_t policy, uint32_t valueLen,
                                                 const void *value);
WINUSB_SHIM_EXPORT int32_t  WinUsb_ReadPipe(intptr_t h, uint8_t pipe, uint8_t *buf, uint32_t len,
                                            uint32_t *transferred, void *overlapped);
WINUSB_SHIM_EXPORT int32_t  WinUsb_WritePipe(intptr_t h, uint8_t pipe, const uint8_t *buf, uint32_t len,
                                             uint32_t *transferred, void *overlapped);
WINUSB_SHIM_EXPORT int32_t  WinUsb_ControlTransfer(intptr_t h, WINUSB_SETUP_PACKET setup, uint8_t *buf,
                                                   uint32_t len, uint32_t *transferred, void *overlapped);
WINUSB_SHIM_EXPORT int32_t  WinUsb_ResetPipe(intptr_t h, uint8_t pipe);

#ifdef __cplusplus
}
#endif

#endif /* WINUSB_SHIM_H */
