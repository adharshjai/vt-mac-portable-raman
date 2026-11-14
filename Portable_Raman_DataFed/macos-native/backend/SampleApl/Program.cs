// Mac-native port of laser_test/hardware/SampleApl/SampleApl.cpp (PORT_SPEC.md section 4).
// Each ported block cites its source line; console strings are kept byte-identical because the
// controller (laser_test/main.cpp) parses several of them.
using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using WRRUSB2_DLL;

namespace SampleApl
{
    public static class Program
    {
        //---------------------------------------------------------------------
        // define (SampleApl.cpp:29-37)
        //---------------------------------------------------------------------
        const int IMAGE_HEADER_SIZE = 256;          // Header size
        const int DATA_COUNT_MAX_VALUE = 1;         // Data count
        const int DATA_TRANSMIT_COUNT = 1;          // Data transmit count
        const int FREQUENCYMODE_FRAME_COUNT = 1;    // Frame count(Frequency specified mode)
        const int OTHERMODE_FRAME_COUNT = 5;        // Frame count(Capture mode continuous and trigger)
        const int SINGLE_FRAME_COUNT = 1;
        const int EXPOSURE_TIME = 1000000;          // Exposure Time(usec)
        const int CYCLE_TIME = 1100000;             // Cycle time(usec)
        const int REPEAT_COUNT = 5;                 // Repeat count

        //---------------------------------------------------------------------
        // enum (SampleApl.cpp:66-98)
        //---------------------------------------------------------------------
        enum E_CAPTUREMODE { D_CAPTUREMODE_COUNT = 0x00000000, D_CAPTUREMODE_CONTINUOUS = 0x00000001, D_CAPTUREMODE_TRIGGER = 0x00000002 }
        enum E_TRIGGERMODE
        {
            D_TRIGGERMODE_INTERNAL = 0x00000000, D_TRIGGERMODE_SOFTASYNC = 0x00000001, D_TRIGGERMODE_SOFTSYNC = 0x00000002,
            D_TRIGGERMODE_EXTASYNCEDGE = 0x00000003, D_TRIGGERMODE_EXTASYNCLEVEL = 0x00000004, D_TRIGGERMODE_EXTSYNCEDGE = 0x00000005,
            D_TRIGGERMODE_EXTSYNCLEVEL = 0x00000006, D_TRIGGERMODE_EXTSYNCPULSE = 0x00000007
        }
        enum E_CALIBRATIONCOEFFICIENT { D_CALIBRATIONCOEFFICIENT_WAVELENGTH = 0x00000000, D_CALIBRATIONCOEFFICIENT_SENSIBILITY = 0x00000001 }
        enum E_GAINMODE { D_GAINMODE_SENSOR = 0x00000000, D_GAINMODE_CIRCUIT = 0x00000001 }
        enum E_SENSORGAINMODE { D_SENSORGAINMODE_LOWGAIN = 0x00000000, D_SENSORGAINMODE_HIGHGAIN = 0x00000001, D_SENSORGAINMODE_NOTHING = 0x000000FF }

        // Compile-time constants: no type from wrrusb2a.dll is loaded for these.
        const int usb2Success = (int)Usb2Struct.Cusb2Err.usb2Success;
        const int usb2Err_unsuccess = (int)Usb2Struct.Cusb2Err.usb2Err_unsuccess;

        //---------------------------------------------------------------------
        // global (SampleApl.cpp:60-61)
        //---------------------------------------------------------------------
        static int g_nDeviceId;             // Device ID
        static ushort[] gCapturedData;      // Image data
        static bool simulate;               // --simulate / RAMAN_BACKEND_SIMULATE=1

        //---------------------------------------------------------------------
        // entry point
        //---------------------------------------------------------------------
        public static int Main(string[] args)
        {
            Watchdog.RememberController();   // first: which process started us (safety stop, Watchdog.cs)
            // The controller shows stdout live and parses it line by line: flush after every write.
            // SafeConsoleWriter (Watchdog.cs): a broken pipe after the controller is gone must not throw.
            Console.SetOut(new SafeConsoleWriter(new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true }));
            // Deterministic number formatting ("{0:E}", doubles) regardless of the Mac's locale.
            CultureInfo.DefaultThreadCurrentCulture = CultureInfo.InvariantCulture;
            CultureInfo.CurrentCulture = CultureInfo.InvariantCulture;
            Native.InstallManagedHooks();

            bool selftest = false;
            foreach (string arg in args)
            {
                if (arg == "--simulate") simulate = true;
                else if (arg == "--selftest") selftest = true;
                else if (arg == "--help" || arg == "-h")
                {
                    Console.WriteLine("SampleApl [--simulate] [--selftest]   (env RAMAN_BACKEND_SIMULATE=1 selects the simulator)");
                    return 0;
                }
            }
            if (Environment.GetEnvironmentVariable("RAMAN_BACKEND_SIMULATE") == "1") simulate = true;

            int code = selftest ? SelfTest.Run() : RunMain();
            Console.Out.Flush();
            // Exit explicitly: terminates even if the DLL left a foreground thread behind.
            Environment.Exit(code);
            return code;
        }

        // Device object creation (SampleApl.cpp:231-237).
        static IWrrUsb2 CreateDevice()
        {
            if (simulate) return new SimulatedDevice();
            try
            {
                return new RealDevice();
            }
            catch (Exception ex)
            {
                Console.WriteLine("[Error] Cannot create the WRRUSB2 object: " + ex.Message);
                Console.Out.Flush();
                return null;
            }
        }

