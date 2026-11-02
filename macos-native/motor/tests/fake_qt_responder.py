"""Fake Qt controller for manual end-to-end runs (not a unittest; discovery only collects test*.py).

Answers every request.txt in <sessionDir> with `ACK <run> <id> OK <heartbeat>` every 10 ms,
like laser_test/tests/motor_path_test.cpp, until <sessionDir>/responder_stop exists.
Usage:  python3 fake_qt_responder.py <sessionDir> &
        RAMAN_MOTOR_SIMULATE=1 ./RamanAutofocus <sessionDir> run1 1d-x
        touch <sessionDir>/responder_stop
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scan_handshake
d = sys.argv[1]; stop = os.path.join(d, 'responder_stop'); seen = set(); n = 0
while not os.path.exists(stop):
    try: text = open(os.path.join(d, 'request.txt')).readline()
    except OSError: text = ''
    t = text.split()
    if len(t) >= 6 and t[0] == 'STEP':
        key = (t[1], t[2])
        if key not in seen:
            seen.add(key); n += 1
        scan_handshake.publish(os.path.join(d, 'response.txt'), 'ACK %s %s OK %d\n' % (t[1], t[2], int(time.time()*1000)))
    time.sleep(0.01)
print('[responder] acknowledged %d distinct points' % n)
