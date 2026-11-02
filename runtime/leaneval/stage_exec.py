"""Gate target exec until the controller has assigned its fresh cgroup."""
import os
import sys

fd = int(sys.argv[1])
try:
    permitted = os.read(fd, 1) == b'1'
finally:
    os.close(fd)
if not permitted:
    raise SystemExit(125)
os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