        // "S <unique-id>" returns a separate atomic result; command_completed.txt only
        // acknowledges consumption and MUST NOT be interpreted as acquisition success.
        // (SampleApl.cpp:127-157)
        static void PublishCaptureResult(string command, int result)
        {
            if (command.Length <= 2) return; // Legacy manual "S" remains usable.
            string id = command.Substring(2).Trim();
            if (!Regex.IsMatch(id, "^[a-zA-Z0-9_-]{1,100}$")) return;
            try
            {
                ushort[] data = gCapturedData;
                bool ok = result == 0 && data != null && data.Length == 2048;
                StringBuilder text = new StringBuilder();
                text.Append("CAPTURE ").Append(id).Append(ok ? " OK 2048\n" : " FAIL " + result.ToString() + "\n");
                if (ok)
                {
                    foreach (ushort pixel in data) text.Append(pixel.ToString("X4")).Append(" ");
                    text.Append("\n");
                }
                text.Append("END ").Append(id).Append("\n");
                if (command.StartsWith("M "))
                {
                    Console.WriteLine("MONITOR_FRAME " + text.ToString().Replace("\n", " "));
                    Console.Out.Flush();
                    return;
                }
                Directory.CreateDirectory("capture_results");
                string path = Path.Combine("capture_results", id + ".txt");
                byte[] bytes = new UTF8Encoding(false).GetBytes(text.ToString());
                using (FileStream file = new FileStream(path + ".tmp", FileMode.Create, FileAccess.Write, FileShare.None))
                {
                    file.Write(bytes, 0, bytes.Length);
                    file.Flush(true);
                }
                File.Move(path + ".tmp", path);
            }
            catch (Exception ex)
            {
                Console.WriteLine("[Result publish failed] " + ex.Message);
            }
            Console.Out.Flush();
        }

