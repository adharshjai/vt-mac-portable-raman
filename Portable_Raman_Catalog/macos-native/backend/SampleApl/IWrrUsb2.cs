// The subset of WRRUSB2_DLL.WRRUSB2 that SampleApl.cpp calls, with the DLL's exact signatures
// (macos-native/tools/wrrusb2a_api.txt, class WRRUSB2_DLL.WRRUSB2). SampleApl.cpp uses 29 distinct
// USB2_* operations; a grep for "USB2_" also hits the namespace token WRRUSB2_DLL, which is not one.
using WRRUSB2_DLL;

namespace SampleApl
{
    public interface IWrrUsb2
    {
        long USB2_initialize();
        long USB2_uninitialize();
        long USB2_getModuleConnectionList(short[] list, ref ushort number);
        long USB2_open(long id);
        long USB2_close(long id);
        long USB2_getModuleInformation(long id, ref Usb2Struct.CModuleInformation information);
        long USB2_getSpectroInformation(long id, ref Usb2Struct.CSpectroInformation information);
        long USB2_getCalibrationCoefficient(long id, long index, double[] coefficient);
        long USB2_getLDPower(long id, ref long LDpower);
        long USB2_setLDPower(long id, long LDpower);
        long USB2_getLDPowerEnable(long id, ref long PowerEnable);
        long USB2_setLDPowerEnable(long id, long PowerEnable);
        long USB2_getLDTemp(long id, ref long LDTemp);
        long USB2_getLDBoardTemp(long id, ref long LDBoardTemp);
        long USB2_setCaptureMode(long id, long mode);
        long USB2_setTriggerMode(long id, long mode);
        long USB2_setDataCount(long id, long count);
        long USB2_setDataTransmit(long id, long transmit);
        long USB2_setExposureCycle(long id, double sec);
        long USB2_setExposureTime(long id, double sec);
        long USB2_setGain(long id, long mode, long gain);
        long USB2_getImageSize(long id, ref long sizeX, ref long sizeY);
        long USB2_allocateBuffer(long id, long frame);
        long USB2_captureStart(long id, long frame);
        long USB2_captureStop(long id);
        long USB2_getCaptureStatus(long id, ref long frame, ref long index);
        long USB2_getImageHeaderData(long id, ref ushort[] headerData, ref ushort[] imageData, long index, long frame, ref ulong[] time);
        long USB2_releaseBuffer(long id);
        long USB2_fireTrigger(long id);
    }

    /// <summary>Thin wrapper around Hamamatsu's WRRUSB2 object; every call is a direct delegation.</summary>
    public sealed class RealDevice : IWrrUsb2
    {
        readonly WRRUSB2 dll;

        /// <summary>Installs the P/Invoke resolver (shim) and creates the WRRUSB2 object (SampleApl.cpp:234).</summary>
        public RealDevice()
        {
            Native.InstallDllImportResolver();
            dll = Create();
        }

        // Separate method so the JIT touches wrrusb2a.dll only after the resolver is in place.
        static WRRUSB2 Create() { return new WRRUSB2(); }

        public long USB2_initialize() { return dll.USB2_initialize(); }
        public long USB2_uninitialize() { return dll.USB2_uninitialize(); }
        public long USB2_getModuleConnectionList(short[] list, ref ushort number) { return dll.USB2_getModuleConnectionList(list, ref number); }
        public long USB2_open(long id) { return dll.USB2_open(id); }
        public long USB2_close(long id) { return dll.USB2_close(id); }
        public long USB2_getModuleInformation(long id, ref Usb2Struct.CModuleInformation information) { return dll.USB2_getModuleInformation(id, ref information); }
        public long USB2_getSpectroInformation(long id, ref Usb2Struct.CSpectroInformation information) { return dll.USB2_getSpectroInformation(id, ref information); }
        public long USB2_getCalibrationCoefficient(long id, long index, double[] coefficient) { return dll.USB2_getCalibrationCoefficient(id, index, coefficient); }
        public long USB2_getLDPower(long id, ref long LDpower) { return dll.USB2_getLDPower(id, ref LDpower); }
        public long USB2_setLDPower(long id, long LDpower) { return dll.USB2_setLDPower(id, LDpower); }
        public long USB2_getLDPowerEnable(long id, ref long PowerEnable) { return dll.USB2_getLDPowerEnable(id, ref PowerEnable); }
        public long USB2_setLDPowerEnable(long id, long PowerEnable) { return dll.USB2_setLDPowerEnable(id, PowerEnable); }
        public long USB2_getLDTemp(long id, ref long LDTemp) { return dll.USB2_getLDTemp(id, ref LDTemp); }
        public long USB2_getLDBoardTemp(long id, ref long LDBoardTemp) { return dll.USB2_getLDBoardTemp(id, ref LDBoardTemp); }
        public long USB2_setCaptureMode(long id, long mode) { return dll.USB2_setCaptureMode(id, mode); }
        public long USB2_setTriggerMode(long id, long mode) { return dll.USB2_setTriggerMode(id, mode); }
        public long USB2_setDataCount(long id, long count) { return dll.USB2_setDataCount(id, count); }
        public long USB2_setDataTransmit(long id, long transmit) { return dll.USB2_setDataTransmit(id, transmit); }
        public long USB2_setExposureCycle(long id, double sec) { return dll.USB2_setExposureCycle(id, sec); }
        public long USB2_setExposureTime(long id, double sec) { return dll.USB2_setExposureTime(id, sec); }
        public long USB2_setGain(long id, long mode, long gain) { return dll.USB2_setGain(id, mode, gain); }
        public long USB2_getImageSize(long id, ref long sizeX, ref long sizeY) { return dll.USB2_getImageSize(id, ref sizeX, ref sizeY); }
        public long USB2_allocateBuffer(long id, long frame) { return dll.USB2_allocateBuffer(id, frame); }
        public long USB2_captureStart(long id, long frame) { return dll.USB2_captureStart(id, frame); }
        public long USB2_captureStop(long id) { return dll.USB2_captureStop(id); }
        public long USB2_getCaptureStatus(long id, ref long frame, ref long index) { return dll.USB2_getCaptureStatus(id, ref frame, ref index); }
        public long USB2_getImageHeaderData(long id, ref ushort[] headerData, ref ushort[] imageData, long index, long frame, ref ulong[] time) { return dll.USB2_getImageHeaderData(id, ref headerData, ref imageData, index, frame, ref time); }
        public long USB2_releaseBuffer(long id) { return dll.USB2_releaseBuffer(id); }
        public long USB2_fireTrigger(long id) { return dll.USB2_fireTrigger(id); }
    }
}
