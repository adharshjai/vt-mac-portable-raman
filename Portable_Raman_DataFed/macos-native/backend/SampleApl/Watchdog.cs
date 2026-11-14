// Mac-native addition with no counterpart in SampleApl.cpp: a safety stop.
//
// The controller starts this backend and normally stops it. When the controller disappears
// without doing so (crash, force quit, Qt Creator's Stop button), or when this process is asked
// to terminate (SIGTERM from the controller's window close or from pkill, SIGINT, SIGHUP,
// SIGQUIT), or when an exception would end it, the backend switches the laser off, confirms that
// by reading the state back, closes the device and exits. Without this it would keep running
// with the spectrometer open and the laser in whatever state it was in.
//
// How it works
//  - RememberController() runs as the first statement of Main and records which process to watch.
//  - A watchdog thread polls the parent process id; a changed parent means the controller is gone.
//  - The stop itself runs on the MAIN thread, between two device calls, so Hamamatsu's library is
//    not entered from two threads. The laser is switched off BEFORE anything is logged.
//  - Last resort: if the main thread has not switched the laser off within GraceMs (it is stuck
//    inside a device call), the watchdog side does it from a separate thread with its own
//    deadline, then ends the process.
//  - The laser counts as off only when the disable call AND the read-back both succeed and the
//    state reads 0. Otherwise it is retried, and if it still cannot be confirmed the log says so
//    and the exit code is UnconfirmedExitCode.
//  - Everything is logged to watchdog.log in the working directory, never to stdout: a full or
//    broken pipe must not be able to delay the stop.
//
// Timing: the controller's destructor sends SIGTERM, waits (2000 ms in the original, 3000 ms in
// the Mac build of laser_test/main.cpp) and then sends SIGKILL. GraceMs + EmergencyAttemptMs must
// stay below 2000 ms, or the kill could arrive before the last resort has acted.
//
// What this cannot do: SIGKILL (kill -9) on this process cannot be handled by any process; a
// controller that hangs or is suspended without dying is not "gone"; and a device that no longer
// answers cannot be switched off by software. In those cases unplug the spectrometer's USB cable
// (the module is USB powered).
using System;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace SampleApl
{
    /// <summary>stdout that never throws. Once the controller is gone the pipe is broken, and an
    /// IOException from Console.WriteLine must not end the process before the laser is off.</summary>
    public sealed class SafeConsoleWriter : TextWriter
    {
        readonly TextWriter inner;
        volatile bool broken;

        public SafeConsoleWriter(TextWriter inner) { this.inner = inner; }

        public override Encoding Encoding { get { return inner.Encoding; } }
        public bool Broken { get { return broken; } }

        public override void Write(char value)
        {
            if (broken) return;
            try { inner.Write(value); }
            catch (IOException) { broken = true; }
            catch (ObjectDisposedException) { broken = true; }
        }

        public override void Write(char[] buffer, int index, int count)
        {
            if (broken) return;
            try { inner.Write(buffer, index, count); }
            catch (IOException) { broken = true; }
            catch (ObjectDisposedException) { broken = true; }
        }

        public override void Write(string value)
        {
            if (broken) return;
            try { inner.Write(value); }
            catch (IOException) { broken = true; }
            catch (ObjectDisposedException) { broken = true; }
        }

        // One write per line, like the StreamWriter underneath (text and terminator together).
        public override void WriteLine(string value)
        {
            if (broken) return;
            try { inner.WriteLine(value); }
            catch (IOException) { broken = true; }
            catch (ObjectDisposedException) { broken = true; }
        }

        public override void Flush()
        {
            if (broken) return;
            try { inner.Flush(); }
            catch (IOException) { broken = true; }
            catch (ObjectDisposedException) { broken = true; }
        }
    }

    public static class Watchdog
    {
        /// <summary>Laser confirmed off and device closed; the stop ran on the main thread.</summary>
        public const int ExitCode = 5;
        /// <summary>Laser confirmed off by the watchdog side; the main thread did not respond.</summary>
        public const int EmergencyExitCode = 6;
        /// <summary>The laser could NOT be confirmed off. Unplug the spectrometer.</summary>
        public const int UnconfirmedExitCode = 7;
        /// <summary>Set by the controller (and, as a fallback, by the launcher) to the pid to watch.</summary>
        public const string ControllerPidVariable = "RAMAN_CONTROLLER_PID";

        const int PollMs = 200;               // parent check interval
        const int GraceMs = 1000;             // time the main thread gets to switch the laser off
        const int EmergencyAttemptMs = 700;   // deadline for the last-resort attempt (GraceMs + this < 2000, see header)
        const int ExitGuardMs = 1500;         // after the laser is off: time the process gets to close and exit by itself
        const int LaserOffAttempts = 3;
        const int LaserOffRetryMs = 100;
        const string LogFile = "watchdog.log";   // in the backend's working directory
        const string NotConfirmed = "THE LASER IS NOT CONFIRMED OFF - unplug the spectrometer's USB cable";

        [DllImport("libc", EntryPoint = "getppid")]
        static extern int getppid();

        [DllImport("libc", EntryPoint = "_exit")]
        static extern void exit_now(int code);

        static readonly ManualResetEventSlim stop = new ManualResetEventSlim(false);
        static readonly ManualResetEventSlim mainLaserDone = new ManualResetEventSlim(false);
        static readonly object logLock = new object();
        static readonly object reasonLock = new object();
        static string reason = "";
        static int controllerPid = -1;            // the process to watch; <= 1: none
        static IWrrUsb2 device;
        static Func<int> deviceId;
        static volatile bool laserConfirmedOff;
        static volatile int finalExitCode = ExitCode;
        static PosixSignalRegistration[] signals;  // referenced so the registrations stay alive

        public static bool StopRequested { get { return stop.IsSet; } }
        public static string Reason { get { lock (reasonLock) return reason; } }

        /// <summary>Sleeps up to `milliseconds`. Returns true as soon as a safety stop is requested,
        /// so that waits in the main thread end early.</summary>
        public static bool Wait(int milliseconds)
        {
            return stop.Wait(milliseconds);
        }

        static int CurrentParent()
        {
            try { return getppid(); }
            catch (Exception) { return -1; }
        }

        /// <summary>First statement of Main: which process started us. Recorded before the runtime
        /// and the device take their time, so that a controller dying meanwhile is noticed.
        /// A live parent is the controller. Only when this process is already orphaned does the
        /// pid handed down in RAMAN_CONTROLLER_PID count: that process is gone.</summary>
        public static void RememberController()
        {
            int parent = CurrentParent();
            if (parent > 1)
            {
                controllerPid = parent;
                return;
            }
            int recorded;
            string text = Environment.GetEnvironmentVariable(ControllerPidVariable);
            if (!string.IsNullOrEmpty(text)
                && int.TryParse(text.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out recorded) && recorded > 1)
                controllerPid = recorded;
            else
                controllerPid = parent;
        }

        /// <summary>Call once, on the main thread, after the device has been opened.</summary>
        public static void Start(IWrrUsb2 openDevice, Func<int> id)
        {
            device = openDevice;
            deviceId = id;
            try
            {
                signals = new[]
                {
                    PosixSignalRegistration.Create(PosixSignal.SIGTERM, OnSignal),
                    PosixSignalRegistration.Create(PosixSignal.SIGINT, OnSignal),
                    PosixSignalRegistration.Create(PosixSignal.SIGHUP, OnSignal),
                    PosixSignalRegistration.Create(PosixSignal.SIGQUIT, OnSignal),
                };
            }
            catch (Exception ex)
            {
                Log("signal handlers not installed: " + ex.Message);
            }
            AppDomain.CurrentDomain.UnhandledException += OnUnhandledException;

            if (controllerPid <= 1)
            {
                Log("no controller process to watch (parent id " + controllerPid.ToString(CultureInfo.InvariantCulture)
                    + "): only termination signals trigger the safety stop");
            }
            else
            {
                int parent = CurrentParent();
                if (parent > 0 && parent != controllerPid)
                    Request("the controller process " + controllerPid.ToString(CultureInfo.InvariantCulture)
                        + " was gone before start-up finished");
            }

            Thread thread = new Thread(Run);
            thread.IsBackground = true;
            thread.Name = "controller-watchdog";
            thread.Start();
        }

        static void OnSignal(PosixSignalContext context)
        {
            context.Cancel = true;   // no default termination: the process exits after the laser is off
            Request("received " + context.Signal);
        }

        static void Request(string why)
        {
            lock (reasonLock)
            {
                if (stop.IsSet) return;
                reason = why;
                stop.Set();
            }
        }

        static void Run()
        {
            while (!stop.IsSet)
            {
                if (controllerPid > 1)
                {
                    int parent = CurrentParent();
                    if (parent > 0 && parent != controllerPid)
                    {
                        Request("the controller process " + controllerPid.ToString(CultureInfo.InvariantCulture) + " is gone");
                        break;
                    }
                }
                stop.Wait(PollMs);
            }
            Log("stop requested: " + Reason);

            if (mainLaserDone.Wait(GraceMs))
            {
                // The main thread has dealt with the laser. Let it close the device and exit, but do
                // not let a blocked flush or exit handler keep the process alive.
                Thread.Sleep(ExitGuardMs);
                exit_now(finalExitCode);
            }

            // Last resort: the main thread is stuck, possibly inside a device call. Laser first,
            // from a separate thread with a deadline so that a second hang cannot keep us alive.
            Thread attempt = new Thread(EmergencyLaserOff);
            attempt.IsBackground = true;
            attempt.Name = "emergency-laser-off";
            attempt.Start();
            bool returned = attempt.Join(EmergencyAttemptMs);
            Log("the main thread did not switch the laser off within " + GraceMs.ToString(CultureInfo.InvariantCulture)
                + " ms (" + Reason + "): the watchdog side did it instead");
            if (!returned)
                Log("the emergency laser-off did not return within " + EmergencyAttemptMs.ToString(CultureInfo.InvariantCulture) + " ms");
            int code = laserConfirmedOff ? EmergencyExitCode : UnconfirmedExitCode;
            if (!laserConfirmedOff) Log(NotConfirmed);
            Log("exiting with code " + code.ToString(CultureInfo.InvariantCulture));
            exit_now(code);
        }

        static void EmergencyLaserOff()
        {
            LaserOff();
        }

        /// <summary>The safety stop itself, on the main thread: laser off first, then the log, then
        /// the device is closed. Returns the process exit code.</summary>
        public static int CompleteOnMainThread()
        {
            bool confirmed = LaserOff();
            finalExitCode = confirmed ? ExitCode : UnconfirmedExitCode;
            mainLaserDone.Set();
            Log("safety stop: " + Reason);
            try
            {
                int id = deviceId();
                long closeResult = device.USB2_close(id);
                long uninitResult = device.USB2_uninitialize();
                Log(string.Format(CultureInfo.InvariantCulture, "device closed (close rc={0}, uninitialize rc={1})", closeResult, uninitResult));
            }
            catch (Exception ex)
            {
                Log("closing the device failed: " + ex.GetType().Name + ": " + ex.Message);
            }
            if (!confirmed) Log(NotConfirmed);
            Log("exiting with code " + finalExitCode.ToString(CultureInfo.InvariantCulture));
            return finalExitCode;
        }

        /// <summary>For the main thread when something other than a stop request ends the command
        /// loop (an exception): same stop, with its own reason.</summary>
        public static int FailSafe(string why)
        {
            Request(why);
            return CompleteOnMainThread();
        }

        static void OnUnhandledException(object sender, UnhandledExceptionEventArgs e)
        {
            // The runtime ends the process when this handler returns: laser first, with a deadline.
            try
            {
                Thread attempt = new Thread(EmergencyLaserOff);
                attempt.IsBackground = true;
                attempt.Start();
                attempt.Join(EmergencyAttemptMs);
                Exception ex = e.ExceptionObject as Exception;
                Log("unhandled exception, the process is ending: " + (ex != null ? ex.GetType().Name + ": " + ex.Message : "unknown"));
                if (!laserConfirmedOff) Log(NotConfirmed);
            }
            catch (Exception) { }
        }

        // USB2_setLDPowerEnable(id, 0) is the call the controller's Laser OFF makes (menu item 6 with
        // the value 0, SampleApl.cpp:733-790); the state is read back like menu item 5. The laser
        // counts as off only when both calls succeed and the state reads 0. A failed attempt is
        // followed by a capture stop (the library may refuse the call while capturing) and a retry.
        static bool LaserOff()
        {
            if (laserConfirmedOff) return true;
            for (int attempt = 1; attempt <= LaserOffAttempts; attempt++)
            {
                long setResult = -1, getResult = -1, state = -1;
                string error = null;
                try
                {
                    int id = deviceId();
                    setResult = device.USB2_setLDPowerEnable(id, 0);
                    getResult = device.USB2_getLDPowerEnable(id, ref state);
                }
                catch (Exception ex)
                {
                    error = ex.GetType().Name + ": " + ex.Message;
                }
                bool confirmed = error == null && setResult == 0 && getResult == 0 && state == 0;
                if (confirmed) laserConfirmedOff = true;
                Log(string.Format(CultureInfo.InvariantCulture, "laser disable rc={0}; readback rc={1}, LD Power Enable : 0x{2:x08}{3}{4}",
                    setResult, getResult, state,
                    attempt > 1 ? " (attempt " + attempt.ToString(CultureInfo.InvariantCulture) + ")" : "",
                    error != null ? " [" + error + "]" : ""));
                if (confirmed) return true;
                if (laserConfirmedOff) return true;   // the other side got there meanwhile
                if (attempt < LaserOffAttempts)
                {
                    try
                    {
                        long stopResult = device.USB2_captureStop(deviceId());
                        Log("capture stop before the retry rc=" + stopResult.ToString(CultureInfo.InvariantCulture));
                    }
                    catch (Exception ex)
                    {
                        Log("capture stop before the retry failed: " + ex.GetType().Name + ": " + ex.Message);
                    }
                    Thread.Sleep(LaserOffRetryMs);
                }
            }
            return false;
        }

        /// <summary>One line to watchdog.log in the working directory. Never throws and never
        /// touches stdout.</summary>
        public static void Log(string text)
        {
            string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff", CultureInfo.InvariantCulture) + " [Watchdog] " + text + "\n";
            lock (logLock)
            {
                try { File.AppendAllText(LogFile, line); }
                catch (Exception) { }
            }
        }
    }
}
