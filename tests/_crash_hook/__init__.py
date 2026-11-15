# Crash injection: imported via PYTHONPATH as `sitecustomize`.
# When CORPUS_CRASH_PACKAGE is set, the worker hard-exits (os._exit, no
# finally/atexit) right after the replacement temp file is fully written and
# fsynced but BEFORE os.replace — simulating power loss mid-rewrite.
