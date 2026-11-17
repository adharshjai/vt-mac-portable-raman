#pragma once
#ifdef _WIN32
#include <windows.h>
#else
#include <cstdio>       // rename(2): atomic replace, the POSIX counterpart of MoveFileExA
#include <sys/stat.h>   // stat(2): file-exists probe for cancel.txt
#include <unistd.h>     // usleep(3)
#endif
#include <fstream>
#include <sstream>
#include <string>
#include <chrono>
#include <thread>

// One outstanding point per session. Files are published atomically.
namespace ScanHandshake {
inline bool publish(const std::string& path, const std::string& text) {
    const std::string tmp = path + ".tmp";
    { std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
      out << text; out.flush(); if (!out) return false; }
    for (int attempt = 0; attempt < 20; ++attempt) {
#ifdef _WIN32
        if (MoveFileExA(tmp.c_str(), path.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) return true;
        Sleep(10);
#else
        if (std::rename(tmp.c_str(), path.c_str()) == 0) return true;
        usleep(10 * 1000);
#endif
    }
    return false;
}
inline bool cancelled(const std::string& dir) {
#ifdef _WIN32
    return GetFileAttributesA((dir + "/cancel.txt").c_str()) != INVALID_FILE_ATTRIBUTES;
#else
    struct stat st;
    return stat((dir + "/cancel.txt").c_str(), &st) == 0;
#endif
}
inline bool waitForAck(const std::string& dir, const std::string& run, unsigned point,
                       int timeoutMs = 30000) {
    auto lastContact = std::chrono::steady_clock::now();
    std::string previousHeartbeat;
    while (!cancelled(dir)) {
        std::ifstream in(dir + "/response.txt");
        std::string text; std::getline(in, text); in.close();
        std::istringstream row(text);
        std::string tag, actualRun, state, heartbeat; unsigned actualPoint = 0;
        if (row >> tag >> actualRun >> actualPoint >> state >> heartbeat) {
            if (tag == "ACK" && actualRun == run && actualPoint == point) {
                if (state == "OK") return true;
                if (state == "FAIL") return false;
                if ((state == "PAUSED" || state == "HOLD") && heartbeat != previousHeartbeat) {
                    previousHeartbeat = heartbeat;
                    lastContact = std::chrono::steady_clock::now();
                }
            }
        }
        if (std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - lastContact).count() >= timeoutMs) return false;
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    return false;
}
}
