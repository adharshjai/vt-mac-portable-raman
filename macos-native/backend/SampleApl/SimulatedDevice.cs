// Instrument-free stand-in for WRRUSB2 (PORT_SPEC.md section 4, "SimulatedDevice"): selected by
// `--simulate` or env RAMAN_BACKEND_SIMULATE=1. Never touches wrrusb2a.dll's code or the shim; only
// the DLL's plain data classes (CModuleInformation, CSpectroInformation) appear in the signatures.
using System;
using System.Diagnostics;
using WRRUSB2_DLL;

namespace SampleApl
{
    public sealed class SimulatedDevice : IWrrUsb2
    {
        const long DeviceId = 0;
        const long Pixels = 2048;
        const int HeaderWords = 256;

        // Return codes (tools/wrrusb2a_api.txt, enum Usb2Struct.Cusb2Err).
        const long Success = (long)Usb2Struct.Cusb2Err.usb2Success;
        const long ErrNotFrame = (long)Usb2Struct.Cusb2Err.usb2Err_notFrame;
        const long ErrNotBuffer = (long)Usb2Struct.Cusb2Err.usb2Err_notBuffer;
        const long ErrInvalidDeviceIndex = (long)Usb2Struct.Cusb2Err.usb2Err_invalidDeviceIndex;
        const long ErrInvalidIndex = (long)Usb2Struct.Cusb2Err.usb2Err_invalidIndex;
        const long ErrNowCapturing = (long)Usb2Struct.Cusb2Err.usb2Err_nowCapturing;

        bool initialized, opened, allocated, capturing;
        long bufferFrames;
        long ldPower, ldPowerEnable;
        long captureMode, triggerMode, dataCount = 1, dataTransmit = 1, gainMode, gain;
        // SampleApl.cpp passes its microsecond constants (EXPOSURE_TIME/CYCLE_TIME) to these
        // double parameters; the simulator keeps them as given and treats them as microseconds.
        double exposureValue = 1000000, cycleValue = 1100000;
        readonly Stopwatch captureClock = new Stopwatch();
        int frameCounter;
        readonly Random random = new Random(20251027);

        static bool ValidId(long id) { return id == DeviceId; }

        /// <summary>Capture is complete once exposure (µs) + 100 ms have elapsed since captureStart.</summary>
        bool FrameReady()
        {
            double readyMs = exposureValue / 1000.0 + 100.0;
            return captureClock.Elapsed.TotalMilliseconds >= readyMs;
        }

        public long USB2_initialize() { initialized = true; return Success; }
        public long USB2_uninitialize() { initialized = false; opened = false; return Success; }

        public long USB2_getModuleConnectionList(short[] list, ref ushort number)
        {
            if (!initialized) return (long)Usb2Struct.Cusb2Err.usb2Err_notList;
            if (list == null || list.Length < 1) return (long)Usb2Struct.Cusb2Err.usb2Err_invalidParameter;
            list[0] = (short)DeviceId;      // one device, id 0
            number = 1;
            return Success;
        }

        public long USB2_open(long id) { if (!ValidId(id)) return ErrInvalidDeviceIndex; opened = true; return Success; }
        public long USB2_close(long id) { if (!ValidId(id)) return ErrInvalidDeviceIndex; opened = false; capturing = false; allocated = false; return Success; }

        public long USB2_getModuleInformation(long id, ref Usb2Struct.CModuleInformation information)
        {
            if (!ValidId(id)) return ErrInvalidDeviceIndex;
            if (information == null) information = new Usb2Struct.CModuleInformation();
            information.vender = "Hamamatsu";
            information.product = "C13560-0274";
            information.serial = "SIMULATED";
            information.firmware = "1.00";
            information.driver = "1.00";
            information.library = "1.00";
            return Success;
        }