        // SampleApl.cpp:161-200
        static string CheckCommandFile()
        {
            string commandFilePath = "command.txt";
            string completedFilePath = "command_completed.txt";

            try
            {
                if (File.Exists(commandFilePath))
                {
                    // Read the command
                    string command = File.ReadAllText(commandFilePath);
                    // Delete command file
                    File.Delete(commandFilePath);
                    // Write to completion file
                    File.WriteAllText(completedFilePath, "done");
                    return command;
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine("[Error] File operation failed: " + ex.Message);
                Console.WriteLine("[Error] Stack trace: " + ex.StackTrace);
                Console.Out.Flush();
            }

            return null;
        }

        //---------------------------------------------------------------------
        // main function (SampleApl.cpp:205-399)
        //---------------------------------------------------------------------
        static int RunMain()
        {
            try
            {
                // Test file writing capability
                File.WriteAllText("test_write.txt", "Test write access");
                Console.WriteLine("[Debug] Successfully wrote test file");
                File.Delete("test_write.txt");
                Console.WriteLine("[Debug] Successfully deleted test file");
                Console.Out.Flush();
            }
            catch (Exception ex)
            {
                Console.WriteLine("[Error] File permission test failed: " + ex.Message);
                Console.Out.Flush();
            }

            Console.WriteLine("[main] in main");
            Console.Out.Flush();
            int lReturn = 0;
            int nLoopFlag = 1;

            Console.WriteLine("Portable Ramanspectrometer Application");
            Console.Out.Flush();

            IWrrUsb2 objWrrUSB = null;

            //Create object of WRRUSB2 class
            objWrrUSB = CreateDevice();
            if (objWrrUSB == null)
            {
                return 1;
            }
            Console.WriteLine("[main] created object");
            Console.Out.Flush();

            //Device initialize
            lReturn = InitializeDevice(objWrrUSB);
            if (lReturn != usb2Success)
            {
                return 1;
            }
            Console.WriteLine("initialized device");
            Console.Out.Flush();
            AppDomain.CurrentDomain.ProcessExit += OnProcessExit;
            // Safety stop (Watchdog.cs, Mac-native addition): laser off, close and exit when the controller
            // is gone or this process is asked to terminate. Started only now that a device is open.
            Watchdog.Start(objWrrUSB, () => g_nDeviceId);
            // DataFed edition: CAPTURE_SETTINGS / MODULE_INFO. After Watchdog.Start, so that the safety
            // stop already covers the two device calls behind MODULE_INFO.
            PrintStartupInfo(objWrrUSB);

            // The DataCapture/DataSaver threads (R/T/C) are not part of this build: the controller
            // never uses them (PORT_SPEC.md section 4).

            PrintMenu();
            Console.Out.Flush();

            try   // Mac-native addition, see the catch below (the loop is not re-indented to keep the port readable)
            {
            while (nLoopFlag == 1)
            {
                // Safety stop requested (Watchdog.cs): laser off, close the device, exit.
                if (Watchdog.StopRequested)
                {
                    return Watchdog.CompleteOnMainThread();
                }

                // Check for commands from file
                string strInput = CheckCommandFile();

                // If no command, wait briefly and continue
                if (strInput == null)
                {
                    Watchdog.Wait(100);   // Thread.Sleep(100) that ends early on a safety stop
                    continue;
                }

                Console.WriteLine("[Debug] Received input: '" + strInput + "'");
                Console.Out.Flush();

                // Process input as before
                strInput = strInput.Trim();
                if (string.IsNullOrWhiteSpace(strInput)) continue;

                char cInput = char.ToUpperInvariant(strInput[0]);

                switch (cInput)
                {
                    case '0':
                        PrintMenu();
                        Console.Out.Flush();
                        break;

                    case '1':
                        lReturn = GetDeivceInformation(objWrrUSB);
                        break;

                    case '2':
                        lReturn = GetCalibrationValue(objWrrUSB);
                        break;

                    case '3':
                        lReturn = GetLDPower(objWrrUSB);
                        break;

                    case '4':
                        lReturn = SetLDPower(objWrrUSB);
                        break;

                    case '5':
                        lReturn = GetLDPowerEnable(objWrrUSB);
                        break;

                    case '6':
                        lReturn = SetLDPowerEnable(objWrrUSB);
                        break;

                    case '7':
                        lReturn = GetLDTemperature(objWrrUSB);
                        break;

                    case '8':
                        lReturn = GetLDBoardTemperature(objWrrUSB);
                        break;

                    case 'F':
                        NotAvailable("FrequencyMode (F)");
                        break;

                    case 'G':
                        NotAvailable("ContinuousMode (G)");
                        break;

                    case 'H':
                        NotAvailable("TriggerMode (H)");
                        break;

                    case 'S':
                        AnnounceCaptureToSimulator(objWrrUSB, strInput);
                        lReturn = StartMeasureForSingleSpectrum(objWrrUSB);
                        PublishCaptureResult(strInput, lReturn);
                        break;

                    case 'M':
                        AnnounceCaptureToSimulator(objWrrUSB, strInput);
                        lReturn = StartMeasureForSingleSpectrum(objWrrUSB, true);
                        PublishCaptureResult(strInput, lReturn);
                        break;

                    case 'R': // Start Capture
                        NotAvailable("StartCapture (R)");
                        break;

                    case 'T': // Stop Capture
                        NotAvailable("StopCapture (T)");
                        break;

                    case 'C': // Save Data Manually
                        NotAvailable("SaveData (C)");
                        break;

                    case 'Q':
                        nLoopFlag = 0;
                        break;

                    default:
                        break;
                }
            }
            }
            catch (Exception ex)
            {
                // Mac-native addition: an exception that would end the process must not leave the laser on.
                int code = Watchdog.FailSafe("unhandled " + ex.GetType().Name + " in the command loop: " + ex.Message);
                try { Console.Error.WriteLine("[Error] " + ex); } catch (Exception) { }
                return code;
            }

            //Device uninitialize
            lReturn = UninitializeDevice(objWrrUSB);
            if (lReturn != usb2Success)
            {
                return 1;
            }

            return 0;
        }

        // Addition for the DataFed edition (data_layer/PROTOCOL.md section 3), no counterpart in
        // SampleApl.cpp: two lines for the controller's capture metadata, printed once at start-up.
        // The exposure settings are the constants every capture uses; the module information comes from
        // the two read-only calls of menu item 1. Nothing here may stop the backend: a refused call or
        // an exception gives "MODULE_INFO unavailable", and with a safety stop pending the device is
        // not asked at all.
        static void PrintStartupInfo(IWrrUsb2 objWrrUSB)
        {
            Console.WriteLine("CAPTURE_SETTINGS exposure_time_us={0} cycle_time_us={1}", EXPOSURE_TIME, CYCLE_TIME);
            string moduleInfo = null;
            try
            {
                if (!Watchdog.StopRequested) moduleInfo = ReadModuleInfo(objWrrUSB);
            }
            catch (Exception)
            {
                // Reported as unavailable below; the capture path does not depend on this information.
            }
            Console.WriteLine(moduleInfo ?? "MODULE_INFO unavailable");
            Console.Out.Flush();
        }

        // The MODULE_INFO line, or null when the device refuses one of the two calls. A method of its
        // own that is never inlined: whatever goes wrong in here, loading the library's data classes
        // included, surfaces inside the try block of PrintStartupInfo.
        [MethodImpl(MethodImplOptions.NoInlining)]
        static string ReadModuleInfo(IWrrUsb2 objWrrUSB)
        {
            Usb2Struct.CModuleInformation pModuleInfo = new Usb2Struct.CModuleInformation();
            Usb2Struct.CSpectroInformation pSpecInfo = new Usb2Struct.CSpectroInformation();
            if ((int)objWrrUSB.USB2_getModuleInformation(g_nDeviceId, ref pModuleInfo) != usb2Success) return null;
            if ((int)objWrrUSB.USB2_getSpectroInformation(g_nDeviceId, ref pSpecInfo) != usb2Success) return null;
            return string.Format("MODULE_INFO model=\"{0}\" serial=\"{1}\" unit=\"{2}\" firmware=\"{3}\"",
                InfoValue(pModuleInfo.product), InfoValue(pModuleInfo.serial), InfoValue(pSpecInfo.unit), InfoValue(pModuleInfo.firmware));
        }

        // The library pads its fixed-length strings with NULs. A double quote or a control character
        // inside a value would break the one-line key="value" format, so those are dropped as well.
        static string InfoValue(string text)
        {
            StringBuilder value = new StringBuilder();
            foreach (char c in text ?? "")
            {
                if (c != '"' && !char.IsControl(c)) value.Append(c);
            }
            return value.ToString().Trim();
        }

        // Simulation only (data_layer/PROTOCOL.md section 4): the virtual sample returns a dark frame
        // for the controller's Capture Dark, which it recognises by the capture id, and the library
        // interface has no place for that id. The real device is not a SimulatedDevice, so for it this
        // does nothing.
        static void AnnounceCaptureToSimulator(IWrrUsb2 objWrrUSB, string command)
        {
            SimulatedDevice simulated = objWrrUSB as SimulatedDevice;
            if (simulated != null) simulated.BeginCapture(command.Length > 2 ? command.Substring(2).Trim() : "");
        }

        static void NotAvailable(string item)
        {
            Console.WriteLine("[Not available] " + item + " is not available in this build.");
            Console.Out.Flush();
        }

        // SampleApl.cpp:402-408
        static void OnProcessExit(object sender, EventArgs e)
        {
            if (g_nDeviceId != 0)
            {
                try
                {
                    IWrrUsb2 tempUsb = simulate ? (IWrrUsb2)new SimulatedDevice() : new RealDevice();
                    tempUsb.USB2_close(g_nDeviceId);
                    tempUsb.USB2_uninitialize();
                }
                catch (Exception)
                {
                    // Best effort at process exit.
                }
            }
        }

        // The original calls Console::ReadKey() when Environment::UserInteractive. On Unix that flag is
        // always true and ReadKey throws when stdin is redirected (as it is under the controller), so
        // the pause happens only for a human at a terminal.
        static void WaitForKeyIfInteractive()
        {
            if (Environment.UserInteractive && !Console.IsInputRedirected)
            {
                try { Console.ReadKey(); } catch (Exception) { }
            }
        }

        //---------------------------------------------------------------------
        // Initialize device function (SampleApl.cpp:414-465)
        //---------------------------------------------------------------------
        static int InitializeDevice(IWrrUsb2 objWrrUSB)
        {
            int lReturn = 0;
            ushort usNum = 0;
            short[] aryDeviceId = null;

            //Argument error check
            if (objWrrUSB == null)
            {
                return usb2Err_unsuccess;
            }

            //Initialization
            lReturn = (int)objWrrUSB.USB2_initialize();
            if (lReturn != usb2Success)
            {
                Console.WriteLine("Error code 0x{0:x04}: Dll Can not be Initialized.", lReturn);
                Console.WriteLine("Exit by pressing the key");
                Console.Out.Flush();
                return lReturn;
            }

            //Device ID storage area allocation
            aryDeviceId = new short[8];
            Array.Clear(aryDeviceId, 0, aryDeviceId.Length);

            //Get connected device list
            lReturn = (int)objWrrUSB.USB2_getModuleConnectionList(aryDeviceId, ref usNum);
            if (lReturn != usb2Success)
            {
                objWrrUSB.USB2_uninitialize();
                Console.WriteLine("Error code 0x{0:x04}: Module Connection list failed.", lReturn);
                Console.WriteLine("Exit by pressing the key");
                Console.Out.Flush();
                return lReturn;
            }

            //Set acquired device ID
            g_nDeviceId = aryDeviceId[0];

            //Open device
            lReturn = (int)objWrrUSB.USB2_open(g_nDeviceId);
            if (lReturn != usb2Success)
            {
                objWrrUSB.USB2_uninitialize();
                Console.WriteLine("Error code 0x{0:x04}: Device can not be open.", lReturn);
                Console.WriteLine("Exit by pressing the key");
                Console.Out.Flush();
                return lReturn;
            }

            return lReturn;
        }

        //---------------------------------------------------------------------
        // Uninitialize device function (SampleApl.cpp:470-498)
        //---------------------------------------------------------------------
        static int UninitializeDevice(IWrrUsb2 objWrrUSB)
        {
            int lReturn = 0;

            //Argument error check
            if (objWrrUSB == null)
            {
                return usb2Err_unsuccess;
            }

            //Close device
            lReturn = (int)objWrrUSB.USB2_close(g_nDeviceId);
            if (lReturn != usb2Success)
            {
                Console.WriteLine("Error code 0x{0:x04}: Device can not be closed.", lReturn);
                Console.WriteLine("Exit by pressing the key");
                Console.Out.Flush();

                return lReturn;
            }

            //Uninitialization
            lReturn = (int)objWrrUSB.USB2_uninitialize();
            if (lReturn != usb2Success)
            {
                Console.WriteLine("Error code 0x{0:x04}: USB2_uninitialize failed.", lReturn);
                Console.WriteLine("Exit by pressing the key");
                Console.Out.Flush();
            }

            return lReturn;
        }

        //---------------------------------------------------------------------
        // Get device information function (SampleApl.cpp:503-554)
        //---------------------------------------------------------------------
        static int GetDeivceInformation(IWrrUsb2 objWrrUSB)
        {
            int lReturn = 0;
            Usb2Struct.CModuleInformation pModuleInfo = null;
            Usb2Struct.CSpectroInformation pSpecInfo = null;

            //Argument error check
            if (objWrrUSB == null)
            {
                return usb2Err_unsuccess;
            }

            try
            {
                pModuleInfo = new Usb2Struct.CModuleInformation();
                pSpecInfo = new Usb2Struct.CSpectroInformation();
            }
            catch (OutOfMemoryException)
            {
                return usb2Err_unsuccess;
            }

            lReturn = (int)objWrrUSB.USB2_getModuleInformation(g_nDeviceId, ref pModuleInfo);
            if (lReturn != usb2Success)
            {
                Console.WriteLine("Error code 0x{0:x04}: USB2_getModuleInformation failed.", lReturn);
                Console.WriteLine("Exit by pressing the key");
                Console.Out.Flush();
                WaitForKeyIfInteractive();
            }

            lReturn = (int)objWrrUSB.USB2_getSpectroInformation(g_nDeviceId, ref pSpecInfo);
            if (lReturn != usb2Success)
            {
                Console.WriteLine("Error code 0x{0:x04}: USB2_getSpectroInformation failed.", lReturn);
                Console.WriteLine("Exit by pressing the key");
                Console.Out.Flush();
                WaitForKeyIfInteractive();
            }

            Console.WriteLine("Deivce information");
            Console.WriteLine("ModelName : {0}", pModuleInfo.product);
            Console.WriteLine("SerialNo  : {0}", pModuleInfo.serial);
            Console.WriteLine("UnitID    : {0}", pSpecInfo.unit);
            Console.WriteLine("FirmVer   : {0}", pModuleInfo.firmware);
            Console.Out.Flush();

            return lReturn;
        }

        //---------------------------------------------------------------------
        // Get calibration value function (SampleApl.cpp:559-601)
        //---------------------------------------------------------------------
        static int GetCalibrationValue(IWrrUsb2 objWrrUSB)
        {
            int lReturn = 0;
            double[] arydblDataArray = null;

            //Argument error check
            if (objWrrUSB == null)
            {
                return usb2Err_unsuccess;
            }

            try
            {
                arydblDataArray = new double[6];
            }
            catch (OutOfMemoryException)
            {
                return usb2Err_unsuccess;
            }

            lReturn = (int)objWrrUSB.USB2_getCalibrationCoefficient(g_nDeviceId, (long)E_CALIBRATIONCOEFFICIENT.D_CALIBRATIONCOEFFICIENT_WAVELENGTH, arydblDataArray);
            if (lReturn != usb2Success)
            {
                Console.WriteLine("Error code 0x{0:x04}: USB2_getCalibrationCoefficient failed.", lReturn);
                Console.WriteLine("Exit by pressing the key");
                Console.Out.Flush();
                WaitForKeyIfInteractive();
                return lReturn;
            }

            Console.WriteLine("Calibration coefficients");
            Console.WriteLine("  A0 : {0:E}", arydblDataArray[0]);
            Console.WriteLine("  B1 : {0:E}", arydblDataArray[1]);
            Console.WriteLine("  B2 : {0:E}", arydblDataArray[2]);
            Console.WriteLine("  B3 : {0:E}", arydblDataArray[3]);
            Console.WriteLine("  B4 : {0:E}", arydblDataArray[4]);
            Console.WriteLine("  B5 : {0:E}", arydblDataArray[5]);
            Console.WriteLine("");
            Console.Out.Flush();

            return lReturn;
        }

        //---------------------------------------------------------------------
        // Get LD Power (SampleApl.cpp:606-634)
        //---------------------------------------------------------------------
        static int GetLDPower(IWrrUsb2 objWrrUSB)
        {
            int lReturn = 0;
            long lLDPower = 0;

            //Argument error check
            if (objWrrUSB == null)
            {
                return usb2Err_unsuccess;
            }

            lReturn = (int)objWrrUSB.USB2_getLDPower(g_nDeviceId, ref lLDPower);
            if (lReturn != usb2Success)
            {
                Console.WriteLine("Error code 0x{0:x04}: USB2_getLDPower failed.", lReturn);
                Console.WriteLine("Exit by pressing the key");
                Console.Out.Flush();
                WaitForKeyIfInteractive();
                return lReturn;
            }
            Console.WriteLine("LD Power : 0x{0:X08}", lLDPower);
            Console.WriteLine("");
            Console.Out.Flush();

            return lReturn;
        }

        //---------------------------------------------------------------------
        // Set LD Power (SampleApl.cpp:639-689)
        //---------------------------------------------------------------------
        static int SetLDPower(IWrrUsb2 objWrrUSB)
        {
            int lReturn = 0;
            long lLDPower = 0;

            //Argument error check
            if (objWrrUSB == null)
            {
                return usb2Err_unsuccess;
            }

            Console.WriteLine("Input LDPower (long)");
            Console.WriteLine("LOW    : 1");
            Console.WriteLine("MIDDLE : 2");
            Console.WriteLine("HIGH   : 3");
            Console.Out.Flush();
            Console.Write("Set LD Power > ");
            Console.Out.Flush();

            // Wait for a command file with the parameter value
            bool gotInput = false;
            while (!gotInput)
            {
                if (Watchdog.StopRequested) return usb2Err_unsuccess;   // safety stop: never reach the device call below
                string value = CheckCommandFile();
                if (value != null)
                {
                    Console.WriteLine(value); // Echo the input value
                    Console.Out.Flush();
                    if (long.TryParse(value, out lLDPower))
                    {
                        gotInput = true;
                    }
                    else
                    {
                        Console.WriteLine("Invalid input. Please try again.");
                        Console.Out.Flush();
                    }
                }
                Watchdog.Wait(100); // Don't hog the CPU (ends early on a safety stop)
            }

            if (Watchdog.StopRequested) return usb2Err_unsuccess;   // a stop that arrived after the value was read
            lReturn = (int)objWrrUSB.USB2_setLDPower(g_nDeviceId, lLDPower);
            if (lReturn != usb2Success)
            {
                Console.WriteLine("Error code 0x{0:x04}: USB2_setLDPower failed.", lReturn);
                Console.WriteLine("Exit by pressing the key");
                Console.Out.Flush();
                WaitForKeyIfInteractive();
                return lReturn;
            }

            Console.WriteLine("");
            Console.Out.Flush();
            return lReturn;
        }

        //---------------------------------------------------------------------
        // Get LD Power enable (SampleApl.cpp:697-726)
        //---------------------------------------------------------------------
        static int GetLDPowerEnable(IWrrUsb2 objWrrUSB)
        {
            int lReturn = 0;
            long lLDPowerEnable = 0;

            //Argument error check
            if (objWrrUSB == null)
            {
                return usb2Err_unsuccess;
            }

            lReturn = (int)objWrrUSB.USB2_getLDPowerEnable(g_nDeviceId, ref lLDPowerEnable);
            if (lReturn != usb2Success)
            {
                Console.WriteLine("Error code 0x{0:x04}: USB2_getLDPowerEnable failed.", lReturn);
                Console.WriteLine("Exit by pressing the key");
                Console.Out.Flush();
                WaitForKeyIfInteractive();
                return lReturn;
            }
            Console.WriteLine("LD Power Enable : 0x{0:x08}", lLDPowerEnable);
            Console.WriteLine("");
            Console.Out.Flush();

            return lReturn;
        }

        //---------------------------------------------------------------------
        // Set LD Power enable (SampleApl.cpp:733-790)
        //---------------------------------------------------------------------
        static int SetLDPowerEnable(IWrrUsb2 objWrrUSB)
        {
            int lReturn = 0;
            long lLDPowerEnable = 0;
            //Argument error check
            if (objWrrUSB == null)
            {
                return usb2Err_unsuccess;
            }

            try
            {
                Console.WriteLine("Input LDPowerEnable (long)");
                Console.WriteLine("Enable  : 1");
                Console.WriteLine("Disable : Other than 1");
                Console.Out.Flush();
                Console.Write("Set LD Power Enable > ");
                Console.Out.Flush();

                // Wait for a command file with the parameter value
                bool gotInput = false;
                while (!gotInput)
                {
                    if (Watchdog.StopRequested) return usb2Err_unsuccess;   // safety stop: never reach the device call below
                    string value = CheckCommandFile();
                    if (value != null)
                    {
                        Console.WriteLine(value); // Echo the input value
                        Console.Out.Flush();
                        if (long.TryParse(value, out lLDPowerEnable))
                        {
                            gotInput = true;
                        }
                        else
                        {
                            Console.WriteLine("Invalid input. Please try again.");
                            Console.Out.Flush();
                        }
                    }
                    Watchdog.Wait(100); // Don't hog the CPU (ends early on a safety stop)
                }
            }
            catch (Exception)
            {
                return usb2Err_unsuccess;
            }

            if (Watchdog.StopRequested) return usb2Err_unsuccess;   // a stop that arrived after the value was read: never enable now
            lReturn = (int)objWrrUSB.USB2_setLDPowerEnable(g_nDeviceId, lLDPowerEnable);
            if (lReturn != usb2Success)
            {
                Console.WriteLine("Error code 0x{0:x04}: USB2_setLDPowerEnable failed.", lReturn);
                Console.WriteLine("Exit by pressing the key");
                Console.Out.Flush();
                WaitForKeyIfInteractive();
                return lReturn;
            }
            Console.WriteLine("");
            Console.Out.Flush();
            return lReturn;
        }

        //---------------------------------------------------------------------
        // Get LD temperature (SampleApl.cpp:798-827)
        //---------------------------------------------------------------------
        static int GetLDTemperature(IWrrUsb2 objWrrUSB)
        {
            int lReturn = 0;
            long lLDTemp = 0;

            //Argument error check
            if (objWrrUSB == null)
            {
                return usb2Err_unsuccess;
            }

            lReturn = (int)objWrrUSB.USB2_getLDTemp(g_nDeviceId, ref lLDTemp);
            if (lReturn != usb2Success)
            {
                Console.WriteLine("Error code 0x{0:x04}: USB2_getLDTemp failed.", lReturn);
                Console.WriteLine("Exit by pressing the key");
                Console.Out.Flush();
                WaitForKeyIfInteractive();
                return lReturn;
            }
            Console.WriteLine("LDTemp : {0}[degC]", (double)lLDTemp / 100);
            Console.WriteLine("");
            Console.Out.Flush();

            return lReturn;
        }

        //---------------------------------------------------------------------
        // Get LD board temperature (SampleApl.cpp:834-863)
        //---------------------------------------------------------------------
        static int GetLDBoardTemperature(IWrrUsb2 objWrrUSB)
        {
            int lReturn = 0;
            long lLDBoardTemp = 0;

            //Argument error check
            if (objWrrUSB == null)
            {
                return usb2Err_unsuccess;
            }

            lReturn = (int)objWrrUSB.USB2_getLDBoardTemp(g_nDeviceId, ref lLDBoardTemp);
            if (lReturn != usb2Success)
            {
                Console.WriteLine("Error code 0x{0:x04}: USB2_getLDBoardTemp failed.", lReturn);
                Console.WriteLine("Exit by pressing the key");
                Console.Out.Flush();
                WaitForKeyIfInteractive();
                return lReturn;
            }
            Console.WriteLine("LDBoardTemp : {0}[degC]", (double)lLDBoardTemp / 100);
            Console.WriteLine("");
            Console.Out.Flush();

            return lReturn;
        }

        //---------------------------------------------------------------------
        // Menu (SampleApl.cpp:868-884)
        //---------------------------------------------------------------------
        static void PrintMenu()
        {
            Console.WriteLine("--------------------------------------------------------------------------");
            Console.WriteLine("  Menu               (0) Menu");
            Console.WriteLine("  DeviceInformation  (1) Get");
            Console.WriteLine("  Calibration        (2) Get");
            Console.WriteLine("  LDPower            (3) Get            (4) Set");
            Console.WriteLine("  LDPowerEnable      (5) Get            (6) Set");
            Console.WriteLine("  LDTemperature      (7) Get");
            Console.WriteLine("  LDBoardTemp        (8) Get");
            Console.WriteLine("  Measure            (F) FrequencyMode  (G) ContinuousMode (H) TriggerMode  (S) SingleSpectrum");
            Console.WriteLine("  Capture            (R) StartCapture    (T) StopCapture");
            Console.WriteLine("  SaveData           (C) Save");
            Console.WriteLine("------------------------------Jeremy--------------------------------------");
            Console.Out.Flush();
        }

        //---------------------------------------------------------------------
        // Modified Single Spectrum Capture Function (SampleApl.cpp:1812-1919)
        //---------------------------------------------------------------------
        static int StartMeasureForSingleSpectrum(IWrrUsb2 objWrrUSB, bool preview = false)
        {
            int result = 0;
            bool allocated = false, started = false;
            long width = 0, height = 0, frames = 0, index = 0;
            ushort[] image = null;
            gCapturedData = null;
            if (objWrrUSB == null) return -1;

            // CHECK_SINGLE macro (SampleApl.cpp:1821); `call` is the stringified C++ call text.
            void CHECK_SINGLE(long rc, string call)
            {
                result = (int)rc;
                if (result != usb2Success)
                {
                    Console.WriteLine("[SDK failure] rc=" + result + "; call=" + call);
                    Console.Out.Flush();
                    throw new InvalidOperationException(call);
                }
            }

            // Preserve the legacy five-frame capture window; read exactly ONE complete
            // frame for this stationary point, then stop. No exposure/gain/focus change.
            try
            {
                CHECK_SINGLE(objWrrUSB.USB2_setCaptureMode(g_nDeviceId, (long)E_CAPTUREMODE.D_CAPTUREMODE_COUNT),
                    "objWrrUSB->USB2_setCaptureMode(g_nDeviceId, static_cast<long>(E_CAPTUREMODE::D_CAPTUREMODE_COUNT))");
                CHECK_SINGLE(objWrrUSB.USB2_setTriggerMode(g_nDeviceId, (long)E_TRIGGERMODE.D_TRIGGERMODE_INTERNAL),
                    "objWrrUSB->USB2_setTriggerMode(g_nDeviceId, static_cast<long>(E_TRIGGERMODE::D_TRIGGERMODE_INTERNAL))");
                CHECK_SINGLE(objWrrUSB.USB2_setDataCount(g_nDeviceId, DATA_COUNT_MAX_VALUE),
                    "objWrrUSB->USB2_setDataCount(g_nDeviceId, DATA_COUNT_MAX_VALUE)");
                CHECK_SINGLE(objWrrUSB.USB2_setDataTransmit(g_nDeviceId, DATA_TRANSMIT_COUNT),
                    "objWrrUSB->USB2_setDataTransmit(g_nDeviceId, DATA_TRANSMIT_COUNT)");
                // The DLL parameters are doubles; the C++ passes the integer microsecond constants (SampleApl.cpp:1827-1828).
                CHECK_SINGLE(objWrrUSB.USB2_setExposureCycle(g_nDeviceId, CYCLE_TIME),
                    "objWrrUSB->USB2_setExposureCycle(g_nDeviceId, CYCLE_TIME)");
                CHECK_SINGLE(objWrrUSB.USB2_setExposureTime(g_nDeviceId, EXPOSURE_TIME),
                    "objWrrUSB->USB2_setExposureTime(g_nDeviceId, EXPOSURE_TIME)");
                CHECK_SINGLE(objWrrUSB.USB2_setGain(g_nDeviceId, (long)E_GAINMODE.D_GAINMODE_SENSOR, (long)E_SENSORGAINMODE.D_SENSORGAINMODE_LOWGAIN),
                    "objWrrUSB->USB2_setGain(g_nDeviceId, static_cast<long>(E_GAINMODE::D_GAINMODE_SENSOR), static_cast<long>(E_SENSORGAINMODE::D_SENSORGAINMODE_LOWGAIN))");
                CHECK_SINGLE(objWrrUSB.USB2_getImageSize(g_nDeviceId, ref width, ref height),
                    "objWrrUSB->USB2_getImageSize(g_nDeviceId, width, height)");
                if (width * height != 2048) throw new InvalidOperationException("Expected 2048 pixels");
                CHECK_SINGLE(objWrrUSB.USB2_allocateBuffer(g_nDeviceId, OTHERMODE_FRAME_COUNT),
                    "objWrrUSB->USB2_allocateBuffer(g_nDeviceId, OTHERMODE_FRAME_COUNT)");
                allocated = true;
                // Attempt stop even if the start API reports an error.
                started = true;
                CHECK_SINGLE(objWrrUSB.USB2_captureStart(g_nDeviceId, OTHERMODE_FRAME_COUNT),
                    "objWrrUSB->USB2_captureStart(g_nDeviceId, OTHERMODE_FRAME_COUNT)");
                Stopwatch timer = Stopwatch.StartNew();
                int initialWaitMs = CaptureTiming.firstCaptureStatusDelayMs(EXPOSURE_TIME, CYCLE_TIME);
                Console.WriteLine("[Capture handshake v2.2] Waiting {0} ms; rc=1 polls the SAME capture, deadline=8000 ms.", initialWaitMs);
                Console.Out.Flush();
                if (Watchdog.Wait(initialWaitMs))   // Thread.Sleep(initialWaitMs) that ends early on a safety stop
                    throw new OperationCanceledException("safety stop requested during the exposure");
                long lastWaitLogMs = -1000;
                for (; ; )
                {
                    if (timer.ElapsedMilliseconds >= 8000)
                    {
                        result = -2;
                        throw new TimeoutException("No readable frame within 8 seconds; same capture was not restarted");
                    }
                    // This SDK leaves output references unchanged on rc=1: clear them
                    // so an earlier frame/index can never be mistaken for a new frame.
                    frames = 0; index = -1;
                    result = (int)objWrrUSB.USB2_getCaptureStatus(g_nDeviceId, ref frames, ref index);
                    CaptureStatusDecision decision = CaptureTiming.captureStatusDecision(result, frames,
                        index, OTHERMODE_FRAME_COUNT, timer.ElapsedMilliseconds, 8000);
                    if (decision == CaptureStatusDecision.Timeout)
                    {
                        result = -2;
                        throw new TimeoutException("Status query exceeded the 8-second frame deadline");
                    }
                    if (decision == CaptureStatusDecision.Error)
                    {
                        Console.WriteLine("[SDK status error] rc={0}, frames={1}, index={2}", result, frames, index);
                        Console.Out.Flush();
                        throw new InvalidOperationException("USB2_getCaptureStatus failed or returned invalid frame metadata");
                    }
                    if (decision == CaptureStatusDecision.Ready)
                    {
                        Console.WriteLine("[Capture ready] elapsed_ms={0}, frames={1}, index={2}", timer.ElapsedMilliseconds, frames, index);
                        Console.Out.Flush();
                        break;
                    }
                    if (timer.ElapsedMilliseconds - lastWaitLogMs >= 500)
                    {
                        Console.WriteLine("[Capture waiting] rc={0}, elapsed_ms={1}; no readable frame yet", result, timer.ElapsedMilliseconds);
                        Console.Out.Flush();
                        lastWaitLogMs = timer.ElapsedMilliseconds;
                    }
                    if (Watchdog.Wait(50))   // Thread.Sleep(50) that ends early on a safety stop
                        throw new OperationCanceledException("safety stop requested while waiting for the frame");
                }
                int firstIndex = CaptureTiming.firstAvailableCaptureIndex(frames, index, OTHERMODE_FRAME_COUNT);
                if (firstIndex < 0) throw new InvalidOperationException("Invalid capture frame count/index");
                ushort[] header = new ushort[IMAGE_HEADER_SIZE];
                image = new ushort[2048];
                ulong[] times = new ulong[1];
                CHECK_SINGLE(objWrrUSB.USB2_getImageHeaderData(g_nDeviceId, ref header, ref image, firstIndex, 1, ref times),
                    "objWrrUSB->USB2_getImageHeaderData(g_nDeviceId, header, image, firstIndex, 1, times)");
            }
            catch (Exception ex)
            {
                if (result == 0) result = -1;
                Console.WriteLine("[Capture failed] " + ex.Message);
                Console.Out.Flush();
            }
            finally
            {
                // Completion is published only AFTER capture has stopped and the buffer is released.
                if (started)
                {
                    int stopResult = (int)objWrrUSB.USB2_captureStop(g_nDeviceId);
                    if (stopResult != 0)
                    {
                        Console.WriteLine("[Capture cleanup] stop rc={0}", stopResult);
                        if (result == 0) result = stopResult;
                    }
                }
                if (allocated)
                {
                    int releaseResult = (int)objWrrUSB.USB2_releaseBuffer(g_nDeviceId);
                    if (releaseResult != 0)
                    {
                        Console.WriteLine("[Capture cleanup] release rc={0}", releaseResult);
                        if (result == 0) result = releaseResult;
                    }
                }
            }
            if (result == 0 && image != null && image.Length == 2048)
            {
                gCapturedData = image;
                if (!preview)
                {
                    Console.Write("Image Data ");
                    DisplayData(image);
                }
                Console.WriteLine("End of measurement");
            }
            else
            {
                gCapturedData = null;
                if (result == 0) result = -1;
            }
            Console.Out.Flush();
            return result;
        }

        //---------------------------------------------------------------------
        // Display data function (SampleApl.cpp:1970-1985)
        //---------------------------------------------------------------------
        static bool DisplayData(ushort[] aryusImageData)
        {
            // Same bytes as the per-pixel Console::Write loop, assembled once so the
            // auto-flushing writer issues one write instead of 2048.
            StringBuilder strData = new StringBuilder(aryusImageData.Length * 5);
            for (int nIdx = 0; nIdx < aryusImageData.Length; nIdx++)
            {
                int nData = aryusImageData[nIdx];
                strData.Append(string.Format("{0:X04} ", nData));
            }
            Console.Write(strData.ToString());
            Console.WriteLine("");
            Console.Out.Flush();

            return true;
        }
    }

