// Runtime plumbing that lets Hamamatsu's unmodified wrrusb2a.dll run on macOS (PORT_SPEC.md section 4):
//  - Shift-JIS and the other Windows code pages the DLL asks Encoding.GetEncoding for;
//  - the System.Windows.Forms stand-in, resolved from the app folder;
//  - every Win32 library the DLL P/Invokes (tools/wrrusb2a_refs.txt, "== P/Invoke (ImplMap) ==":
//    winusb.dll, setupapi.dll, kernel32.dll, kernel32, user32.dll, shell32.dll) mapped onto the
//    single shim dylib built by ../shim.
using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.Loader;
using System.Text;

namespace SampleApl
{
    public static class Native
    {
        public const string ShimFileName = "libwinusb_shim.dylib";
        public const string ShimPathEnv = "RAMAN_SHIM_PATH";

        static readonly string[] MappedLibraries = { "winusb.dll", "setupapi.dll", "kernel32.dll", "kernel32", "user32.dll", "shell32.dll" };
        static readonly object sync = new object();
        static bool hooksInstalled;
        static bool resolverInstalled;
        static IntPtr shimHandle;

        public static string AppDir => AppContext.BaseDirectory;

        /// <summary>The library names the resolver maps onto the shim (copy; for the --selftest report).</summary>
        public static string[] MappedLibraryNames => (string[])MappedLibraries.Clone();

        /// <summary>Code pages + managed assembly resolution. Safe in simulation: nothing from the DLL is loaded.</summary>
        public static void InstallManagedHooks()
        {
            lock (sync)
            {
                if (hooksInstalled) return;
                hooksInstalled = true;
                // The DLL uses Shift-JIS; register before any call into it.
                Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
                // Fallback for "System.Windows.Forms, Version=2.0.0.0" (and anything else next to the app)
                // when the host's deps.json does not already answer; same pattern as tools/dumpmeta/Program.cs.
                AssemblyLoadContext.Default.Resolving += (context, name) =>
                {
                    string candidate = Path.Combine(AppDir, name.Name + ".dll");
                    return File.Exists(candidate) ? context.LoadFromAssemblyPath(candidate) : null;
                };
            }
        }

        /// <summary>Candidate locations for the shim, in probe order.</summary>
        public static string[] ShimCandidates()
        {
            var list = new System.Collections.Generic.List<string>();
            string env = Environment.GetEnvironmentVariable(ShimPathEnv);
            if (!string.IsNullOrEmpty(env)) list.Add(env);
            list.Add(Path.Combine(AppDir, ShimFileName));
            // Development tree: backend/SampleApl/bin/publish -> backend/shim/libwinusb_shim.dylib.
            string dir = AppDir.TrimEnd(Path.DirectorySeparatorChar);
            for (int level = 0; level < 4 && !string.IsNullOrEmpty(dir); level++)
            {
                dir = Path.GetDirectoryName(dir);
                if (string.IsNullOrEmpty(dir)) break;
                list.Add(Path.Combine(dir, "shim", ShimFileName));
            }
            return list.ToArray();
        }

        /// <summary>Full path of the shim dylib, or null when it has not been built/deployed.</summary>
        public static string FindShim()
        {
            foreach (string candidate in ShimCandidates())
                if (File.Exists(candidate)) return Path.GetFullPath(candidate);
            return null;
        }

        public static string ShimMissingMessage()
        {
            return ShimFileName + " not found; looked in: " + string.Join(", ", ShimCandidates()) +
                   ". Build it with `make -C macos-native/backend/shim` and copy it next to SampleApl.dll" +
                   " (or set " + ShimPathEnv + ").";
        }

        /// <summary>
        /// Maps every Win32 library name wrrusb2a.dll imports onto the shim. Must run before the first
        /// P/Invoke of the DLL (SetDllImportResolver can be installed once per assembly).
        /// </summary>
        public static void InstallDllImportResolver()
        {
            InstallManagedHooks();
            lock (sync)
            {
                if (resolverInstalled) return;
                string shim = FindShim();
                if (shim == null) throw new FileNotFoundException(ShimMissingMessage());
                // Load eagerly so a broken dylib fails here with a clear message, not inside the DLL.
                shimHandle = NativeLibrary.Load(shim);
                NativeLibrary.SetDllImportResolver(WrrUsbAssembly(), ResolveImport);
                // The --selftest probes (ShimProbes.cs) declare the DLL's imports again in this assembly;
                // route them through the identical mapping so they prove what the DLL's own calls get.
                NativeLibrary.SetDllImportResolver(typeof(Native).Assembly, ResolveImport);
                resolverInstalled = true;
                if (Environment.GetEnvironmentVariable("WRRUSB_SHIM_DEBUG") == "1")
                    Console.Error.WriteLine("[shim] " + shim);
            }
        }

        // Kept in its own method: the JIT loads the wrrusb2a assembly when compiling it, nowhere else.
        static Assembly WrrUsbAssembly() { return typeof(WRRUSB2_DLL.WRRUSB2).Assembly; }

        static IntPtr ResolveImport(string libraryName, Assembly assembly, DllImportSearchPath? searchPath)
        {
            foreach (string mapped in MappedLibraries)
                if (string.Equals(mapped, libraryName, StringComparison.OrdinalIgnoreCase))
                    return shimHandle;
            return IntPtr.Zero; // anything else: default probing
        }
    }
}
