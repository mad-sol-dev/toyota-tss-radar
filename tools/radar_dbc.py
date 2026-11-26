"""
radar_dbc.py — shared path resolution for the Toyota DBC files.

The DBCs are not bundled (separate license, see README); they are fetched
into dbc/ (repo root) by tools/get_dbc.sh. Alternative location via environment variable:

  export RADAR_DBC_DIR=/path/to/opendbc

If a file is missing, load() aborts with instructions instead of a traceback.
"""
import os
import sys

import cantools.database

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR   = os.path.dirname(SCRIPT_DIR)
DBC_DIR    = os.environ.get('RADAR_DBC_DIR', os.path.join(REPO_DIR, 'dbc'))

ADAS = 'toyota_prius_2017_adas.dbc'          # frk2 revision, used by the tools
PT   = 'toyota_prius_2017_pt_generated.dbc'
ADAS_CURRENT = 'toyota_adas.dbc'             # current opendbc: + SCORE, STATUS_MSG


def path(name):
    """Absolute path of a DBC file; exits the program if it is missing."""
    p = os.path.join(DBC_DIR, name)
    if not os.path.isfile(p):
        sys.exit(
            f"ERROR: DBC file not found: {p}\n"
            f"  → Run once in the repo directory:  tools/get_dbc.sh\n"
            f"  → or set RADAR_DBC_DIR to a folder containing {name}.")
    return p


def load(name=ADAS):
    """Loads a DBC (strict=False due to overlap in STEERING_IPAS_COMMA)."""
    return cantools.database.load_file(path(name), strict=False)
