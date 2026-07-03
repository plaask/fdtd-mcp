"""FDTD MCP installer — prints the claude mcp add registration command.

Usage:
    python install.py                          # auto-detect Lumerical
    python install.py --lumerical-home PATH    # specify path manually
"""

import sys


def main():
    if sys.version_info < (3, 10):
        sys.exit('Error: Python 3.10 or later is required.')

    # Parse optional --lumerical-home arg
    home = None
    for i, arg in enumerate(sys.argv):
        if arg == '--lumerical-home' and i + 1 < len(sys.argv):
            home = sys.argv[i + 1]
            break

    if not home:
        from fdtd_mcp.discovery import find_lumerical
        try:
            home = find_lumerical()
        except FileNotFoundError as e:
            sys.exit(
                'Error: ' + str(e) + '\n\n'
                'Run with explicit path:\n'
                '  python install.py --lumerical-home "D:/Software/Lumerical/v202"'
            )

    print()
    print('  FDTD MCP — Lumerical found at:')
    print('  ' + home)
    print()
    print('  Register with Claude Code:')
    print()
    print(f'  claude mcp add fdtd -- python -m fdtd_mcp.server --lumerical-home "{home}"')
    print()
    print('  Then restart Claude Code.')
    print()


if __name__ == '__main__':
    main()
