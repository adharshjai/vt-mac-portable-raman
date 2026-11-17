#pragma once

// Keep the device's previously working first-status-query delay.
inline int firstCaptureStatusDelayMs(long exposureUs, long cycleUs) {
    const long durationUs = exposureUs > cycleUs ? exposureUs : cycleUs;
    const int delay = static_cast<int>((durationUs + 999) / 1000) + 150;
    return delay < 1250 ? 1250 : delay;
}

inline int firstAvailableCaptureIndex(long frames, long currentIndex, int capacity) {
    if (capacity <= 0 || frames < 1 || frames > capacity || currentIndex < 0 || currentIndex >= capacity) return -1;
    return static_cast<int>((currentIndex - frames + 1 + capacity) % capacity);
}

enum class CaptureStatusDecision { Wait, Ready, Error, Timeout };

// ONLY for USB2_getCaptureStatus in the installed wrrusb2a.dll: its IL returns
// 1 when captured-minus-delivered frame count is <= 0. This is NOT a blanket
// rule for other SDK calls. Bound the wait; never turn rc=1 into a success ACK.
inline CaptureStatusDecision captureStatusDecision(long rc, long frames, long index,
                                                    int capacity, long long elapsedMs,
                                                    long long timeoutMs) {
    if (elapsedMs >= timeoutMs) return CaptureStatusDecision::Timeout;
    if (rc == 1) return CaptureStatusDecision::Wait;
    if (rc != 0) return CaptureStatusDecision::Error;
    if (frames == 0) return CaptureStatusDecision::Wait;
    return firstAvailableCaptureIndex(frames, index, capacity) >= 0
        ? CaptureStatusDecision::Ready : CaptureStatusDecision::Error;
}
