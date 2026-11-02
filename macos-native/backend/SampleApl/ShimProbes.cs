// --selftest helpers (PORT_SPEC.md section 4): P/Invoke probes that reach every Win32 library
// name wrrusb2a.dll imports, without hardware.
//
// Without a device the DLL's own calls reach only setupapi.dll (USB2_initialize enumerates) and
// user32.dll RegisterDeviceNotification (USBRemovableCheck). Its kernel32.dll / kernel32 imports
// (CreateFile, CloseHandle, QueryPerformance*, GetPrivateProfileString) and everything in
// winusb.dll are first used by USB2_open, and the shell32.dll ordinal imports (#2 / #4) sit in a
// private USBRemovableCheck path that the System.Windows.Forms stand-in never triggers.
//
// The declarations below mirror the DLL's ImplMap entries (tools/wrrusb2a_refs.txt,
// "== P/Invoke (ImplMap) ==", and the PINVOKE lines of tools/wrrusb2a_api.txt): same module name,
// entry point, CharSet/SetLastError and marshaling. Native.InstallDllImportResolver routes this
// assembly through the same ResolveImport as wrrusb2a.dll, so a passing probe proves (a) the
// mapping-table entry for that library name, (b) the shim exports the symbol under the name .NET
// asks dlsym for (including the literal "#2"/"#4" ordinal names) and (c) the Unix marshaling of
// that call, return value and errno (Marshal.GetLastWin32Error) all the way through.
using System;
using System.Runtime.InteropServices;

namespace SampleApl
{
    static class ShimProbes
    {
        // kernel32.dll :: QueryPerformanceFrequency / QueryPerformanceCounter (wrrusb2a_api.txt:84-86)
        [DllImport("kernel32.dll", EntryPoint = "QueryPerformanceFrequency")]
        static extern bool QueryPerformanceFrequency(out long lpFrequency);
        [DllImport("kernel32.dll", EntryPoint = "QueryPerformanceCounter")]
        static extern bool QueryPerformanceCounter(out long lpPerformanceCount);

        // kernel32.dll :: GetPrivateProfileString (wrrusb2a_api.txt:936): char[] arguments, default (Ansi) CharSet.
        [DllImport("kernel32.dll", EntryPoint = "GetPrivateProfileString")]
        static extern int GetPrivateProfileString(char[] lpAppName, char[] lpKeyName, char[] lpDefault,
                                                  [In, Out] char[] lpReturnedString, ulong nSize, char[] lpFileName);

        // kernel32 (module name without ".dll") :: CloseHandle (wrrusb2a_api.txt:147). The DLL passes a
        // SafeFileHandle; the native ABI is the same intptr_t.
        [DllImport("kernel32", EntryPoint = "CloseHandle", CharSet = CharSet.Auto, SetLastError = true)]
        static extern bool CloseHandle(IntPtr hObject);

        // user32.dll :: UnregisterDeviceNotification (wrrusb2a_api.txt:372)
        [DllImport("user32.dll", EntryPoint = "UnregisterDeviceNotification", SetLastError = true)]
        static extern bool UnregisterDeviceNotification(IntPtr Handle);

        // shell32.dll :: #2 / #4 (wrrusb2a_api.txt:719-720): ordinal entry points, SHChangeNotifyEntry by value.
        // Layout as WRRUSB2_DLL.USBRemovableCheck.SHChangeNotifyEntry (Sequential: IntPtr pIdl; bool Recursively
        // marshaled as a 4-byte BOOL) = 16 bytes, which the shim static-asserts.
        [StructLayout(LayoutKind.Sequential)]
        struct SHChangeNotifyEntry
        {
            public IntPtr pIdl;
            [MarshalAs(UnmanagedType.Bool)] public bool Recursively;
        }
        [DllImport("shell32.dll", EntryPoint = "#2", CharSet = CharSet.Auto)]
        static extern uint SHChangeNotifyRegister(IntPtr hWnd, int fSources, int fEvents, uint wMsg, int cEntries, SHChangeNotifyEntry pFsne);
        [DllImport("shell32.dll", EntryPoint = "#4", CharSet = CharSet.Auto)]
        static extern bool SHChangeNotifyUnregister(byte hNotify);

        // winusb.dll :: WinUsb_Free (wrrusb2a_api.txt:817)
        [DllImport("winusb.dll", EntryPoint = "WinUsb_Free", SetLastError = true)]
        static extern bool WinUsb_Free(IntPtr InterfaceHandle);

