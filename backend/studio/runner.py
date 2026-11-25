"""Run one MCP runtime script in a workspace: python3 -m studio.runner <workspace> <script> [args...]."""

import os
import sys
import warnings


def main():
    if len(sys.argv) < 3:
        sys.stderr.write('usage: python3 -m studio.runner <workspace> <script> [args...]\n')
        sys.exit(2)
    workspace = os.path.abspath(sys.argv[1])
    script = sys.argv[2]
    args = sys.argv[3:]

    warnings.filterwarnings('ignore', category=SyntaxWarning)
    warnings.filterwarnings('ignore', category=DeprecationWarning)

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if here not in sys.path:
        sys.path.insert(0, here)
    from studio import py2compat

    os.chdir(workspace)
    runtime = os.path.join(workspace, 'runtime')
    # line-buffer output so the UI receives log lines as they happen
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    py2compat.run_script(runtime, script, args)


if __name__ == '__main__':
    main()