    //-------------------------------------------------------------------------
    // --selftest: load wrrusb2a.dll for real through the shim, without hardware.
    //-------------------------------------------------------------------------
    static class SelfTest
    {
        public static int Run()
        {
            string shim = Native.FindShim();
            if (shim == null)
            {
                Console.WriteLine("SELFTEST SKIPPED: shim missing. " + Native.ShimMissingMessage());
                Console.Out.Flush();
                return 3;
            }
            try
            {
                Native.InstallDllImportResolver();
                Console.WriteLine("[selftest] shim: " + shim);
                return RunWithDll();
            }
            catch (Exception ex)
            {
                Console.WriteLine("SELFTEST FAILED: " + ex);
                Console.Out.Flush();
                return 1;
            }
        }

        // Kept separate so wrrusb2a.dll is touched only after the resolver is installed.
        static int RunWithDll()
        {
            WRRUSB2 usb = new WRRUSB2();
            Console.WriteLine("[selftest] WRRUSB2 created");
            long rc = usb.USB2_initialize();
            Console.WriteLine("[selftest] USB2_initialize rc=" + rc);
            short[] list = new short[8];
            ushort number = 0;
            rc = usb.USB2_getModuleConnectionList(list, ref number);
            Console.WriteLine("[selftest] USB2_getModuleConnectionList rc=" + rc + " devices=" + number);
            USBRemovableCheck check = new USBRemovableCheck();
            check.SetDevicePath("selftest");
            bool removed = check.IsRemoveDevice();
            Console.WriteLine("[selftest] USBRemovableCheck.IsRemoveDevice=" + removed);
            // Without hardware the DLL's own calls above reach only setupapi.dll (enumeration) and
            // user32.dll RegisterDeviceNotification: its shell32.dll ordinal imports (#2/#4) sit in a
            // private USBRemovableCheck path the Forms stand-in never triggers, and kernel32(.dll) /
            // winusb.dll are first used by USB2_open. Probe those names explicitly (ShimProbes.cs) so
            // every entry of the resolver's mapping table is resolved and called here.
            ShimProbes.Run();
            Console.WriteLine("[selftest] resolver probes OK: " + string.Join(" ", Native.MappedLibraryNames) + " (shell32 ordinals #2/#4)");
            try
            {
                rc = usb.USB2_uninitialize();
                Console.WriteLine("[selftest] USB2_uninitialize rc=" + rc);
            }
            catch (Exception ex)
            {
                Console.WriteLine("[selftest] USB2_uninitialize threw " + ex.GetType().Name + ": " + ex.Message);
            }
            Console.WriteLine("SELFTEST OK");
            Console.Out.Flush();
            return 0;
        }
    }
}