        // Values the DLL uses (public members of WRRUSB2_DLL.USBRemovableCheck in wrrusb2a_api.txt:705-744).
        const int WRR_SHNOTIFY = 1025;
        const int SHCNF_IDLIST = 0;
        const int SHCNE_MEDIAINSERTED = 32;
        const int SHCNE_MEDIAREMOVED = 64;
        // Win32 error codes the shim reports through errno (shim/winusb_shim.h).
        const int ERROR_INVALID_HANDLE = 6;

        /// <summary>Runs every probe; throws on the first unexpected result (the self-test then fails).</summary>
        public static void Run()
        {
            long frequency;
            Expect(QueryPerformanceFrequency(out frequency), "kernel32.dll QueryPerformanceFrequency returned FALSE");
            Expect(frequency == 1000000000L, "kernel32.dll QueryPerformanceFrequency: expected 1000000000, got " + frequency);
            long counter;
            Expect(QueryPerformanceCounter(out counter), "kernel32.dll QueryPerformanceCounter returned FALSE");
            Expect(counter > 0, "kernel32.dll QueryPerformanceCounter: expected a positive count, got " + counter);
            Console.WriteLine("[selftest] probe kernel32.dll QueryPerformanceFrequency=" + frequency + " QueryPerformanceCounter=" + counter);

            // No HUSBDCAM.ini on macOS: the shim hands back the default and its length.
            char[] returned = new char[64];
            int length = GetPrivateProfileString("selftest\0".ToCharArray(), "key\0".ToCharArray(), "default-value\0".ToCharArray(),
                                                 returned, (ulong)returned.Length, "HUSBDCAM.ini\0".ToCharArray());
            string text = new string(returned, 0, Math.Max(0, Math.Min(length, returned.Length)));
            Expect(length == "default-value".Length && text == "default-value",
                   "kernel32.dll GetPrivateProfileString: expected 13/\"default-value\", got " + length + "/\"" + text + "\"");
            Console.WriteLine("[selftest] probe kernel32.dll GetPrivateProfileString -> " + length + " \"" + text + "\"");

            // An unknown handle: FALSE with errno ERROR_INVALID_HANDLE, read back the way the DLL does.
            bool closed = CloseHandle(IntPtr.Zero);
            int error = Marshal.GetLastWin32Error();
            Expect(!closed && error == ERROR_INVALID_HANDLE,
                   "kernel32 CloseHandle(0): expected FALSE/ERROR_INVALID_HANDLE(6), got " + closed + "/" + error);
            Console.WriteLine("[selftest] probe kernel32 CloseHandle(0) -> FALSE, GetLastWin32Error=" + error);

            Expect(UnregisterDeviceNotification(IntPtr.Zero), "user32.dll UnregisterDeviceNotification returned FALSE");
            Console.WriteLine("[selftest] probe user32.dll UnregisterDeviceNotification -> TRUE");

            SHChangeNotifyEntry entry = new SHChangeNotifyEntry { pIdl = IntPtr.Zero, Recursively = true };
            uint notify = SHChangeNotifyRegister(IntPtr.Zero, SHCNF_IDLIST, SHCNE_MEDIAINSERTED | SHCNE_MEDIAREMOVED,
                                                 WRR_SHNOTIFY, 1, entry);
            Expect(notify == 1, "shell32.dll #2 (SHChangeNotifyRegister): expected 1, got " + notify);
            Expect(SHChangeNotifyUnregister((byte)notify), "shell32.dll #4 (SHChangeNotifyUnregister) returned FALSE");
            Console.WriteLine("[selftest] probe shell32.dll #2 SHChangeNotifyRegister -> " + notify + ", #4 SHChangeNotifyUnregister -> TRUE");

            bool freed = WinUsb_Free(IntPtr.Zero);
            error = Marshal.GetLastWin32Error();
            Expect(!freed && error == ERROR_INVALID_HANDLE,
                   "winusb.dll WinUsb_Free(0): expected FALSE/ERROR_INVALID_HANDLE(6), got " + freed + "/" + error);
            Console.WriteLine("[selftest] probe winusb.dll WinUsb_Free(0) -> FALSE, GetLastWin32Error=" + error);
        }

        static void Expect(bool condition, string message)
        {
            if (!condition) throw new InvalidOperationException("resolver probe failed: " + message);
        }
    }
}
