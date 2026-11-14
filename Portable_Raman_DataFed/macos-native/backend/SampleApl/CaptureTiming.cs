// Port of laser_test/hardware/SampleApl/capture_timing.h (shared with the controller's mocks).
namespace SampleApl
{
    // capture_timing.h:15
    public enum CaptureStatusDecision { Wait, Ready, Error, Timeout }

    public static class CaptureTiming
    {
        // capture_timing.h:3-8 — keep the device's previously working first-status-query delay.
        public static int firstCaptureStatusDelayMs(long exposureUs, long cycleUs)
        {
            long durationUs = exposureUs > cycleUs ? exposureUs : cycleUs;
            int delay = (int)((durationUs + 999) / 1000) + 150;
            return delay < 1250 ? 1250 : delay;
        }

        // capture_timing.h:10-13
        public static int firstAvailableCaptureIndex(long frames, long currentIndex, int capacity)
        {
            if (capacity <= 0 || frames < 1 || frames > capacity || currentIndex < 0 || currentIndex >= capacity) return -1;
            return (int)((currentIndex - frames + 1 + capacity) % capacity);
        }

        // capture_timing.h:17-29 — ONLY for USB2_getCaptureStatus in the installed wrrusb2a.dll:
        // it returns 1 when captured-minus-delivered frame count is <= 0. This is NOT a blanket
        // rule for other SDK calls. Bound the wait; never turn rc=1 into a success ACK.
        public static CaptureStatusDecision captureStatusDecision(long rc, long frames, long index,
                                                                  int capacity, long elapsedMs,
                                                                  long timeoutMs)
        {
            if (elapsedMs >= timeoutMs) return CaptureStatusDecision.Timeout;
            if (rc == 1) return CaptureStatusDecision.Wait;
            if (rc != 0) return CaptureStatusDecision.Error;
            if (frames == 0) return CaptureStatusDecision.Wait;
            return firstAvailableCaptureIndex(frames, index, capacity) >= 0
                ? CaptureStatusDecision.Ready : CaptureStatusDecision.Error;
        }
    }
}