        public long USB2_getSpectroInformation(long id, ref Usb2Struct.CSpectroInformation information)
        {
            if (!ValidId(id)) return ErrInvalidDeviceIndex;
            if (information == null) information = new Usb2Struct.CSpectroInformation();
            information.unit = "SIM";
            information.sensor = "F4"; // Usb2Struct.D_TYPE_CMOS_2048PIXEL
            return Success;
        }

        public long USB2_getCalibrationCoefficient(long id, long index, double[] coefficient)
        {
            if (!ValidId(id)) return ErrInvalidDeviceIndex;
            if (coefficient == null || coefficient.Length < 6) return (long)Usb2Struct.Cusb2Err.usb2Err_invalidParameter;
            // Fixed wavelength polynomial (index 0) / sensibility (index 1) coefficients.
            double[] wavelength = { 7.8512345e2, 3.2123456e-1, -1.2345678e-5, 2.3456789e-9, -1.0e-13, 0.0 };
            double[] sensibility = { 1.0, 0.0, 0.0, 0.0, 0.0, 0.0 };
            double[] source = index == 1 ? sensibility : wavelength;
            Array.Copy(source, coefficient, 6);
            return Success;
        }

        public long USB2_getLDPower(long id, ref long LDpower) { if (!ValidId(id)) return ErrInvalidDeviceIndex; LDpower = ldPower; return Success; }
        public long USB2_setLDPower(long id, long LDpower) { if (!ValidId(id)) return ErrInvalidDeviceIndex; ldPower = LDpower; return Success; }
        public long USB2_getLDPowerEnable(long id, ref long PowerEnable) { if (!ValidId(id)) return ErrInvalidDeviceIndex; PowerEnable = ldPowerEnable; return Success; }
        public long USB2_setLDPowerEnable(long id, long PowerEnable) { if (!ValidId(id)) return ErrInvalidDeviceIndex; ldPowerEnable = PowerEnable; return Success; }
        // Temperatures are reported in 1/100 degC (SampleApl.cpp:821 divides by 100): 25 degC.
        public long USB2_getLDTemp(long id, ref long LDTemp) { if (!ValidId(id)) return ErrInvalidDeviceIndex; LDTemp = 2500; return Success; }
        public long USB2_getLDBoardTemp(long id, ref long LDBoardTemp) { if (!ValidId(id)) return ErrInvalidDeviceIndex; LDBoardTemp = 2500; return Success; }

        public long USB2_setCaptureMode(long id, long mode) { if (!ValidId(id)) return ErrInvalidDeviceIndex; captureMode = mode; return Success; }
        public long USB2_setTriggerMode(long id, long mode) { if (!ValidId(id)) return ErrInvalidDeviceIndex; triggerMode = mode; return Success; }
        public long USB2_setDataCount(long id, long count) { if (!ValidId(id)) return ErrInvalidDeviceIndex; dataCount = count; return Success; }
        public long USB2_setDataTransmit(long id, long transmit) { if (!ValidId(id)) return ErrInvalidDeviceIndex; dataTransmit = transmit; return Success; }
        public long USB2_setExposureCycle(long id, double sec) { if (!ValidId(id)) return ErrInvalidDeviceIndex; cycleValue = sec; return Success; }
        public long USB2_setExposureTime(long id, double sec) { if (!ValidId(id)) return ErrInvalidDeviceIndex; exposureValue = sec; return Success; }
        public long USB2_setGain(long id, long mode, long gainValue) { if (!ValidId(id)) return ErrInvalidDeviceIndex; gainMode = mode; gain = gainValue; return Success; }

        public long USB2_getImageSize(long id, ref long sizeX, ref long sizeY)
        {
            if (!ValidId(id)) return ErrInvalidDeviceIndex;
            sizeX = Pixels; sizeY = 1;      // 1 x 2048
            return Success;
        }

        public long USB2_allocateBuffer(long id, long frame)
        {
            if (!ValidId(id)) return ErrInvalidDeviceIndex;
            if (frame < 1) return (long)Usb2Struct.Cusb2Err.usb2Err_invalidFrame;
            allocated = true; bufferFrames = frame;
            return Success;
        }

        public long USB2_captureStart(long id, long frame)
        {
            if (!ValidId(id)) return ErrInvalidDeviceIndex;
            if (!opened) return (long)Usb2Struct.Cusb2Err.usb2Err_notAccess;
            if (!allocated) return ErrNotBuffer;
            if (capturing) return ErrNowCapturing;
            capturing = true;
            captureClock.Restart();          // records the start time
            return Success;
        }

        public long USB2_captureStop(long id)
        {
            if (!ValidId(id)) return ErrInvalidDeviceIndex;
            capturing = false;
            captureClock.Stop();
            return Success;
        }

        /// <summary>rc=1 (pending, outputs untouched) until the frame is ready; then 0 with frame=1, index=0.</summary>
        public long USB2_getCaptureStatus(long id, ref long frame, ref long index)
        {
            if (!ValidId(id)) return ErrInvalidDeviceIndex;
            if (!capturing) return ErrNotFrame;
            if (!FrameReady()) return 1;
            frame = 1; index = 0;
            return Success;
        }

        public long USB2_getImageHeaderData(long id, ref ushort[] headerData, ref ushort[] imageData, long index, long frame, ref ulong[] time)
        {
            if (!ValidId(id)) return ErrInvalidDeviceIndex;
            if (!allocated) return ErrNotBuffer;
            if (index < 0 || index >= bufferFrames || frame < 1) return ErrInvalidIndex;
            if (imageData == null || imageData.Length < Pixels) imageData = new ushort[Pixels];
            if (headerData == null || headerData.Length < HeaderWords) headerData = new ushort[HeaderWords];
            if (time == null || time.Length < 1) time = new ulong[1];
            FillSpectrum(imageData);
            FillHeader(headerData);
            time[0] = (ulong)Stopwatch.GetTimestamp();
            return Success;
        }

        public long USB2_releaseBuffer(long id) { if (!ValidId(id)) return ErrInvalidDeviceIndex; allocated = false; bufferFrames = 0; return Success; }
        public long USB2_fireTrigger(long id) { if (!ValidId(id)) return ErrInvalidDeviceIndex; return Success; }

        // Synthetic Raman spectrum: ~800-count baseline, a few Gaussian peaks, small noise, < 65535.
        void FillSpectrum(ushort[] image)
        {
            frameCounter++;
            double[] centers = { 420, 760, 1010, 1240, 1590 };   // pixel positions
            double[] heights = { 2400, 5200, 1800, 3100, 900 };
            double[] widths = { 9, 14, 7, 11, 18 };
            double drift = 1.0 + 0.01 * Math.Sin(frameCounter * 0.7);
            for (int i = 0; i < Pixels; i++)
            {
                double value = 800.0 + 0.02 * i;
                for (int p = 0; p < centers.Length; p++)
                {
                    double d = (i - centers[p]) / widths[p];
                    value += heights[p] * drift * Math.Exp(-0.5 * d * d);
                }
                value += (random.NextDouble() - 0.5) * 24.0;   // +/- 12 counts of noise
                if (value < 0) value = 0;
                if (value > 65000) value = 65000;
                image[i] = (ushort)Math.Round(value);
            }
        }

        void FillHeader(ushort[] header)
        {
            Array.Clear(header, 0, header.Length);
            header[0] = (ushort)HeaderWords;
            header[1] = (ushort)frameCounter;
            header[2] = (ushort)Pixels;
            header[3] = 1;
            header[4] = (ushort)Math.Min(ushort.MaxValue, (long)(exposureValue / 1000.0)); // exposure in ms
            header[5] = (ushort)gain;
            header[6] = (ushort)captureMode;
            header[7] = (ushort)triggerMode;
            header[8] = (ushort)dataCount;
            header[9] = (ushort)dataTransmit;
            header[10] = (ushort)Math.Min(ushort.MaxValue, (long)(cycleValue / 1000.0));
        }
    }
}
